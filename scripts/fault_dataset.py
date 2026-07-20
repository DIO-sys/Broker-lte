#!/usr/bin/env python3
"""
fault_dataset.py — Sprint 2 Step 1: Labeled fault dataset generation

For every (fault_type, severity, target_ue) combination, runs a full
labeled session against the live backend:

    clean (60s) -> inject -> onset (5s) -> active -> clear -> recovery (60s)

Every sample in every phase is tagged with ground truth (fault_type,
severity, target_ue, phase) and the full snapshot at that instant. This
is the CNN's fault_active training data and the LSTM's clean/anomaly/
recovery transition data, from the same sessions.

Prerequisite: Step 0 (step0_baseline.py) should already be done — this
script assumes the broker artifact floor and kill/revive reversibility
are known-good, and doesn't re-verify them.

Design notes:
  - Sessions run one at a time, serially, each starting from a full
    reset + preflight. No parallel fault injection across UEs — keeps
    every session's ground truth unambiguous.
  - Each session starts a `voice_call` iperf profile on all 3 UEs before
    the clean phase, so transport_stall / scheduler_starvation have real
    traffic to collapse/starve. This requires iperf3 servers listening
    on the SPGW side (172.16.0.1:5201-5203) — the script starts and
    stops them itself (see IperfServerManager) unless --no-iperf-servers
    is passed (use that if you're running them manually / they're
    already up).
  - active_s per fault is informed by the backend's live fault catalog:
    for ramp faults (bler_degradation), active_s is stretched to cover
    the full measured ramp duration + settle time, never guessed.
  - Resumable: pass --resume to skip combos whose session file already
    exists in the output directory.

Usage:
    # single combo smoke test first, always do this before a full run
    python3 fault_dataset.py --faults transport_stall --ues ue2 --dry-run
    python3 fault_dataset.py --faults transport_stall --ues ue2

    # full battery (54 sessions, hours) with confirmation prompt
    python3 fault_dataset.py

    # full battery, no prompt, resumable
    python3 fault_dataset.py --yes --resume

    # regenerate specific known-bad combo(s), deleting old data first
    python3 fault_dataset.py --faults transport_stall --severities medium --ues ue3 --yes --force
    python3 fault_dataset.py --faults transport_stall --severities high --ues ue2,ue3 --yes --force

Output:
    ml/fault_dataset/
        sessions/<fault_type>__<severity>__<ue>__<timestamp>.jsonl
        manifest.json         (appended to after every session)
"""

import argparse
import itertools
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Backend client
# ---------------------------------------------------------------------------

class BackendClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(f"{self.base_url}{path}", timeout=10) as r:
            return json.loads(r.read().decode())

    def _post(self, path: str, body: dict) -> dict:
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{self.base_url}{path}", data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body_text = e.read().decode(errors="replace")
            raise RuntimeError(f"POST {path} -> HTTP {e.code}: {body_text}") from e

    def status(self) -> dict:
        return self._get("/api/status")

    def reset(self) -> dict:
        return self._post("/api/reset", {})

    def fault_catalog(self) -> dict:
        return self._get("/api/fault/catalog")

    def inject_fault(self, ue: str, fault_type: str, severity: str) -> dict:
        return self._post("/api/fault", {"ue": ue, "fault_type": fault_type, "severity": severity})

    def clear_fault(self, ue: str) -> dict:
        return self._post("/api/fault/clear", {"ue": ue})

    def traffic_start(self, ue: str, profile: str, duration: int = 3600) -> dict:
        return self._post("/api/traffic", {"ue": ue, "action": "start",
                                            "profile": profile, "duration": duration})

    def traffic_stop(self, ue: str) -> dict:
        return self._post("/api/traffic", {"ue": ue, "action": "stop"})


# ---------------------------------------------------------------------------
# iperf3 server management (SPGW side, ports 5201-5203)
# ---------------------------------------------------------------------------

class IperfServerManager:
    def __init__(self, ports=(5201, 5202, 5203)):
        self.ports = ports
        self.procs = []

    def start(self):
        print(f"Starting iperf3 servers on ports {list(self.ports)} ...")
        for port in self.ports:
            proc = subprocess.Popen(
                ["iperf3", "-s", "-p", str(port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self.procs.append(proc)
        time.sleep(1)
        alive = [p.poll() is None for p in self.procs]
        if not all(alive):
            self.stop()
            raise RuntimeError(
                "One or more iperf3 servers failed to start (port already in use? "
                "iperf3 not installed?). Check manually with `iperf3 -s -p 5201`."
            )
        print(f"  {len(self.procs)} iperf3 servers up.")

    def stop(self):
        for proc in self.procs:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self.procs = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def wait_for_traffic(client: BackendClient, ue_ids, timeout_s=30, min_brate=1):
    deadline = time.time() + timeout_s
    pending = set(ue_ids)
    while time.time() < deadline and pending:
        snap = client.status()
        still_pending = set()
        for ue_id in pending:
            u = next(u for u in snap["ues"] if u["id"] == ue_id)
            dl = (u.get("throughput") or {}).get("dl_brate", {}).get("value") or 0
            ul = (u.get("throughput") or {}).get("ul_brate", {}).get("value") or 0
            if u["status"] not in ("Online", "Idle") or (dl < min_brate and ul < min_brate):
                still_pending.add(ue_id)
        pending = still_pending
        if pending:
            time.sleep(1)
    if pending:
        raise RuntimeError(
            f"Preflight failed: no traffic on {sorted(pending)} within {timeout_s}s. "
            f"Verify keepalives are running."
        )


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


ALL_FAULTS = [
    "co_channel_interference", "bler_degradation", "transport_stall",
    "link_dropout", "scheduler_starvation", "uplink_contamination",
]
ALL_SEVERITIES = ["low", "medium", "high"]
ALL_UES = ["ue1", "ue2", "ue3"]

# Fallback default active-window length per fault type (seconds), used
# when the catalog has no ramp info for that (fault, severity). Chosen
# to give each fault's signature time to fully develop per the project
# doc's fault descriptions (§ Fault -> Response Mapping).
DEFAULT_ACTIVE_S = {
    "co_channel_interference": 45,
    "bler_degradation": 60,       # overridden upward by ramp secs, see resolve_active_s
    "transport_stall": 45,
    "link_dropout": 45,
    "scheduler_starvation": 60,
    "uplink_contamination": 45,
}


def resolve_active_s(catalog: dict, fault_type: str, severity: str, floor_s: int) -> int:
    """Stretch active_s to cover a fault's ramp duration (if any) + 15s settle,
    never guessing shorter than the ramp actually needs to complete."""
    base = max(DEFAULT_ACTIVE_S.get(fault_type, 45), floor_s)
    for f in catalog.get("faults", []):
        if f["type"] != fault_type:
            continue
        mech = f.get("mechanism", {}).get(severity, {})
        ramp = mech.get("ramp")
        if ramp and "secs" in ramp:
            return max(base, ramp["secs"] + 15)
    return base


# ---------------------------------------------------------------------------
# Session runner
# ---------------------------------------------------------------------------

def run_session(client: BackendClient, out_dir: Path, fault_type: str, severity: str, ue: str,
                 ue_ids, clean_s: int, active_s: int, recovery_s: int, poll_interval: float,
                 baseline_profile: str):
    session_id = f"{fault_type}__{severity}__{ue}"
    stamp = utc_stamp()
    session_file = out_dir / "sessions" / f"{session_id}__{stamp}.jsonl"
    session_file.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n--- Session: {session_id} ---")

    print("  reset + preflight ...")
    client.reset()
    time.sleep(2)
    wait_for_traffic(client, ue_ids)

    print(f"  starting baseline traffic ({baseline_profile}) on all UEs ...")
    for uid in ue_ids:
        client.traffic_start(uid, baseline_profile)
    time.sleep(3)  # let iperf sessions actually establish before measuring "clean"

    samples = []

    def record(phase):
        snap = client.status()
        samples.append({
            "t": time.time(), "phase": phase,
            "fault_type": fault_type, "severity": severity, "target_ue": ue,
            "snapshot": snap,
        })

    def run_phase(label, duration_s):
        t_end = time.time() + duration_s
        while time.time() < t_end:
            record(label)
            time.sleep(poll_interval)

    print(f"  clean phase ({clean_s}s) ...")
    run_phase("clean", clean_s)

    print(f"  injecting {fault_type}/{severity} on {ue} ...")
    inject_resp = client.inject_fault(ue, fault_type, severity)
    if "error" in inject_resp:
        raise RuntimeError(f"inject_fault failed: {inject_resp}")

    onset_s = min(5, active_s)
    print(f"  onset phase ({onset_s}s) ...")
    run_phase("onset", onset_s)

    remaining_active = max(active_s - onset_s, 0)
    print(f"  active phase ({remaining_active}s) ...")
    run_phase("active", remaining_active)

    print(f"  clearing fault ...")
    clear_resp = client.clear_fault(ue)

    print(f"  recovery phase ({recovery_s}s) ...")
    run_phase("recovery", recovery_s)

    for uid in ue_ids:
        client.traffic_stop(uid)

    with open(session_file, "w") as f:
        for row in samples:
            f.write(json.dumps(row) + "\n")

    print(f"  wrote {len(samples)} samples -> {session_file}")

    return {
        "session_id": session_id, "fault_type": fault_type, "severity": severity,
        "target_ue": ue, "file": str(session_file), "n_samples": len(samples),
        "clean_s": clean_s, "active_s": active_s, "recovery_s": recovery_s,
        "inject_response_ok": "error" not in inject_resp,
        "clear_response_ok": "error" not in clear_resp,
        "completed_at": time.time(),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def already_done(out_dir: Path, session_id: str) -> bool:
    return any((out_dir / "sessions").glob(f"{session_id}__*.jsonl"))


def append_manifest(out_dir: Path, entry: dict):
    manifest_path = out_dir / "manifest.json"
    manifest = []
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
    manifest.append(entry)
    manifest_path.write_text(json.dumps(manifest, indent=2))


def force_clear_combo(out_dir: Path, session_id: str):
    """Delete existing session file(s) for this combo and drop their manifest
    entries, so a rerun regenerates clean data instead of appending a second,
    ambiguous entry alongside a known-bad one."""
    removed_files = []
    for f in (out_dir / "sessions").glob(f"{session_id}__*.jsonl"):
        f.unlink()
        removed_files.append(str(f))

    manifest_path = out_dir / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        kept = [e for e in manifest if e["session_id"] != session_id]
        removed_entries = len(manifest) - len(kept)
        manifest_path.write_text(json.dumps(kept, indent=2))
    else:
        removed_entries = 0

    if removed_files or removed_entries:
        print(f"  --force: cleared {session_id} "
              f"({len(removed_files)} file(s), {removed_entries} manifest entr{'y' if removed_entries==1 else 'ies'})")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", default="http://localhost:8080")
    ap.add_argument("--faults", default=",".join(ALL_FAULTS))
    ap.add_argument("--severities", default=",".join(ALL_SEVERITIES))
    ap.add_argument("--ues", default=",".join(ALL_UES))
    ap.add_argument("--clean-s", type=int, default=60)
    ap.add_argument("--active-s", type=int, default=0, help="floor for active phase; actual may be longer for ramp faults (0 = use per-fault defaults)")
    ap.add_argument("--recovery-s", type=int, default=60)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--baseline-profile", default="voice_call")
    ap.add_argument("--out", default="ml/fault_dataset")
    ap.add_argument("--no-iperf-servers", action="store_true", help="don't start/stop iperf3 servers (assume already running)")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--force", action="store_true", help="delete existing session file(s) for the requested combo(s) and regenerate them, even without --resume")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    args = ap.parse_args()

    faults = args.faults.split(",")
    severities = args.severities.split(",")
    ues = args.ues.split(",")
    ue_ids = ALL_UES  # always preflight/report against the full 3-UE cell

    for f in faults:
        if f not in ALL_FAULTS:
            print(f"FATAL: unknown fault type '{f}'. Options: {ALL_FAULTS}", file=sys.stderr)
            sys.exit(1)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    client = BackendClient(args.backend)
    try:
        catalog = client.fault_catalog()
    except Exception as e:
        print(f"FATAL: cannot reach backend at {args.backend}: {e}", file=sys.stderr)
        sys.exit(1)

    combos = list(itertools.product(faults, severities, ues))

    if args.force:
        print(f"\n--force: clearing existing data for {len(combos)} requested combo(s) before planning ...")
        for fault_type, severity, ue in combos:
            session_id = f"{fault_type}__{severity}__{ue}"
            force_clear_combo(out_dir, session_id)

    plan = []
    total_s = 0
    for fault_type, severity, ue in combos:
        session_id = f"{fault_type}__{severity}__{ue}"
        # after --force, the combo's old data is already gone, so this will
        # never skip it; --resume still applies normally to everything else
        if args.resume and not args.force and already_done(out_dir, session_id):
            continue
        active_s = resolve_active_s(catalog, fault_type, severity, args.active_s)
        dur = args.clean_s + active_s + args.recovery_s
        total_s += dur
        plan.append((fault_type, severity, ue, active_s, dur))

    print(f"\nPlanned sessions: {len(plan)} (of {len(combos)} requested combos)")
    for fault_type, severity, ue, active_s, dur in plan:
        print(f"  {fault_type:28s} {severity:6s} {ue:4s}  active={active_s:3d}s  total~{dur:4d}s")
    print(f"\nEstimated total wall time: {total_s/60:.1f} minutes ({total_s/3600:.2f} hours)")

    if args.dry_run:
        print("\n--dry-run: not executing.")
        return

    if not plan:
        print("Nothing to do (all combos already have session files; drop --resume to rerun).")
        return

    if not args.yes:
        resp = input("\nProceed? [y/N] ").strip().lower()
        if resp != "y":
            print("Aborted.")
            return

    iperf_mgr = None
    try:
        if not args.no_iperf_servers:
            iperf_mgr = IperfServerManager()
            iperf_mgr.start()

        print("Resetting cell before starting ...")
        client.reset()
        time.sleep(2)

        for i, (fault_type, severity, ue, active_s, dur) in enumerate(plan, 1):
            print(f"\n[{i}/{len(plan)}] ", end="")
            entry = run_session(
                client, out_dir, fault_type, severity, ue, ue_ids,
                args.clean_s, active_s, args.recovery_s, args.interval,
                args.baseline_profile,
            )
            append_manifest(out_dir, entry)

        print(f"\nAll {len(plan)} sessions complete. Manifest: {out_dir / 'manifest.json'}")

    except KeyboardInterrupt:
        print("\nInterrupted — cleaning up (reset + stop traffic). Completed sessions are saved; rerun with --resume to continue.")
    finally:
        try:
            for uid in ue_ids:
                client.traffic_stop(uid)
            client.reset()
        except Exception as e:
            print(f"Warning: cleanup reset failed: {e}", file=sys.stderr)
        if iperf_mgr is not None:
            iperf_mgr.stop()


if __name__ == "__main__":
    main()