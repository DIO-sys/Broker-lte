#!/usr/bin/env python3
"""
step0_baseline.py — Sprint 2 Step 0: Broker Artifact Characterization

Runs the three prerequisites the project doc requires BEFORE any ML
training data is collected:

  Phase 1 — Gain floor sweep
      Sweep one UE's DL/UL gain down from 1.0 to find the empirical
      decode floor (the gain below which the UE can't hold the link).
      Confirms/refines the "~0.03-0.05" estimate in the handover doc.

  Phase 2 — Detached-UE ZMQ behavior
      Soft-kill one UE (gain=0 both dirs, reversible per v7), observe
      broker instrumentation (backlog/late_dropped) and the OTHER UEs'
      metrics during the detach window, then revive and time the
      reattach. This is also the first real confirmation of the
      kill->revive round trip end-to-end (flagged as unconfirmed in
      the last handover doc).

  Phase 3 — 30-minute clean baseline
      Full 3-UE clean run (all gains 1.0, no noise, keepalive traffic
      verified nonzero) sampled at 1 Hz. Produces the broker artifact
      floor: BLER-nonzero rate, backlog/late_dropped distribution,
      per-metric mean/std — the numbers the LSTM autoencoder's anomaly
      threshold will be calibrated against, and the "is this actually
      anomalous or just broker noise" reference for every fault-dataset
      session that follows.

Talks only to the FastAPI backend (localhost:8080 by default) — never
touches the broker control port directly, since backend.py already
merges broker status + CSV metrics + process state into one snapshot.

Usage:
    python3 step0_baseline.py --phase all
    python3 step0_baseline.py --phase gain --sweep-ue ue3
    python3 step0_baseline.py --phase detach --detach-ue ue3
    python3 step0_baseline.py --phase baseline --duration 1800
    python3 step0_baseline.py --phase all --duration 300   # smoke test

Output:
    ml/baseline/<UTC timestamp>/
        gain_sweep.json
        detach_behavior.json
        clean_baseline_raw.jsonl      (one snapshot per line, streamed)
        clean_baseline_summary.json
        SUMMARY.md                   (human-readable rollup)

No third-party dependencies — stdlib only (urllib), so it runs inside
the project venv with nothing extra installed.
"""

import argparse
import json
import statistics
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
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body_text = e.read().decode(errors="replace")
            raise RuntimeError(f"POST {path} -> HTTP {e.code}: {body_text}") from e

    def status(self) -> dict:
        return self._get("/api/status")

    def reset(self) -> dict:
        return self._post("/api/reset", {})

    def set_gain(self, ue: str, direction: str, value: float) -> dict:
        return self._post("/api/gain", {"ue": ue, "direction": direction, "value": value})

    def kill(self, ue: str) -> dict:
        return self._post("/api/kill", {"ue": ue, "confirm": True})

    def revive(self, ue: str) -> dict:
        return self._post("/api/revive", {"ue": ue})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ue_snapshot(status: dict, ue_id: str) -> dict:
    for u in status.get("ues", []):
        if u["id"] == ue_id:
            return u
    raise KeyError(f"UE {ue_id} not found in snapshot")


def rf_value(ue_snap: dict, key: str):
    rf = ue_snap.get("rf")
    if not rf:
        return None
    entry = rf.get(key)
    return entry.get("value") if entry else None


def wait_for_traffic(client: BackendClient, ue_ids, timeout_s=30, min_brate=1):
    """Preflight: confirm keepalive/traffic is actually flowing on every UE
    before trusting any measurement. Backend doc flags this as a repeat
    failure mode ('keepalives die on every stack restart')."""
    deadline = time.time() + timeout_s
    pending = set(ue_ids)
    while time.time() < deadline and pending:
        snap = client.status()
        still_pending = set()
        for ue_id in pending:
            u = ue_snapshot(snap, ue_id)
            dl = (u.get("throughput") or {}).get("dl_brate", {}).get("value") or 0
            ul = (u.get("throughput") or {}).get("ul_brate", {}).get("value") or 0
            if u["status"] not in ("Online", "Idle") or (dl < min_brate and ul < min_brate):
                still_pending.add(ue_id)
        pending = still_pending
        if pending:
            time.sleep(1)
    if pending:
        raise RuntimeError(
            f"Preflight failed: no traffic detected on {sorted(pending)} within "
            f"{timeout_s}s. Verify keepalives are running before measuring anything."
        )


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def mean_std(values):
    values = [v for v in values if v is not None]
    if not values:
        return {"mean": None, "std": None, "n": 0}
    if len(values) == 1:
        return {"mean": values[0], "std": 0.0, "n": 1}
    return {"mean": statistics.mean(values), "std": statistics.stdev(values), "n": len(values)}


# ---------------------------------------------------------------------------
# Phase 1: gain floor sweep
# ---------------------------------------------------------------------------

DEFAULT_GAIN_STEPS = [1.0, 0.5, 0.3, 0.2, 0.15, 0.10, 0.07, 0.05, 0.04, 0.03, 0.02]


def phase_gain_sweep(client: BackendClient, ue: str, steps, settle_s=3.0, samples_per_step=5, sample_interval=0.5):
    print(f"\n=== Phase 1: gain floor sweep on {ue} ===")
    results = []
    try:
        for gain in steps:
            client.set_gain(ue, "dl", gain)
            client.set_gain(ue, "ul", gain)
            time.sleep(settle_s)

            samples = []
            for _ in range(samples_per_step):
                snap = client.status()
                u = ue_snapshot(snap, ue)
                samples.append({
                    "status": u["status"],
                    "rsrp": rf_value(u, "rsrp"),
                    "dl_snr": rf_value(u, "dl_snr"),
                    "dl_bler": rf_value(u, "dl_bler"),
                    "dl_mcs": rf_value(u, "dl_mcs"),
                })
                time.sleep(sample_interval)

            attached_frac = sum(1 for s in samples if s["status"] in ("Online", "Idle")) / len(samples)
            row = {
                "gain": gain,
                "attached_fraction": attached_frac,
                "rsrp": mean_std([s["rsrp"] for s in samples]),
                "dl_snr": mean_std([s["dl_snr"] for s in samples]),
                "dl_bler": mean_std([s["dl_bler"] for s in samples]),
                "dl_mcs": mean_std([s["dl_mcs"] for s in samples]),
                "raw_samples": samples,
            }
            results.append(row)
            print(f"  gain={gain:5.2f}  attached={attached_frac:4.0%}  "
                  f"rsrp={row['rsrp']['mean']}  snr={row['dl_snr']['mean']}  "
                  f"bler={row['dl_bler']['mean']}  mcs={row['dl_mcs']['mean']}")

            if attached_frac == 0.0:
                print(f"  -> UE dropped attach at gain={gain}. Stopping sweep (lower "
                      f"steps would just confirm it stays detached).")
                break
    finally:
        print("  restoring gain to 1.0 ...")
        client.set_gain(ue, "dl", 1.0)
        client.set_gain(ue, "ul", 1.0)

    # empirical floor = lowest gain where UE was still fully attached
    floor = None
    for row in results:
        if row["attached_fraction"] == 1.0:
            floor = row["gain"]
    return {"ue": ue, "steps": results, "empirical_decode_floor_gain": floor}


# ---------------------------------------------------------------------------
# Phase 2: detached-UE behavior
# ---------------------------------------------------------------------------

def phase_detach_behavior(client: BackendClient, ue: str, other_ues, pre_s=5, detach_hold_s=30,
                           reattach_timeout_s=90, poll_interval=1.0):
    print(f"\n=== Phase 2: detach/revive behavior on {ue} ===")
    timeline = []

    def sample(tag):
        snap = client.status()
        row = {"t": time.time(), "tag": tag}
        for uid in [ue] + list(other_ues):
            u = ue_snapshot(snap, uid)
            row[uid] = {
                "status": u["status"],
                "rsrp": rf_value(u, "rsrp"),
                "dl_snr": rf_value(u, "dl_snr"),
                "dl_bler": rf_value(u, "dl_bler"),
                "backlog": (u.get("gains") or {}).get("backlog"),
                "late_dropped": (u.get("gains") or {}).get("late_dropped"),
            }
        timeline.append(row)
        return row

    print(f"  recording {pre_s}s pre-detach baseline ...")
    t_end = time.time() + pre_s
    while time.time() < t_end:
        sample("pre")
        time.sleep(poll_interval)

    print(f"  killing {ue} ...")
    kill_resp = client.kill(ue)
    t_kill = time.time()

    print(f"  holding detached for {detach_hold_s}s, watching other UEs + broker instrumentation ...")
    t_end = time.time() + detach_hold_s
    while time.time() < t_end:
        sample("detached")
        time.sleep(poll_interval)

    print(f"  reviving {ue} ...")
    revive_resp = client.revive(ue)
    t_revive = time.time()

    print(f"  waiting up to {reattach_timeout_s}s for reattach ...")
    reattach_time = None
    t_end = time.time() + reattach_timeout_s
    while time.time() < t_end:
        row = sample("reattaching")
        if row[ue]["status"] in ("Online", "Idle") and reattach_time is None:
            reattach_time = time.time() - t_revive
            print(f"  -> {ue} reattached after {reattach_time:.1f}s")
            # keep sampling a bit past reattach to confirm it holds
            for _ in range(5):
                time.sleep(poll_interval)
                sample("post-reattach")
            break
        time.sleep(poll_interval)

    if reattach_time is None:
        print(f"  -> {ue} did NOT reattach within {reattach_timeout_s}s. "
              f"Reversibility does not hold at this timeout — document as-is, "
              f"do not extend the timeout silently.")

    # Did other UEs stay stable during the detach window?
    other_stability = {}
    for uid in other_ues:
        detached_rows = [r[uid] for r in timeline if r["tag"] == "detached"]
        other_stability[uid] = {
            "rsrp": mean_std([r["rsrp"] for r in detached_rows]),
            "dl_snr": mean_std([r["dl_snr"] for r in detached_rows]),
            "dl_bler": mean_std([r["dl_bler"] for r in detached_rows]),
            "any_status_change": len({r["status"] for r in detached_rows}) > 1,
        }

    return {
        "ue": ue,
        "other_ues": list(other_ues),
        "kill_response": kill_resp,
        "revive_response": revive_resp,
        "reattach_time_s": reattach_time,
        "reattach_confirmed": reattach_time is not None,
        "other_ue_stability_during_detach": other_stability,
        "timeline": timeline,
    }


# ---------------------------------------------------------------------------
# Phase 3: 30-minute clean baseline
# ---------------------------------------------------------------------------

def phase_clean_baseline(client: BackendClient, ue_ids, duration_s, out_dir: Path, poll_interval=1.0, flush_every=60):
    print(f"\n=== Phase 3: clean baseline collection ({duration_s}s) ===")
    raw_path = out_dir / "clean_baseline_raw.jsonl"
    n_samples = 0
    per_ue_series = {uid: {"rsrp": [], "dl_snr": [], "dl_bler": [], "ul_bler": [],
                            "dl_mcs": [], "ul_mcs": [], "dl_brate": [], "ul_brate": [],
                            "backlog": [], "late_dropped": []} for uid in ue_ids}

    t_start = time.time()
    t_end = t_start + duration_s
    with open(raw_path, "w") as f:
        while time.time() < t_end:
            snap = client.status()
            f.write(json.dumps(snap) + "\n")
            n_samples += 1
            if n_samples % flush_every == 0:
                f.flush()
                elapsed = time.time() - t_start
                remaining = max(0, duration_s - elapsed)
                print(f"  {n_samples} samples, {elapsed:5.0f}s elapsed, {remaining:5.0f}s remaining")

            for uid in ue_ids:
                u = ue_snapshot(snap, uid)
                s = per_ue_series[uid]
                s["rsrp"].append(rf_value(u, "rsrp"))
                s["dl_snr"].append(rf_value(u, "dl_snr"))
                s["dl_bler"].append(rf_value(u, "dl_bler"))
                s["ul_bler"].append(rf_value(u, "ul_bler"))
                s["dl_mcs"].append(rf_value(u, "dl_mcs"))
                s["ul_mcs"].append(rf_value(u, "ul_mcs"))
                s["dl_brate"].append((u.get("throughput") or {}).get("dl_brate", {}).get("value"))
                s["ul_brate"].append((u.get("throughput") or {}).get("ul_brate", {}).get("value"))
                s["backlog"].append((u.get("gains") or {}).get("backlog"))
                s["late_dropped"].append((u.get("gains") or {}).get("late_dropped"))

            time.sleep(poll_interval)

    summary = {"duration_s": duration_s, "n_samples": n_samples, "per_ue": {}}
    for uid in ue_ids:
        s = per_ue_series[uid]
        bler_vals = [v for v in s["dl_bler"] if v is not None]
        nonzero_bler_frac = (sum(1 for v in bler_vals if v > 0) / len(bler_vals)) if bler_vals else None

        late_dropped_series = [v for v in s["late_dropped"] if v is not None]
        late_dropped_delta = None
        if len(late_dropped_series) >= 2:
            late_dropped_delta = late_dropped_series[-1] - late_dropped_series[0]

        summary["per_ue"][uid] = {
            "rsrp": mean_std(s["rsrp"]),
            "dl_snr": mean_std(s["dl_snr"]),
            "dl_bler": mean_std(s["dl_bler"]),
            "ul_bler": mean_std(s["ul_bler"]),
            "dl_mcs": mean_std(s["dl_mcs"]),
            "ul_mcs": mean_std(s["ul_mcs"]),
            "dl_brate": mean_std(s["dl_brate"]),
            "ul_brate": mean_std(s["ul_brate"]),
            "backlog": mean_std(s["backlog"]),
            "dl_bler_nonzero_fraction": nonzero_bler_frac,
            "late_dropped_total_over_run": late_dropped_delta,
        }
    return summary, raw_path


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------

def write_summary_md(out_dir: Path, gain_result, detach_result, baseline_summary):
    lines = ["# Step 0 — Broker Artifact Characterization", "",
             f"Generated: {datetime.now(timezone.utc).isoformat()}", ""]

    if gain_result:
        lines += ["## Phase 1 — Gain floor sweep", "",
                   f"UE: `{gain_result['ue']}`",
                   f"Empirical decode floor (lowest gain fully attached): "
                   f"**{gain_result['empirical_decode_floor_gain']}**", "",
                   "| gain | attached | rsrp mean | snr mean | bler mean | mcs mean |",
                   "|---|---|---|---|---|---|"]
        for row in gain_result["steps"]:
            lines.append(
                f"| {row['gain']} | {row['attached_fraction']:.0%} | "
                f"{row['rsrp']['mean']} | {row['dl_snr']['mean']} | "
                f"{row['dl_bler']['mean']} | {row['dl_mcs']['mean']} |"
            )
        lines.append("")

    if detach_result:
        lines += ["## Phase 2 — Detach/revive behavior", "",
                   f"UE: `{detach_result['ue']}`",
                   f"Reattach confirmed: **{detach_result['reattach_confirmed']}**",
                   f"Reattach time: **{detach_result['reattach_time_s']}s**", "",
                   "Other UEs' stability during detach window:", ""]
        for uid, stab in detach_result["other_ue_stability_during_detach"].items():
            lines.append(f"- `{uid}`: rsrp={stab['rsrp']['mean']}, snr={stab['dl_snr']['mean']}, "
                          f"bler={stab['dl_bler']['mean']}, status_changed={stab['any_status_change']}")
        lines.append("")

    if baseline_summary:
        lines += ["## Phase 3 — Clean baseline (broker artifact floor)", "",
                   f"Duration: {baseline_summary['duration_s']}s, "
                   f"samples: {baseline_summary['n_samples']}", ""]
        for uid, s in baseline_summary["per_ue"].items():
            lines += [f"### {uid}", "",
                      f"- RSRP: mean={s['rsrp']['mean']}, std={s['rsrp']['std']}",
                      f"- DL SNR: mean={s['dl_snr']['mean']}, std={s['dl_snr']['std']}",
                      f"- DL BLER: mean={s['dl_bler']['mean']}, "
                      f"nonzero fraction={s['dl_bler_nonzero_fraction']}  "
                      f"(**this is the broker artifact floor for BLER — LSTM "
                      f"anomaly threshold must sit above this, not above zero**)",
                      f"- DL MCS: mean={s['dl_mcs']['mean']}, std={s['dl_mcs']['std']}",
                      f"- DL throughput: mean={s['dl_brate']['mean']}, std={s['dl_brate']['std']}",
                      f"- Backlog: mean={s['backlog']['mean']}, std={s['backlog']['std']}",
                      f"- late_dropped over full run: {s['late_dropped_total_over_run']}",
                      ""]

    (out_dir / "SUMMARY.md").write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", default="http://localhost:8080")
    ap.add_argument("--phase", choices=["gain", "detach", "baseline", "all"], default="all")
    ap.add_argument("--ues", default="ue1,ue2,ue3")
    ap.add_argument("--sweep-ue", default="ue3")
    ap.add_argument("--detach-ue", default="ue3")
    ap.add_argument("--duration", type=int, default=1800, help="clean baseline duration in seconds")
    ap.add_argument("--out", default="ml/baseline")
    ap.add_argument("--skip-preflight", action="store_true")
    args = ap.parse_args()

    ue_ids = args.ues.split(",")
    client = BackendClient(args.backend)

    out_dir = Path(args.out) / utc_stamp()
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {out_dir}")

    # sanity check backend is reachable before anything else
    try:
        client.status()
    except Exception as e:
        print(f"FATAL: cannot reach backend at {args.backend}: {e}", file=sys.stderr)
        sys.exit(1)

    print("Resetting cell to baseline (gains->1.0, noise->0, traffic stopped, faults cleared) ...")
    client.reset()
    time.sleep(2)

    if not args.skip_preflight:
        print("Preflight: waiting for traffic on all UEs (keepalives must be running externally) ...")
        wait_for_traffic(client, ue_ids)
        print("Preflight OK — all UEs online with traffic.")

    gain_result = detach_result = baseline_summary = None

    if args.phase in ("gain", "all"):
        gain_result = phase_gain_sweep(client, args.sweep_ue, DEFAULT_GAIN_STEPS)
        (out_dir / "gain_sweep.json").write_text(json.dumps(gain_result, indent=2))
        if not args.skip_preflight:
            wait_for_traffic(client, ue_ids, timeout_s=30)

    if args.phase in ("detach", "all"):
        others = [u for u in ue_ids if u != args.detach_ue]
        detach_result = phase_detach_behavior(client, args.detach_ue, others)
        (out_dir / "detach_behavior.json").write_text(json.dumps(detach_result, indent=2))
        client.reset()
        time.sleep(2)
        if not args.skip_preflight:
            wait_for_traffic(client, ue_ids, timeout_s=30)

    if args.phase in ("baseline", "all"):
        baseline_summary, raw_path = phase_clean_baseline(client, ue_ids, args.duration, out_dir)
        (out_dir / "clean_baseline_summary.json").write_text(json.dumps(baseline_summary, indent=2))
        print(f"Raw time series written to {raw_path}")

    write_summary_md(out_dir, gain_result, detach_result, baseline_summary)
    print(f"\nDone. See {out_dir / 'SUMMARY.md'} for the rollup.")


if __name__ == "__main__":
    main()
    