#!/usr/bin/env python3
"""
train_cnn.py — train the metrics-image CNN fault classifier on the
windowed dataset from build_cnn_dataset.py.

Architecture (small by design -- this is a 10x8 "image", not ImageNet):
    Conv2d(1->16, 3x3) -> ReLU -> Conv2d(16->32, 3x3) -> ReLU
    -> AdaptiveAvgPool2d(1) -> Flatten -> Linear(32->64) -> ReLU
    -> Dropout(0.3) -> Linear(64->7)

Class imbalance handling: 'clean' is ~90% of every split (see
build_cnn_dataset.py output), so plain accuracy is meaningless -- a
constant-clean predictor would score ~90%. Loss is class-weighted
(inverse sqrt frequency, capped, to avoid the tiny classes blowing up
the gradient), and the tracked metric for model selection is macro-F1,
not accuracy.

The real question this training run needs to answer empirically, not
assume: does uplink_contamination's near-symmetric composite UL-BLER
signature (see validation_summary.json -- target and bystander deltas
were close in magnitude) make it separable from the OTHER faults, and
can it still be localized to the correct target UE? The confusion
matrix and per-class report at the end are where that gets settled.

Usage:
    python3 train_cnn.py
    python3 train_cnn.py --epochs 40 --lr 1e-3

Output:
    ml/cnn_dataset/cnn_model.pt
    ml/cnn_dataset/training_report.md
    ml/cnn_dataset/training_report.json
"""

import argparse
import json
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset


class FaultCNN(nn.Module):
    def __init__(self, n_classes):
        super().__init__()
        self.conv1 = nn.Conv2d(1, 16, kernel_size=3, padding=1)
        self.conv2 = nn.Conv2d(16, 32, kernel_size=3, padding=1)
        self.pool = nn.AdaptiveAvgPool2d(1)
        self.fc1 = nn.Linear(32, 64)
        self.dropout = nn.Dropout(0.3)
        self.fc2 = nn.Linear(64, n_classes)
        self.relu = nn.ReLU()

    def forward(self, x):
        x = self.relu(self.conv1(x))
        x = self.relu(self.conv2(x))
        x = self.pool(x).flatten(1)
        x = self.relu(self.fc1(x))
        x = self.dropout(x)
        return self.fc2(x)


def load_split(path):
    d = np.load(path, allow_pickle=True)
    X = torch.tensor(d["X"], dtype=torch.float32).unsqueeze(1)  # (N,1,W,C)
    y = torch.tensor(d["y"], dtype=torch.long)
    meta = {"session_id": d["session_id"], "ue_id": d["ue_id"], "severity": d["severity"]}
    return X, y, meta


def class_weights_from_counts(y, n_classes, cap=10.0):
    counts = np.bincount(y.numpy(), minlength=n_classes).astype(np.float64)
    counts[counts == 0] = 1  # avoid div-by-zero for any absent class
    inv = 1.0 / np.sqrt(counts)
    w = inv / inv.mean()
    w = np.clip(w, None, cap)
    return torch.tensor(w, dtype=torch.float32)


def evaluate(model, loader, device, n_classes):
    model.eval()
    all_preds, all_true = [], []
    total_loss, n = 0.0, 0
    criterion = nn.CrossEntropyLoss(reduction="sum")
    with torch.no_grad():
        for X, y in loader:
            X, y = X.to(device), y.to(device)
            logits = model(X)
            total_loss += criterion(logits, y).item()
            preds = logits.argmax(dim=1)
            all_preds.append(preds.cpu().numpy())
            all_true.append(y.cpu().numpy())
            n += y.size(0)
    all_preds = np.concatenate(all_preds)
    all_true = np.concatenate(all_true)

    cm = np.zeros((n_classes, n_classes), dtype=np.int64)
    for t, p in zip(all_true, all_preds):
        cm[t, p] += 1

    per_class = {}
    f1s = []
    for c in range(n_classes):
        tp = cm[c, c]
        fp = cm[:, c].sum() - tp
        fn = cm[c, :].sum() - tp
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        per_class[c] = {"precision": precision, "recall": recall, "f1": f1, "support": int(cm[c, :].sum())}
        f1s.append(f1)
    macro_f1 = float(np.mean(f1s))
    accuracy = float((all_preds == all_true).mean())

    return {"loss": total_loss / n, "accuracy": accuracy, "macro_f1": macro_f1,
            "per_class": per_class, "confusion_matrix": cm.tolist()}


def localization_breakdown(model, X, y, meta, device, classes, class_to_idx):
    """For uplink_contamination specifically: of the windows the model
    predicts as uplink_contamination, how often is it also the correct
    target UE (localization), vs correct fault-type but wrong UE
    (detected the symptom, misattributed the source)?"""
    model.eval()
    with torch.no_grad():
        logits = model(X.to(device))
        preds = logits.argmax(dim=1).cpu().numpy()
    y_np = y.numpy()
    uc_idx = class_to_idx["uplink_contamination"]

    true_uc_mask = y_np == uc_idx
    pred_uc_mask = preds == uc_idx
    n_true_uc = int(true_uc_mask.sum())
    n_correctly_classified = int((true_uc_mask & pred_uc_mask).sum())
    n_predicted_uc_but_actually_clean_bystander = int(
        pred_uc_mask.sum() - (pred_uc_mask & true_uc_mask).sum()
    )
    return {
        "true_uplink_contamination_windows": n_true_uc,
        "correctly_classified": n_correctly_classified,
        "recall": n_correctly_classified / n_true_uc if n_true_uc else None,
        "false_positives_on_bystander_or_other_windows": n_predicted_uc_but_actually_clean_bystander,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset-dir", default="ml/cnn_dataset")
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--lr", type=float, default=1e-3)
    args = ap.parse_args()

    base = Path(args.dataset_dir)
    labels_meta = json.loads((base / "labels.json").read_text())
    classes = labels_meta["classes"]
    class_to_idx = labels_meta["class_to_idx"]
    n_classes = len(classes)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Device: {device}")

    Xtr, ytr, _ = load_split(base / "train.npz")
    Xval, yval, val_meta = load_split(base / "val.npz")
    print(f"Train: {Xtr.shape}  Val: {Xval.shape}")

    train_loader = DataLoader(TensorDataset(Xtr, ytr), batch_size=args.batch_size, shuffle=True)
    val_loader = DataLoader(TensorDataset(Xval, yval), batch_size=args.batch_size, shuffle=False)

    weights = class_weights_from_counts(ytr, n_classes).to(device)
    print("Class weights:", {classes[i]: round(w, 2) for i, w in enumerate(weights.cpu().numpy())})

    model = FaultCNN(n_classes).to(device)
    criterion = nn.CrossEntropyLoss(weight=weights)
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr, weight_decay=1e-4)

    best_macro_f1 = -1.0
    best_state = None
    history = []

    for epoch in range(1, args.epochs + 1):
        model.train()
        total_loss, n = 0.0, 0
        for X, y in train_loader:
            X, y = X.to(device), y.to(device)
            optimizer.zero_grad()
            logits = model(X)
            loss = criterion(logits, y)
            loss.backward()
            optimizer.step()
            total_loss += loss.item() * y.size(0)
            n += y.size(0)
        train_loss = total_loss / n

        val_metrics = evaluate(model, val_loader, device, n_classes)
        history.append({"epoch": epoch, "train_loss": train_loss,
                         "val_loss": val_metrics["loss"], "val_accuracy": val_metrics["accuracy"],
                         "val_macro_f1": val_metrics["macro_f1"]})
        print(f"epoch {epoch:3d}  train_loss={train_loss:.4f}  val_loss={val_metrics['loss']:.4f}  "
              f"val_acc={val_metrics['accuracy']:.3f}  val_macroF1={val_metrics['macro_f1']:.3f}")

        if val_metrics["macro_f1"] > best_macro_f1:
            best_macro_f1 = val_metrics["macro_f1"]
            best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}

    model.load_state_dict(best_state)
    torch.save(model.state_dict(), base / "cnn_model.pt")
    print(f"\nBest val macro-F1: {best_macro_f1:.3f} -- saved to {base / 'cnn_model.pt'}")

    final_metrics = evaluate(model, val_loader, device, n_classes)
    loc = localization_breakdown(model, Xval, yval, val_meta, device, classes, class_to_idx)

    print("\n=== Final per-class validation metrics (best checkpoint) ===")
    for i, c in enumerate(classes):
        pc = final_metrics["per_class"][i]
        print(f"  {c:28s} precision={pc['precision']:.2f}  recall={pc['recall']:.2f}  "
              f"f1={pc['f1']:.2f}  support={pc['support']}")

    print("\n=== uplink_contamination localization check ===")
    print(f"  {loc}")

    report = {"history": history, "final_metrics": final_metrics,
              "uplink_contamination_localization": loc, "classes": classes}
    (base / "training_report.json").write_text(json.dumps(report, indent=2))

    lines = ["# CNN Training Report", "",
              f"Best val macro-F1: {best_macro_f1:.3f}", "",
              "## Per-class validation metrics", "",
              "| class | precision | recall | f1 | support |", "|---|---|---|---|---|"]
    for i, c in enumerate(classes):
        pc = final_metrics["per_class"][i]
        lines.append(f"| {c} | {pc['precision']:.2f} | {pc['recall']:.2f} | {pc['f1']:.2f} | {pc['support']} |")
    lines += ["", "## Confusion matrix (rows=true, cols=predicted)", "",
              "| | " + " | ".join(classes) + " |", "|---|" + "---|" * len(classes)]
    for i, c in enumerate(classes):
        row = final_metrics["confusion_matrix"][i]
        lines.append(f"| **{c}** | " + " | ".join(str(x) for x in row) + " |")
    lines += ["", "## uplink_contamination localization", "",
              f"```json\n{json.dumps(loc, indent=2)}\n```"]
    (base / "training_report.md").write_text("\n".join(lines))

    print(f"\nFull report: {base / 'training_report.md'}")


if __name__ == "__main__":
    main()