# BUP Fuel Operations

Repository: `https://github.com/arijit547/BUP-check`

## Milestone 1

Clone the repository, run Docker with the Linux engine, then start the official simulator and the FastAPI snapshot service.

```powershell
git clone https://github.com/arijit547/BUP-check.git
cd BUP-check
docker compose up --build --wait
Invoke-RestMethod http://localhost:8001/api/snapshot
```

The simulator starts paused by default. Its liveness endpoint is at `http://localhost:8000/v1/health`. Set `SIMULATOR_PORT` or `SNAPSHOT_PORT` before running Compose to change the host ports.

The operator dashboard is available at `http://localhost:3000`. Set `DASHBOARD_PORT` to change its host port. It refreshes the simulator snapshot every 10 seconds.

`GET /api/snapshot` returns the simulator tick and time, depot and station stock and capacity in liters, and routes with their status and transit time. The API reads the simulator's public REST endpoints and checks the instance before and after collection. Upstream data is validated before it reaches clients.

The [OpenAPI contract](docs/openapi.json) is exported from the FastAPI schemas and route definitions. The running app serves the same contract at `/openapi.json`. After an intentional API change, refresh the saved artifact with the running service.

```powershell
Invoke-WebRequest http://localhost:8001/openapi.json -OutFile docs/openapi.json
```

See the [submission checklist](docs/submission-checklist.md) for the acceptance checks for later milestones.

## Milestone 3

The dashboard shows demand forecasts and ranked shipment previews from the read-only planning API.

```powershell
Invoke-RestMethod 'http://localhost:8001/api/planning?history_ticks=8&horizon_ticks=96'
```

- `history_ticks` selects 1–96 completed ticks. The rolling mean uses observed demand in liters for each station and fuel, while projections incorporate hour-of-day demand profiles, active/scheduled `demand_spike` events, confidence scoring, and a reorder safety fallback when history is incomplete.
- `horizon_ticks` selects 1–288 ticks, starting with the current simulator tick. The stockout tick is the first projected tick to end with zero stock during positive demand.
- Projected arrivals are applied before demand. Existing `IN_TRANSIT` and `PENDING` shipments count at their expected arrival ticks.
- Recommendations are standalone previews ranked by urgency-weighted unmet liters avoided, then by total unmet avoided and earliest arrival, and include risk reduction, signals, constraints, and alternative routes.
- Anomaly detection flags demand spikes/drops, stockout risk, disrupted routes, station outages, depot dispatch bottlenecks, and delayed supply arrivals.
- `GET /api/health` and `GET /api/metrics` expose component health (API, SQLite ledger, simulator REST, `/v1/stream` SSE watcher, forecast/decision services), p50/p95/p99 latency, error rate, and simulator metrics.

## Milestone 4

Select **Review** on a recommendation, check the displayed route, fuel and quantity, then select **Approve shipment**. The API checks the proposal against fresh simulator state and returns the recorded outcome. The shipment panel shows the simulator allocation ID and status after confirmation.

- `PUT /api/approvals/{intent_id}` accepts a UUID identifying the operator intent and an exact shipment proposal with `source_depot_id`, `destination_station_id`, `route_id`, `fuel_type`, and `quantity` in liters. Reuse the same UUID and body for retries. A changed payload for the same UUID returns `409`.
- `GET /api/approvals` lists approval attempts and reconciles them with the simulator allocation ledger. The `approvals` Compose volume persists SQLite records across backend restarts. Run the approval API as one worker.
- **Waiting for fresh state** means the proposal needs validation. **Outcome unknown** means a write may have reached the simulator. Retry the stored approval to reconcile the result. A retry reuses the original simulator key and payload.
- A stale-data response or simulator outage pauses new approvals. The open dashboard shows when the displayed snapshot and forecast were last validated. The simulator must preserve its allocation ledger for safe replay across application restarts. A simulator reset clears that ledger, so use a fresh approval volume for a new simulator scenario.

## Milestone 5

Use Node.js 24 and Docker Compose with support for the `!reset` override directive. The simulator and base images are pinned by digest. The frontend installs from its npm lockfile, and the backend pins its Python dependencies.

```powershell
npm ci --prefix frontend
npm run build --prefix frontend
npm exec --prefix frontend -- playwright install chromium
node scripts/verify.mjs e2e
node scripts/verify.mjs benchmark
node scripts/verify.mjs forecast-policy
node scripts/verify.mjs reorder-policy
```

Each verification command starts a new Compose project with random loopback ports and a new approval volume. It writes logs, health checks, and results into `artifacts/<mode>/` and removes the temporary stack. The benchmark defaults to 2 concurrent clients for 15 seconds. Set `BUP_BENCH_CONCURRENCY` or `BUP_BENCH_SECONDS` to run a different workload. To use a locally installed Chromium browser for the end-to-end test, set `BUP_CHROMIUM_PATH` to the executable path.

### Run a tagged release

CI runs on branch pushes and pull requests. Pushing a `v*` tag triggers the same checks, then publishes both images to GHCR and smoke-tests them in a fresh Compose project. Use your lowercase GitHub owner and repository in the image prefix. Authenticate with GHCR before pulling private packages.

```powershell
$env:BUP_IMAGE_PREFIX = 'ghcr.io/arijit547/bup-check'
$env:BUP_RELEASE_TAG = 'v1.0.0'
docker compose -f compose.yaml -f compose.release.yaml up --no-build --pull always --wait
```

Open the dashboard at `http://localhost:3000`. Use `node scripts/verify.mjs release` for an isolated smoke test with those same images. See the [submission evidence](docs/submission-evidence.md) and [live demo runbook](docs/demo-runbook.md).
