# Step 0 — Broker Artifact Characterization

Generated: 2026-07-15T19:23:51.224090+00:00

## Phase 1 — Gain floor sweep

UE: `ue3`
Empirical decode floor (lowest gain fully attached): **0.02**

| gain | attached | rsrp mean | snr mean | bler mean | mcs mean |
|---|---|---|---|---|---|
| 1.0 | 100% | 60.0 | 141.0 | 0.0 | 20.0 |
| 0.5 | 100% | 54.0 | 141.0 | 0.0 | 20.0 |
| 0.3 | 100% | 49.0 | 141.0 | 0.0 | 20.0 |
| 0.2 | 100% | 46.0 | 142.0 | 0.0 | 20.0 |
| 0.15 | 100% | 43.0 | 141.0 | 0.0 | 20.0 |
| 0.1 | 100% | 40.0 | 142.0 | 0.0 | 20.0 |
| 0.07 | 100% | 37.0 | 142.0 | 0.0 | 20.0 |
| 0.05 | 100% | 34.0 | 142.0 | 0.0 | 20.0 |
| 0.04 | 100% | 32.0 | 141.0 | 0.0 | 20.0 |
| 0.03 | 100% | 29.0 | 142.0 | 0.0 | 20.0 |
| 0.02 | 100% | 26.0 | 141.0 | 0.0 | 20.0 |

## Phase 2 — Detach/revive behavior

UE: `ue3`
Reattach confirmed: **True**
Reattach time: **4.03349757194519s**

Other UEs' stability during detach window:

- `ue1`: rsrp=60.0, snr=141.0, bler=0.0, status_changed=False
- `ue2`: rsrp=60.0, snr=141.0, bler=0.0, status_changed=False

## Phase 3 — Clean baseline (broker artifact floor)

Duration: 120s, samples: 119

### ue1

- RSRP: mean=60.0, std=0.0
- DL SNR: mean=141.0, std=0.0
- DL BLER: mean=0.0, nonzero fraction=0.0  (**this is the broker artifact floor for BLER — LSTM anomaly threshold must sit above this, not above zero**)
- DL MCS: mean=20.0, std=0.0
- DL throughput: mean=815.9367275714286, std=57.04620888351648
- Backlog: mean=36496.13445378151, std=24612.75530926947
- late_dropped over full run: 80640

### ue2

- RSRP: mean=60.0, std=0.0
- DL SNR: mean=141.0, std=0.0
- DL BLER: mean=0.0, nonzero fraction=0.0  (**this is the broker artifact floor for BLER — LSTM anomaly threshold must sit above this, not above zero**)
- DL MCS: mean=19.49579831932773, std=3.1485104080324517
- DL throughput: mean=810.151649277311, std=186.12541839409144
- Backlog: mean=18974.117647058825, std=23694.007874829505
- late_dropped over full run: 172800

### ue3

- RSRP: mean=60.0, std=0.0
- DL SNR: mean=141.0, std=0.0
- DL BLER: mean=0.0, nonzero fraction=0.0  (**this is the broker artifact floor for BLER — LSTM anomaly threshold must sit above this, not above zero**)
- DL MCS: mean=18.65546218487395, std=3.00405650827574
- DL throughput: mean=814.7977223109243, std=34.714552709890434
- Backlog: mean=6582.857142857143, std=16873.078001926024
- late_dropped over full run: 0
