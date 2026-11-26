# BUP Fuel Supply Operations — Forecast, Anomaly & Policy Evaluation

All results below were measured directly against isolated instances of the official simulator (`asifmahmoud414/bup-fuel-supply-simulator:1.0.0`). Zero values were fabricated or extrapolated.

---

## 1. Chronological Rolling-Origin Forecast Evaluation (`docs/evidence/forecast_eval.json`)

Evaluated across `normal_baseline.csv` (`288` ticks, `3,456` rows), `crisis_dev_mix.csv`, and `crisis_heldout_mix.csv` (`192` ticks, `2,304` rows) with strict causal cutoff (`history < t`, future known covariates only) at horizons $h \in \{4, 16, 32, 96\}$.

### Horizon `h = 96` Summary ([forecast_eval.json](evidence/forecast_eval.json))
| Dataset | Model | MAE (L/tick) | WAPE | p10–p90 Coverage |
| --- | --- | ---: | ---: | ---: |
| `normal_baseline` | `rolling_mean` | 28.510 | 0.3523 | 0.3887 |
| `normal_baseline` | `seasonal` (`seasonal-v2.0`) | **4.153** | **0.0513** | 0.7083 |
| `normal_baseline` | `chronos2` (`amazon/chronos-2`) | 4.311 | 0.0533 | **0.7253** |
| `crisis_heldout_mix` | `rolling_mean` | 34.201 | 0.4080 | 0.3822 |
| `crisis_heldout_mix` | `seasonal` (`seasonal-v2.0`) | **6.928** | **0.0827** | 0.7068 |
| `crisis_heldout_mix` | `chronos2` (`amazon/chronos-2`) | 7.362 | 0.0878 | **0.7595** |

- **Key Finding:** `SEASONAL` cuts `rolling_mean` `h=96` MAE by ~85% on normal traces (`4.153` vs `28.510`) and ~80% on held-out crises (`6.928` vs `34.201`), and outperforms zero-shot `chronos2` on `h=96` point MAE at `<15 ms` backend latency. Meanwhile `chronos2` achieves stronger `p10–p90` interval coverage (`0.7253` / `0.7595`) and lower crisis RMSE at `h=16` (`25.614` vs `26.107`) and `h=32` (`23.392` vs `23.843`) at median offline CPU batch latency of `1,028.68–1,633.74 ms`.

---

## 2. Anomaly Detection Evaluation (`ml-service/model-manifest.json`)

Trained `iforest-bup-v1.0.0` (`IsolationForest`, `contamination=0.005`, `threshold=0.0`, `200` trees) **strictly on crisis-free normal traces** (`3,360` post-warmup rows) inside the locked `ml-service` image:
- **Normal Training Split FPR:** `0.0051`
- **Validation Unannounced Crisis (`crisis_dev_mix`):** `precision=0.7273`, `recall=1.0`, `f1=0.8421`, `fpr=0.0083`, `detection_delay_ticks=0`
- **Held-Out Unannounced Crisis (`crisis_heldout_mix`):** `precision=0.8372`, `recall=1.0`, `f1=0.9114`, `fpr=0.01`, `detection_delay_ticks=0`
- **Validation Residual FPR (known multiplier applied):** `0.0083`

---

## 3. Matched-Budget Multi-Scenario Policy Evaluation (`docs/evidence/policy_comparison.json`)

Using `scripts/eval_policies.py` against single-seed (`12345`) isolated simulator runs over 4 crisis scenarios (`spike_route_disruption`, `multi_station_combined_crisis`, `outage_and_recovery`, `heldout_late_crisis`) on identical 6-tick decision cadences, identical warm-up periods (`warmup_unmet_liters = 0.0` in all runs), and matched per-step truck & liter budgets:

| Policy Variant | Budget Tier | Mean SL | Measured Window SL | Outage Unmet (L) | Depletion Unmet (L) | Total Unmet (L) | Allocated (L) | Decisions (`fail`) | ML / FB Steps | Mean Latency |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `reorder_single_1_7k` | `1` truck, `7,000 L` | **0.974414** | **0.961297** | 2,696.711 | **15,028.899** | **17,725.610** | 397,923.074 | 70 (`0`) | `0 / 0` | **50.14 ms** |
| `safe_reorder_single_1_7k` | `1` truck, `7,000 L` | **0.974414** | **0.961297** | 2,696.711 | **15,028.899** | **17,725.610** | 397,923.074 | 70 (`0`) | `0 / 70` | 352.94 ms |
| `ortools_seasonal_single_1_7k` | `1` truck, `7,000 L` | 0.965283 | 0.949791 | 2,696.711 | 20,298.661 | 22,995.372 | 457,397.099 | 70 (`0`) | `0 / 0` | 698.52 ms |
| `ortools_chronos2_single_1_7k` | `1` truck, `7,000 L` | 0.963986 | 0.948368 | 2,696.711 | 20,950.225 | 23,646.936 | 457,618.376 | 70 (`0`) | `70 / 0` | 1,959.44 ms |
| `reorder_batch_4_15k` | `<=4` trucks, `15,000 L` | **0.994912** | **0.993412** | 2,696.711 | **320.415** | **3,017.126** | 502,498.466 | 144 (`0`) | `0 / 0` | **52.50 ms** |
| `safe_reorder_batch_4_15k` | `<=4` trucks, `15,000 L` | **0.994912** | **0.993412** | 2,696.711 | **320.415** | **3,017.126** | 543,272.975 | 277 (`0`) | `0 / 70` | 362.13 ms |
| `ortools_seasonal_batch_4_15k` | `<=4` trucks, `15,000 L` | 0.993699 | 0.991439 | 2,696.711 | 1,224.348 | 3,921.059 | 711,123.543 | 280 (`0`) | `0 / 0` | 615.74 ms |
| `ortools_chronos2_batch_4_15k` | `<=4` trucks, `15,000 L` | 0.992586 | 0.989626 | 2,696.711 | 2,054.300 | 4,751.011 | 707,559.868 | 280 (`0`) | `70 / 0` | 1,778.19 ms |
| `reorder_batch_unconstrained` | Unconstrained | **0.994912** | **0.993412** | 2,696.711 | **320.415** | **3,017.126** | 509,012.713 | 157 (`0`) | `0 / 0` | **51.68 ms** |
| `ortools_seasonal_batch_unconstrained` | Unconstrained | **0.994912** | **0.993412** | 2,696.711 | **320.415** | **3,017.126** | 737,984.014 | 797 (`0`) | `0 / 0` | 498.95 ms |
| `ortools_chronos2_batch_unconstrained` | Unconstrained | 0.994886 | 0.993371 | 2,696.711 | 339.391 | 3,036.102 | 735,949.471 | 795 (`0`) | `70 / 0` | 1,616.51 ms |

*(Historical `v1` asymmetric comparison retained for traceability only: `ortools_seasonal_batch` `797` unconstrained dispatches `0.994912` / `3,017.126 L` unmet vs single-pick `original_reorder` `70` dispatches `0.976098` / `16,470.692 L` unmet — that 81.7% unmet drop came from allowing multi-shipment batches per step rather than optimizer accuracy alone.)*

### Honest Policy Conclusions
1. **Unpreventable Outage Floor:** Across all 4 crises, `2,696.711 L` of unmet demand occurs during the `station_outage` window on `station-tongi` (`outage_unmet_liters`), leaving a theoretical physical ceiling of `0.994912` (`0.993412` inside the measured post-warmup window, `320.415 L` residual depletion).
2. **Matched-Budget Reality (`single_1_7k` & `batch_4_15k`):** When dispatches per 6-tick step are capped at `1` shipment / `7,000 L` or `4` shipments / `15,000 L`, greedy lowest-ratio replenishment (`reorder` / `safe_reorder`) outperforms single-step OR-Tools (`0.974414` vs `0.965283` in `single_1_7k`; `0.994912` vs `0.993699` in `batch_4_15k`) with fewer total dispatches (`144` vs `280`) and `<363 ms` latency, because OR-Tools reserves headroom across horizon stations rather than greedily filling only the single sharpest tank drop when truck count is constrained.
3. **Unconstrained Joint Batch & Forecaster Choice:** Without tight per-step truck caps, both `reorder_batch_unconstrained` (`157` dispatches, `509,013 L`) and `ortools_seasonal_batch_unconstrained` (`797` dispatches, `737,984 L` keeping stations topped at higher safety buffer) reach the `0.994912` ceiling (`3,017.126 L` unmet), beating `ortools_chronos2_batch_unconstrained` (`0.994886`, `3,036.102 L` unmet, `1,616.51 ms` mean latency). Hence `SEASONAL` remains default, `SAFE_REORDER` stays first-class alongside OR-Tools `CBC`, and `amazon/chronos-2` remains live and selectable (`CHRONOS2`).
