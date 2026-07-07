#!/usr/bin/env python3
"""
FastAPI backend for the LTE Network Control Panel.
Reads srsRAN UE CSV metrics files and serves live data via REST + WebSocket.
Controls broker gains via TCP socket. Manages iperf3 traffic via subprocess.

Endpoints added to existing backend:
  POST /api/gain       — set per-UE broker gain (DL/UL)
  POST /api/traffic    — start/stop iperf3 traffic per UE
  POST /api/kill       — permanent soft kill (gain=0 both directions)
  POST /api/reset      — reset all gains to 1.0, stop all traffic
  POST /api/fault      — inject a fault scenario
  POST /api/fault/clear — clear an active fault
  GET  /api/status     — full status (metrics + gains + faults)

CSV columns (semicolon-separated, from srsRAN UE metrics):
  time;cc;earfcn;pci;rsrp;pl;cfo;pci_neigh;rsrp_neigh;cfo_neigh;
  dl_mcs;dl_snr;dl_turbo;dl_brate;dl_bler;ul_ta;distance_km;speed_kmph;
  ul_mcs;ul_buff;ul_brate;ul_bler;rf_o;rf_u;rf_l;is_attached;
  proc_rmem;proc_rmem_kB;proc_vmem_kB;sys_mem;sys_load;thread_count;
  cpu_0;cpu_1;...cpu_N
"""

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from broker_client import BrokerControlClient

app = FastAPI(title="LTE Network Control Panel")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Broker client (talks to broker's TCP control server on port 4000)
# ---------------------------------------------------------------------------

broker = BrokerControlClient()

# ---------------------------------------------------------------------------
# iperf3 traffic management
# ---------------------------------------------------------------------------

IPERF_SERVER = "172.16.0.1"

iperf_processes: dict[str, dict] = {}  # ue_id -> {"process": Popen, "profile": str, ...}

STRESS_PROFILES = {
    "hd_streaming":  {"bitrate": "5M",   "pkt_size": 1400, "bidir": False, "label": "HD Streaming (5 Mbps DL)"},
    "voice_call":    {"bitrate": "64K",   "pkt_size": 160,  "bidir": True,  "label": "Voice Call (64K bidir)"},
    "video_call":    {"bitrate": "1.5M",  "pkt_size": 1200, "bidir": True,  "label": "Video Call (1.5 Mbps bidir)"},
    "bulk_download": {"bitrate": "0",     "pkt_size": 1400, "bidir": False, "label": "Bulk Download (max rate)"},
    "file_upload":   {"bitrate": "0",     "pkt_size": 1400, "bidir": False, "label": "File Upload (max rate UL)"},
    "ping_flood":    {"bitrate": "512K",  "pkt_size": 64,   "bidir": False, "label": "Ping Flood"},
}

UE_NETNS = {"ue1": "ue1", "ue2": "ue2", "ue3": "ue3"}
UE_IPERF_PORTS = {"ue1": 5201, "ue2": 5202, "ue3": 5203}

# ---------------------------------------------------------------------------
# Fault injection state
# ---------------------------------------------------------------------------

active_faults: dict[str, dict] = {}  # ue_id -> fault info with pre-fault gains

FAULT_CONFIGS = {
    "co_channel_interference": {
        "low":    {"dl": 0.7,  "ul": None},
        "medium": {"dl": 0.4,  "ul": None},
        "high":   {"dl": 0.15, "ul": None},
    },
    "bler_degradation": {
        "low":    {"dl": 0.6,  "ul": None},
        "medium": {"dl": 0.35, "ul": None},
        "high":   {"dl": 0.15, "ul": None},
    },
    "transport_stall": {
        # Gain NOT touched — that's the diagnostic signature
        "low":    {"dl": None, "ul": None, "traffic": "stop"},
        "medium": {"dl": None, "ul": None, "traffic": "stop"},
        "high":   {"dl": None, "ul": None, "traffic": "stop"},
    },
    "link_dropout": {
        "low":    {"dl": 0.1,  "ul": 0.1},
        "medium": {"dl": 0.05, "ul": 0.05},
        "high":   {"dl": 0.0,  "ul": 0.0},  # PERMANENT
    },
    "scheduler_starvation": {
        "low":    {"dl": 0.5,  "ul": None, "traffic": "heavy_1M"},
        "medium": {"dl": 0.3,  "ul": None, "traffic": "heavy_3M"},
        "high":   {"dl": 0.15, "ul": None, "traffic": "heavy_5M"},
    },
    "uplink_contamination": {
        "low":    {"dl": None, "ul": 0.7},
        "medium": {"dl": None, "ul": 0.4},
        "high":   {"dl": None, "ul": 0.15},
    },
}

FAULT_DESCRIPTIONS = {
    "co_channel_interference": "Second LTE signal on {ue}'s downlink. SNR drops, BLER spikes, MCS falls.",
    "bler_degradation": "{ue} moving to cell edge. SNR falling, BLER rising, scheduler lowering MCS.",
    "transport_stall": "{ue}'s ZMQ transport stalling. Throughput drops, jitter rises, SNR stays stable.",
    "link_dropout": "{ue} lost radio link. RLF, detach, attach rate zero.",
    "scheduler_starvation": "{ue} in deep fade with heavy traffic. HARQ retransmissions consuming PRBs.",
    "uplink_contamination": "{ue}'s noisy uplink corrupting composite signal. Cell-wide UL degradation.",
}

# ---------------------------------------------------------------------------
# UE Config (from existing backend)
# ---------------------------------------------------------------------------

UE_CONFIGS = [
    {
        "id": "ue1", "label": "UE1", "imsi": "901700123456789",
        "ip": "172.16.0.2", "csv_path": "/tmp/ue1_metrics.csv",
        "zmq_tx": 3001, "zmq_rx": 3000,
    },
    {
        "id": "ue2", "label": "UE2", "imsi": "901700123456790",
        "ip": "172.16.0.3", "csv_path": "/tmp/ue2_metrics.csv",
        "zmq_tx": 3011, "zmq_rx": 3010,
    },
    {
        "id": "ue3", "label": "UE3", "imsi": "901700123456791",
        "ip": "172.16.0.4", "csv_path": "/tmp/ue3_metrics.csv",
        "zmq_tx": 3021, "zmq_rx": 3020,
    },
]

ENB_CONFIG = {
    "id": "enb1", "label": "eNB1", "cell_id": "0x01", "pci": 1,
    "earfcn": 2850, "n_prb": 25, "zmq_tx": 3101, "zmq_rx": 3100,
}

CSV_COLUMNS = [
    "time", "cc", "earfcn", "pci", "rsrp", "pl", "cfo",
    "pci_neigh", "rsrp_neigh", "cfo_neigh",
    "dl_mcs", "dl_snr", "dl_turbo", "dl_brate", "dl_bler",
    "ul_ta", "distance_km", "speed_kmph",
    "ul_mcs", "ul_buff", "ul_brate", "ul_bler",
    "rf_o", "rf_u", "rf_l", "is_attached",
    "proc_rmem", "proc_rmem_kB", "proc_vmem_kB",
    "sys_mem", "sys_load", "thread_count",
]

latest_metrics: dict[str, dict] = {}
metrics_history: dict[str, list] = {ue["id"]: [] for ue in UE_CONFIGS}
HISTORY_MAX = 300
start_time = time.time()
sample_count = 0


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class GainRequest(BaseModel):
    ue: str          # "ue1", "ue2", "ue3"
    direction: str   # "dl" or "ul"
    value: float     # 0.0-1.0

class TrafficRequest(BaseModel):
    ue: str
    action: str      # "start" or "stop"
    profile: str = "voice_call"
    bitrate: str = "64K"
    duration: int = 300

class KillRequest(BaseModel):
    ue: str

class FaultRequest(BaseModel):
    ue: str
    fault_type: str  # key from FAULT_CONFIGS
    severity: str = "medium"  # "low", "medium", "high"

class FaultClearRequest(BaseModel):
    ue: str


# ---------------------------------------------------------------------------
# CSV metrics parsing (kept from existing backend)
# ---------------------------------------------------------------------------

def parse_csv_line(line: str) -> Optional[dict]:
    parts = line.strip().split(";")
    if len(parts) < len(CSV_COLUMNS):
        return None
    result = {}
    for i, col in enumerate(CSV_COLUMNS):
        val = parts[i]
        if val == "n/a" or val == "":
            result[col] = None
        else:
            try:
                result[col] = float(val)
            except ValueError:
                result[col] = val
    cpu_vals = []
    for j in range(len(CSV_COLUMNS), len(parts)):
        try:
            cpu_vals.append(float(parts[j]))
        except ValueError:
            pass
    if cpu_vals:
        result["cpu_cores"] = cpu_vals
        result["cpu_avg"] = sum(cpu_vals) / len(cpu_vals)
    return result


def read_latest_metrics(ue_id: str, csv_path: str) -> Optional[dict]:
    global sample_count
    if not os.path.exists(csv_path):
        return None
    try:
        with open(csv_path, "r") as f:
            lines = f.readlines()
        data_lines = [
            l for l in lines
            if l.strip() and not l.startswith("#") and not l.startswith("time;")
        ]
        if not data_lines:
            return None
        latest = parse_csv_line(data_lines[-1])
        if latest:
            sample_count += 1
            recent = data_lines[-HISTORY_MAX:]
            history = []
            for line in recent:
                parsed = parse_csv_line(line)
                if parsed:
                    history.append(parsed)
            metrics_history[ue_id] = history
        return latest
    except Exception as e:
        print(f"Error reading {csv_path}: {e}")
        return None


def classify_signal(rsrp):
    if rsrp is None or rsrp == 0:
        return "Unknown"
    if rsrp >= 50:
        return "Excellent"
    if rsrp >= 30:
        return "Good"
    if rsrp >= 10:
        return "Fair"
    return "Weak"


def classify_bler(bler):
    if bler is None:
        return "Unknown"
    if bler == 0:
        return "Clean"
    if bler <= 2:
        return "Low"
    if bler <= 10:
        return "Fair"
    return "High"


def classify_throughput(brate):
    if brate is None or brate <= 0:
        return "Idle"
    if brate < 10000:
        return "Low"
    if brate < 100000:
        return "Active"
    return "High"


def build_ue_snapshot(ue_cfg: dict) -> dict:
    metrics = latest_metrics.get(ue_cfg["id"])
    ue_id = ue_cfg["id"]

    # Get broker gains
    broker_status = broker.get_status()
    gains = broker_status.get("gains", {})
    killed_list = broker_status.get("killed", [])

    dl_gain = gains.get(f"{ue_id}_dl", 1.0)
    ul_gain = gains.get(f"{ue_id}_ul", 1.0)
    is_killed = ue_id in killed_list

    # Traffic state
    traffic_info = None
    if ue_id in iperf_processes:
        proc_info = iperf_processes[ue_id]
        # Check if process is still running
        if proc_info["process"].poll() is None:
            traffic_info = {"active": True, "profile": proc_info["profile"]}
        else:
            del iperf_processes[ue_id]

    # Active fault
    fault_info = active_faults.get(ue_id)

    if metrics is None:
        return {
            "id": ue_id, "label": ue_cfg["label"], "imsi": ue_cfg["imsi"],
            "ip": None, "status": "No Data",
            "zmq": {"tx": ue_cfg["zmq_tx"], "rx": ue_cfg["zmq_rx"]},
            "rf": None, "throughput": None, "process": None,
            "gains": {"dl": dl_gain, "ul": ul_gain},
            "killed": is_killed,
            "traffic": traffic_info,
            "fault": fault_info,
        }

    attached = metrics.get("is_attached", 0) == 1.0
    has_traffic = (metrics.get("dl_brate", 0) or 0) > 0 or (metrics.get("ul_brate", 0) or 0) > 0

    if is_killed:
        status = "Killed"
    elif attached and has_traffic:
        status = "Online"
    elif attached:
        status = "Idle"
    else:
        status = "Detached"

    return {
        "id": ue_id, "label": ue_cfg["label"], "imsi": ue_cfg["imsi"],
        "ip": ue_cfg["ip"] if attached else None,
        "status": status,
        "zmq": {"tx": ue_cfg["zmq_tx"], "rx": ue_cfg["zmq_rx"]},
        "rf": {
            "rsrp": {"value": metrics.get("rsrp"), "unit": "dBm", "descriptor": classify_signal(metrics.get("rsrp"))},
            "dl_snr": {"value": metrics.get("dl_snr"), "unit": "dB", "descriptor": classify_signal(metrics.get("dl_snr"))},
            "dl_mcs": {"value": metrics.get("dl_mcs"), "unit": "", "descriptor": None},
            "ul_mcs": {"value": metrics.get("ul_mcs"), "unit": "", "descriptor": None},
            "dl_bler": {"value": metrics.get("dl_bler"), "unit": "%", "descriptor": classify_bler(metrics.get("dl_bler"))},
            "ul_bler": {"value": metrics.get("ul_bler"), "unit": "%", "descriptor": classify_bler(metrics.get("ul_bler"))},
            "cfo": {"value": metrics.get("cfo"), "unit": "Hz", "descriptor": None},
            "pathloss": {"value": metrics.get("pl"), "unit": "dB", "descriptor": None},
            "earfcn": {"value": metrics.get("earfcn"), "unit": "", "descriptor": None},
            "pci": {"value": metrics.get("pci"), "unit": "", "descriptor": None},
        },
        "throughput": {
            "dl_brate": {"value": metrics.get("dl_brate", 0), "unit": "bps", "descriptor": classify_throughput(metrics.get("dl_brate"))},
            "ul_brate": {"value": metrics.get("ul_brate", 0), "unit": "bps", "descriptor": classify_throughput(metrics.get("ul_brate"))},
        },
        "process": {
            "sys_load": {"value": metrics.get("sys_load"), "unit": "%", "descriptor": None},
            "proc_mem_kb": {"value": metrics.get("proc_rmem_kB"), "unit": "KB", "descriptor": None},
            "sys_mem": {"value": metrics.get("sys_mem"), "unit": "%", "descriptor": None},
            "cpu_avg": {"value": round(metrics.get("cpu_avg", 0), 1) if metrics.get("cpu_avg") else None, "unit": "%", "descriptor": None},
        },
        "gains": {"dl": dl_gain, "ul": ul_gain},
        "killed": is_killed,
        "traffic": traffic_info,
        "fault": fault_info,
    }


def build_full_snapshot() -> dict:
    for ue_cfg in UE_CONFIGS:
        m = read_latest_metrics(ue_cfg["id"], ue_cfg["csv_path"])
        if m:
            latest_metrics[ue_cfg["id"]] = m

    ues = [build_ue_snapshot(ue_cfg) for ue_cfg in UE_CONFIGS]
    online = sum(1 for u in ues if u["status"] == "Online")
    attached = sum(1 for u in ues if u["status"] in ("Online", "Idle"))

    return {
        "timestamp": time.time(),
        "elapsed_s": round(time.time() - start_time),
        "sample_count": sample_count,
        "enb": ENB_CONFIG,
        "summary": {
            "ues_online": online,
            "ues_attached": attached,
            "ues_total": len(ues),
        },
        "ues": ues,
        "broker_connected": broker.is_connected(),
        "active_faults": active_faults,
    }


# ---------------------------------------------------------------------------
# iperf3 helpers
# ---------------------------------------------------------------------------

def start_iperf(ue_id: str, profile: str, bitrate: str = "64K",
                pkt_size: int = 160, bidir: bool = True, duration: int = 300) -> dict:
    """Start iperf3 in the UE's network namespace."""
    if ue_id not in UE_NETNS:
        return {"error": f"unknown UE: {ue_id}"}

    # Kill existing
    stop_iperf(ue_id)

    netns = UE_NETNS[ue_id]
    port = UE_IPERF_PORTS[ue_id]

    cmd = [
        "sudo", "ip", "netns", "exec", netns,
        "iperf3", "-c", IPERF_SERVER,
        "-u", "-b", bitrate, "-l", str(pkt_size),
        "-t", str(duration), "-p", str(port),
    ]
    if bidir:
        cmd.append("--bidir")

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        iperf_processes[ue_id] = {
            "process": proc,
            "profile": profile,
            "bitrate": bitrate,
            "pid": proc.pid,
            "started": time.time(),
        }
        return {"status": "started", "ue": ue_id, "profile": profile, "pid": proc.pid}
    except Exception as e:
        return {"error": str(e)}


def stop_iperf(ue_id: str) -> dict:
    """Stop iperf3 on a UE."""
    if ue_id not in iperf_processes:
        return {"status": "no active traffic", "ue": ue_id}

    proc_info = iperf_processes[ue_id]
    try:
        proc_info["process"].terminate()
        proc_info["process"].wait(timeout=5)
    except Exception:
        proc_info["process"].kill()

    profile = proc_info["profile"]
    del iperf_processes[ue_id]
    return {"status": "stopped", "ue": ue_id, "was": profile}


# ---------------------------------------------------------------------------
# REST endpoints — existing
# ---------------------------------------------------------------------------

@app.get("/")
async def serve_frontend():
    html_path = Path(__file__).parent / "frontend.html"
    if html_path.exists():
        return FileResponse(html_path, media_type="text/html")
    return HTMLResponse("<h1>frontend.html not found</h1>", status_code=404)


@app.get("/api/status")
async def get_status():
    return build_full_snapshot()


@app.get("/api/history/{ue_id}")
async def get_history(ue_id: str, limit: int = 60):
    if ue_id not in metrics_history:
        return {"error": f"Unknown UE: {ue_id}"}
    hist = metrics_history[ue_id][-limit:]
    return {"ue_id": ue_id, "count": len(hist), "metrics": hist}


@app.get("/api/export")
async def export_dataset():
    return {
        "exported_at": time.time(),
        "enb": ENB_CONFIG,
        "ues": {ue["id"]: metrics_history[ue["id"]] for ue in UE_CONFIGS},
    }


# ---------------------------------------------------------------------------
# REST endpoints — NEW: controls
# ---------------------------------------------------------------------------

@app.post("/api/gain")
async def set_gain(req: GainRequest):
    """Set per-UE broker gain via TCP control socket."""
    if req.ue not in UE_NETNS:
        return {"error": f"unknown UE: {req.ue}"}
    if req.direction not in ("dl", "ul"):
        return {"error": "direction must be 'dl' or 'ul'"}
    if req.value < 0.0 or req.value > 1.0:
        return {"error": "value must be 0.0-1.0"}
    return broker.set_gain(req.ue, req.direction, req.value)


@app.post("/api/traffic")
async def manage_traffic(req: TrafficRequest):
    """Start or stop iperf3 traffic on a UE."""
    if req.ue not in UE_NETNS:
        return {"error": f"unknown UE: {req.ue}"}

    if req.action == "stop":
        return stop_iperf(req.ue)

    if req.action == "start":
        # Look up stress profile if provided
        if req.profile in STRESS_PROFILES:
            p = STRESS_PROFILES[req.profile]
            return start_iperf(
                req.ue, req.profile,
                bitrate=p["bitrate"], pkt_size=p["pkt_size"],
                bidir=p["bidir"], duration=req.duration,
            )
        return start_iperf(
            req.ue, req.profile,
            bitrate=req.bitrate, duration=req.duration,
        )

    return {"error": "action must be 'start' or 'stop'"}


@app.post("/api/kill")
async def kill_ue(req: KillRequest):
    """Permanent soft kill via broker. Cannot be reversed without stack restart."""
    if req.ue not in UE_NETNS:
        return {"error": f"unknown UE: {req.ue}"}
    stop_iperf(req.ue)
    return broker.kill(req.ue)


@app.post("/api/reset")
async def reset_cell():
    """Reset all gains to 1.0, stop all traffic, clear non-permanent faults."""
    # Stop all iperf3
    for ue_id in list(iperf_processes.keys()):
        stop_iperf(ue_id)

    # Clear faults (restore pre-fault gains where possible)
    for ue_id in list(active_faults.keys()):
        fault = active_faults[ue_id]
        if not fault.get("permanent"):
            pre = fault.get("pre_gains", {})
            if pre.get("dl") is not None:
                broker.set_gain(ue_id, "dl", pre["dl"])
            if pre.get("ul") is not None:
                broker.set_gain(ue_id, "ul", pre["ul"])
            del active_faults[ue_id]

    # Reset remaining gains
    result = broker.reset()
    result["traffic"] = "all stopped"
    result["faults_cleared"] = True
    return result


@app.post("/api/fault")
async def inject_fault(req: FaultRequest):
    """Inject a fault scenario on a specific UE."""
    if req.ue not in UE_NETNS:
        return {"error": f"unknown UE: {req.ue}"}
    if req.fault_type not in FAULT_CONFIGS:
        return {"error": f"unknown fault: {req.fault_type}. Options: {list(FAULT_CONFIGS.keys())}"}
    if req.severity not in ("low", "medium", "high"):
        return {"error": "severity must be low, medium, or high"}

    config = FAULT_CONFIGS[req.fault_type][req.severity]

    # Get current gains to save pre-fault state
    current = broker.get_status()
    current_gains = current.get("gains", {})
    pre_dl = current_gains.get(f"{req.ue}_dl", 1.0)
    pre_ul = current_gains.get(f"{req.ue}_ul", 1.0)

    # Save fault state
    active_faults[req.ue] = {
        "type": req.fault_type,
        "severity": req.severity,
        "started_at": time.time(),
        "pre_gains": {"dl": pre_dl, "ul": pre_ul},
        "permanent": req.fault_type == "link_dropout" and req.severity == "high",
        "description": FAULT_DESCRIPTIONS[req.fault_type].format(ue=req.ue.upper()),
    }

    # Apply gain changes
    results = {}
    if config.get("dl") is not None:
        results["dl"] = broker.set_gain(req.ue, "dl", config["dl"])
    if config.get("ul") is not None:
        results["ul"] = broker.set_gain(req.ue, "ul", config["ul"])

    # Apply traffic actions
    traffic_action = config.get("traffic")
    if traffic_action == "stop":
        results["traffic"] = stop_iperf(req.ue)
    elif traffic_action and traffic_action.startswith("heavy_"):
        rate = traffic_action.replace("heavy_", "")
        results["traffic"] = start_iperf(
            req.ue, f"fault_stress_{rate}",
            bitrate=rate, pkt_size=1400, bidir=False,
        )

    # Permanent kill at high link_dropout
    if req.fault_type == "link_dropout" and req.severity == "high":
        results["kill"] = broker.kill(req.ue)

    return {
        "status": "injected",
        "ue": req.ue,
        "fault": req.fault_type,
        "severity": req.severity,
        "description": active_faults[req.ue]["description"],
        "permanent": active_faults[req.ue]["permanent"],
        "results": results,
    }


@app.post("/api/fault/clear")
async def clear_fault(req: FaultClearRequest):
    """Clear an active fault and restore pre-fault gains."""
    if req.ue not in active_faults:
        return {"status": "no active fault", "ue": req.ue}

    fault = active_faults[req.ue]

    if fault.get("permanent"):
        return {"error": f"{req.ue} was permanently killed. Cannot clear."}

    # Restore pre-fault gains
    pre = fault.get("pre_gains", {})
    results = {}
    if pre.get("dl") is not None:
        results["dl"] = broker.set_gain(req.ue, "dl", pre["dl"])
    if pre.get("ul") is not None:
        results["ul"] = broker.set_gain(req.ue, "ul", pre["ul"])

    del active_faults[req.ue]
    return {"status": "cleared", "ue": req.ue, "gains_restored": pre, "results": results}


@app.get("/api/fault/active")
async def get_active_faults():
    """Return all active faults."""
    return active_faults


@app.get("/api/fault/catalog")
async def fault_catalog():
    """Return available fault types and stress profiles."""
    return {
        "faults": [
            {"type": k, "severities": ["low", "medium", "high"],
             "description": v.format(ue="UE-X"),
             "permanent_at_high": k == "link_dropout"}
            for k, v in FAULT_DESCRIPTIONS.items()
        ],
        "stress_profiles": [
            {"id": k, "label": v["label"], "bitrate": v["bitrate"]}
            for k, v in STRESS_PROFILES.items()
        ],
    }


@app.get("/api/broker/status")
async def broker_status():
    """Direct broker status check (gains, killed UEs, connectivity)."""
    return broker.get_status()


# ---------------------------------------------------------------------------
# WebSocket — push live updates (expanded with gains + faults)
# ---------------------------------------------------------------------------

connected_clients: set[WebSocket] = set()


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    global connected_clients
    await ws.accept()
    connected_clients.add(ws)
    try:
        while True:
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        connected_clients.discard(ws)
    except Exception:
        connected_clients.discard(ws)


async def broadcast_loop():
    global connected_clients
    while True:
        await asyncio.sleep(1)
        if connected_clients:
            snapshot = build_full_snapshot()
            msg = json.dumps({"type": "snapshot", "data": snapshot})
            dead = set()
            for ws in connected_clients:
                try:
                    await ws.send_text(msg)
                except Exception:
                    dead.add(ws)
            connected_clients -= dead


@app.on_event("startup")
async def startup():
    asyncio.create_task(broadcast_loop())


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    print("=" * 60)
    print("  LTE Network Control Panel — Backend")
    print("  http://localhost:8080")
    print("  Broker control: tcp://127.0.0.1:4000")
    print("=" * 60)
    if broker.is_connected():
        print("  ✓ Broker connected")
    else:
        print("  ✗ Broker not reachable (start broker first)")
    print("=" * 60)
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")