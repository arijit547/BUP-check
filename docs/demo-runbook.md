# BUP Fuel Supply Operations — Operator & Live Demo Runbook

## 1. Local Deployment Modes

### Core Mode (Simulator + Backend + Dashboard; No ML Weights Required, `<15 ms` Cached Planning)
```powershell
docker compose up -d --build --wait
# Dashboard: http://127.0.0.1:3000
# Backend API / OpenAPI: http://127.0.0.1:8001/docs
# Official Simulator: http://127.0.0.1:8000/docs
```

### Full ML + Monitoring Mode (Offline `amazon/chronos-2` + `IsolationForest` + Prometheus)
On a fresh checkout, download/verify weights and train the locked `IsolationForest` artifact once:
```powershell
python -u scripts/prepare-models.py
docker compose -f compose.yaml -f compose.ml.yaml --profile ml --profile monitoring up -d --build --wait
# Prometheus UI: http://127.0.0.1:9090
```

> **Isolated Disposable Demo Stack (Safe Alongside Proof/Operator Stacks):** Pass `-p bup-demo` and custom ports `DASHBOARD_PORT=13000 SNAPSHOT_PORT=18001 SIMULATOR_PORT=18000 PROMETHEUS_PORT=19090` in environment variables so demo steps never disturb an existing operator stack.

> **Optional Operator Auth:** Set `OPERATOR_AUTH_MODE=required`, `OPERATOR_USERNAME=operator`, `OPERATOR_PASSWORD_HASH=pbkdf2_sha256$...` (or `OPERATOR_TOKEN_SHA256`), and `SESSION_SECRET` before starting Compose to enforce PBKDF2 session / Bearer authentication on all write endpoints.

---

## 2. Deterministic 3–5 Minute Repeatable Judge Demo

1. **Seed Clean State & 36 Ticks (Reset + Warm History):**
   ```powershell
   Invoke-RestMethod -Method Post http://127.0.0.1:8000/admin/reset -ContentType 'application/json' -Body '{"seed":12345}'
   Invoke-RestMethod -Method Post http://127.0.0.1:8000/admin/step -ContentType 'application/json' -Body '{"ticks":36}'
   ```
   - Open `http://127.0.0.1:3000` -> **`Overview`** and **`Models & Eval`**: confirm green component badges (`ml_service: ok`, `Chronos-2: loaded, IForest: loaded` when ML profile is enabled), matching `Gen ...` ID, live `sim_time` + `Observed` provenance strip, and curated benchmark tables.
2. **Demand Forecast Comparison (`Forecasts` tab):**
   - Select `station-mirpur · DIESEL`, switch `seasonal` baseline vs `CHRONOS2` candidate, and show the `p10–p90` uncertainty band over horizon `96` ticks alongside `seasonal-v2.0` vs `amazon/chronos-2` comparison in `Models & Eval`.
3. **Inject Combined Crisis & Inspect Network Rerouting:**
   ```powershell
   $t = (Invoke-RestMethod http://127.0.0.1:8000/v1/instance).tick
   Invoke-RestMethod -Method Post http://127.0.0.1:8000/admin/events -ContentType 'application/json' -Body ('{"type":"demand_spike","start_tick":' + $t + ',"duration_ticks":16,"parameters":{"station_ids":["station-mirpur"],"multiplier":1.8}}')
   Invoke-RestMethod -Method Post http://127.0.0.1:8000/admin/events -ContentType 'application/json' -Body ('{"type":"route_disruption","start_tick":' + $t + ',"duration_ticks":12,"parameters":{"route_ids":["route-gazipur-mirpur"]}}')
   Invoke-RestMethod -Method Post http://127.0.0.1:8000/admin/step -ContentType 'application/json' -Body '{"ticks":1}'
   ```
   - Switch to **`Network`**: show `route-gazipur-mirpur` dashed red (`DISRUPTED`) and `station-mirpur` highlighted.
   - Switch to **`Shortages & Incidents`**: show `DEMAND_SPIKE` (`HYBRID` with ML score when ML active) and `ROUTE_DISRUPTED`.
4. **Joint Batch Plan Review, Execution & Replay-Safe Idempotency (`Optimizer & What-If`):**
   - Inspect `unmet_after_liters` vs `No Action` and `Safe Reorder Baseline`, plus active constraints showing shipments shifted off disrupted `route-gazipur-mirpur` onto `route-patiya-mirpur`.
   - Click **`Review joint batch`** -> inspect the line-item table (`Depot -> Destination`, `Route`, `Fuel`, `Quantity`, `ETA`) -> click **`Confirm & Submit Batch`** (`PUT /api/plans/{plan_id}/approval`).
   - Show all confirmed shipment `#id · PENDING` badges. **Refresh the browser** and step 1 tick (`{"ticks":1}`): confirm the restored plan banner and clicking **`Confirm & Submit Batch`** / **`Reconcile`** reports **`All N confirmed (replay safe)`** with zero duplicate simulator shipments!
   - Optionally open **`Allocations`** -> **`Cancel`** one `PENDING` shipment to prove depot inventory refund.
5. **Graceful ML Degradation & Reset Isolation:**
   - Stop `ml-service` (`docker compose stop ml-service`): show `/api/health` marks `ml_service: degraded` while planning continues seamlessly on `SEASONAL` + `OR_TOOLS_SEASONAL`.
   - Reset simulator (`POST /admin/reset`): confirm backend mints a new `generation_id` and blocks stale pre-reset intent/plan execution with `409 Conflict`.
