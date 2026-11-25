import asyncio
from contextlib import asynccontextmanager, closing, suppress
import logging
import os
import sqlite3
import time
from urllib.parse import urlencode
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError

from approvals import (
    SHIPMENTS,
    ApprovalAttempt,
    ApprovalLedger,
    ApprovalStore,
    Proposal,
    Shipment,
    matches,
    submit,
    upstream_problem,
)
from observability import (
    ComponentHealth,
    HealthReport,
    MetricsReport,
    RequestMetrics,
    SSEState,
    utc_now,
)
from planning import (
    FUELS,
    Allocation,
    DemandObservation,
    PlanningReport,
    Region,
    SimulatorEvent,
    SimulatorMetrics,
    StationDetail,
    SupplyArrival,
    build_report,
)
from schemas import Depot, Route, Simulation, Snapshot, SnapshotError, Station

logger = logging.getLogger(__name__)

INSTANCE = TypeAdapter(Simulation)
DEPOTS = TypeAdapter(list[Depot])
STATIONS = TypeAdapter(list[Station])
STATION_DETAILS = TypeAdapter(list[StationDetail])
ROUTES = TypeAdapter(list[Route])
REGIONS = TypeAdapter(list[Region])
SUPPLY_ARRIVALS = TypeAdapter(list[SupplyArrival])
EVENTS = TypeAdapter(list[SimulatorEvent])
SIMULATOR_METRICS = TypeAdapter(SimulatorMetrics)
HISTORY = TypeAdapter(list[DemandObservation])
ALLOCATIONS = TypeAdapter(list[Allocation])

PLANNING_CACHE_TTL_SECONDS = 2.0
UPSTREAM_ERRORS = {
    502: {"model": SnapshotError, "description": "Simulator response failed schema validation."},
    503: {
        "model": SnapshotError,
        "description": "Simulator is unavailable, reports stale data, or changed during every read.",
    },
}


def clear_planning_cache(app: FastAPI) -> None:
    cache = getattr(app.state, "planning_cache", None)
    if cache is not None:
        cache.clear()
    app.state.planning_epoch = getattr(app.state, "planning_epoch", 0) + 1


async def sse_watcher(app: FastAPI, base_url: str) -> None:
    """Listen to simulator SSE notifications and invalidate cached planning state."""
    sse_state: SSEState = app.state.sse_state
    attempt = 0
    timeout = httpx.Timeout(connect=3.0, read=25.0, write=3.0, pool=3.0)
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as stream_client:
        while True:
            try:
                sse_state.status = "connecting" if attempt == 0 else "degraded"
                async with stream_client.stream("GET", "/v1/stream") as resp:
                    if resp.status_code == 503:
                        sse_state.status = "degraded"
                        sse_state.last_error = "SSE stream fault injected (503)"
                        raise httpx.HTTPStatusError("SSE 503", request=resp.request, response=resp)
                    resp.raise_for_status()
                    sse_state.status = "connected"
                    sse_state.last_error = None
                    attempt = 0
                    current_event: str | None = None
                    async for line in resp.aiter_lines():
                        if not line:
                            if current_event is not None:
                                sse_state.events_received += 1
                                sse_state.last_event_type = current_event
                                sse_state.last_event_at = utc_now()
                                if current_event in (
                                    "simulation.tick",
                                    "allocation.status_changed",
                                    "inventory.updated",
                                    "simulator.notice",
                                ):
                                    clear_planning_cache(app)
                                current_event = None
                        elif line.startswith("event:"):
                            current_event = line.split(":", 1)[1].strip()
            except asyncio.CancelledError:
                sse_state.status = "disconnected"
                raise
            except (httpx.HTTPError, OSError) as exc:
                sse_state.status = "degraded"
                sse_state.reconnects += 1
                sse_state.last_error = str(exc)
                attempt += 1
                await asyncio.sleep(min(5.0, 0.5 * (2 ** min(attempt, 4))))


@asynccontextmanager
async def lifespan(app: FastAPI):
    base_url = os.environ.get("SIMULATOR_BASE_URL", "http://localhost:8000")
    async with httpx.AsyncClient(base_url=base_url, timeout=3.0) as client:
        app.state.simulator = client
        app.state.approval_store = ApprovalStore(os.environ.get("APPROVAL_DB", "data/approvals.sqlite3"))
        app.state.approval_lock = asyncio.Lock()
        app.state.metrics = RequestMetrics()
        app.state.sse_state = SSEState()
        app.state.planning_cache = {}
        app.state.planning_epoch = 0
        app.state.planning_inflight = {}
        watcher = None
        if os.environ.get("DISABLE_SSE_WATCHER") != "1":
            watcher = asyncio.create_task(sse_watcher(app, base_url))
        try:
            yield
        finally:
            if watcher is not None:
                watcher.cancel()
                with suppress(asyncio.CancelledError):
                    await watcher


app = FastAPI(title="BUP Fuel Operations Snapshot API", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def record_metrics_middleware(request: Request, call_next):
    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        if request.url.path.startswith("/api/"):
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            metrics = getattr(request.app.state, "metrics", None)
            if metrics is not None:
                metrics.record_request(time.monotonic(), request.url.path, status_code, elapsed_ms)


async def read_raw(client: httpx.AsyncClient, path: str) -> bytes:
    response = await client.get(path)
    response.raise_for_status()
    if response.headers.get("X-Simulator-Stale", "").lower() == "true":
        raise HTTPException(503, "Simulator marked data stale")
    return response.content


async def read[T](client: httpx.AsyncClient, path: str, schema: TypeAdapter[T]) -> T:
    return schema.validate_json(await read_raw(client, path))


async def read_snapshot(client: httpx.AsyncClient, simulation: Simulation) -> Snapshot:
    depots, stations, routes = await asyncio.gather(
        read(client, "/v1/depots", DEPOTS),
        read(client, "/v1/stations", STATIONS),
        read(client, "/v1/routes", ROUTES),
    )
    return Snapshot(simulation=simulation, depots=depots, stations=stations, routes=routes)


async def read_approval_state(client: httpx.AsyncClient) -> tuple[Snapshot, list[Shipment]]:
    for _ in range(3):
        before = await read(client, "/v1/instance", INSTANCE)
        snapshot, shipments = await asyncio.gather(
            read_snapshot(client, before),
            read(client, "/v1/allocations", SHIPMENTS),
        )
        if before == await read(client, "/v1/instance", INSTANCE):
            return snapshot, shipments
    raise HTTPException(503, "Simulation changed during approval. Refresh and try again.")


@app.get(
    "/api/snapshot",
    operation_id="getSnapshot",
    response_model=Snapshot,
    description=(
        "Reads the official simulator's current tick, inventory in liters, and routes. "
        "Retries a read if the simulator changes during collection."
    ),
    responses={
        200: {"headers": {"Cache-Control": {"schema": {"type": "string", "const": "no-store"}}}},
        **UPSTREAM_ERRORS,
    },
)
async def get_snapshot(request: Request, response: Response) -> Snapshot:
    client = request.app.state.simulator
    try:
        for _ in range(3):
            before = await read(client, "/v1/instance", INSTANCE)
            snapshot = await read_snapshot(client, before)
            after = await read(client, "/v1/instance", INSTANCE)
            if before == after:
                response.headers["Cache-Control"] = "no-store"
                return snapshot
    except HTTPException:
        clear_planning_cache(request.app)
        raise
    except httpx.HTTPError as exc:
        clear_planning_cache(request.app)
        raise HTTPException(503, "Simulator request failed") from exc
    except ValidationError as exc:
        raise HTTPException(502, "Simulator returned invalid data") from exc
    raise HTTPException(503, "Simulation changed while collecting snapshot")


async def _build_planning_report(app_instance: FastAPI, history_ticks: int, horizon_ticks: int) -> PlanningReport:
    client: httpx.AsyncClient = app_instance.state.simulator
    cache: dict[tuple[int, int], tuple[float, int, str, PlanningReport]] = app_instance.state.planning_cache
    key = (history_ticks, horizon_ticks)

    for _ in range(3):
        epoch = getattr(app_instance.state, "planning_epoch", 0)
        before = await read(client, "/v1/instance", INSTANCE)
        cached = cache.get(key)
        if cached is not None:
            cached_at, cached_tick, cached_status, cached_report = cached
            if (
                cached_tick == before.tick
                and cached_status == before.status
                and (time.monotonic() - cached_at) <= PLANNING_CACHE_TTL_SECONDS
            ):
                return cached_report

        depots_raw, stations_raw, routes_raw, regions, allocations, supply_arrivals, events, sim_metrics = (
            await asyncio.gather(
                read(client, "/v1/depots", DEPOTS),
                read_raw(client, "/v1/stations"),
                read(client, "/v1/routes", ROUTES),
                read(client, "/v1/regions", REGIONS),
                read(client, "/v1/allocations", ALLOCATIONS),
                read(client, "/v1/supply-arrivals", SUPPLY_ARRIVALS),
                read(client, "/v1/events", EVENTS),
                read(client, "/v1/metrics", SIMULATOR_METRICS),
            )
        )
        stations = STATIONS.validate_json(stations_raw)
        station_details = STATION_DETAILS.validate_json(stations_raw)
        snapshot = Snapshot(simulation=before, depots=depots_raw, stations=stations, routes=routes_raw)

        history_paths = [
            "/v1/demand-history?" + urlencode({"station_id": station.id, "limit": history_ticks * len(FUELS)})
            for station in snapshot.stations
        ]
        history_batches = await asyncio.gather(*(read(client, path, HISTORY) for path in history_paths))
        after = await read(client, "/v1/instance", INSTANCE)
        if before == after:
            history = [entry for batch in history_batches for entry in batch]
            report = build_report(
                snapshot,
                history,
                allocations,
                history_ticks,
                horizon_ticks,
                station_details=station_details,
                regions=regions,
                supply_arrivals=supply_arrivals,
                events=events,
                simulator_metrics=sim_metrics,
            )
            if getattr(app_instance.state, "planning_epoch", 0) == epoch:
                cache[key] = (time.monotonic(), before.tick, before.status, report)
            app_instance.state.metrics.record_intelligence(report.fallback_active, len(report.anomalies))
            return report
    raise HTTPException(503, "Simulation changed while collecting snapshot")


@app.get(
    "/api/planning",
    operation_id="getPlanningReport",
    response_model=PlanningReport,
    description=(
        "Projects station demand from recent history with seasonality/event awareness, detects anomalies "
        "and bottlenecks, activates safety reorder fallback when history is incomplete, and ranks "
        "standalone shipment alternatives by urgency-weighted unmet demand avoided."
    ),
    responses=UPSTREAM_ERRORS,
)
async def get_planning_report(
    request: Request,
    response: Response,
    history_ticks: int = Query(8, ge=1, le=96, description="Completed ticks used for the rolling mean."),
    horizon_ticks: int = Query(96, ge=1, le=288, description="Number of ticks to project from the current tick."),
) -> PlanningReport:
    key = (history_ticks, horizon_ticks)
    inflight: dict[tuple[int, int], asyncio.Task[PlanningReport]] = request.app.state.planning_inflight
    task = inflight.get(key)
    owner = task is None or task.done()
    if owner:
        task = asyncio.create_task(_build_planning_report(request.app, history_ticks, horizon_ticks))
        inflight[key] = task

    try:
        report = await asyncio.shield(task)
        response.headers["Cache-Control"] = "no-store"
        return report
    except HTTPException:
        clear_planning_cache(request.app)
        raise
    except httpx.HTTPError as exc:
        clear_planning_cache(request.app)
        raise HTTPException(503, "Simulator request failed") from exc
    except ValidationError as exc:
        raise HTTPException(502, "Simulator returned invalid data") from exc
    finally:
        if owner and inflight.get(key) is task:
            inflight.pop(key, None)


@app.put(
    "/api/approvals/{intent_id}",
    operation_id="submitApproval",
    response_model=ApprovalAttempt,
    description="Approve or retry an exact shipment intent. The response reports its persisted outcome.",
    responses={409: {"description": "The intent ID already belongs to a different shipment payload."}},
)
async def approve(intent_id: UUID, proposal: Proposal, request: Request) -> ApprovalAttempt:
    client = request.app.state.simulator
    store = request.app.state.approval_store
    clear_planning_cache(request.app)
    async with request.app.state.approval_lock:
        attempt = store.get_or_create(intent_id, proposal)
        result = await submit(
            store,
            attempt,
            client,
            lambda: read(client, "/v1/allocations", SHIPMENTS),
            lambda: read_approval_state(client),
        )
        clear_planning_cache(request.app)
        return result


@app.get(
    "/api/approvals",
    operation_id="listApprovals",
    response_model=ApprovalLedger,
    description="Lists durable approval attempts and reconciles simulator shipment statuses when fresh.",
)
async def approvals(request: Request, response: Response) -> ApprovalLedger:
    response.headers["Cache-Control"] = "no-store"
    store = request.app.state.approval_store
    async with request.app.state.approval_lock:
        attempts = store.list()
        try:
            shipments = await read(request.app.state.simulator, "/v1/allocations", SHIPMENTS)
        except (httpx.HTTPError, ValidationError, HTTPException) as exc:
            clear_planning_cache(request.app)
            return ApprovalLedger(attempts=attempts, simulator_error=upstream_problem(exc))

        by_key = {shipment.idempotency_key: shipment for shipment in shipments}
        for index, attempt in enumerate(attempts):
            shipment = by_key.get(attempt.proposal.idempotency_key)
            if shipment is not None and matches(shipment, attempt.proposal):
                if attempt.shipment != shipment or attempt.message is not None:
                    attempts[index] = store.save(attempt, "CONFIRMED", shipment=shipment)
            elif attempt.shipment is not None:
                attempts[index] = attempt.model_copy(
                    update={"message": "Shipment absent from the current simulator ledger. Showing last known status."},
                )
        return ApprovalLedger(attempts=attempts, simulator_error=None)


@app.get(
    "/api/health",
    operation_id="getHealth",
    response_model=HealthReport,
    description="Reports component health for the backend API, SQLite ledger, simulator, SSE stream, and intelligence modules.",
    responses={503: {"model": HealthReport, "description": "One or more critical components are degraded."}},
)
async def get_health(request: Request, response: Response) -> HealthReport | JSONResponse:
    response.headers["Cache-Control"] = "no-store"
    store: ApprovalStore = request.app.state.approval_store
    client: httpx.AsyncClient = request.app.state.simulator
    metrics: RequestMetrics = request.app.state.metrics
    sse_state: SSEState = request.app.state.sse_state

    try:
        with closing(sqlite3.connect(store.path)) as conn:
            conn.execute("SELECT 1").fetchone()
        db_health = ComponentHealth(status="ok", detail="SQLite approval ledger reachable")
    except sqlite3.Error as exc:
        db_health = ComponentHealth(status="error", detail=str(exc))

    started = time.perf_counter()
    try:
        await read(client, "/v1/health", TypeAdapter(dict))
        await read(client, "/v1/instance", INSTANCE)
        sim_latency = round((time.perf_counter() - started) * 1000.0, 2)
        sim_health = ComponentHealth(status="ok", detail="Fresh simulator REST reachable", latency_ms=sim_latency)
    except HTTPException as exc:
        sim_latency = round((time.perf_counter() - started) * 1000.0, 2)
        sim_health = ComponentHealth(status="degraded", detail=exc.detail, latency_ms=sim_latency)
    except (httpx.HTTPError, ValidationError) as exc:
        sim_latency = round((time.perf_counter() - started) * 1000.0, 2)
        sim_health = ComponentHealth(status="error", detail=str(exc), latency_ms=sim_latency)

    sse_component = ComponentHealth(
        status="ok" if sse_state.status == "connected" else "degraded",
        detail=sse_state.last_error or f"SSE {sse_state.status} ({sse_state.events_received} events)",
    )
    snap = metrics.snapshot(time.monotonic())
    components = {
        "backend_api": ComponentHealth(status="ok", detail="FastAPI serving requests"),
        "database": db_health,
        "simulator": sim_health,
        "sse_stream": sse_component,
        "prediction_service": ComponentHealth(
            status="ok" if sim_health.status == "ok" else "degraded",
            detail="Seasonality & spike-aware forecast with reorder fallback",
        ),
        "decision_engine": ComponentHealth(
            status="ok" if sim_health.status == "ok" else "degraded",
            detail="Urgency-weighted constrained allocation optimizer",
        ),
    }
    healthy = db_health.status == "ok" and sim_health.status == "ok"
    report = HealthReport(
        status="ok" if healthy else "degraded",
        timestamp=utc_now(),
        version=app.version,
        components=components,
        p95_latency_ms=snap["p95_latency_ms"],
        error_rate_percent=float(snap["error_rate_percent"] or 0.0),
    )
    if not healthy:
        return JSONResponse(status_code=503, content=report.model_dump(mode="json"), headers={"Cache-Control": "no-store"})
    return report


@app.get(
    "/api/metrics",
    operation_id="getMetrics",
    response_model=MetricsReport,
    description="Returns application request latency percentiles, error rate, intelligence fallback/anomaly counts, SSE counters, and upstream simulator metrics.",
)
async def get_metrics(request: Request, response: Response) -> MetricsReport:
    response.headers["Cache-Control"] = "no-store"
    metrics: RequestMetrics = request.app.state.metrics
    sse_state: SSEState = request.app.state.sse_state
    snap = metrics.snapshot(time.monotonic())

    sim_data: dict[str, float | int] | None = None
    try:
        sim_metrics = await read(request.app.state.simulator, "/v1/metrics", SIMULATOR_METRICS)
        sim_data = sim_metrics.model_dump()
    except (httpx.HTTPError, ValidationError, HTTPException):
        sim_data = None

    return MetricsReport(
        timestamp=utc_now(),
        total_requests=int(snap["total_requests"] or 0),
        error_count=int(snap["error_count"] or 0),
        error_rate_percent=float(snap["error_rate_percent"] or 0.0),
        requests_per_minute=float(snap["requests_per_minute"] or 0.0),
        p50_latency_ms=snap["p50_latency_ms"],
        p95_latency_ms=snap["p95_latency_ms"],
        p99_latency_ms=snap["p99_latency_ms"],
        fallback_activations=int(snap["fallback_activations"] or 0),
        active_anomalies=int(snap["active_anomalies"] or 0),
        sse_status=sse_state.status,
        sse_events_received=sse_state.events_received,
        sse_reconnects=sse_state.reconnects,
        simulator=sim_data,
    )
