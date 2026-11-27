# BUP Fuel Supply Operations Platform

Repository: `https://github.com/arijit547/BUP-check`

A full-stack, decision-intelligent fuel supply operations platform built on top of the unmodified official BUP Fuel Supply Simulator (`asifmahmoud414/bup-fuel-supply-simulator:1.0.0@sha256:7067050693f49d377d91ca91f2faa6e63f673f69a69429d4693e78e9c0598e92`).

## Quick Start

### Core Mode (No Local ML Weights Required; `<15 ms` Cached Planning)
```powershell
docker compose up -d --build --wait
# Dashboard: http://127.0.0.1:3000
# Backend API & OpenAPI: http://127.0.0.1:8001/docs
# Official Simulator: http://127.0.0.1:8000/docs
```

### Full ML + Prometheus Mode (Offline `amazon/chronos-2` + `IsolationForest` + Prometheus)
On a clean checkout, download/verify the pinned Hugging Face weights (`ml-service/weights/chronos-2`) and train the locked `IsolationForest` artifact (`ml-service/artifacts/isolation_forest.joblib`) first:
```powershell
python -u scripts/prepare-models.py
docker compose -f compose.yaml -f compose.ml.yaml --profile ml --profile monitoring up -d --build --wait
# Prometheus UI: http://127.0.0.1:9090
```

See [`.env.example`](.env.example) for optional operator authentication (`OPERATOR_AUTH_MODE=required`), `RUNNING_COHERENT_GRACE_SECONDS` (`15.0`), timeout, and port overrides.

## Key Capabilities

- **Resilient Simulator Integration:** Atomic `before==after` snapshot collection, SSE stream invalidation & reconnect, bounded `RUNNING` coherence grace window (`15.0 s`) with explicit provenance headers (`X-Bup-{As-Of-Tick,Current-Tick,Age-Ticks,Observed-At,Cached,Degraded}`), circuit breaker, and SQLite WAL generation ledger isolating simulator resets (`POST /api/generations/rebind`).
- **Demand Forecasting (`GET /api/forecasts`):** Seasonal covariate baseline (`seasonal-v2.0`, `<15 ms`), offline Hugging Face `amazon/chronos-2` candidate (`policy_override=CHRONOS2`) with native `p10–p90` quantiles, and cold-start profile fallback (`profile-fallback-v1`).
- **Hybrid Anomaly & Incident Ledger (`GET /api/anomalies`, `/api/incidents`):** Deterministic domain rules for all 6 simulator events (`demand_spike`, `route_disruption`, `station_outage`, `depot_constraint`, `shipment_delay`, `supply_shortfall`) enriched by `iforest-bup-v1.0.0` (`IsolationForest`) with 2-tick clear hysteresis.
- **Shortage Risk & Constrained Optimization (`GET /api/shortages`, `POST /api/optimize`, `POST /api/simulate`):** Separates unpreventable station outages from stock depletion, computes lead-time safety stock targets, solves global constrained dispatch with Google OR-Tools `CBC` (with optional `max_recommendations` and `max_total_liters` budgets), independently validates every shipment, and retains a safe reorder fallback (`SAFE_REORDER`).
- **Backend-Owned Joint Plan & Human-Gated Execution:** Explicit operator review and idempotent execution via backend-persisted `plan_id` (`GET /api/plans/latest`, `PUT /api/plans/{plan_id}/approval`) and single-shipment ledger (`PUT /api/approvals/{intent_id}`, `POST /api/approvals/{intent_id}/cancel`) with generation-safe `409` reset blocking, timeout-after-commit reconciliation, and optional PBKDF2/Bearer authentication.
- **6-Tab Operator Dashboard:** Overview, Interactive Network Topology SVG, Demand Forecast Explorer (`p10–p90` band), Shortage & Incident Timeline, Joint Optimizer & Counterfactual Simulator, and Models & Evaluation View.

## Verification Commands

```powershell
npm ci --prefix frontend --ignore-scripts
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

## Technical Documentation

- [System Architecture](docs/architecture.md)
- [Model Card (`Chronos-2` & `IsolationForest`)](docs/model-card.md)
- [Decision & Optimization Methodology](docs/decision-methodology.md)
- [Forecast, Anomaly & Policy Evaluation](docs/evaluation.md)
- [Test & Verification Report](docs/test-report.md)
- [Requirements Traceability Matrix](docs/requirements-traceability.md)
- [Implementation Status](docs/implementation-status.md)
- [Live Demo Runbook](docs/demo-runbook.md)
- [Submission Evidence](docs/submission-evidence.md) & [Submission Checklist](docs/submission-checklist.md)
- [Saved OpenAPI Contract](docs/openapi.json)
