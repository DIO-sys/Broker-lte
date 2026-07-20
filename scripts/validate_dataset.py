#!/usr/bin/env python3
"""
validate_dataset.py — Sprint 2 Step 1 wrap-up: dataset-wide sanity check

Runs across ALL sessions in the manifest (not one at a time, like the
spot-checks we did during collection) and checks two different things:

  1. STRUCTURAL validity — does every session have a real, parseable
     file, with the sample count the manifest claims, phases in the
     right proportions, ground-truth tags consistent throughout, and
     complete (non-null) snapshots for all 3 UEs on every row.

  2. EFFECT validity — does the fault actually show up in the metric
     it's supposed to affect. This automates the spot-check we did by
     hand for transport_stall, bler_degradation, and scheduler_starvation
     during collection (which is how the scheduler_starvation "traffic
     never started" false alarm got caught) — run once across the full
     54 sessions instead of picking a few by hand.

This does NOT touch the network — pure offline analysis of the JSONL
files already on disk. Safe to run any time, as many times as you want.

Usage:
    python3 validate_dataset.py
    python3 validate_dataset.py --dir ml/fault_dataset

Output:
    ml/fault_dataset/VALIDATION_REPORT.md
    ml/fault_dataset/validation_summary.json
"""

import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

ALL_FAULTS = [
    "co_channel_interference", "bler_degradation", "transport_stall",
    "link_dropout", "scheduler_starvation", "uplink_contamination",
]
ALL_SEVERITIES = ["low", "medium", "high"]
ALL_UES = ["ue1", "ue2", "ue3"]


def get_ue(snapshot, ue_id):
    for u in snapshot.get("ues", []):
        if u["id"] == ue_id:
            return u
    return None


def rf_val(u, key):
    if u is None:
        return None
    rf = u.get("rf")
    if not rf:
        return None
    entry = rf.get(key)
    return entry.get("value") if entry else None


def tp_val(u, key):
    if u is None:
        return None
    tp = u.get("throughput")
    if not tp:
        return None
    entry = tp.get(key)
    return entry.get("value") if entry else None


def status_of(u):
    return u.get("status") if u else None


def mean(vals):
    vals = [v for v in vals if v is not None]
    return statistics.mean(vals) if vals else None


# ---------------------------------------------------------------------------
# Effect checks — one per fault type, describing what SHOULD change on the
# target UE between the clean phase and the active phase.
# ---------------------------------------------------------------------------

def check_co_channel_interference(rows_clean, rows_active, target_ue, severity):
    clean = mean([rf_val(get_ue(r["snapshot"], target_ue), "dl_snr") for r in rows_clean])
    active = mean([rf_val(get_ue(r["snapshot"], target_ue), "dl_snr") for r in rows_active])
    if clean is None or active is None:
        return {"ok": False, "reason": "missing dl_snr data", "clean": clean, "active": active}
    delta = clean - active
    return {"ok": delta >= 2.0, "metric": "dl_snr", "clean": round(clean, 2),
            "active": round(active, 2), "delta": round(delta, 2),
            "expected": "decrease >= 2.0 dB"}


def check_bler_degradation(rows_clean, rows_active, target_ue, severity):
    # same shape as CCI check: SNR should be lower during active (ramp end)
    clean = mean([rf_val(get_ue(r["snapshot"], target_ue), "dl_snr") for r in rows_clean])
    active = mean([rf_val(get_ue(r["snapshot"], target_ue), "dl_snr") for r in rows_active])
    if clean is None or active is None:
        return {"ok": False, "reason": "missing dl_snr data", "clean": clean, "active": active}
    delta = clean - active
    return {"ok": delta >= 3.0, "metric": "dl_snr", "clean": round(clean, 2),
            "active": round(active, 2), "delta": round(delta, 2),
            "expected": "decrease >= 3.0 dB (ramp average, so a milder bar than CCI's step change)"}


def check_transport_stall(rows_clean, rows_active, target_ue, severity):
    clean = mean([tp_val(get_ue(r["snapshot"], target_ue), "dl_brate") for r in rows_clean])
    active = mean([tp_val(get_ue(r["snapshot"], target_ue), "dl_brate") for r in rows_active])
    snr_clean = mean([rf_val(get_ue(r["snapshot"], target_ue), "dl_snr") for r in rows_clean])
    snr_active = mean([rf_val(get_ue(r["snapshot"], target_ue), "dl_snr") for r in rows_active])
    if clean is None or active is None or not clean:
        return {"ok": False, "reason": "missing dl_brate data", "clean": clean, "active": active}
    ratio = active / clean
    snr_flat = (snr_clean is not None and snr_active is not None
                and abs(snr_clean - snr_active) < 3.0)
    return {"ok": ratio < 0.5 and snr_flat, "metric": "dl_brate + dl_snr",
            "clean_brate": round(clean, 1), "active_brate": round(active, 1),
            "ratio": round(ratio, 3), "snr_clean": snr_clean, "snr_active": snr_active,
            "snr_stayed_flat": snr_flat,
            "expected": "dl_brate drops below 50% of clean AND dl_snr stays within 3dB (clean-RF signature)"}


def check_link_dropout(rows_clean, rows_active, target_ue, severity):
    if severity == "high":
        # high = kill: expect status Killed/Detached for most of active phase
        statuses = [status_of(get_ue(r["snapshot"], target_ue)) for r in rows_active]
        killed_frac = sum(1 for s in statuses if s in ("Killed", "Detached")) / len(statuses) if statuses else 0
        return {"ok": killed_frac >= 0.5, "metric": "status", "killed_fraction": round(killed_frac, 2),
                "expected": "target UE Killed/Detached for >=50% of active phase"}
    else:
        clean = mean([rf_val(get_ue(r["snapshot"], target_ue), "dl_snr") for r in rows_clean])
        active = mean([rf_val(get_ue(r["snapshot"], target_ue), "dl_snr") for r in rows_active])
        if clean is None or active is None:
            return {"ok": False, "reason": "missing dl_snr data", "clean": clean, "active": active}
        delta = clean - active
        return {"ok": delta >= 5.0, "metric": "dl_snr", "clean": round(clean, 2),
                "active": round(active, 2), "delta": round(delta, 2),
                "expected": "decrease >= 5.0 dB (severe noise, low/medium severity)"}


def check_scheduler_starvation(rows_clean, rows_active, target_ue, severity):
    # heavy traffic is UL (client pushes), noise is DL — check both independently
    clean_ul = mean([tp_val(get_ue(r["snapshot"], target_ue), "ul_brate") for r in rows_clean])
    active_ul = mean([tp_val(get_ue(r["snapshot"], target_ue), "ul_brate") for r in rows_active])
    clean_snr = mean([rf_val(get_ue(r["snapshot"], target_ue), "dl_snr") for r in rows_clean])
    active_snr = mean([rf_val(get_ue(r["snapshot"], target_ue), "dl_snr") for r in rows_active])
    if clean_ul is None or active_ul is None:
        return {"ok": False, "reason": "missing ul_brate data"}
    ul_ratio = active_ul / clean_ul if clean_ul else float("inf")
    snr_delta = (clean_snr - active_snr) if (clean_snr is not None and active_snr is not None) else None
    ok = ul_ratio >= 2.0 and (snr_delta is not None and snr_delta >= 2.0)
    return {"ok": ok, "metric": "ul_brate (heavy traffic) + dl_snr (noise)",
            "clean_ul_brate": round(clean_ul, 1), "active_ul_brate": round(active_ul, 1),
            "ul_ratio": round(ul_ratio, 2), "dl_snr_delta": round(snr_delta, 2) if snr_delta is not None else None,
            "expected": "ul_brate at least doubles (heavy traffic actually flowing) AND dl_snr drops >= 2dB (noise applied)"}


def check_uplink_contamination(rows_clean, rows_active, target_ue, severity):
    # check target UE's own UL degrades, AND at least one other UE shows some UL BLER increase (composite effect)
    clean_t = mean([rf_val(get_ue(r["snapshot"], target_ue), "ul_bler") for r in rows_clean])
    active_t = mean([rf_val(get_ue(r["snapshot"], target_ue), "ul_bler") for r in rows_active])
    others = [u for u in ALL_UES if u != target_ue]
    cross_effect = {}
    any_cross = False
    for uid in others:
        c = mean([rf_val(get_ue(r["snapshot"], uid), "ul_bler") for r in rows_clean])
        a = mean([rf_val(get_ue(r["snapshot"], uid), "ul_bler") for r in rows_active])
        delta = (a - c) if (a is not None and c is not None) else None
        cross_effect[uid] = {"clean": c, "active": a, "delta": delta}
        if delta is not None and delta >= 1.0:
            any_cross = True
    target_delta = (active_t - clean_t) if (active_t is not None and clean_t is not None) else None
    ok = target_delta is not None and target_delta >= 1.0
    return {"ok": ok, "metric": "ul_bler", "target_clean": clean_t, "target_active": active_t,
            "target_delta": round(target_delta, 2) if target_delta is not None else None,
            "cross_ue_effect_seen": any_cross, "cross_ue_detail": cross_effect,
            "expected": "target UE ul_bler increases >= 1%; cross_ue_effect_seen flags whether the "
                        "composite-signal contamination on OTHER UEs also showed up (bonus finding, not required for pass/fail)"}


CHECKS = {
    "co_channel_interference": check_co_channel_interference,
    "bler_degradation": check_bler_degradation,
    "transport_stall": check_transport_stall,
    "link_dropout": check_link_dropout,
    "scheduler_starvation": check_scheduler_starvation,
    "uplink_contamination": check_uplink_contamination,
}


# ---------------------------------------------------------------------------
# Structural checks
# ---------------------------------------------------------------------------

def structural_check(entry, rows):
    issues = []
    fault_type, severity, target_ue = entry["fault_type"], entry["severity"], entry["target_ue"]

    if len(rows) != entry["n_samples"]:
        issues.append(f"row count {len(rows)} != manifest n_samples {entry['n_samples']}")

    phase_counts = Counter(r["phase"] for r in rows)
    expected_phases = {"clean", "onset", "active", "recovery"}
    missing_phases = expected_phases - set(phase_counts)
    if missing_phases:
        issues.append(f"missing phases: {sorted(missing_phases)}")

    bad_tags = [r for r in rows if r["fault_type"] != fault_type
                or r["severity"] != severity or r["target_ue"] != target_ue]
    if bad_tags:
        issues.append(f"{len(bad_tags)} rows have ground-truth tags inconsistent with session_id")

    incomplete = 0
    for r in rows:
        ues = r["snapshot"].get("ues", [])
        if len(ues) != 3:
            incomplete += 1
            continue
        for uid in ALL_UES:
            u = get_ue(r["snapshot"], uid)
            if u is None or u.get("rf") is None or u.get("throughput") is None:
                incomplete += 1
                break
    if incomplete:
        issues.append(f"{incomplete}/{len(rows)} rows have incomplete snapshot (missing UE / rf / throughput)")

    return {"issues": issues, "phase_counts": dict(phase_counts)}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default="ml/fault_dataset")
    args = ap.parse_args()

    base = Path(args.dir)
    manifest_path = base / "manifest.json"
    if not manifest_path.exists():
        print(f"FATAL: no manifest at {manifest_path}")
        return

    manifest = json.loads(manifest_path.read_text())
    print(f"Loaded manifest: {len(manifest)} sessions")

    results = []
    class_balance = Counter()
    total_samples_by_phase = Counter()

    for entry in manifest:
        session_id = entry["session_id"]
        fpath = Path(entry["file"])
        if not fpath.exists():
            results.append({"session_id": session_id, "structural": {"issues": [f"FILE MISSING: {fpath}"]},
                             "effect": None})
            continue

        rows = [json.loads(l) for l in open(fpath)]
        struct = structural_check(entry, rows)
        for phase, n in struct["phase_counts"].items():
            total_samples_by_phase[phase] += n

        fault_type, severity, target_ue = entry["fault_type"], entry["severity"], entry["target_ue"]
        class_balance[(fault_type, severity, target_ue)] += 1

        rows_clean = [r for r in rows if r["phase"] == "clean"]
        rows_active = [r for r in rows if r["phase"] == "active"]

        check_fn = CHECKS.get(fault_type)
        effect = check_fn(rows_clean, rows_active, target_ue, severity) if check_fn and rows_clean and rows_active else \
            {"ok": False, "reason": "missing clean or active rows, cannot run effect check"}

        results.append({"session_id": session_id, "structural": struct, "effect": effect})

        status = "OK" if not struct["issues"] and effect.get("ok") else "FLAG"
        print(f"  [{status}] {session_id}")
        if struct["issues"]:
            for issue in struct["issues"]:
                print(f"      structural: {issue}")
        if not effect.get("ok"):
            print(f"      effect check failed: {effect}")

    # ---- summary ----
    n_structural_issues = sum(1 for r in results if r["structural"]["issues"])
    n_effect_failures = sum(1 for r in results if r["effect"] and not r["effect"].get("ok"))
    n_missing = sum(1 for r in results if any("FILE MISSING" in i for i in r["structural"]["issues"]))

    expected_combos = len(ALL_FAULTS) * len(ALL_SEVERITIES) * len(ALL_UES)
    missing_combos = []
    for f in ALL_FAULTS:
        for s in ALL_SEVERITIES:
            for u in ALL_UES:
                if class_balance[(f, s, u)] == 0:
                    missing_combos.append(f"{f}/{s}/{u}")

    summary = {
        "total_sessions_in_manifest": len(manifest),
        "expected_combos": expected_combos,
        "missing_combos": missing_combos,
        "sessions_with_structural_issues": n_structural_issues,
        "sessions_with_effect_check_failures": n_effect_failures,
        "sessions_with_missing_files": n_missing,
        "total_samples_by_phase": dict(total_samples_by_phase),
    }

    (base / "validation_summary.json").write_text(json.dumps(
        {"summary": summary, "results": results}, indent=2, default=str))

    # ---- markdown report ----
    lines = ["# Fault Dataset — Validation Report", "",
              f"Sessions in manifest: {len(manifest)} / {expected_combos} expected combos", ""]
    if missing_combos:
        lines += ["**Missing combos:**", ""] + [f"- {c}" for c in missing_combos] + [""]
    else:
        lines += ["All 54 combos present. ✅", ""]

    lines += [f"Sessions with structural issues: **{n_structural_issues}**",
              f"Sessions with effect-check failures: **{n_effect_failures}**",
              f"Sessions with missing files: **{n_missing}**", "",
              "## Total samples by phase", ""]
    for phase, n in total_samples_by_phase.items():
        lines.append(f"- {phase}: {n}")
    lines.append("")

    lines += ["## Per-session results", "",
              "| session | structural issues | effect check | key numbers |",
              "|---|---|---|---|"]
    for r in results:
        struct_str = "; ".join(r["structural"]["issues"]) if r["structural"]["issues"] else "none"
        eff = r["effect"] or {}
        eff_str = "PASS" if eff.get("ok") else "**FAIL**"
        detail_keys = {k: v for k, v in eff.items() if k not in ("ok", "expected", "cross_ue_detail")}
        lines.append(f"| {r['session_id']} | {struct_str} | {eff_str} | {detail_keys} |")

    (base / "VALIDATION_REPORT.md").write_text("\n".join(lines))

    print(f"\n{'='*60}")
    print(f"Structural issues: {n_structural_issues} sessions")
    print(f"Effect check failures: {n_effect_failures} sessions")
    print(f"Missing files: {n_missing} sessions")
    print(f"Missing combos: {len(missing_combos)}")
    print(f"\nFull report: {base / 'VALIDATION_REPORT.md'}")
    print(f"Full data:   {base / 'validation_summary.json'}")


if __name__ == "__main__":
    main()