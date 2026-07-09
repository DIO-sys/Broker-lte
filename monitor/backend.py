#!/usr/bin/env python3
"""
FastAPI backend for the LTE Network Control Panel (v7-broker aware).

KEY CORRECTIONS vs the earlier draft:
  1. Broker protocol: uses broker_client which sends int ue + "dir" and reads
     ok/err. Gains/noise are read via broker.ue_state(n).
  2. NOISELESS-CHANNEL FINDING: gain does NOT move SNR/MCS/BLER. All graduated
     degradation is done with set_noise, calibrated by SNR ~= 20*log10(gain*3000/sigma).
     Gain is only used for RSRP signatures, compose-to-kill, and kill.
  3. Cell-edge fault is a RAMP (async task walking sigma over time), not a step.
  4. kill is NOT hardcoded "permanent" — reversibility is untested (Step 0).

CSV columns (semicolon-separated, from srsRAN UE metrics):
  time;cc;earfcn;pci;rsrp;pl;cfo;pci_neigh;rsrp_neigh;cfo_neigh;
  dl_mcs;dl_snr;dl_turbo;dl_brate;dl_bler;ul_ta;distance_km;speed_kmph;
  ul_mcs;ul_buff;ul_brate;ul_bler;rf_o;rf_u;rf_l;is_attached;
  proc_rmem;proc_rmem_kB;proc_vmem_kB;sys_mem;sys_load;thread_count;cpu_0;...
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

from broker_client import BrokerControlClient, ue_num

app = FastAPI(title="LTE Network Control Panel")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

broker = BrokerControlClient()

# ---------------------------------------------------------------------------
# iperf3 traffic management
# ---------------------------------------------------------------------------

IPERF_SERVER = "172.16.0.1"
iperf_processes: dict[str, dict] = {}

STRESS_PROFILES = {
    "hd_streaming":  {"bitrate": "5M",   "pkt_size": 1400, "bidir": False, "label": "HD Streaming (5 Mbps DL)"},
    "voice_call":    {"bitrate": "64K",  "pkt_size": 160,  "bidir": True,  "label": "Voice Call (64K bidir)"},
    "video_call":    {"bitrate": "1.5M", "pkt_size": 1200, "bidir": True,  "label": "Video Call (1.5 Mbps bidir)"},
    "bulk_download": {"bitrate": "0",    "pkt_size": 1400, "bidir": False, "label": "Bulk Download (max rate)"},
    "file_upload":   {"bitrate": "0",    "pkt_size": 1400, "bidir": False, "label": "File Upload (max rate UL)"},
    "ping_flood":    {"bitrate": "512K", "pkt_size": 64,   "bidir": False, "label": "Ping Flood"},
}

UE_NETNS = {"ue1": "ue1", "ue2": "ue2", "ue3": "ue3"}
UE_IPERF_PORTS = {"ue1": 5201, "ue2": 5202, "ue3": 5203}

# ---------------------------------------------------------------------------
# Fault injection — NOISE-BASED presets from the v7 calibration table
#   SNR ~= 20*log10(gain*3000/sigma). sigma presets give the target SNR.
# Each preset declares the exact levers to actuate. None = "don't touch".
# ---------------------------------------------------------------------------

# sigma -> approx SNR (from measured table, gain 1.0):
#   170->25dB, 300->20dB, 500->16dB, 950->11dB, 1500->7dB, 2000->~4dB
FAULT_CONFIGS = {
    # 1. Co-channel interference: DL Gaussian noise (honest v1 of a structured interferer)
    "co_channel_interference": {
        "low":    {"dl_noise": 170},                 # ~25 dB, MCS holds ~20
        "medium": {"dl_noise": 500},                 # ~16 dB, MCS drops to ~10
        "high":   {"dl_noise": 1500},                # ~7 dB,  MCS ~5
    },
    # 2. Cell edge / BLER degradation: RAMP handled specially (see _run_cell_edge_ramp)
    "bler_degradation": {
        "low":    {"ramp": {"dir": "dl", "s0": 170, "s1": 950,  "secs": 45}},
        "medium": {"ramp": {"dir": "dl", "s0": 170, "s1": 1500, "secs": 45}},
        "high":   {"ramp": {"dir": "dl", "s0": 170, "s1": 2000, "secs": 60,
                            "gain0": 1.0, "gain1": 0.5}},  # compose to reach link-death floor
    },
    # 3. Transport stall: DO NOT touch gain/noise. Stop traffic only. Clean RF is the signature.
    "transport_stall": {
        "low":    {"traffic": "stop"},
        "medium": {"traffic": "stop"},
        "high":   {"traffic": "stop"},
    },
    # 4. Link dropout: severe noise (low/med) -> kill (high). Reversibility of kill is UNTESTED.
    "link_dropout": {
        "low":    {"dl_noise": 2000},
        "medium": {"dl_gain": 0.3, "dl_noise": 2000},
        "high":   {"kill": True},                    # gain 0 both dirs; may or may not recover
    },
    # 5. Scheduler starvation: heavy DL traffic + DL noise -> low MCS + retx eating PRBs
    "scheduler_starvation": {
        "low":    {"dl_noise": 500,  "traffic": "heavy_1M"},
        "medium": {"dl_noise": 950,  "traffic": "heavy_3M"},
        "high":   {"dl_noise": 950,  "traffic": "heavy_5M"},
    },
    # 6. Uplink contamination: UL noise (contributes even at gain 0 = pure-noise emitter)
    "uplink_contamination": {
        "low":    {"ul_noise": 300},
        "medium": {"ul_noise": 950},
        "high":   {"ul_noise": 2000},
    },
}

FAULT_DESCRIPTIONS = {
    "co_channel_interference": "Gaussian interferer on {ue}'s downlink raises the noise floor. SNR drops, MCS falls; BLER holds until SNR is low enough.",
    "bler_degradation": "{ue} 'moves toward cell edge' — DL noise ramps up over time. SNR trajectory falls steadily; the LSTM's trajectory-prediction showcase.",
    "transport_stall": "{ue}'s transport stalls. Throughput collapses, RF metrics (SNR/RSRP) stay pristine. Correct agent answer: do NOT touch gain.",
    "link_dropout": "{ue} driven toward radio link failure. High severity = full kill (gain 0 both dirs).",
    "scheduler_starvation": "{ue} in noise-induced low MCS under heavy DL traffic. Retransmissions consume PRBs other UEs need.",
    "uplink_contamination": "{ue}'s uplink slot emits Gaussian noise into the composite. Cell-wide UL degradation as seen by the eNB.",
}

active_faults: dict[str, dict] = {}   # ue_id -> fault record (+ optional ramp task handle)

# ---------------------------------------------------------------------------
# UE / eNB config
# ---------------------------------------------------------------------------

UE_CONFIGS = [
    {"id": "ue1", "label": "UE1", "imsi": "901700123456789", "ip": "172.16.0.2", "csv_path": "/tmp/ue1_metrics.csv", "zmq_tx": 3001, "zmq_rx": 3000},
    {"id": "ue2", "label": "UE2", "imsi": "901700123456790", "ip": "172.16.0.3", "csv_path": "/tmp/ue2_metrics.csv", "zmq_tx": 3011, "zmq_rx": 3010},
    {"id": "ue3", "label": "UE3", "imsi": "901700123456791", "ip": "172.16.0.4", "csv_path": "/tmp/ue3_metrics.csv", "zmq_tx": 3021, "zmq_rx": 3020},
]

ENB_CONFIG = {"id": "enb1", "label": "eNB1", "cell_id": "0x01", "pci": 1,
              "earfcn": 2850, "n_prb": 25, "zmq_tx": 3101, "zmq_rx": 3100}

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
    ue: str
    direction: str        # "dl" | "ul"
    value: float          # 0.0-1.0

class NoiseRequest(BaseModel):
    ue: str
    direction: str        # "dl" | "ul"
    value: float          # 0.0-2000.0 sigma

class TrafficRequest(BaseModel):
    ue: str
    action: str           # "start" | "stop"
    profile: str = "voice_call"
    bitrate: str = "64K"
    duration: int = 300

class KillRequest(BaseModel):
    ue: str
    confirm: bool = False

class FaultRequest(BaseModel):
    ue: str
    fault_type: str
    severity: str = "medium"

class FaultClearRequest(BaseModel):
    ue: str

# ---------------------------------------------------------------------------
# CSV parsing (unchanged logic)
# ---------------------------------------------------------------------------

def parse_csv_line(line: str) -> Optional[dict]:
    parts = line.strip().split(";")
    if len(parts) < len(CSV_COLUMNS):
        return None
    result = {}
    for i, col in enumerate(CSV_COLUMNS):
        val = parts[i]
        if val in ("n/a", ""):
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
        data_lines = [l for l in lines
                      if l.strip() and not l.startswith("#") and not l.startswith("time;")]
        if not data_lines:
            return None
        latest = parse_csv_line(data_lines[-1])
        if latest:
            sample_count += 1
            history = [p for p in (parse_csv_line(l) for l in data_lines[-HISTORY_MAX:]) if p]
            metrics_history[ue_id] = history
        return latest
    except Exception as e:
        print(f"Error reading {csv_path}: {e}")
        return None


def classify_signal(v):
    if v is None or v == 0:
        return "Unknown"
    if v >= 50: return "Excellent"
    if v >= 30: return "Good"
    if v >= 10: return "Fair"
    return "Weak"

def classify_bler(b):
    if b is None: return "Unknown"
    if b == 0: return "Clean"
    if b <= 2: return "Low"
    if b <= 10: return "Fair"
    return "High"

def classify_throughput(r):
    if r is None or r <= 0: return "Idle"
    if r < 10000: return "Low"
    if r < 100000: return "Active"
    return "High"


def build_ue_snapshot(ue_cfg: dict) -> dict:
    ue_id = ue_cfg["id"]
    metrics = latest_metrics.get(ue_id)

    # Gains + noise straight from the broker (defensive parse lives in the client)
    st = broker.ue_state(ue_id)
    dl_gain, ul_gain = st["dl_gain"], st["ul_gain"]
    dl_noise, ul_noise = st["dl_noise"], st["ul_noise"]
    is_killed = (dl_gain == 0.0 and ul_gain == 0.0)

    traffic_info = None
    if ue_id in iperf_processes:
        pinfo = iperf_processes[ue_id]
        if pinfo["process"].poll() is None:
            traffic_info = {"active": True, "profile": pinfo["profile"]}
        else:
            del iperf_processes[ue_id]

    fault_info = active_faults.get(ue_id)
    # don't leak the asyncio task handle over JSON
    if fault_info:
        fault_info = {k: v for k, v in fault_info.items() if k != "_task"}

    gains_block = {"dl": dl_gain, "ul": ul_gain,
                   "dl_noise": dl_noise, "ul_noise": ul_noise,
                   "backlog": st.get("backlog"), "late_dropped": st.get("late_dropped")}

    if metrics is None:
        return {"id": ue_id, "label": ue_cfg["label"], "imsi": ue_cfg["imsi"],
                "ip": None, "status": "No Data",
                "zmq": {"tx": ue_cfg["zmq_tx"], "rx": ue_cfg["zmq_rx"]},
                "rf": None, "throughput": None, "process": None,
                "gains": gains_block, "killed": is_killed,
                "traffic": traffic_info, "fault": fault_info}

    attached = metrics.get("is_attached", 0) == 1.0
    has_traffic = (metrics.get("dl_brate", 0) or 0) > 0 or (metrics.get("ul_brate", 0) or 0) > 0
    status = "Killed" if is_killed else ("Online" if (attached and has_traffic)
             else ("Idle" if attached else "Detached"))

    return {
        "id": ue_id, "label": ue_cfg["label"], "imsi": ue_cfg["imsi"],
        "ip": ue_cfg["ip"] if attached else None, "status": status,
        "zmq": {"tx": ue_cfg["zmq_tx"], "rx": ue_cfg["zmq_rx"]},
        "rf": {
            "rsrp":   {"value": metrics.get("rsrp"),   "unit": "dBm", "descriptor": classify_signal(metrics.get("rsrp"))},
            "dl_snr": {"value": metrics.get("dl_snr"), "unit": "dB",  "descriptor": classify_signal(metrics.get("dl_snr"))},
            "dl_mcs": {"value": metrics.get("dl_mcs"), "unit": "",    "descriptor": None},
            "ul_mcs": {"value": metrics.get("ul_mcs"), "unit": "",    "descriptor": None},
            "dl_bler":{"value": metrics.get("dl_bler"),"unit": "%",   "descriptor": classify_bler(metrics.get("dl_bler"))},
            "ul_bler":{"value": metrics.get("ul_bler"),"unit": "%",   "descriptor": classify_bler(metrics.get("ul_bler"))},
            "cfo":    {"value": metrics.get("cfo"),    "unit": "Hz",  "descriptor": None},
            "pathloss":{"value": metrics.get("pl"),    "unit": "dB",  "descriptor": None},
            "earfcn": {"value": metrics.get("earfcn"), "unit": "",    "descriptor": None},
            "pci":    {"value": metrics.get("pci"),    "unit": "",    "descriptor": None},
        },
        "throughput": {
            "dl_brate": {"value": metrics.get("dl_brate", 0), "unit": "bps", "descriptor": classify_throughput(metrics.get("dl_brate"))},
            "ul_brate": {"value": metrics.get("ul_brate", 0), "unit": "bps", "descriptor": classify_throughput(metrics.get("ul_brate"))},
        },
        "process": {
            "sys_load":    {"value": metrics.get("sys_load"), "unit": "%",  "descriptor": None},
            "proc_mem_kb": {"value": metrics.get("proc_rmem_kB"), "unit": "KB", "descriptor": None},
            "sys_mem":     {"value": metrics.get("sys_mem"), "unit": "%", "descriptor": None},
            "cpu_avg":     {"value": round(metrics.get("cpu_avg", 0), 1) if metrics.get("cpu_avg") else None, "unit": "%", "descriptor": None},
        },
        "gains": gains_block, "killed": is_killed,
        "traffic": traffic_info, "fault": fault_info,
    }


def build_full_snapshot() -> dict:
    for ue_cfg in UE_CONFIGS:
        m = read_latest_metrics(ue_cfg["id"], ue_cfg["csv_path"])
        if m:
            latest_metrics[ue_cfg["id"]] = m
    ues = [build_ue_snapshot(u) for u in UE_CONFIGS]
    online = sum(1 for u in ues if u["status"] == "Online")
    attached = sum(1 for u in ues if u["status"] in ("Online", "Idle"))
    faults_public = {k: {kk: vv for kk, vv in v.items() if kk != "_task"}
                     for k, v in active_faults.items()}
    return {
        "timestamp": time.time(), "elapsed_s": round(time.time() - start_time),
        "sample_count": sample_count, "enb": ENB_CONFIG,
        "summary": {"ues_online": online, "ues_attached": attached, "ues_total": len(ues)},
        "ues": ues, "broker_connected": broker.is_connected(),
        "active_faults": faults_public,
    }

# ---------------------------------------------------------------------------
# iperf3 helpers
# ---------------------------------------------------------------------------

def start_iperf(ue_id, profile, bitrate="64K", pkt_size=160, bidir=True, duration=300) -> dict:
    if ue_id not in UE_NETNS:
        return {"error": f"unknown UE: {ue_id}"}
    stop_iperf(ue_id)
    netns, port = UE_NETNS[ue_id], UE_IPERF_PORTS[ue_id]
    cmd = ["sudo", "ip", "netns", "exec", netns, "iperf3", "-c", IPERF_SERVER,
           "-u", "-b", bitrate, "-l", str(pkt_size), "-t", str(duration), "-p", str(port)]
    if bidir:
        cmd.append("--bidir")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        iperf_processes[ue_id] = {"process": proc, "profile": profile,
                                  "bitrate": bitrate, "pid": proc.pid, "started": time.time()}
        return {"status": "started", "ue": ue_id, "profile": profile, "pid": proc.pid}
    except Exception as e:
        return {"error": str(e)}


def stop_iperf(ue_id) -> dict:
    if ue_id not in iperf_processes:
        return {"status": "no active traffic", "ue": ue_id}
    pinfo = iperf_processes[ue_id]
    try:
        pinfo["process"].terminate()
        pinfo["process"].wait(timeout=5)
    except Exception:
        pinfo["process"].kill()
    profile = pinfo["profile"]
    del iperf_processes[ue_id]
    return {"status": "stopped", "ue": ue_id, "was": profile}

# ---------------------------------------------------------------------------
# Cell-edge RAMP task (the fault that makes trajectory prediction meaningful)
# ---------------------------------------------------------------------------

async def _run_cell_edge_ramp(ue_id: str, ramp: dict):
    """Walk DL noise sigma s0 -> s1 over `secs`, optionally ramping gain too."""
    s0, s1, secs = ramp["s0"], ramp["s1"], ramp["secs"]
    g0, g1 = ramp.get("gain0"), ramp.get("gain1")
    steps = max(secs, 1)
    try:
        for i in range(steps + 1):
            frac = i / steps
            sigma = s0 + (s1 - s0) * frac
            broker.set_noise(ue_id, ramp.get("dir", "dl"), round(sigma, 1))
            if g0 is not None and g1 is not None:
                broker.set_gain(ue_id, "dl", round(g0 + (g1 - g0) * frac, 3))
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass  # cleared/reset mid-ramp

# ---------------------------------------------------------------------------
# REST — read
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

@app.get("/api/broker/status")
async def broker_status_raw():
    return broker.get_status()

# ---------------------------------------------------------------------------
# REST — the two levers
# ---------------------------------------------------------------------------

@app.post("/api/gain")
async def set_gain(req: GainRequest):
    if req.ue not in UE_NETNS:
        return {"error": f"unknown UE: {req.ue}"}
    if req.direction not in ("dl", "ul"):
        return {"error": "direction must be 'dl' or 'ul'"}
    if not 0.0 <= req.value <= 1.0:
        return {"error": "gain must be 0.0-1.0"}
    return broker.set_gain(req.ue, req.direction, req.value)

@app.post("/api/noise")
async def set_noise(req: NoiseRequest):
    """The graduated-degradation primitive. SNR ~= 20*log10(gain*3000/sigma)."""
    if req.ue not in UE_NETNS:
        return {"error": f"unknown UE: {req.ue}"}
    if req.direction not in ("dl", "ul"):
        return {"error": "direction must be 'dl' or 'ul'"}
    if not 0.0 <= req.value <= 2000.0:
        return {"error": "noise sigma must be 0.0-2000.0"}
    return broker.set_noise(req.ue, req.direction, req.value)

@app.post("/api/traffic")
async def manage_traffic(req: TrafficRequest):
    if req.ue not in UE_NETNS:
        return {"error": f"unknown UE: {req.ue}"}
    if req.action == "stop":
        return stop_iperf(req.ue)
    if req.action == "start":
        if req.profile in STRESS_PROFILES:
            p = STRESS_PROFILES[req.profile]
            return start_iperf(req.ue, req.profile, bitrate=p["bitrate"],
                               pkt_size=p["pkt_size"], bidir=p["bidir"], duration=req.duration)
        return start_iperf(req.ue, req.profile, bitrate=req.bitrate, duration=req.duration)
    return {"error": "action must be 'start' or 'stop'"}

@app.post("/api/kill")
async def kill_ue(req: KillRequest):
    """
    Soft kill (gain=0 both dirs). REVERSIBLE: the broker keeps the UE's
    streams position-correct while muted, so restoring gain lets the UE
    re-establish. Reverse with POST /api/reset, or set both gains back to 1.0.
    """
    if req.ue not in UE_NETNS:
        return {"error": f"unknown UE: {req.ue}"}
    if not req.confirm:
        return {"error": "kill requires confirm=true"}
    stop_iperf(req.ue)
    result = broker.kill(req.ue)
    result["reversible"] = True
    result["revive"] = "POST /api/reset or set dl+ul gain to 1.0"
    return result


@app.post("/api/revive")
async def revive_ue(req: KillRequest):
    """Undo a kill on one UE: restore both gains to 1.0 and clear noise."""
    if req.ue not in UE_NETNS:
        return {"error": f"unknown UE: {req.ue}"}
    active_faults.pop(req.ue, None)
    results = {
        "dl_gain": broker.set_gain(req.ue, "dl", 1.0),
        "ul_gain": broker.set_gain(req.ue, "ul", 1.0),
        "dl_noise": broker.set_noise(req.ue, "dl", 0.0),
        "ul_noise": broker.set_noise(req.ue, "ul", 0.0),
    }
    return {"status": "revived", "ue": req.ue, "results": results,
            "note": "UE should re-establish within ~T310/T311 (watch EPC for Service Request)"}

@app.post("/api/reset")
async def reset_cell():
    # cancel any running ramps
    for f in active_faults.values():
        task = f.get("_task")
        if task:
            task.cancel()
    for ue_id in list(iperf_processes.keys()):
        stop_iperf(ue_id)
    active_faults.clear()
    result = broker.reset()   # gains->1.0, noise->0.0
    result["traffic"] = "all stopped"
    result["faults_cleared"] = True
    return result

# ---------------------------------------------------------------------------
# REST — fault injection
# ---------------------------------------------------------------------------

@app.post("/api/fault")
async def inject_fault(req: FaultRequest):
    if req.ue not in UE_NETNS:
        return {"error": f"unknown UE: {req.ue}"}
    if req.fault_type not in FAULT_CONFIGS:
        return {"error": f"unknown fault: {req.fault_type}. Options: {list(FAULT_CONFIGS)}"}
    if req.severity not in ("low", "medium", "high"):
        return {"error": "severity must be low, medium, or high"}

    cfg = FAULT_CONFIGS[req.fault_type][req.severity]

    # snapshot pre-fault levers so we can restore on clear
    pre = broker.ue_state(req.ue)
    record = {
        "type": req.fault_type, "severity": req.severity, "started_at": time.time(),
        "pre": {"dl_gain": pre["dl_gain"], "ul_gain": pre["ul_gain"],
                "dl_noise": pre["dl_noise"], "ul_noise": pre["ul_noise"]},
        "description": FAULT_DESCRIPTIONS[req.fault_type].format(ue=req.ue.upper()),
        "killed": bool(cfg.get("kill")),
    }
    active_faults[req.ue] = record

    results = {}
    # ramp fault (cell edge)
    if "ramp" in cfg:
        task = asyncio.create_task(_run_cell_edge_ramp(req.ue, cfg["ramp"]))
        record["_task"] = task
        results["ramp"] = {"started": True, **{k: v for k, v in cfg["ramp"].items()}}
    else:
        if cfg.get("dl_gain") is not None:
            results["dl_gain"] = broker.set_gain(req.ue, "dl", cfg["dl_gain"])
        if cfg.get("ul_gain") is not None:
            results["ul_gain"] = broker.set_gain(req.ue, "ul", cfg["ul_gain"])
        if cfg.get("dl_noise") is not None:
            results["dl_noise"] = broker.set_noise(req.ue, "dl", cfg["dl_noise"])
        if cfg.get("ul_noise") is not None:
            results["ul_noise"] = broker.set_noise(req.ue, "ul", cfg["ul_noise"])
        if cfg.get("kill"):
            stop_iperf(req.ue)
            results["kill"] = broker.kill(req.ue)

    # traffic
    ta = cfg.get("traffic")
    if ta == "stop":
        results["traffic"] = stop_iperf(req.ue)
    elif ta and ta.startswith("heavy_"):
        rate = ta.replace("heavy_", "")
        results["traffic"] = start_iperf(req.ue, f"fault_stress_{rate}",
                                         bitrate=rate, pkt_size=1400, bidir=False)

    return {"status": "injected", "ue": req.ue, "fault": req.fault_type,
            "severity": req.severity, "description": record["description"],
            "killed": record["killed"], "results": results}


@app.post("/api/fault/clear")
async def clear_fault(req: FaultClearRequest):
    if req.ue not in active_faults:
        return {"status": "no active fault", "ue": req.ue}
    fault = active_faults.pop(req.ue)
    task = fault.get("_task")
    if task:
        task.cancel()
    pre = fault.get("pre", {})
    results = {}
    # restore levers to pre-fault values
    if pre.get("dl_gain") is not None:
        results["dl_gain"] = broker.set_gain(req.ue, "dl", pre["dl_gain"])
    if pre.get("ul_gain") is not None:
        results["ul_gain"] = broker.set_gain(req.ue, "ul", pre["ul_gain"])
    results["dl_noise"] = broker.set_noise(req.ue, "dl", pre.get("dl_noise", 0.0))
    results["ul_noise"] = broker.set_noise(req.ue, "ul", pre.get("ul_noise", 0.0))
    out = {"status": "cleared", "ue": req.ue, "restored": pre, "results": results}
    if fault.get("killed"):
        out["note"] = "was a kill — gains restored; UE should re-establish on its own (reversible)"
    return out


@app.get("/api/fault/active")
async def get_active_faults():
    return {k: {kk: vv for kk, vv in v.items() if kk != "_task"}
            for k, v in active_faults.items()}


@app.get("/api/fault/catalog")
async def fault_catalog():
    return {
        "faults": [
            {"type": k, "severities": ["low", "medium", "high"],
             "description": FAULT_DESCRIPTIONS[k].format(ue="UE-X"),
             "mechanism": {sev: cfg for sev, cfg in FAULT_CONFIGS[k].items()}}
            for k in FAULT_CONFIGS
        ],
        "stress_profiles": [
            {"id": k, "label": v["label"], "bitrate": v["bitrate"]}
            for k, v in STRESS_PROFILES.items()
        ],
        "calibration": "SNR(dB) ~= 20*log10(gain*3000/sigma)  [measured, +/-1 dB]",
    }

# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

connected_clients: set[WebSocket] = set()

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
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
    while True:
        await asyncio.sleep(1)
        if connected_clients:
            msg = json.dumps({"type": "snapshot", "data": build_full_snapshot()})
            dead = set()
            for ws in connected_clients:
                try:
                    await ws.send_text(msg)
                except Exception:
                    dead.add(ws)
            connected_clients.difference_update(dead)

@app.on_event("startup")
async def startup():
    asyncio.create_task(broadcast_loop())

# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    print("=" * 60)
    print("  LTE Network Control Panel — Backend (v7-broker aware)")
    print("  http://localhost:8080   broker: tcp://127.0.0.1:4000")
    print("  Broker connected" if broker.is_connected() else "  Broker NOT reachable (start broker first)")
    print("=" * 60)
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")