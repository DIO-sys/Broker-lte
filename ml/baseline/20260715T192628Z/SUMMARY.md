# Step 0 — Broker Artifact Characterization

Generated: 2026-07-15T19:58:18.502385+00:00

## Phase 1 — Gain floor sweep

UE: `ue3`
Empirical decode floor (lowest gain fully attached): **0.02**

| gain | attached | rsrp mean | snr mean | bler mean | mcs mean |
|---|---|---|---|---|---|
| 1.0 | 100% | 60.0 | 141.0 | 0.0 | 15.2 |
| 0.5 | 100% | 54.0 | 141.0 | 0.0 | 18.4 |
| 0.3 | 100% | 49.0 | 141.0 | 0.0 | 18.4 |
| 0.2 | 100% | 46.0 | 142.0 | 0.0 | 20.0 |
| 0.15 | 100% | 43.0 | 141.0 | 0.0 | 20.0 |
| 0.1 | 100% | 40.0 | 142.0 | 0.0 | 20.0 |
| 0.07 | 100% | 37.0 | 142.0 | 0.0 | 20.0 |
| 0.05 | 100% | 34.0 | 142.0 | 0.0 | 20.0 |
| 0.04 | 100% | 32.0 | 141.0 | 0.0 | 20.0 |
| 0.03 | 100% | 29.0 | 142.0 | 0.0 | 20.0 |
| 0.02 | 100% | 26.0 | 141.0 | 0.0 | 16.8 |

## Phase 2 — Detach/revive behavior

UE: `ue3`
Reattach confirmed: **True**
Reattach time: **4.066726446151733s**

Other UEs' stability during detach window:

- `ue1`: rsrp=60.0, snr=141.0, bler=0.0, status_changed=False
- `ue2`: rsrp=60.0, snr=141.0, bler=0.0, status_changed=False

## Phase 3 — Clean baseline (broker artifact floor)

Duration: 1800s, samples: 1778

### ue1

- RSRP: mean=60.0, std=0.0
- DL SNR: mean=141.0, std=0.0
- DL BLER: mean=0.0, nonzero fraction=0.0  (**this is the broker artifact floor for BLER — LSTM anomaly threshold must sit above this, not above zero**)
- DL MCS: mean=19.921259842519685, std=1.2527912286823133
- DL throughput: mean=814.6494610224972, std=106.15694493596463
- Backlog: mean=34339.70753655793, std=29790.528396314778
- late_dropped over full run: 1704960

### ue2

- RSRP: mean=60.0, std=0.0
- DL SNR: mean=141.0, std=0.0
- DL BLER: mean=0.0, nonzero fraction=0.0  (**this is the broker artifact floor for BLER — LSTM anomaly threshold must sit above this, not above zero**)
- DL MCS: mean=19.921259842519685, std=1.2527912286823133
- DL throughput: mean=813.4094716805399, std=79.37704518115854
- Backlog: mean=26292.553430821146, std=28885.46667699309
- late_dropped over full run: 1935360

### ue3

- RSRP: mean=60.0, std=0.0
- DL SNR: mean=141.0, std=0.0
- DL BLER: mean=0.0, nonzero fraction=0.0  (**this is the broker artifact floor for BLER — LSTM anomaly threshold must sit above this, not above zero**)
- DL MCS: mean=19.943757030371202, std=1.0593995238693092
- DL throughput: mean=814.4092319336334, std=95.50953956243438
- Backlog: mean=12368.773903262092, std=21426.570779191825
- late_dropped over full run: 1612800
