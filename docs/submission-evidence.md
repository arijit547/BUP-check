# BUP Fuel Operations submission evidence

## Architecture & Methodology

- System Architecture & Mermaid Diagram: [architecture.md](architecture.md)
- Pretrained ML & Anomaly Model Card: [model-card.md](model-card.md)
- Optimization & Decision Methodology: [decision-methodology.md](decision-methodology.md)
- Requirements Traceability: [requirements-traceability.md](requirements-traceability.md)

## Startup, Unit Tests, E2E & ML Lifecycle

- **Backend Suite (`14/14` PASS):** Isolated container run (`python -m unittest discover -s tests -v`, `5.764 s`) validates data hygiene, causal features, OR-Tools constraints, joint `PUT /api/plans/{plan_id}/approval` replay/partial/timeout/reset handling, `RUNNING` provenance headers, `stale_data` blocking, and curated-evidence vs unmeasured `/api/evaluation` fallback.
- **ML Lifecycle & Offline Verification (`4/4` PASS, [ml_verification.json](evidence/ml_verification.json)):**
  1. Core Compose stack boots with zero model weights (`SEASONAL` default).
  2. `ml-service /health/ready` returns `503` `partial` when `chronos-2` is absent even when `iforest` loads.
  3. [prepare-models.py](../scripts/prepare-models.py) prepares artifacts and passes `--network=none` offline load (`OFFLINE_PREP_OK`, `amazon/chronos-2` `29ec3766d36d` + `iforest-bup-v1.0.0`, `9,191.8 ms`). Tampered weights fail SHA-256 verification (`download-models.py --verify-only` exits `1`).
  4. Live `ml-service` runs `CHRONOS2` (`3,104.91 ms`), gracefully degrades to `SEASONAL` on container stop, and recovers `CHRONOS2` on restart.

## Forecast, Anomaly & Matched-Budget Policy Evaluation

Measured results stored cleanly in curated [evidence/](evidence/) and detailed in [evaluation.md](evaluation.md):
- **Chronological Forecast Evaluation ([forecast_eval.json](evidence/forecast_eval.json)):** `SEASONAL` achieves `4.153` MAE (`0.0513` WAPE) on `normal_baseline` and `6.928` MAE (`0.0827` WAPE) on `crisis_heldout_mix` at `h=96` (`~85%` / `~80%` MAE drop vs `rolling_mean`), while offline `amazon/chronos-2` provides higher `p10–p90` coverage (`0.7253` / `0.7595`) and lower `h=16, 32` crisis RMSE (`25.614` / `23.392`).
- **Matched-Budget Policy Comparison ([policy_comparison.json](evidence/policy_comparison.json)):** Across 4 crisis scenarios under `single_1_7k`, `batch_4_15k`, and `unconstrained` budgets (`0` failures, `warmup_unmet_liters = 0.0`, `2,696.711 L` unpreventable `outage_unmet_liters`):
  - `reorder` / `safe_reorder` win tight matched budgets (`0.974414` in `single_1_7k`, `0.994912` in `batch_4_15k`).
  - `reorder_batch_unconstrained` (`157` dispatches) and `ortools_seasonal_batch_unconstrained` (`797` dispatches) both hit the `0.994912` service ceiling (`0.993412` measured window, `320.415 L` depletion unmet); `ortools_chronos2_batch_unconstrained` reaches `0.994886` with `70/70` live ML calls.

## Resilience, Concurrency & Sustained Load Evidence

Full details recorded in [test-report.md](test-report.md):
- **Resilience Matrix (`17/17` PASS, [resilience_matrix.json](evidence/resilience_matrix.json)):** All 6 domain events, combined crisis, all 5 simulator faults (`4000ms` latency timeout `503`, `unavailable`, `error_rate`, strict `stale_data` write block, forced `stream_disconnect` SSE reconnect), ML stop/restart, 6-way concurrent plan submit (`10` allocs) + tick-37 replay (`10` allocs) + restart replay + reset `409` on plan & intent, `RUNNING` `age_ticks=0` provenance, security headers, and Prometheus `/metrics` (`10,306 B`).
- **Load & Sustained Matrix (`0` errors across all 8 scenarios, [benchmark_matrix.json](evidence/benchmark_matrix.json)):**
  - Cached planning: `107.5 req/s` (`c=2`, `p95 17.10 ms`), `269.6 req/s` (`c=10`, `p95 40.09 ms`), `244.74 req/s` (`c=25`, `p95 117.87 ms`).
  - Sustained planning (`c=10`, `120.39 s`): `106.37 req/s` (`12,806` reqs, `p50 53.34 ms`, `p95 266.15 ms`, `0` errors).
  - Uncached OR-Tools (`c=2`): `2.03 req/s` (`p95 1,173.85 ms`); Live `CHRONOS2` (`12/12`): `p50 976.77 ms` / `p95 1,029.86 ms`.
  - `RUNNING` simulator (`c=8`): `11.24 req/s` (`age_ticks_max=0` across `50` provenance samples); Plan contention (`c=4`): `35/35` HTTP `200`, `10` total simulator allocations; Prometheus target `up`.

## Reproduction Commands

```powershell
npm ci --prefix frontend --dry-run
npm run build --prefix frontend
$env:BUP_CHROMIUM_PATH = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
node scripts/verify.mjs e2e
node scripts/verify.mjs reorder-policy
node scripts/verify.mjs forecast-policy
python -u scripts/verify_ml_lifecycle.py
python -u scripts/run-resilience-matrix.py
python -u scripts/eval_policies.py
python -u scripts/benchmark_matrix.py --sustained-seconds 120
python -u scripts/test_live_scenarios.py
node scripts/verify_live_ui.mjs
go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.12 -no-color .github/workflows/ci.yml .github/workflows/release.yml
```
