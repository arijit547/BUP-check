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

> **Optional Operator Auth:** Set `OPERATOR_AUTH_MODE=required`, `OPERATOR_USERNAME=operator`, `OPERATOR_PASSWORD_HASH=pbkdf2_sha256$...` (or `OPERATOR_TOKEN_SHA256`), and `SESSION_SECRET` before starting Compose to enforce PBKDF2 session / Bearer authentication on all write endpoints.

---

## 2. Deterministic 3–5 Minute Repeatable Judge Demo

Start a disposable project for the reset, crisis, and dispatch in this demo. For the optional ML profile, use the same `-p bup-demo` and port settings with the full ML command above.

```powershell
$env:SIMULATOR_START_MODE = 'paused'
$env:SIMULATOR_PORT = '18000'
$env:SNAPSHOT_PORT = '18001'
$env:DASHBOARD_PORT = '13000'
$env:PROMETHEUS_PORT = '19090'
docker compose -p bup-demo up -d --build --wait
$simulator = "http://127.0.0.1:$env:SIMULATOR_PORT"
$dashboard = "http://127.0.0.1:$env:DASHBOARD_PORT"
```

1. **Seed Clean State & 36 Ticks (Reset + Warm History):**
   ```powershell
   Invoke-RestMethod -Method Post "$simulator/admin/reset"
   for ($i = 0; $i -lt 36; $i++) {
     Invoke-RestMethod -Method Post "$simulator/admin/step" | Out-Null
   }
   (Invoke-RestMethod "$simulator/v1/instance").tick  # 36
   ```
   - Open `$dashboard` and check **Overview** and **Models & Eval** for simulator health, generation, and snapshot provenance. With ML enabled, confirm that Chronos-2 and Isolation Forest are loaded.
2. **Demand Forecast Comparison (`Forecasts` tab):**
   - Select `station-mirpur · DIESEL`, switch `seasonal` baseline vs `CHRONOS2` candidate, and show the `p10–p90` uncertainty band over horizon `96` ticks alongside `seasonal-v2.0` vs `amazon/chronos-2` comparison in `Models & Eval`.
3. **Inject Combined Crisis & Inspect Network Rerouting:**
   ```powershell
   $t = (Invoke-RestMethod "$simulator/v1/instance").tick
   Invoke-RestMethod -Method Post "$simulator/admin/events" -ContentType 'application/json' -Body ('{"type":"demand_spike","start_tick":' + $t + ',"duration_ticks":16,"parameters":{"station_ids":["station-mirpur"],"multiplier":1.8}}')
   Invoke-RestMethod -Method Post "$simulator/admin/events" -ContentType 'application/json' -Body ('{"type":"route_disruption","start_tick":' + $t + ',"duration_ticks":12,"parameters":{"route_ids":["route-gazipur-mirpur"]}}')
   Invoke-RestMethod -Method Post "$simulator/admin/step"
   ```
   - Switch to **`Network`**: show `route-gazipur-mirpur` dashed red (`DISRUPTED`) and `station-mirpur` highlighted.
   - Switch to **`Shortages & Incidents`**: show `DEMAND_SPIKE` (`HYBRID` with ML score when ML active) and `ROUTE_DISRUPTED`.
4. **Joint Batch Plan Review, Execution & Replay-Safe Idempotency (`Optimizer & What-If`):**
   - Inspect `unmet_after_liters` vs `No Action` and `Safe Reorder Baseline`, plus active constraints showing shipments shifted off disrupted `route-gazipur-mirpur` onto `route-patiya-mirpur`.
   - Click **`Review joint batch`** -> inspect the line-item table (`Depot -> Destination`, `Route`, `Fuel`, `Quantity`, `ETA`) -> click **`Confirm & Submit Batch`** (`PUT /api/plans/{plan_id}/approval`).
   - Record the simulator IDs from the `#id · PENDING` line items. Reload the browser, return to **Optimizer & What-If**, and run `Invoke-RestMethod -Method Post "$simulator/admin/step"`. Click **All N confirmed (replay safe)** and verify the same IDs and count at `$simulator/v1/allocations`.
   - Advance the simulator through the displayed ETA and watch the same line items progress from `PENDING` to `IN_TRANSIT` and `ARRIVED`.
5. **Graceful ML Degradation & Reset Isolation:**
   - With ML enabled, stop `ml-service` using `docker compose -p bup-demo -f compose.yaml -f compose.ml.yaml --profile ml stop ml-service`. Check `$dashboard/api/health` for `ml_service: degraded` and confirm that planning continues with `SEASONAL` and `OR_TOOLS_SEASONAL`.
   - Reset with `Invoke-RestMethod -Method Post "$simulator/admin/reset"`. Confirm that the backend mints a new `generation_id` and blocks stale pre-reset intent or plan execution with `409 Conflict`.
