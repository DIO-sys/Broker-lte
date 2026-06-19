#!/usr/bin/env python3
"""
FastAPI backend for the Cell Topology Monitor.
Reads srsRAN UE CSV metrics files and serves live data via REST + WebSocket.

CSV columns (semicolon-separated):
  time;cc;earfcn;pci;rsrp;pl;cfo;pci_neigh;rsrp_neigh;cfo_neigh;
  dl_mcs;dl_snr;dl_turbo;dl_brate;dl_bler;ul_ta;distance_km;speed_kmph;
  ul_mcs;ul_buff;ul_brate;ul_bler;rf_o;rf_u;rf_l;is_attached;
  proc_rmem;proc_rmem_kB;proc_vmem_kB;sys_mem;sys_load;thread_count;
  cpu_0;cpu_1;...cpu_N

Usage:
    pip install fastapi uvicorn
    python3 backend.py
"""

import asyncio
import csv
import json
import os
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

app = FastAPI(title="Cell Topology Monitor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

UE_CONFIGS = [
    {
        "id": "ue1",
        "label": "UE1",
        "imsi": "901700123456789",
        "ip": "172.16.0.2",
        "csv_path": "/tmp/ue1_metrics.csv",
        "zmq_tx": 3001,
        "zmq_rx": 3000,
    },
    {
        "id": "ue2",
        "label": "UE2",
        "imsi": "901700123456790",
        "ip": "172.16.0.3",
        "csv_path": "/tmp/ue2_metrics.csv",
        "zmq_tx": 3011,
        "zmq_rx": 3010,
    },
    {
        "id": "ue3",
        "label": "UE3",
        "imsi": "901700123456791",
        "ip": "172.16.0.4",
        "csv_path": "/tmp/ue3_metrics.csv",
        "zmq_tx": 3021,
        "zmq_rx": 3020,
    },
]

ENB_CONFIG = {
    "id": "enb1",
    "label": "eNB1",
    "cell_id": "0x01",
    "pci": 1,
    "earfcn": 2850,
    "n_prb": 50,
    "zmq_tx": 3101,
    "zmq_rx": 3100,
}

# CSV column names (from srsRAN UE metrics output)
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
# cpu_0..cpu_N are dynamic, handled separately

# Track file positions for incremental reads
file_positions: dict[str, int] = {}
latest_metrics: dict[str, dict] = {}
metrics_history: dict[str, list] = {ue["id"]: [] for ue in UE_CONFIGS}
HISTORY_MAX = 300  # ~5 min at 1s interval

start_time = time.time()
sample_count = 0


def parse_csv_line(line: str) -> Optional[dict]:
    """Parse a semicolon-separated metrics line into a dict."""
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

    # Remaining columns are per-CPU usage
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
    """Read the latest line from a UE's metrics CSV."""
    global sample_count

    if not os.path.exists(csv_path):
        return None

    try:
        with open(csv_path, "r") as f:
            lines = f.readlines()

        # Skip header and comment lines
        data_lines = [
            l for l in lines
            if l.strip() and not l.startswith("#") and not l.startswith("time;")
        ]

        if not data_lines:
            return None

        latest = parse_csv_line(data_lines[-1])
        if latest:
            sample_count += 1

            # Also grab recent history
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


def classify_signal(rsrp: Optional[float]) -> str:
    if rsrp is None or rsrp == 0:
        return "Unknown"
    if rsrp >= 50:
        return "Excellent"
    if rsrp >= 30:
        return "Good"
    if rsrp >= 10:
        return "Fair"
    return "Weak"


def classify_bler(bler: Optional[float]) -> str:
    if bler is None:
        return "Unknown"
    if bler == 0:
        return "Clean"
    if bler <= 2:
        return "Low"
    if bler <= 10:
        return "Fair"
    return "High"


def classify_throughput(brate: Optional[float]) -> str:
    if brate is None or brate <= 0:
        return "Idle"
    if brate < 10000:
        return "Low"
    if brate < 100000:
        return "Active"
    return "High"


def build_ue_snapshot(ue_cfg: dict) -> dict:
    """Build a structured UE status object from latest metrics."""
    metrics = latest_metrics.get(ue_cfg["id"])

    if metrics is None:
        return {
            "id": ue_cfg["id"],
            "label": ue_cfg["label"],
            "imsi": ue_cfg["imsi"],
            "ip": None,
            "status": "No Data",
            "zmq": {"tx": ue_cfg["zmq_tx"], "rx": ue_cfg["zmq_rx"]},
            "rf": None,
            "throughput": None,
            "process": None,
        }

    attached = metrics.get("is_attached", 0) == 1.0
    has_traffic = (metrics.get("dl_brate", 0) or 0) > 0 or (metrics.get("ul_brate", 0) or 0) > 0

    if attached and has_traffic:
        status = "Online"
    elif attached:
        status = "Idle"
    else:
        status = "Detached"

    return {
        "id": ue_cfg["id"],
        "label": ue_cfg["label"],
        "imsi": ue_cfg["imsi"],
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
    }


def build_full_snapshot() -> dict:
    """Build the complete network snapshot."""
    # Refresh metrics from CSVs
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
    }


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.get("/")
async def serve_frontend():
    """Serve the frontend HTML."""
    html_path = Path(__file__).parent / "frontend.html"
    if html_path.exists():
        return FileResponse(html_path, media_type="text/html")
    return HTMLResponse("<h1>frontend.html not found</h1>", status_code=404)


@app.get("/api/status")
async def get_status():
    """Get current network snapshot."""
    return build_full_snapshot()


@app.get("/api/history/{ue_id}")
async def get_history(ue_id: str, limit: int = 60):
    """Get recent metric history for a specific UE."""
    if ue_id not in metrics_history:
        return {"error": f"Unknown UE: {ue_id}"}
    hist = metrics_history[ue_id][-limit:]
    return {"ue_id": ue_id, "count": len(hist), "metrics": hist}


@app.get("/api/export")
async def export_dataset():
    """Export full history as JSON dataset."""
    return {
        "exported_at": time.time(),
        "enb": ENB_CONFIG,
        "ues": {
            ue["id"]: metrics_history[ue["id"]]
            for ue in UE_CONFIGS
        },
    }


# ---------------------------------------------------------------------------
# WebSocket — push live updates
# ---------------------------------------------------------------------------

connected_clients: set[WebSocket] = set()


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    global connected_clients
    await ws.accept()
    connected_clients.add(ws)
    try:
        while True:
            # Client can send commands (future: fault injection triggers)
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        connected_clients.discard(ws)
    except Exception:
        connected_clients.discard(ws)


async def broadcast_loop():
    global connected_clients
    """Push snapshots to all connected WebSocket clients every second."""
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
    print("  Cell Topology Monitor — Backend")
    print("  http://localhost:8080")
    print("=" * 60)
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")