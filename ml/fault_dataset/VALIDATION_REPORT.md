# Fault Dataset — Validation Report

Sessions in manifest: 54 / 54 expected combos

All 54 combos present. ✅

Sessions with structural issues: **0**
Sessions with effect-check failures: **12**
Sessions with missing files: **0**

## Total samples by phase

- clean: 3230
- onset: 270
- active: 2475
- recovery: 3240

## Per-session results

| session | structural issues | effect check | key numbers |
|---|---|---|---|
| transport_stall__low__ue2 | none | PASS | {'metric': 'dl_brate + dl_snr', 'clean_brate': 91929.0, 'active_brate': 814.0, 'ratio': 0.009, 'snr_clean': 140.0, 'snr_active': 140.0, 'snr_stayed_flat': True} |
| transport_stall__medium__ue2 | none | PASS | {'metric': 'dl_brate + dl_snr', 'clean_brate': 91878.0, 'active_brate': 810.7, 'ratio': 0.009, 'snr_clean': 140.0, 'snr_active': 140.0, 'snr_stayed_flat': True} |
| co_channel_interference__low__ue1 | none | PASS | {'metric': 'dl_snr', 'clean': 140.0, 'active': 25.0, 'delta': 115.0} |
| co_channel_interference__low__ue2 | none | PASS | {'metric': 'dl_snr', 'clean': 140.0, 'active': 25.0, 'delta': 115.0} |
| co_channel_interference__low__ue3 | none | PASS | {'metric': 'dl_snr', 'clean': 140.0, 'active': 25.0, 'delta': 115.0} |
| co_channel_interference__medium__ue1 | none | PASS | {'metric': 'dl_snr', 'clean': 140.0, 'active': 16.0, 'delta': 124.0} |
| co_channel_interference__medium__ue2 | none | PASS | {'metric': 'dl_snr', 'clean': 140.0, 'active': 16.0, 'delta': 124.0} |
| co_channel_interference__medium__ue3 | none | PASS | {'metric': 'dl_snr', 'clean': 140.0, 'active': 16.0, 'delta': 124.0} |
| co_channel_interference__high__ue1 | none | PASS | {'metric': 'dl_snr', 'clean': 140.0, 'active': 7.4, 'delta': 132.6} |
| co_channel_interference__high__ue2 | none | PASS | {'metric': 'dl_snr', 'clean': 140.0, 'active': 7.4, 'delta': 132.6} |
| co_channel_interference__high__ue3 | none | PASS | {'metric': 'dl_snr', 'clean': 140.0, 'active': 7.4, 'delta': 132.6} |
| bler_degradation__low__ue1 | none | PASS | {'metric': 'dl_snr', 'clean': 140.0, 'active': 14.2, 'delta': 125.8} |
| bler_degradation__low__ue2 | none | PASS | {'metric': 'dl_snr', 'clean': 140.0, 'active': 14.27, 'delta': 125.73} |
| bler_degradation__low__ue3 | none | PASS | {'metric': 'dl_snr', 'clean': 140.0, 'active': 14.31, 'delta': 125.69} |
| bler_degradation__medium__ue1 | none | PASS | {'metric': 'dl_snr', 'clean': 140.0, 'active': 11.08, 'delta': 128.92} |
| bler_degradation__medium__ue2 | none | PASS | {'metric': 'dl_snr', 'clean': 140.0, 'active': 11.03, 'delta': 128.97} |
| bler_degradation__medium__ue3 | none | PASS | {'metric': 'dl_snr', 'clean': 140.0, 'active': 11.04, 'delta': 128.96} |
| bler_degradation__high__ue1 | none | PASS | {'metric': 'dl_snr', 'clean': 140.0, 'active': 7.48, 'delta': 132.52} |
| bler_degradation__high__ue2 | none | PASS | {'metric': 'dl_snr', 'clean': 140.0, 'active': 7.48, 'delta': 132.52} |
| bler_degradation__high__ue3 | none | PASS | {'metric': 'dl_snr', 'clean': 140.0, 'active': 7.51, 'delta': 132.49} |
| transport_stall__low__ue1 | none | PASS | {'metric': 'dl_brate + dl_snr', 'clean_brate': 91642.7, 'active_brate': 889.7, 'ratio': 0.01, 'snr_clean': 140.0, 'snr_active': 140.0, 'snr_stayed_flat': True} |
| transport_stall__low__ue3 | none | PASS | {'metric': 'dl_brate + dl_snr', 'clean_brate': 92814.3, 'active_brate': 811.1, 'ratio': 0.009, 'snr_clean': 140.0, 'snr_active': 140.0, 'snr_stayed_flat': True} |
| transport_stall__medium__ue1 | none | PASS | {'metric': 'dl_brate + dl_snr', 'clean_brate': 91696.9, 'active_brate': 809.3, 'ratio': 0.009, 'snr_clean': 140.56666666666666, 'snr_active': 141.0, 'snr_stayed_flat': True} |
| transport_stall__high__ue1 | none | PASS | {'metric': 'dl_brate + dl_snr', 'clean_brate': 91386.4, 'active_brate': 851.1, 'ratio': 0.009, 'snr_clean': 140.56666666666666, 'snr_active': 141.0, 'snr_stayed_flat': True} |
| link_dropout__low__ue1 | none | PASS | {'metric': 'dl_snr', 'clean': 140.0, 'active': 5.6, 'delta': 134.4} |
| link_dropout__low__ue2 | none | PASS | {'metric': 'dl_snr', 'clean': 140.02, 'active': 5.6, 'delta': 134.42} |
| link_dropout__low__ue3 | none | PASS | {'metric': 'dl_snr', 'clean': 141.0, 'active': 5.6, 'delta': 135.4} |
| link_dropout__medium__ue1 | none | PASS | {'metric': 'dl_snr', 'clean': 141.0, 'active': 0.0, 'delta': 141.0} |
| link_dropout__medium__ue2 | none | PASS | {'metric': 'dl_snr', 'clean': 141.0, 'active': 0.0, 'delta': 141.0} |
| link_dropout__medium__ue3 | none | PASS | {'metric': 'dl_snr', 'clean': 141.0, 'active': 0.0, 'delta': 141.0} |
| link_dropout__high__ue1 | none | PASS | {'metric': 'status', 'killed_fraction': 1.0} |
| link_dropout__high__ue2 | none | PASS | {'metric': 'status', 'killed_fraction': 1.0} |
| link_dropout__high__ue3 | none | PASS | {'metric': 'status', 'killed_fraction': 1.0} |
| scheduler_starvation__low__ue1 | none | **FAIL** | {'metric': 'ul_brate (heavy traffic) + dl_snr (noise)', 'clean_ul_brate': 4513.0, 'active_ul_brate': 4559.4, 'ul_ratio': 1.01, 'dl_snr_delta': 125.0} |
| scheduler_starvation__low__ue2 | none | **FAIL** | {'metric': 'ul_brate (heavy traffic) + dl_snr (noise)', 'clean_ul_brate': 4557.6, 'active_ul_brate': 4567.9, 'ul_ratio': 1.0, 'dl_snr_delta': 125.0} |
| scheduler_starvation__low__ue3 | none | **FAIL** | {'metric': 'ul_brate (heavy traffic) + dl_snr (noise)', 'clean_ul_brate': 4468.6, 'active_ul_brate': 4533.1, 'ul_ratio': 1.01, 'dl_snr_delta': 125.0} |
| scheduler_starvation__medium__ue1 | none | **FAIL** | {'metric': 'ul_brate (heavy traffic) + dl_snr (noise)', 'clean_ul_brate': 4560.8, 'active_ul_brate': 4724.2, 'ul_ratio': 1.04, 'dl_snr_delta': 130.0} |
| scheduler_starvation__medium__ue2 | none | **FAIL** | {'metric': 'ul_brate (heavy traffic) + dl_snr (noise)', 'clean_ul_brate': 4622.3, 'active_ul_brate': 4559.6, 'ul_ratio': 0.99, 'dl_snr_delta': 130.0} |
| scheduler_starvation__medium__ue3 | none | **FAIL** | {'metric': 'ul_brate (heavy traffic) + dl_snr (noise)', 'clean_ul_brate': 4533.5, 'active_ul_brate': 4543.5, 'ul_ratio': 1.0, 'dl_snr_delta': 130.0} |
| scheduler_starvation__high__ue1 | none | **FAIL** | {'metric': 'ul_brate (heavy traffic) + dl_snr (noise)', 'clean_ul_brate': 4556.0, 'active_ul_brate': 4565.8, 'ul_ratio': 1.0, 'dl_snr_delta': 130.0} |
| scheduler_starvation__high__ue2 | none | **FAIL** | {'metric': 'ul_brate (heavy traffic) + dl_snr (noise)', 'clean_ul_brate': 4556.6, 'active_ul_brate': 4662.6, 'ul_ratio': 1.02, 'dl_snr_delta': 130.0} |
| scheduler_starvation__high__ue3 | none | **FAIL** | {'metric': 'ul_brate (heavy traffic) + dl_snr (noise)', 'clean_ul_brate': 4556.2, 'active_ul_brate': 4528.3, 'ul_ratio': 0.99, 'dl_snr_delta': 130.0} |
| uplink_contamination__low__ue1 | none | **FAIL** | {'metric': 'ul_bler', 'target_clean': 0.0, 'target_active': 0.0, 'target_delta': 0.0, 'cross_ue_effect_seen': False} |
| uplink_contamination__low__ue2 | none | **FAIL** | {'metric': 'ul_bler', 'target_clean': 0.0, 'target_active': 0.0, 'target_delta': 0.0, 'cross_ue_effect_seen': False} |
| uplink_contamination__low__ue3 | none | **FAIL** | {'metric': 'ul_bler', 'target_clean': 0.0, 'target_active': 0.0, 'target_delta': 0.0, 'cross_ue_effect_seen': False} |
| uplink_contamination__medium__ue1 | none | PASS | {'metric': 'ul_bler', 'target_clean': 0.0, 'target_active': 6.0, 'target_delta': 6.0, 'cross_ue_effect_seen': True} |
| uplink_contamination__medium__ue2 | none | PASS | {'metric': 'ul_bler', 'target_clean': 0.0, 'target_active': 6.025, 'target_delta': 6.03, 'cross_ue_effect_seen': True} |
| uplink_contamination__medium__ue3 | none | PASS | {'metric': 'ul_bler', 'target_clean': 0.0, 'target_active': 6.05, 'target_delta': 6.05, 'cross_ue_effect_seen': True} |
| uplink_contamination__high__ue1 | none | PASS | {'metric': 'ul_bler', 'target_clean': 0.0, 'target_active': 5.045, 'target_delta': 5.04, 'cross_ue_effect_seen': True} |
| uplink_contamination__high__ue2 | none | PASS | {'metric': 'ul_bler', 'target_clean': 0.0, 'target_active': 3.19, 'target_delta': 3.19, 'cross_ue_effect_seen': True} |
| uplink_contamination__high__ue3 | none | PASS | {'metric': 'ul_bler', 'target_clean': 0.0, 'target_active': 3.11, 'target_delta': 3.11, 'cross_ue_effect_seen': True} |
| transport_stall__medium__ue3 | none | PASS | {'metric': 'dl_brate + dl_snr', 'clean_brate': 93074.7, 'active_brate': 813.3, 'ratio': 0.009, 'snr_clean': 140.0, 'snr_active': 140.0, 'snr_stayed_flat': True} |
| transport_stall__high__ue2 | none | PASS | {'metric': 'dl_brate + dl_snr', 'clean_brate': 92767.3, 'active_brate': 811.1, 'ratio': 0.009, 'snr_clean': 140.0, 'snr_active': 140.0, 'snr_stayed_flat': True} |
| transport_stall__high__ue3 | none | PASS | {'metric': 'dl_brate + dl_snr', 'clean_brate': 90988.3, 'active_brate': 802.4, 'ratio': 0.009, 'snr_clean': 140.0, 'snr_active': 140.0, 'snr_stayed_flat': True} |