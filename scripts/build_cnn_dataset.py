#!/usr/bin/env python3
"""
build_cnn_dataset.py — turn the 54 labeled fault sessions into windowed,
per-UE, metrics-image tensors ready for CNN training.

DESIGN DECISIONS (see conversation log for full reasoning):
  - "Image" = time x metric-channel grid, not a spectrogram. Rows are
    consecutive 1Hz samples, columns are metric channels. A 2D CNN finds
    local time x metric motifs the same way it'd find edges in a photo.
  - Window = 10 consecutive samples (~10s). Long enough to see a slope
    (distinguishes a mid-ramp bler_degradation from a step-change
    co_channel_interference at the same noise level -- at any single
    instant these are indistinguishable, since both are literally the
    same AWGN mechanism; only the trajectory differs). Short enough to
    stay meaningfully faster than the LSTM's full-trajectory judgement.
  - Label = the window's LAST sample's ground truth. Only the true
    target_ue gets the fault label during onset/active; every other UE
    (and the target UE during clean/recovery) is labeled 'clean'. This
    is what actually tests per-UE localization rather than just cell-
    wide anomaly detection.
  - KNOWN LIMITATION, measured not assumed: uplink_contamination's
    composite UL BLER effect is near-symmetric across all 3 UEs (see
    validation_summary.json), so this fault's localization label may
    not be cleanly learnable from metrics alone. Left in as-designed;
    the eval confusion matrix is where this actually gets answered.
  - Split by SESSION, not by row. Windows within a session overlap
    almost entirely, so a random row split would leak. For each
    (fault_type, severity) group of 3 sessions (one per target UE),
    ue3's session goes to validation, ue1/ue2 go to train -- guarantees
    every fault/severity combo is represented in both splits without
    letting near-duplicate windows cross the split boundary.
  - Windows never cross a sample-time gap > 1.5x the nominal 1Hz
    interval, so a truncated/resumed session (we have one:
    link_dropout__medium__ue3 at 155/165 samples) can't produce a
    window that silently splices together non-contiguous time.

Usage:
    python3 build_cnn_dataset.py
    python3 build_cnn_dataset.py --dataset-dir ml/fault_dataset --out ml/cnn_dataset

Output:
    ml/cnn_dataset/train.npz   (X, y, session_id, ue_id, fault_type, severity)
    ml/cnn_dataset/val.npz     (same fields)
    ml/cnn_dataset/labels.json (class index -> name, channel order, normalization)
"""

import argparse
import json
from pathlib import Path

import numpy as np

WINDOW = 10
STRIDE = 2
MAX_GAP_S = 1.5  # samples more than this far apart in time can't share a window

CHANNELS = ["rsrp", "dl_snr", "dl_mcs", "ul_mcs", "dl_bler", "ul_bler",
            "dl_brate_log", "ul_brate_log"]

# Fixed normalization ranges, chosen from Step 0 + validation data rather than
# fit per-dataset -- keeps the scaling meaningful and reproducible across any
# future data collected on the same broker calibration.
NORM = {
    "rsrp": (60.0, 34.0),        # (typical max, typical range) -> value/max roughly in [0,1] for the observed 26-60 band
    "dl_snr": (150.0, 150.0),    # srsRAN-internal units, clean ~141, degraded down to ~0
    "dl_mcs": (20.0, 20.0),
    "ul_mcs": (20.0, 20.0),
    "dl_bler": (100.0, 100.0),   # percent
    "ul_bler": (100.0, 100.0),
    "dl_brate_log": (12.0, 12.0),  # log1p(bps), clean voice_call ~91000 -> log1p~11.4
    "ul_brate_log": (12.0, 12.0),
}

ALL_FAULTS = [
    "co_channel_interference", "bler_degradation", "transport_stall",
    "link_dropout", "scheduler_starvation", "uplink_contamination",
]
CLASSES = ["clean"] + ALL_FAULTS
CLASS_TO_IDX = {c: i for i, c in enumerate(CLASSES)}
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
    entry = rf.get(key) if rf else None
    return entry.get("value") if entry else None


def tp_val(u, key):
    if u is None:
        return None
    tp = u.get("throughput")
    entry = tp.get(key) if tp else None
    return entry.get("value") if entry else None


def extract_channels(u):
    """Return the raw (unnormalized) channel vector for one UE at one
    timestep, or None if any required field is missing."""
    vals = {
        "rsrp": rf_val(u, "rsrp"),
        "dl_snr": rf_val(u, "dl_snr"),
        "dl_mcs": rf_val(u, "dl_mcs"),
        "ul_mcs": rf_val(u, "ul_mcs"),
        "dl_bler": rf_val(u, "dl_bler"),
        "ul_bler": rf_val(u, "ul_bler"),
        "dl_brate_log": tp_val(u, "dl_brate"),
        "ul_brate_log": tp_val(u, "ul_brate"),
    }
    if any(v is None for v in vals.values()):
        return None
    vals["dl_brate_log"] = float(np.log1p(vals["dl_brate_log"]))
    vals["ul_brate_log"] = float(np.log1p(vals["ul_brate_log"]))
    return np.array([vals[c] for c in CHANNELS], dtype=np.float32)


def normalize(raw_vec):
    out = np.zeros_like(raw_vec)
    for i, c in enumerate(CHANNELS):
        center, scale = NORM[c]
        out[i] = raw_vec[i] / scale
    return out


def label_for_row(row, target_ue, this_ue):
    if this_ue != target_ue:
        return "clean"
    if row["phase"] in ("onset", "active"):
        return row["fault_type"]
    return "clean"  # clean / recovery


def build_windows_for_ue_session(rows, target_ue, this_ue, session_id, severity):
    """rows: list of parsed JSONL dicts for one session, in time order."""
    per_t = []
    for r in rows:
        u = get_ue(r["snapshot"], this_ue)
        raw = extract_channels(u)
        if raw is None:
            continue
        per_t.append({
            "t": r["t"],
            "raw": raw,
            "label": label_for_row(r, target_ue, this_ue),
        })

    windows = []
    i = 0
    n = len(per_t)
    while i + WINDOW <= n:
        chunk = per_t[i:i + WINDOW]
        # reject windows that straddle a time gap (e.g. the truncated/resumed session)
        gaps_ok = all(
            (chunk[k + 1]["t"] - chunk[k]["t"]) <= MAX_GAP_S
            for k in range(len(chunk) - 1)
        )
        if gaps_ok:
            img = np.stack([normalize(c["raw"]) for c in chunk], axis=0)  # (WINDOW, n_channels)
            label = chunk[-1]["label"]
            windows.append({
                "img": img, "label": label, "session_id": session_id,
                "ue_id": this_ue, "severity": severity,
            })
        i += STRIDE
    return windows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset-dir", default="ml/fault_dataset")
    ap.add_argument("--out", default="ml/cnn_dataset")
    args = ap.parse_args()

    base = Path(args.dataset_dir)
    manifest = json.loads((base / "manifest.json").read_text())
    print(f"Loaded manifest: {len(manifest)} sessions")

    # group sessions by (fault_type, severity) so we can hold out ue3 per group
    groups = {}
    for entry in manifest:
        key = (entry["fault_type"], entry["severity"])
        groups.setdefault(key, {})[entry["target_ue"]] = entry

    train_windows, val_windows = [], []

    for (fault_type, severity), by_ue in groups.items():
        for target_ue, entry in by_ue.items():
            fpath = Path(entry["file"])
            rows = [json.loads(l) for l in open(fpath)]
            split = "val" if target_ue == "ue3" else "train"

            for this_ue in ALL_UES:
                windows = build_windows_for_ue_session(
                    rows, target_ue, this_ue, entry["session_id"], severity)
                (val_windows if split == "val" else train_windows).extend(windows)

    def to_arrays(windows):
        X = np.stack([w["img"] for w in windows], axis=0) if windows else np.zeros((0, WINDOW, len(CHANNELS)))
        y = np.array([CLASS_TO_IDX[w["label"]] for w in windows], dtype=np.int64)
        session_id = np.array([w["session_id"] for w in windows])
        ue_id = np.array([w["ue_id"] for w in windows])
        severity = np.array([w["severity"] for w in windows])
        return X, y, session_id, ue_id, severity

    Xtr, ytr, str_sess, str_ue, str_sev = to_arrays(train_windows)
    Xval, yval, sval_sess, sval_ue, sval_sev = to_arrays(val_windows)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out_dir / "train.npz", X=Xtr, y=ytr, session_id=str_sess, ue_id=str_ue, severity=str_sev)
    np.savez_compressed(out_dir / "val.npz", X=Xval, y=yval, session_id=sval_sess, ue_id=sval_ue, severity=sval_sev)

    labels_meta = {
        "classes": CLASSES, "class_to_idx": CLASS_TO_IDX,
        "channels": CHANNELS, "normalization": NORM,
        "window": WINDOW, "stride": STRIDE, "max_gap_s": MAX_GAP_S,
        "split_rule": "per (fault_type, severity) group: ue3's session -> val, ue1/ue2 -> train",
    }
    (out_dir / "labels.json").write_text(json.dumps(labels_meta, indent=2))

    print(f"\nTrain windows: {len(train_windows)}  |  Val windows: {len(val_windows)}")
    print("\nTrain class balance:")
    for c in CLASSES:
        n = int((ytr == CLASS_TO_IDX[c]).sum())
        print(f"  {c:28s} {n:6d}")
    print("\nVal class balance:")
    for c in CLASSES:
        n = int((yval == CLASS_TO_IDX[c]).sum())
        print(f"  {c:28s} {n:6d}")

    print(f"\nWrote {out_dir / 'train.npz'}")
    print(f"Wrote {out_dir / 'val.npz'}")
    print(f"Wrote {out_dir / 'labels.json'}")


if __name__ == "__main__":
    main()