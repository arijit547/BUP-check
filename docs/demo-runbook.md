# Live demo runbook

Start a fresh demo stack so tick advancement and fault injection are isolated from the operator stack. Use Docker Compose on the demo machine, then open the dashboard at `http://localhost:43000`.

```powershell
$env:SIMULATOR_PORT = '48000'
$env:SNAPSHOT_PORT = '48001'
$env:DASHBOARD_PORT = '43000'
$env:SIMULATOR_START_MODE = 'paused'
$env:TICK_MINUTES = '15'
docker compose -p bup-demo up --build --wait
Invoke-RestMethod http://localhost:48000/v1/health
```

1. Show live inventory and paused tick 0. The forecast panel displays the history requirement before a projection is available.
2. Seed demand by advancing 42 ticks, then show the leading recommendation and its expected impact.

   ```powershell
   $sim = 'http://localhost:48000'
   $api = 'http://localhost:48001'
   1..42 | ForEach-Object { Invoke-RestMethod "$sim/admin/step" -Method Post | Out-Null }
   $plan = Invoke-RestMethod "$api/api/planning"
   $plan.recommendations[0]
   ```

3. Disrupt the leading route and step once. After the dashboard refreshes, show that the affected route has dropped out. Review the next recommendation’s destination, fuel, quantity, ETA, and estimated unmet demand. Approve it and show the official allocation ID.

   ```powershell
   $event = @{
       type = 'route_disruption'
       start_tick = $plan.tick
       duration_ticks = 12
       parameters = @{ route_ids = @($plan.recommendations[0].route_id) }
   } | ConvertTo-Json -Depth 4
   Invoke-RestMethod "$sim/admin/events" -Method Post -ContentType 'application/json' -Body $event
   Invoke-RestMethod "$sim/admin/step" -Method Post
   ```

4. Step once to show the shipment in transit. Step through the reviewed ETA and show arrival and inventory updates. Simulator ticks advance immediately, while the dashboard polls every 10 seconds.
5. Inject stale data. Show the last validated timestamp and disabled approval buttons, then clear the fault and show approvals becoming available.

   ```powershell
   $fault = @{ type = 'stale_data'; duration_seconds = 90 } | ConvertTo-Json
   Invoke-RestMethod "$sim/admin/faults" -Method Post -ContentType 'application/json' -Body $fault
   # Wait for the stale state to appear in the browser before clearing.
   Invoke-RestMethod "$sim/admin/faults/clear" -Method Post
   ```

6. Show the deployment and load evidence, then read the simulator’s service metrics and recent stack logs.

   ```powershell
   Invoke-RestMethod "$sim/v1/metrics"
   docker compose -p bup-demo ps
   docker compose -p bup-demo logs --tail 30
   ```

Remove the disposable demo project when finished with `docker compose -p bup-demo down --volumes`.
