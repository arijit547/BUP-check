# BUP Fuel Supply Operations — Model Card

## 1. Demand Forecasting Models

### 1.1 Pretrained Foundation Model (`amazon/chronos-2`)

- **Model ID & Revision:** `amazon/chronos-2` (`29ec3766d36d6f73f0696f85560a422f50e8498c`, Apache-2.0)
- **Local Artifact Packaging:** Stored outside Git under `ml-service/weights/chronos-2` and verified against committed [model-manifest.json](../ml-service/model-manifest.json) on prepare and container startup (`config.json` `1,067` bytes, SHA-256 `ef1143bfdc9c0376d9a056eefca46cb4b1ec3d0ffacd541ff56feb40fb708031`; `model.safetensors` `477,930,472` bytes, SHA-256 `ddcda3c7508bf2528087723e98a20707cc04b7f370ae275a9fd88078ddba4f42`). Tampered files fail SHA-256 validation (`download-models.py --verify-only` exits `1`).
- **Offline Runtime:** Executed in [chronos_adapter.py](../ml-service/chronos_adapter.py) inside an isolated FastAPI inference service ([ml-service/](../ml-service/main.py)) built from SHA-locked [requirements.lock.txt](../ml-service/requirements.lock.txt) on CPU with `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `local_files_only=True`, and `trust_remote_code=False`.
- **Inputs & Causal Covariates (`v1.0.0`):** Per-(station, fuel) historical tick-regularized `demand_liters`, naive UTC timestamp grid (`pandas==2.2.3`, `numpy==2.2.6`), hourly seasonality covariates, and known/planned horizon demand multipliers.
- **Outputs:** Probabilistic quantile trajectories (`p10`, `p50`, `p90`, guaranteed non-negative and monotone `p10 <= p50 <= p90`) over horizons up to 96 ticks.
- **Measured Offline CPU Performance (`ml-service` with `--network=none`):** Cold offline load `9.19 s` (`9191.8 ms` for both models); offline rolling-origin evaluation median batch latency `1,028.68–1,633.74 ms` ([forecast_eval.json](evidence/forecast_eval.json)); live sustained ML load benchmark (`12/12` `CHRONOS2`, `0` errors) `p50 976.77 ms` / `p95 1029.86 ms` ([benchmark_matrix.json](evidence/benchmark_matrix.json)).
- **Operational Role:** Exposed as the verified ML candidate forecaster (`policy_override=CHRONOS2` in `GET /api/forecasts`, `GET /api/planning`, `POST /api/optimize`) rather than default synchronous planning, because chronological rolling-origin evaluation showed `SEASONAL` achieved lower `h=96` MAE (`4.153` vs `4.311` normal; `6.928` vs `7.362` held-out crisis) at `<15 ms` backend latency, while `CHRONOS2` achieved stronger `p10–p90` interval coverage (`0.7253` / `0.7595` vs `0.7083` / `0.7068`) and lower medium-horizon crisis RMSE at `h=16` (`25.614` vs `26.107`) and `h=32` (`23.392` vs `23.843`).

### 1.2 Seasonal Covariate Baseline (`seasonal-v2.0`)

- **Method:** De-seasonalizes observed ticks using each station's official demand profile diurnal curve and causal known event multiplier (`demand_liters / (hour_factor * known_spike_multiplier)`), then re-projects across horizon ticks multiplied by future diurnal factors and active/scheduled event multipliers, bounded by seasonal standard-deviation sigma bands (`[p10, p90]`).
- **Default Status:** Active default planning forecaster (`SEASONAL`) due to `<15 ms` latency and winning point accuracy in measured rolling-origin benchmarks ([forecast_eval.json](evidence/forecast_eval.json)).

### 1.3 Cold-Start Profile Fallback (`profile-fallback-v1`)

- **Activation Condition:** Fewer than `history_ticks` completed ticks in the current generation, ML service timeout/unavailable, or simulator data-quality fault.
- **Behavior:** Uses official per-profile daily demand baselines (`PROFILE_DAILY_LITERS`), region demand factor, diurnal factor, and current active multiplier with conservative `+/- 20%` bounds. Does not invent synthetic tick histories.

---

## 2. Anomaly Detection Model (`IsolationForest`)

- **Model Artifact & Version:** `iforest-bup-v1.0.0` (`ml-service/artifacts/isolation_forest.joblib`, trained inside the locked `ml-service` image via [scripts/prepare-models.py](../scripts/prepare-models.py) and bind-mounted read-only; SHA-256 `7191a531d25dd06e29b59448a80d4a16a73904eaed06da499cc9895b`).
- **Algorithm & Hyperparameters ([train_anomaly.py](../ml-service/train_anomaly.py)):** `sklearn.pipeline.Pipeline([StandardScaler(), IsolationForest(n_estimators=200, contamination=0.005, random_state=42)])`, `decision_threshold = 0.0`.
- **Feature Vector (7 causal features, [anomaly_model.py](../ml-service/anomaly_model.py)):**
  1. `normalized_demand_ratio` (diurnal + known-event normalized demand vs trailing baseline)
  2. `rolling_residual_z` (`(observed demand - expected) / max(1, rolling_std)`)
  3. `inventory_ratio` (`inventory / capacity`)
  4. `has_inventory_residual` (`1.0` when prior-tick ending stock is available, else `0.0`)
  5. `inventory_balance_residual` (step-over-step normalized tank delta)
  6. `sin_hour`, 7. `cos_hour` (cyclical time-of-day encoding)
- **Strict Split Integrity:** Fitted **exclusively** on crisis-free normal simulation traces (`normal_baseline.csv`, `3,360` post-warmup rows) — zero exposure to crisis validation (`crisis_dev_mix.csv`) or held-out (`crisis_heldout_mix.csv`) traces.
- **Measured Offline Performance ([model-manifest.json](../ml-service/model-manifest.json)):**
  - Normal training split FPR: `0.0051`
  - Unannounced crisis validation (`crisis_dev_mix`): `precision=0.7273`, `recall=1.0`, `f1=0.8421`, `fpr=0.0083`, `detection_delay_ticks=0`
  - Unannounced crisis held-out (`crisis_heldout_mix`): `precision=0.8372`, `recall=1.0`, `f1=0.9114`, `fpr=0.01`, `detection_delay_ticks=0`
  - Validation residual FPR after known multiplier adjustment: `0.0083`
- **Role With Rules (`merge_ml_anomalies` in [anomaly.py](../backend/intelligence/anomaly.py)):** Enriches matching rule-detected spikes to `HYBRID` (with score + evidence), suppresses redundant ML alerts during already-documented `demand_spike` events, and flags unannounced demand shifts as `UNEXPECTED_DEMAND_ANOMALY` (`ML`).

---

## 3. Limitations, Uncertainty & Failure Posture

- **Heuristic Stockout Probabilities:** Per-station `stockout_probability` fields are explicitly tagged `probability_calibration = "SCENARIO_HEURISTIC"` (and labeled `"Uncalibrated Heuristic"` in the UI), derived from normal-tail projection against rolling variance rather than empirically calibrated multi-seed survival curves.
- **Non-Demand Domain Faults:** Structural outages (`station_outage`), highway blocks (`route_disruption`), depot caps (`depot_constraint`), delays (`shipment_delay`), and supply cuts (`supply_shortfall`) are detected by deterministic rules against authoritative simulator state rather than inferred by statistical demand models alone.
- **ML Readiness & Graceful Degradation:** `ml-service` `/health/ready` returns HTTP `200` only when **both** Chronos-2 and Isolation Forest load cleanly (and HTTP `503` with `status="partial"` or `"degraded"` if either model fails). When `ml-service` is stopped, partial, unreachable, or exceeds `ML_FORECAST_TIMEOUT_SECONDS` (`8.0 s`), `snapshot-api` automatically reverts to `SEASONAL` + `RULE` detection while keeping `/api/planning` and OR-Tools optimization online (`status="ok"`, component `ml_service="degraded"`).
