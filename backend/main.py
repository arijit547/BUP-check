import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
import json
import logging
import math
import os
from pathlib import Path
import random
import sqlite3
import time
from typing import Any, Literal
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx
from fastapi import Body, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError

from approvals import (
    SHIPMENTS,
    ApprovalAttempt,
    ApprovalLedger,
    ApprovalStore,
    LatestPlanResponse,
    PlanApprovalRequest,
    PlanApprovalResult,
    Proposal,
    Shipment,
    cancel_pending,
    execute_plan_approval,
    matches,
    reconcile_plan_approval_view,
    submit,
    upstream_problem,
)
from intelligence.collector import hour_factor, index_observations_by_series, regularized_series
from intelligence.contracts import (
    FUELS,
    Allocation,
    AnomaliesResponse,
    DemandObservation,
    ForecastPoint,
    ForecastsResponse,
    Fuel,
    GenerationInfo,
    OptimizeRequest,
    PlanPreviewSummary,
    PlanningReport,
    Region,
    ShortagesResponse,
    SimulateRequest,
    SimulateResponse,
    SimulatorEvent,
    SimulatorMetrics,
    StationDetail,
    SupplyArrival,
)
from intelligence.evaluation import load_evaluation_summary
from intelligence.features import spike_multiplier_at
from intelligence.optimizer import simulate_joint_impact, validate_plan_independently
from observability import (
    ComponentHealth,
    HealthReport,
    MetricsReport,
    RequestMetrics,
    SSEState,
    utc_now,
)
from planning import build_forecasts_response, build_report, build_shortages_response
from schemas import Depot, Route, Simulation, Snapshot, SnapshotError, Station
from security import AuthStatus, LoginRequest, OperatorAuth
from storage import PlatformStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("bup.api")

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

PLANNING_CACHE_TTL_SECONDS = float(os.environ.get("PLANNING_CACHE_TTL_SECONDS", "2.0"))
RUNNING_COHERENT_GRACE_SECONDS = float(os.environ.get("RUNNING_COHERENT_GRACE_SECONDS", "15.0"))
MAX_CACHE_ENTRIES = 64
CIRCUIT_THRESHOLD = 5
CIRCUIT_COOLDOWN_SECONDS = 3.0

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
    fc_cache = getattr(app.state, "forecast_cache", None)
    if fc_cache is not None:
        fc_cache.clear()
    app.state.planning_epoch = getattr(app.state, "planning_epoch", 0) + 1


def clear_coherent_planning(app: FastAPI) -> None:
    coherent = getattr(app.state, "last_coherent_planning", None)
    if coherent is not None:
        coherent.clear()
    app.state.last_coherent_snapshot = None


def _circuit_allow(app: FastAPI) -> None:
    now = time.monotonic()
    opened_until = getattr(app.state, "circuit_open_until", 0.0)
    if opened_until > now:
        app.state.circuit_state = "OPEN"
        raise HTTPException(503, "Simulator circuit breaker open after repeated failures")
    if opened_until > 0:
        app.state.circuit_state = "HALF_OPEN"


def _circuit_success(app: FastAPI) -> None:
    app.state.circuit_failures = 0
    app.state.circuit_open_until = 0.0
    app.state.circuit_state = "CLOSED"
    metrics = getattr(app.state, "metrics", None)
    if metrics is not None:
        metrics.mark_recovered(time.monotonic())


def _circuit_failure(app: FastAPI, *, is_stale: bool = False) -> None:
    metrics = getattr(app.state, "metrics", None)
    if metrics is not None:
        metrics.mark_degraded(time.monotonic())
    if is_stale:
        return
    failures = getattr(app.state, "circuit_failures", 0) + 1
    app.state.circuit_failures = failures
    if failures >= CIRCUIT_THRESHOLD:
        app.state.circuit_open_until = time.monotonic() + CIRCUIT_COOLDOWN_SECONDS
        app.state.circuit_state = "OPEN"


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
                    clear_planning_cache(app)
                    current_event: str | None = None
                    current_data: str = ""
                    async for line in resp.aiter_lines():
                        if not line:
                            if current_event is not None:
                                sse_state.events_received += 1
                                sse_state.last_event_type = current_event
                                sse_state.last_event_at = utc_now()
                                if current_event == "simulator.notice" and "reset" in current_data.lower():
                                    app.state.reset_noticed = True
                                    clear_planning_cache(app)
                                    clear_coherent_planning(app)
                                elif current_event in (
                                    "simulation.tick",
                                    "allocation.status_changed",
                                    "inventory.updated",
                                    "simulator.notice",
                                ):
                                    clear_planning_cache(app)
                                current_event = None
                                current_data = ""
                        elif line.startswith("event:"):
                            current_event = line.split(":", 1)[1].strip()
                        elif line.startswith("data:"):
                            current_data += line[5:].strip()
            except asyncio.CancelledError:
                sse_state.status = "disconnected"
                raise
            except (httpx.HTTPError, OSError) as exc:
                sse_state.status = "degraded"
                sse_state.reconnects += 1
                sse_state.last_error = str(exc)
                attempt += 1
                jitter = random.uniform(0.05, 0.25)
                await asyncio.sleep(min(5.0, 0.5 * (2 ** min(attempt, 4))) + jitter)


@asynccontextmanager
async def lifespan(app: FastAPI):
    base_url = os.environ.get("SIMULATOR_BASE_URL", "http://localhost:8000")
    ml_url = os.environ.get("ML_SERVICE_URL", "").strip()
    sim_timeout = float(os.environ.get("SIMULATOR_TIMEOUT_SECONDS", "3.0"))
    ml_timeout = float(os.environ.get("ML_FORECAST_TIMEOUT_SECONDS", "8.0"))
    db_path = os.environ.get("APPROVAL_DB", "data/approvals.sqlite3")

    async with (
        httpx.AsyncClient(base_url=base_url, timeout=sim_timeout) as client,
        httpx.AsyncClient(base_url=ml_url, timeout=ml_timeout) if ml_url else _null_async_ctx() as ml_client,
    ):
        app.state.simulator = client
        app.state.ml_client = ml_client
        app.state.ml_url = ml_url or None
        app.state.store = PlatformStore(db_path)
        app.state.approval_store = ApprovalStore(db_path)
        app.state.approval_lock = asyncio.Lock()
        app.state.plan_locks = {}
        app.state.metrics = RequestMetrics()
        app.state.sse_state = SSEState()
        app.state.auth = OperatorAuth()
        app.state.planning_cache = {}
        app.state.last_coherent_planning = {}
        app.state.last_coherent_snapshot = None
        app.state.forecast_cache = {}
        app.state.planning_epoch = 0
        app.state.planning_inflight = {}
        app.state.circuit_failures = 0
        app.state.circuit_open_until = 0.0
        app.state.circuit_state = "CLOSED"
        app.state.reset_noticed = False
        app.state.generation = app.state.store.get_active_generation()
        app.state.last_ml_error = None
        app.state.last_ml_latency_ms = None
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


@asynccontextmanager
async def _null_async_ctx():
    yield None


app = FastAPI(title="BUP Fuel Operations Snapshot API", version="2.0.0", lifespan=lifespan)

allowed_origins = [
    item.strip()
    for item in os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")
    if item.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Content-Type", "Authorization"],
)


@app.middleware("http")
async def observability_and_security_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid4().hex[:12]
    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers["X-Request-Id"] = request_id
        return response
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if request.url.path.startswith("/api/"):
            metrics = getattr(request.app.state, "metrics", None)
            if metrics is not None:
                metrics.record_request(time.monotonic(), request.url.path, status_code, elapsed_ms)
            gen = getattr(request.app.state, "generation", None)
            logger.info(json.dumps({
                "event": "api_request",
                "request_id": request_id,
                "generation_id": gen.generation_id if gen else None,
                "path": request.url.path,
                "method": request.method,
                "status": status_code,
                "duration_ms": round(elapsed_ms, 2),
            }))


async def read_raw(client: httpx.AsyncClient, path: str, *, retries: int = 1) -> bytes:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = await client.get(path)
            if response.status_code == 503 and attempt < retries:
                await asyncio.sleep(0.08 * (2 ** attempt) + random.uniform(0.01, 0.04))
                continue
            response.raise_for_status()
            if response.headers.get("X-Simulator-Stale", "").lower() == "true":
                raise HTTPException(503, "Simulator marked data stale")
            return response.content
        except HTTPException:
            raise
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            if 400 <= exc.response.status_code < 500:
                raise
            if attempt < retries:
                await asyncio.sleep(0.08 * (2 ** attempt) + random.uniform(0.01, 0.04))
                continue
            raise
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt < retries:
                await asyncio.sleep(0.08 * (2 ** attempt) + random.uniform(0.01, 0.04))
                continue
            raise
    assert last_exc is not None
    raise last_exc


async def read[T](client: httpx.AsyncClient, path: str, schema: TypeAdapter[T], *, retries: int = 1) -> T:
    return schema.validate_json(await read_raw(client, path, retries=retries))


def _ensure_generation(app_instance: FastAPI, sim: Simulation, sim_allocations: list[Allocation] | None = None) -> GenerationInfo:
    store: PlatformStore = app_instance.state.store
    gen: GenerationInfo | None = getattr(app_instance.state, "generation", None) or store.get_active_generation()
    reset_noticed = bool(getattr(app_instance.state, "reset_noticed", False))

    reason = None
    if gen is None:
        reason = "initial"
    elif reset_noticed:
        reason = "sse_reset_notice"
    elif sim.tick < gen.last_seen_tick:
        reason = f"tick_regression:{gen.last_seen_tick}->{sim.tick}"
    elif sim.sim_time < gen.last_seen_sim_time:
        reason = "sim_time_regression"
    elif sim_allocations is not None:
        confirmed = [
            a for a in app_instance.state.approval_store.list(generation_id=gen.generation_id, limit=25)
            if a.state == "CONFIRMED" and a.shipment is not None
        ]
        if confirmed and len(sim_allocations) == 0:
            reason = "ledger_cleared_after_reset"
        elif confirmed:
            sim_keys = {a.idempotency_key for a in sim_allocations if a.idempotency_key}
            if any(a.proposal.idempotency_key not in sim_keys for a in confirmed):
                reason = "confirmed_intent_missing_in_simulator"

    if reason:
        gen = store.create_generation(
            sim_instance_id=sim.id,
            scenario_id=sim.scenario_id or "baseline",
            scenario_version=sim.scenario_version or "1.0",
            seed=sim.seed if sim.seed is not None else 12345,
            tick=sim.tick,
            sim_time=sim.sim_time,
            reason=reason,
        )
        app_instance.state.reset_noticed = False
        if reason != "initial":
            clear_planning_cache(app_instance)
    else:
        store.touch_generation(gen.generation_id, sim.tick, sim.sim_time)
    app_instance.state.generation = gen
    return gen


async def read_snapshot(client: httpx.AsyncClient, simulation: Simulation) -> Snapshot:
    depots, stations, routes = await asyncio.gather(
        read(client, "/v1/depots", DEPOTS),
        read(client, "/v1/stations", STATIONS),
        read(client, "/v1/routes", ROUTES),
    )
    return Snapshot(simulation=simulation, depots=depots, stations=stations, routes=routes)


async def read_approval_state(client: httpx.AsyncClient) -> tuple[Snapshot, list[Shipment]]:
    for _ in range(3):
        before = await read(client, "/v1/instance", INSTANCE, retries=0)
        snapshot, shipments = await asyncio.gather(
            read_snapshot(client, before),
            read(client, "/v1/allocations", SHIPMENTS, retries=0),
        )
        if before == await read(client, "/v1/instance", INSTANCE, retries=0):
            return snapshot, shipments
    raise HTTPException(503, "Simulation changed during approval. Refresh and try again.")


@app.post("/api/auth/login", operation_id="operatorLogin", response_model=AuthStatus)
async def login(body: LoginRequest, request: Request, response: Response) -> AuthStatus:
    auth: OperatorAuth = request.app.state.auth
    if not auth.auth_required:
        return AuthStatus(auth_required=False, authenticated=True, username="local-operator", role="operator")
    if not auth.verify_password(body.username, body.password):
        raise HTTPException(401, "Invalid operator credentials.")
    auth.set_cookie(response, body.username)
    return AuthStatus(auth_required=True, authenticated=True, username=body.username, role="operator")


@app.post("/api/auth/logout", operation_id="operatorLogout", response_model=AuthStatus)
async def logout(request: Request, response: Response) -> AuthStatus:
    auth: OperatorAuth = request.app.state.auth
    response.delete_cookie(auth.cookie_name, path="/")
    return AuthStatus(auth_required=auth.auth_required, authenticated=not auth.auth_required, username=None, role=None)


@app.get("/api/auth/session", operation_id="authSession", response_model=AuthStatus)
async def auth_session(request: Request) -> AuthStatus:
    return request.app.state.auth.status(request)


@app.post("/api/generations/rebind", operation_id="rebindGeneration", response_model=GenerationInfo)
async def rebind_generation(request: Request) -> GenerationInfo:
    request.app.state.auth.require_operator(request)
    sim_client = request.app.state.simulator
    before = await read(sim_client, "/v1/instance", INSTANCE)
    allocs = await read(sim_client, "/v1/allocations", ALLOCATIONS)
    gen = request.app.state.store.create_generation(
        sim_instance_id=before.id,
        scenario_id=before.scenario_id or "baseline",
        scenario_version=before.scenario_version or "1.0",
        seed=before.seed if before.seed is not None else 12345,
        tick=before.tick,
        sim_time=before.sim_time,
        reason="operator_manual_rebind",
    )
    request.app.state.generation = gen
    request.app.state.reset_noticed = False
    clear_planning_cache(request.app)
    clear_coherent_planning(request.app)
    return gen


def _set_provenance_headers(
    response: Response,
    *,
    as_of_tick: int,
    current_tick: int,
    observed_at: datetime,
    cached: bool,
    degraded: bool,
) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Bup-As-Of-Tick"] = str(as_of_tick)
    response.headers["X-Bup-Current-Tick"] = str(current_tick)
    response.headers["X-Bup-Age-Ticks"] = str(max(0, current_tick - as_of_tick))
    response.headers["X-Bup-Observed-At"] = observed_at.isoformat()
    response.headers["X-Bup-Cached"] = "true" if cached else "false"
    response.headers["X-Bup-Degraded"] = "true" if degraded else "false"


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
    _circuit_allow(request.app)
    client = request.app.state.simulator
    before: Simulation | None = None
    try:
        for _ in range(6):
            before = await read(client, "/v1/instance", INSTANCE)
            snapshot = await read_snapshot(client, before)
            after = await read(client, "/v1/instance", INSTANCE)
            if before == after:
                obs_at = utc_now()
                gen = _ensure_generation(request.app, before)
                request.app.state.last_coherent_snapshot = (time.monotonic(), gen.generation_id, snapshot, obs_at)
                _circuit_success(request.app)
                _set_provenance_headers(
                    response,
                    as_of_tick=before.tick,
                    current_tick=before.tick,
                    observed_at=obs_at,
                    cached=False,
                    degraded=False,
                )
                return snapshot
    except HTTPException as exc:
        clear_planning_cache(request.app)
        _circuit_failure(request.app, is_stale="stale" in str(exc.detail).lower())
        raise
    except httpx.HTTPError as exc:
        clear_planning_cache(request.app)
        _circuit_failure(request.app)
        raise HTTPException(503, "Simulator request failed") from exc
    except ValidationError as exc:
        _circuit_failure(request.app)
        raise HTTPException(502, "Simulator returned invalid data") from exc
    cached_snap = getattr(request.app.state, "last_coherent_snapshot", None)
    cur_gen = getattr(request.app.state, "generation", None)
    if (
        before is not None
        and before.status == "RUNNING"
        and cached_snap is not None
        and cur_gen is not None
        and cached_snap[1] == cur_gen.generation_id
        and (time.monotonic() - cached_snap[0]) <= RUNNING_COHERENT_GRACE_SECONDS
    ):
        snap_cached: Snapshot = cached_snap[2]
        obs_cached: datetime = cached_snap[3] if len(cached_snap) > 3 else utc_now()
        _set_provenance_headers(
            response,
            as_of_tick=snap_cached.simulation.tick,
            current_tick=before.tick,
            observed_at=obs_cached,
            cached=True,
            degraded=True,
        )
        return snap_cached
    if before is None or before.status != "RUNNING":
        _circuit_failure(request.app)
    raise HTTPException(503, "Simulation changed while collecting snapshot")


async def _query_ml_models(
    app_instance: FastAPI,
    *,
    generation_id: str,
    snapshot: Snapshot,
    history: list[DemandObservation],
    station_details: list[StationDetail],
    regions: list[Region],
    events: list[SimulatorEvent],
    history_ticks: int,
    horizon_ticks: int,
    need_chronos: bool,
) -> tuple[dict[tuple[str, Fuel], list[ForecastPoint]] | None, str | None, list[dict[str, Any]], str | None]:
    bounded_horizon = min(288, max(1, int(horizon_ticks)))
    ml_client: httpx.AsyncClient | None = getattr(app_instance.state, "ml_client", None)
    if ml_client is None:
        reason = "ML service disabled or unconfigured; using seasonal/rule mode." if need_chronos else None
        return None, None, [], reason

    tick = snapshot.simulation.tick
    sim_time = snapshot.simulation.sim_time
    tick_minutes = snapshot.simulation.tick_minutes
    details_map = {d.id: d for d in station_details}
    reg_map = {r.id: r.demand_factor for r in regions}
    observed = index_observations_by_series(history, current_tick=tick, history_ticks=max(history_ticks, 96))

    chronos_preds: dict[tuple[str, Fuel], list[ForecastPoint]] | None = None
    chronos_ver: str | None = None
    ml_anomalies: list[dict[str, Any]] = []
    fallback_reason: str | None = None

    # 1. Score latest observations via locally fitted Isolation Forest
    anomaly_items = []
    for station in snapshot.stations:
        detail = details_map.get(station.id)
        profile = detail.demand_profile if detail else "urban_high"
        mult = detail.demand_multiplier if detail else 1.0
        for fuel in FUELS:
            samples = sorted(observed[(station.id, fuel)].items())
            if len(samples) < 8:
                continue
            latest_tick, latest_obs = samples[-1]
            if latest_tick != tick - 1:
                continue
            norm_vals = []
            for s_tick, obs in samples[:-1][-16:]:
                s_time = sim_time - timedelta(minutes=(tick - s_tick) * tick_minutes)
                hf = hour_factor(profile, s_time.hour)
                sf = spike_multiplier_at(events, station.id, station.region_id, s_tick)
                norm_vals.append(obs.demand_liters / max(0.01, hf * sf))
            if not norm_vals:
                continue
            base_norm = sum(norm_vals) / len(norm_vals)
            latest_time = sim_time - timedelta(minutes=tick_minutes)
            hf_now = hour_factor(profile, latest_time.hour)
            # Pass multiplier=1.0 so unannounced demand shifts remain visible to Isolation Forest
            expected = base_norm * hf_now
            r_std = max(1.0, expected * 0.12)
            cap = float(getattr(station.capacity, fuel))
            stock = float(getattr(station.inventory, fuel))
            anomaly_items.append({
                "station_id": station.id,
                "fuel_type": fuel,
                "tick": latest_tick,
                "sim_time": latest_time.isoformat(),
                "profile": profile,
                "known_spike_multiplier": 1.0,
                "demand_liters": latest_obs.demand_liters,
                "baseline_normalized": base_norm,
                "rolling_expected": expected,
                "rolling_std": r_std,
                "inventory_ratio": stock / cap if cap > 0 else 0.5,
                "inventory_balance_residual": None,
            })

    if anomaly_items:
        try:
            a_resp = await ml_client.post("/internal/anomaly", json={"items": anomaly_items})
            if a_resp.status_code == 200:
                data = a_resp.json()
                ml_anomalies = data.get("scores", [])
                app_instance.state.last_ml_error = None
        except Exception as exc:
            app_instance.state.last_ml_error = f"ML anomaly scorer unavailable: {exc}"

    # 2. Call Chronos-2 only when explicitly requested by policy/forecast endpoint
    if need_chronos:
        series_payload = []
        ready = True
        for station in snapshot.stations:
            detail = details_map.get(station.id)
            profile = detail.demand_profile if detail else "urban_high"
            rfac = reg_map.get(station.region_id, 1.0)
            mult = detail.demand_multiplier if detail else 1.0
            for fuel in FUELS:
                obs_map = observed[(station.id, fuel)]
                if len(obs_map) < 8:
                    ready = False
                    break
                start_t = min(obs_map.keys())
                reg_rows, _ = regularized_series(
                    obs_map, start_tick=start_t, end_tick_exclusive=tick,
                    current_tick=tick, tick_minutes=tick_minutes,
                    current_sim_time=sim_time, station_detail=detail,
                )
                for row in reg_rows:
                    row["known_spike_multiplier"] = spike_multiplier_at(events, station.id, station.region_id, row["tick"])
                    row["region_factor"] = rfac
                fut_covs = []
                for off in range(bounded_horizon):
                    ft = tick + off
                    f_ts = sim_time + timedelta(minutes=off * tick_minutes)
                    sf = spike_multiplier_at(events, station.id, station.region_id, ft)
                    if off == 0 and math.isclose(sf, 1.0, abs_tol=1e-9) and not math.isclose(mult, 1.0, abs_tol=1e-9):
                        sf = mult
                    fut_covs.append({
                        "tick": ft,
                        "sim_time": f_ts.isoformat(),
                        "hour_factor": hour_factor(profile, f_ts.hour),
                        "known_spike_multiplier": sf,
                        "region_factor": rfac,
                    })
                series_payload.append({
                    "station_id": station.id,
                    "fuel_type": fuel,
                    "history": reg_rows,
                    "future_covariates": fut_covs,
                })
            if not ready:
                break

        if not ready:
            fallback_reason = "Chronos-2 requires >= 8 completed history ticks; fallback active."
        else:
            try:
                f_resp = await ml_client.post(
                    "/internal/forecast",
                    json={
                        "generation_id": generation_id,
                        "as_of_tick": tick,
                        "tick_minutes": tick_minutes,
                        "horizon_ticks": horizon_ticks,
                        "series": series_payload,
                    },
                )
                if f_resp.status_code != 200:
                    fallback_reason = f"Chronos-2 service returned HTTP {f_resp.status_code}; fallback to seasonal."
                    app_instance.state.last_ml_error = fallback_reason
                else:
                    payload = f_resp.json()
                    if payload.get("generation_id") != generation_id or payload.get("as_of_tick") != tick:
                        fallback_reason = "Chronos-2 generation/tick mismatch; rejected stale ML output."
                    else:
                        parsed: dict[tuple[str, Fuel], list[ForecastPoint]] = {}
                        for s_row in payload.get("series", []):
                            key = (s_row["station_id"], s_row["fuel_type"])
                            pts = [
                                ForecastPoint(
                                    tick=pt["tick"],
                                    sim_time=datetime.fromisoformat(pt["sim_time"]),
                                    p10=pt["p10"],
                                    p50=pt["p50"],
                                    p90=pt["p90"],
                                    quantile_method="CHRONOS2_QUANTILE",
                                    quality_warnings=pt.get("quality_warnings", []),
                                )
                                for pt in s_row.get("points", [])
                            ]
                            parsed[key] = pts
                        chronos_preds = parsed
                        chronos_ver = payload.get("model_version")
                        app_instance.state.last_ml_latency_ms = payload.get("inference_ms")
                        app_instance.state.last_ml_error = None
            except Exception as exc:
                fallback_reason = f"Chronos-2 inference failed ({exc}); using seasonal fallback."
                app_instance.state.last_ml_error = fallback_reason

    return chronos_preds, chronos_ver, ml_anomalies, fallback_reason


async def _collect_world_state(
    app_instance: FastAPI,
    history_ticks: int,
) -> tuple[
    GenerationInfo,
    Snapshot,
    list[StationDetail],
    list[Region],
    list[Allocation],
    list[SupplyArrival],
    list[SimulatorEvent],
    SimulatorMetrics,
    list[DemandObservation],
]:
    client: httpx.AsyncClient = app_instance.state.simulator
    for _ in range(4):
        before = await read(client, "/v1/instance", INSTANCE)
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
        after = await read(client, "/v1/instance", INSTANCE)
        if before != after:
            continue
        stations = STATIONS.validate_json(stations_raw)
        station_details = STATION_DETAILS.validate_json(stations_raw)
        snapshot = Snapshot(simulation=before, depots=depots_raw, stations=stations, routes=routes_raw)

        limit = max(history_ticks * len(FUELS), 48 * len(FUELS))
        limit = min(2000, limit)
        history_paths = [
            "/v1/demand-history?" + urlencode({"station_id": station.id, "limit": limit})
            for station in snapshot.stations
        ]
        history_batches = await asyncio.gather(*(read(client, path, HISTORY) for path in history_paths))
        gen = _ensure_generation(app_instance, before, allocations)
        app_instance.state.last_coherent_snapshot = (time.monotonic(), gen.generation_id, snapshot, utc_now())
        _circuit_success(app_instance)
        history = [entry for batch in history_batches for entry in batch]
        return gen, snapshot, station_details, regions, allocations, supply_arrivals, events, sim_metrics, history
    if before.status != "RUNNING":
        _circuit_failure(app_instance)
    raise HTTPException(503, "Simulation changed while collecting snapshot")


async def _build_planning_report(
    app_instance: FastAPI,
    history_ticks: int,
    horizon_ticks: int,
    policy_override: str = "AUTO",
    single_action_limit: int | None = None,
    max_total_liters: float | None = None,
) -> tuple[PlanningReport, ForecastsResponse, Snapshot, list[Allocation]]:
    client: httpx.AsyncClient = app_instance.state.simulator
    cache: dict[tuple, tuple[float, int, str, str, tuple]] = app_instance.state.planning_cache
    key = (history_ticks, horizon_ticks, policy_override, single_action_limit, max_total_liters)
    store: PlatformStore = app_instance.state.store
    metrics: RequestMetrics = app_instance.state.metrics

    for _ in range(3):
        epoch = getattr(app_instance.state, "planning_epoch", 0)
        before = await read(client, "/v1/instance", INSTANCE)
        cached = cache.get(key)
        if cached is not None:
            cached_at, cached_tick, cached_status, cached_gen, payload_tuple = cached
            cur_gen = getattr(app_instance.state, "generation", None)
            if (
                cur_gen is not None
                and cached_gen == cur_gen.generation_id
                and cached_tick == before.tick
                and cached_status == before.status
                and (time.monotonic() - cached_at) <= PLANNING_CACHE_TTL_SECONDS
            ):
                metrics.cache_hits += 1
                rep_cached, fc_cached, snap_cached, alloc_cached = payload_tuple
                rep_copy = rep_cached.model_copy(
                    update={
                        "current_tick": before.tick,
                        "age_ticks": max(0, before.tick - rep_cached.tick),
                        "cached": True,
                    }
                )
                return rep_copy, fc_cached, snap_cached, alloc_cached

        metrics.cache_misses += 1
        coherent_store = getattr(app_instance.state, "last_coherent_planning", {})
        cur_gen = getattr(app_instance.state, "generation", None)
        try:
            gen, snapshot, station_details, regions, allocations, supply_arrivals, events, sim_metrics, history = (
                await _collect_world_state(app_instance, history_ticks)
            )
        except HTTPException as exc:
            coherent = coherent_store.get(key)
            if (
                exc.status_code == 503
                and "changed while collecting" in str(exc.detail)
                and coherent is not None
                and cur_gen is not None
                and coherent[1] == cur_gen.generation_id
                and before.status == "RUNNING"
                and (time.monotonic() - coherent[0]) <= RUNNING_COHERENT_GRACE_SECONDS
            ):
                metrics.cache_hits += 1
                rep_g, fc_g, snap_g, alloc_g = coherent[2]
                age_g = max(0, before.tick - rep_g.tick)
                rep_grace = rep_g.model_copy(
                    update={
                        "current_tick": before.tick,
                        "age_ticks": age_g,
                        "cached": True,
                        "degraded": True,
                        "degraded_reason": (
                            f"Serving coherent tick-{rep_g.tick} plan while RUNNING simulator ticks rapidly "
                            f"(current tick {before.tick}, age {age_g} ticks)."
                        ),
                    }
                )
                return rep_grace, fc_g, snap_g, alloc_g
            raise

        # Persist coherent observations and world snapshot into versioned SQLite store
        store.upsert_observations(
            gen.generation_id,
            history,
            {s.id: s.region_id for s in snapshot.stations},
            snapshot.simulation.tick_minutes,
            snapshot.simulation.tick,
            snapshot.simulation.sim_time,
        )
        full_history = store.load_observations(gen.generation_id, limit_ticks=max(240, history_ticks)) or history
        store.record_snapshot(
            gen.generation_id,
            snapshot,
            [e.model_dump() for e in events],
            [a.model_dump() for a in supply_arrivals],
            sim_metrics.model_dump(),
        )

        need_chronos = policy_override == "CHRONOS2"
        chronos_preds, chronos_ver, ml_anomalies, ml_reason = await _query_ml_models(
            app_instance,
            generation_id=gen.generation_id,
            snapshot=snapshot,
            history=full_history,
            station_details=station_details,
            regions=regions,
            events=events,
            history_ticks=history_ticks,
            horizon_ticks=horizon_ticks,
            need_chronos=need_chronos,
        )

        report = build_report(
            snapshot,
            full_history,
            allocations,
            history_ticks,
            horizon_ticks,
            station_details=station_details,
            regions=regions,
            supply_arrivals=supply_arrivals,
            events=events,
            simulator_metrics=sim_metrics,
            generation_id=gen.generation_id,
            chronos_predictions=chronos_preds,
            chronos_model_version=chronos_ver,
            ml_anomaly_scores=ml_anomalies,
            policy_override=policy_override,
            single_action_limit=single_action_limit,
            max_total_liters=max_total_liters,
            ml_fallback_reason=ml_reason,
        )

        persisted_incidents = store.sync_incidents(gen.generation_id, snapshot.simulation.tick, report.anomalies)
        report = report.model_copy(update={"anomalies": persisted_incidents})
        if report.joint_plan:
            store.record_plan(report.joint_plan)

        matured = store.matured_forecast_accuracy(gen.generation_id)
        fc_response = build_forecasts_response(
            snapshot,
            full_history,
            history_ticks,
            horizon_ticks,
            generation_id=gen.generation_id,
            station_details=station_details,
            regions=regions,
            events=events,
            chronos_predictions=chronos_preds,
            chronos_model_version=chronos_ver,
            policy_override=policy_override,
            fallback_reason=ml_reason,
            matured_accuracy=matured,
        )
        store.record_forecast_run(fc_response, latency_ms=report.joint_plan.solver_runtime_ms if report.joint_plan else None)

        result_tuple = (report, fc_response, snapshot, allocations)
        coherent_store = getattr(app_instance.state, "last_coherent_planning", None)
        if coherent_store is not None:
            if len(coherent_store) >= MAX_CACHE_ENTRIES:
                coherent_store.pop(next(iter(coherent_store)))
            coherent_store[key] = (time.monotonic(), gen.generation_id, result_tuple)
        if getattr(app_instance.state, "planning_epoch", 0) == epoch:
            if len(cache) >= MAX_CACHE_ENTRIES:
                cache.pop(next(iter(cache)))
            cache[key] = (time.monotonic(), snapshot.simulation.tick, snapshot.simulation.status, gen.generation_id, result_tuple)

        metrics.record_intelligence(
            report.fallback_active,
            len(report.anomalies),
            solver_status=report.solver_status,
            solver_ms=report.joint_plan.solver_runtime_ms if report.joint_plan else None,
            forecast_source=report.forecast_source,
            fallback_reason=report.fallback_reason,
            sim_metrics=sim_metrics.model_dump(),
        )
        return result_tuple

    raise HTTPException(503, "Simulation changed while collecting snapshot")


async def _get_planning_coalesced(
    request: Request,
    history_ticks: int,
    horizon_ticks: int,
    policy_override: str = "AUTO",
    single_action_limit: int | None = None,
    max_total_liters: float | None = None,
) -> tuple[PlanningReport, ForecastsResponse, Snapshot, list[Allocation]]:
    _circuit_allow(request.app)
    key = (history_ticks, horizon_ticks, policy_override, single_action_limit, max_total_liters)
    inflight: dict[tuple, asyncio.Task] = request.app.state.planning_inflight
    task = inflight.get(key)
    owner = task is None or task.done()
    if owner:
        task = asyncio.create_task(
            _build_planning_report(
                request.app, history_ticks, horizon_ticks, policy_override, single_action_limit, max_total_liters
            )
        )
        inflight[key] = task

    try:
        return await asyncio.shield(task)
    except HTTPException as exc:
        clear_planning_cache(request.app)
        _circuit_failure(request.app, is_stale="stale" in str(exc.detail).lower())
        raise
    except httpx.HTTPError as exc:
        clear_planning_cache(request.app)
        _circuit_failure(request.app)
        raise HTTPException(503, "Simulator request failed") from exc
    except ValidationError as exc:
        _circuit_failure(request.app)
        raise HTTPException(502, "Simulator returned invalid data") from exc
    finally:
        if owner and inflight.get(key) is task:
            inflight.pop(key, None)


@app.get(
    "/api/planning",
    operation_id="getPlanningReport",
    response_model=PlanningReport,
    description=(
        "Projects station demand with seasonality/events/Chronos candidate, detects rule and ML anomalies, "
        "runs OR-Tools constrained optimization with safety-stock replenishment and fallback, and validates plans independently."
    ),
    responses=UPSTREAM_ERRORS,
)
async def get_planning_report(
    request: Request,
    response: Response,
    history_ticks: int = Query(8, ge=1, le=96, description="Completed ticks used for baseline context."),
    horizon_ticks: int = Query(96, ge=1, le=288, description="Number of ticks to project from the current tick."),
    policy_override: Literal["AUTO", "SEASONAL", "CHRONOS2", "SAFE_REORDER"] = Query("AUTO"),
    single_action_limit: int | None = Query(None, ge=1, le=20),
    max_total_liters: float | None = Query(None, gt=0, le=500_000),
) -> PlanningReport:
    report, _, _, _ = await _get_planning_coalesced(
        request, history_ticks, horizon_ticks, policy_override, single_action_limit, max_total_liters
    )
    _set_provenance_headers(
        response,
        as_of_tick=report.tick,
        current_tick=report.current_tick if report.current_tick is not None else report.tick,
        observed_at=report.observed_at or utc_now(),
        cached=report.cached,
        degraded=report.degraded,
    )
    return report


@app.get("/api/forecasts", operation_id="getForecasts", response_model=ForecastsResponse, responses=UPSTREAM_ERRORS)
async def get_forecasts(
    request: Request,
    response: Response,
    history_ticks: int = Query(24, ge=1, le=96),
    horizon_ticks: int = Query(32, ge=1, le=288),
    policy_override: Literal["AUTO", "SEASONAL", "CHRONOS2", "SAFE_REORDER"] = Query("AUTO"),
) -> ForecastsResponse:
    _, fc, _, _ = await _get_planning_coalesced(request, history_ticks, horizon_ticks, policy_override)
    response.headers["Cache-Control"] = "no-store"
    return fc


@app.get("/api/shortages", operation_id="getShortages", response_model=ShortagesResponse, responses=UPSTREAM_ERRORS)
async def get_shortages(
    request: Request,
    response: Response,
    history_ticks: int = Query(8, ge=1, le=96),
    horizon_ticks: int = Query(96, ge=1, le=288),
) -> ShortagesResponse:
    report, _, snapshot, allocations = await _get_planning_coalesced(request, history_ticks, horizon_ticks)
    response.headers["Cache-Control"] = "no-store"
    return build_shortages_response(snapshot=snapshot, report=report, allocations=allocations)


@app.get("/api/anomalies", operation_id="getAnomalies", response_model=AnomaliesResponse, responses=UPSTREAM_ERRORS)
@app.get("/api/incidents", operation_id="getIncidents", response_model=AnomaliesResponse, responses=UPSTREAM_ERRORS)
async def get_anomalies(
    request: Request,
    response: Response,
    status: Literal["OPEN", "RESOLVED", "ALL"] = Query("ALL"),
    category: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> AnomaliesResponse:
    report, _, _, _ = await _get_planning_coalesced(request, 8, 96)
    gen_id = report.generation_id
    items, open_c, res_c = request.app.state.store.list_incidents(gen_id, status=status, category=category, limit=limit)
    det_src = "HYBRID" if any(i.source in ("ML", "HYBRID") for i in items) else "RULE"
    response.headers["Cache-Control"] = "no-store"
    return AnomaliesResponse(
        generation_id=gen_id,
        as_of_tick=report.tick,
        detector_source=det_src,
        anomaly_model_version="iforest-bup-v1.0.0" if request.app.state.ml_url else None,
        total=len(items),
        open_count=open_c,
        resolved_count=res_c,
        incidents=items,
    )


@app.post("/api/optimize", operation_id="optimizePlan", response_model=PlanPreviewSummary, responses=UPSTREAM_ERRORS)
async def optimize_plan(body: OptimizeRequest, request: Request, response: Response) -> PlanPreviewSummary:
    report, _, _, _ = await _get_planning_coalesced(
        request,
        body.history_ticks,
        body.horizon_ticks,
        body.policy_override,
        body.single_action_limit,
        body.max_total_liters,
    )
    response.headers["Cache-Control"] = "no-store"
    assert report.joint_plan is not None
    return report.joint_plan


@app.post("/api/simulate", operation_id="simulateShipments", response_model=SimulateResponse, responses=UPSTREAM_ERRORS)
async def simulate_shipments(body: SimulateRequest, request: Request, response: Response) -> SimulateResponse:
    report, fc, snapshot, allocations = await _get_planning_coalesced(request, body.history_ticks, body.horizon_ticks)
    routes_map = {r.id: r for r in snapshot.routes}
    dispatch_used: dict[str, float] = {}
    incoming: dict[tuple[str, Fuel], list[float]] = {
        (s.id, f): [0.0] * body.horizon_ticks for s in snapshot.stations for f in FUELS
    }
    tick = snapshot.simulation.tick
    for a in allocations:
        if a.status in ("PENDING", "IN_TRANSIT") and a.created_tick == tick:
            dispatch_used[a.source_depot_id] = dispatch_used.get(a.source_depot_id, 0.0) + a.quantity
        if a.status == "IN_TRANSIT" and a.expected_arrival_tick is not None and tick <= a.expected_arrival_tick < tick + body.horizon_ticks:
            incoming[(a.destination_station_id, a.fuel_type)][a.expected_arrival_tick - tick] += a.quantity
        elif a.status == "PENDING" and a.route_id in routes_map:
            eta = routes_map[a.route_id].transit_ticks
            if eta < body.horizon_ticks:
                incoming[(a.destination_station_id, a.fuel_type)][eta] += a.quantity

    dem_p50 = {(s.station_id, s.fuel_type): [pt.p50 for pt in s.forecast] for s in fc.series}
    dem_p10 = {(s.station_id, s.fuel_type): [pt.p10 or pt.p50 for pt in s.forecast] for s in fc.series}
    dem_p90 = {(s.station_id, s.fuel_type): [pt.p90 or pt.p50 for pt in s.forecast] for s in fc.series}

    parsed_ships = []
    v_errors = []
    for item in body.shipments:
        route = routes_map.get(item.route_id)
        if route is None:
            v_errors.append(f"Unknown route {item.route_id}")
            continue
        if (route.source_depot_id, route.destination_station_id) != (item.source_depot_id, item.destination_station_id):
            v_errors.append(f"Route {item.route_id} endpoints do not match proposal")
            continue
        parsed_ships.append((route, item.fuel_type, float(item.quantity)))

    ok_inv, inv_errors = validate_plan_independently(
        shipments=parsed_ships, snapshot=snapshot, dispatch_used=dispatch_used,
        incoming=incoming, demand_series_by_key=dem_p50,
    )
    all_errors = v_errors + inv_errors
    stats = simulate_joint_impact(
        shipments=parsed_ships, snapshot=snapshot, incoming=incoming,
        demand_series_by_key=dem_p50, demand_p10_by_key=dem_p10, demand_p90_by_key=dem_p90,
    )
    response.headers["Cache-Control"] = "no-store"
    return SimulateResponse(
        generation_id=report.generation_id,
        as_of_tick=tick,
        horizon_ticks=body.horizon_ticks,
        valid=len(all_errors) == 0,
        validation_errors=all_errors,
        forecast_source=report.forecast_source,
        total_allocated_liters=round(sum(s[2] for s in parsed_ships), 3),
        unmet_before_liters=float(stats["unmet_before"] or 0.0),
        unmet_after_liters=float(stats["unmet_after"] or 0.0),
        unmet_avoided_liters=float(stats["unmet_avoided"] or 0.0),
        station_impacts=[{"route_id": r.id, "station_id": r.destination_station_id, "fuel_type": f, "quantity": q, "eta_ticks": r.transit_ticks} for r, f, q in parsed_ships],
    )


@app.get("/api/plans/latest", operation_id="getLatestPlan", response_model=LatestPlanResponse)
async def get_latest_plan(request: Request, response: Response) -> LatestPlanResponse:
    response.headers["Cache-Control"] = "no-store"
    store: PlatformStore = request.app.state.store
    cur_gen: GenerationInfo | None = getattr(request.app.state, "generation", None) or store.get_active_generation()
    gen_id = cur_gen.generation_id if cur_gen else "default"
    plan = store.get_latest_plan(gen_id)
    if plan is None:
        return LatestPlanResponse(generation_id=gen_id, plan=None, approval=None)
    shipments: list[Shipment] | None = None
    cur_tick: int | None = cur_gen.last_seen_tick if cur_gen else None
    try:
        sim_inst = await read(request.app.state.simulator, "/v1/instance", INSTANCE, retries=0)
        shipments = await read(request.app.state.simulator, "/v1/allocations", SHIPMENTS, retries=0)
        updated_gen = _ensure_generation(request.app, sim_inst, shipments)
        gen_id = updated_gen.generation_id
        cur_tick = sim_inst.tick
        if plan.generation_id != gen_id:
            plan = store.get_latest_plan(gen_id)
    except (httpx.HTTPError, ValidationError, HTTPException):
        pass
    approval_view = (
        reconcile_plan_approval_view(
            platform_store=store, plan_id=plan.plan_id, shipments=shipments, current_tick=cur_tick
        )
        if plan is not None
        else None
    )
    return LatestPlanResponse(generation_id=gen_id, plan=plan, approval=approval_view)


@app.get("/api/plans/{plan_id}", operation_id="getPlan", response_model=PlanPreviewSummary)
async def get_plan(plan_id: str, request: Request, response: Response) -> PlanPreviewSummary:
    response.headers["Cache-Control"] = "no-store"
    plan = request.app.state.store.get_plan(plan_id)
    if plan is None:
        raise HTTPException(404, f"Plan {plan_id} not found.")
    return plan


@app.get("/api/plans/{plan_id}/approval", operation_id="getPlanApproval", response_model=PlanApprovalResult)
async def get_plan_approval(plan_id: str, request: Request, response: Response) -> PlanApprovalResult:
    response.headers["Cache-Control"] = "no-store"
    store: PlatformStore = request.app.state.store
    plan = store.get_plan(plan_id)
    if plan is None:
        raise HTTPException(404, f"Plan {plan_id} not found.")
    shipments: list[Shipment] | None = None
    cur_tick: int | None = None
    try:
        sim_inst = await read(request.app.state.simulator, "/v1/instance", INSTANCE, retries=0)
        shipments = await read(request.app.state.simulator, "/v1/allocations", SHIPMENTS, retries=0)
        cur_tick = sim_inst.tick
        _ensure_generation(request.app, sim_inst, shipments)
    except (httpx.HTTPError, ValidationError, HTTPException):
        pass
    view = reconcile_plan_approval_view(
        platform_store=store, plan_id=plan_id, shipments=shipments, current_tick=cur_tick
    )
    if view is None:
        raise HTTPException(404, f"No batch approval recorded yet for plan {plan_id}.")
    return view


@app.put(
    "/api/plans/{plan_id}/approval",
    operation_id="approvePlan",
    response_model=PlanApprovalResult,
    description="Server-owned batch approval and idempotent reconciliation for an immutable optimization plan.",
    responses={
        404: {"description": "Plan ID not found."},
        409: {"description": "Plan belongs to an earlier generation, requires rebind, or config hash mismatched."},
        **UPSTREAM_ERRORS,
    },
)
async def approve_plan(
    plan_id: str,
    request: Request,
    response: Response,
    body: PlanApprovalRequest = Body(default_factory=PlanApprovalRequest),
) -> PlanApprovalResult:
    response.headers["Cache-Control"] = "no-store"
    actor = request.app.state.auth.require_operator(request)
    store: PlatformStore = request.app.state.store
    approval_store: ApprovalStore = request.app.state.approval_store
    client: httpx.AsyncClient = request.app.state.simulator

    plan = store.get_plan(plan_id)
    if plan is None:
        raise HTTPException(404, f"Plan {plan_id} not found.")
    if body.expected_config_hash and body.expected_config_hash != plan.config_hash:
        raise HTTPException(409, "Plan payload/configuration hash mismatch; refresh and review the current plan.")

    plan_locks: dict[str, asyncio.Lock] = request.app.state.plan_locks
    plan_lock = plan_locks.setdefault(plan_id, asyncio.Lock())

    def _verify_gen(snapshot: Snapshot, shipments: list[Shipment]) -> None:
        gen = _ensure_generation(request.app, snapshot.simulation, shipments)
        if gen.status == "REBIND_REQUIRED":
            raise HTTPException(409, "Simulator generation requires operator rebind before approving plans.")
        if plan.generation_id != gen.generation_id:
            raise HTTPException(
                409,
                "This plan belongs to an earlier simulator generation and is blocked after reset. Generate a new plan.",
            )

    clear_planning_cache(request.app)
    async with plan_lock, request.app.state.approval_lock:
        result = await execute_plan_approval(
            platform_store=store,
            approval_store=approval_store,
            plan=plan,
            actor=actor,
            decision_rationale=body.decision_rationale,
            reconcile_only=body.reconcile_only,
            client=client,
            read_shipments=lambda: read(client, "/v1/allocations", SHIPMENTS, retries=0),
            read_fresh_state=lambda: read_approval_state(client),
            verify_generation=_verify_gen,
        )
        clear_planning_cache(request.app)
        return result


@app.put(
    "/api/approvals/{intent_id}",
    operation_id="submitApproval",
    response_model=ApprovalAttempt,
    description="Approve or retry an exact shipment intent. The response reports its persisted outcome.",
    responses={409: {"description": "The intent ID already belongs to a different shipment payload or earlier generation."}},
)
async def approve(
    intent_id: UUID,
    proposal: Proposal,
    request: Request,
    plan_id: str | None = Query(None),
    decision_rationale: str | None = Query(None, max_length=500),
) -> ApprovalAttempt:
    actor = request.app.state.auth.require_operator(request)
    client = request.app.state.simulator
    store = request.app.state.approval_store
    platform_store: PlatformStore = request.app.state.store
    clear_planning_cache(request.app)
    async with request.app.state.approval_lock:
        current_gen = getattr(request.app.state, "generation", None)
        if current_gen and current_gen.status == "REBIND_REQUIRED":
            raise HTTPException(409, "Simulator generation requires operator rebind before approving allocations.")
        gen_id = current_gen.generation_id if current_gen else "default"
        if plan_id:
            plan = platform_store.get_plan(plan_id)
            if plan is None:
                raise HTTPException(404, f"Plan {plan_id} not found.")
            if plan.generation_id != gen_id:
                raise HTTPException(
                    409,
                    "This plan belongs to an earlier simulator generation and is blocked after reset.",
                )
            matching_rec = next(
                (
                    r for r in plan.shipments
                    if r.source_depot_id == proposal.source_depot_id
                    and r.destination_station_id == proposal.destination_station_id
                    and r.route_id == proposal.route_id
                    and r.fuel_type == proposal.fuel_type
                    and abs(r.quantity_liters - proposal.quantity) <= 1e-6
                ),
                None,
            )
            if matching_rec is None:
                raise HTTPException(409, "Submitted shipment payload does not match the stored plan.")
            existing_item = platform_store.find_plan_item_for_shipment(
                plan_id,
                source_depot_id=proposal.source_depot_id,
                destination_station_id=proposal.destination_station_id,
                route_id=proposal.route_id,
                fuel_type=proposal.fuel_type,
                quantity=proposal.quantity,
            )
            if existing_item and existing_item["intent_id"] != str(intent_id):
                raise HTTPException(
                    409,
                    "This plan shipment already has a persisted identity; use PUT /api/plans/{plan_id}/approval.",
                )
        attempt = store.get_or_create(
            intent_id,
            proposal,
            generation_id=gen_id,
            plan_id=plan_id,
            actor=actor,
            decision_rationale=decision_rationale,
        )
        result = await submit(
            store,
            attempt,
            client,
            lambda: read(client, "/v1/allocations", SHIPMENTS, retries=0),
            lambda: read_approval_state(client),
        )
        if platform_store.get_plan_item_by_intent(str(intent_id)):
            platform_store.sync_plan_item_by_intent(
                intent_id=str(intent_id),
                state=result.state,
                message=result.message,
                shipment_json=result.shipment.model_dump_json() if result.shipment else None,
                allocation_id=result.shipment.id if result.shipment else None,
            )
        clear_planning_cache(request.app)
        return result


@app.post(
    "/api/approvals/{intent_id}/cancel",
    operation_id="cancelApproval",
    response_model=ApprovalAttempt,
    description="Explicit operator cancellation of a PENDING simulator allocation.",
)
async def cancel_approval(intent_id: UUID, request: Request) -> ApprovalAttempt:
    request.app.state.auth.require_operator(request)
    store = request.app.state.approval_store
    platform_store: PlatformStore = request.app.state.store
    client = request.app.state.simulator
    async with request.app.state.approval_lock:
        attempt = store.get(str(intent_id))
        if attempt is None:
            raise HTTPException(404, "Approval intent not found.")
        res = await cancel_pending(store, attempt, client)
        if platform_store.get_plan_item_by_intent(str(intent_id)):
            platform_store.sync_plan_item_by_intent(
                intent_id=str(intent_id),
                state=res.state,
                message=res.message,
                shipment_json=res.shipment.model_dump_json() if res.shipment else None,
                allocation_id=res.shipment.id if res.shipment else None,
            )
        clear_planning_cache(request.app)
        return res


@app.get(
    "/api/approvals",
    operation_id="listApprovals",
    response_model=ApprovalLedger,
    description="Lists durable approval attempts and reconciles simulator shipment statuses when fresh.",
)
async def approvals(
    request: Request,
    response: Response,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    all_generations: bool = Query(False),
) -> ApprovalLedger:
    response.headers["Cache-Control"] = "no-store"
    store = request.app.state.approval_store
    cur_gen = getattr(request.app.state, "generation", None)
    gen_id = cur_gen.generation_id if cur_gen else None
    async with request.app.state.approval_lock:
        attempts = store.list(generation_id=None if all_generations else gen_id, limit=limit, offset=offset)
        try:
            sim_inst = await read(request.app.state.simulator, "/v1/instance", INSTANCE, retries=0)
            shipments = await read(request.app.state.simulator, "/v1/allocations", SHIPMENTS, retries=0)
            updated_gen = _ensure_generation(request.app, sim_inst, shipments)
            gen_id = updated_gen.generation_id
            if not all_generations:
                attempts = store.list(generation_id=gen_id, limit=limit, offset=offset)
            _circuit_success(request.app)
        except (httpx.HTTPError, ValidationError, HTTPException) as exc:
            clear_planning_cache(request.app)
            _circuit_failure(request.app, is_stale="stale" in str(getattr(exc, "detail", "")).lower())
            return ApprovalLedger(
                generation_id=gen_id,
                attempts=attempts,
                simulator_error=upstream_problem(exc),
                rebind_required=bool(cur_gen and cur_gen.status == "REBIND_REQUIRED"),
            )

        by_key = {shipment.idempotency_key: shipment for shipment in shipments}
        platform_store: PlatformStore = request.app.state.store
        for index, attempt in enumerate(attempts):
            shipment = by_key.get(attempt.proposal.idempotency_key)
            if shipment is not None and matches(shipment, attempt.proposal):
                if attempt.shipment != shipment or attempt.message is not None:
                    updated = store.save(attempt, "CONFIRMED", shipment=shipment)
                    attempts[index] = updated
                    if attempt.plan_id and platform_store.get_plan_item_by_intent(attempt.intent_id):
                        platform_store.sync_plan_item_by_intent(
                            intent_id=attempt.intent_id,
                            state="CONFIRMED",
                            message="Cancelled by operator." if shipment.status == "CANCELLED" else None,
                            shipment_json=shipment.model_dump_json(),
                            allocation_id=shipment.id,
                        )
            elif attempt.shipment is not None:
                attempts[index] = attempt.model_copy(
                    update={"message": "Shipment absent from the current simulator ledger. Showing last known status."},
                )
        return ApprovalLedger(
            generation_id=gen_id,
            attempts=attempts,
            simulator_error=None,
            rebind_required=False,
        )


@app.get("/api/models", operation_id="getModels")
async def get_models(request: Request, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    manifest_file = Path(os.environ.get("MODEL_MANIFEST_PATH", "ml-service/model-manifest.json"))
    manifest = json.loads(manifest_file.read_text(encoding="utf-8")) if manifest_file.exists() else {}
    ml_ready = None
    ml_client: httpx.AsyncClient | None = getattr(request.app.state, "ml_client", None)
    if ml_client is not None:
        try:
            r = await ml_client.get("/health/ready", timeout=1.5)
            ml_ready = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"status": r.status_code}
        except Exception as exc:
            ml_ready = {"status": "unavailable", "error": str(exc)}
    return {
        "default_forecast_policy": "OR_TOOLS_SEASONAL",
        "experimental_ml_policy": "OR_TOOLS_CHRONOS2",
        "ml_service_configured": ml_client is not None,
        "ml_service_status": ml_ready,
        "last_ml_latency_ms": getattr(request.app.state, "last_ml_latency_ms", None),
        "last_ml_error": getattr(request.app.state, "last_ml_error", None),
        "manifest": manifest,
    }


@app.get("/api/evaluation", operation_id="getEvaluation")
async def get_evaluation(request: Request, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    summary = load_evaluation_summary(
        eval_dir=os.environ.get("EVAL_ARTIFACTS_DIR", "artifacts/evaluation"),
        manifest_path=os.environ.get("MODEL_MANIFEST_PATH", "ml-service/model-manifest.json"),
        evidence_dir=os.environ.get("EVIDENCE_DIR", "docs/evidence"),
    )
    gen = getattr(request.app.state, "generation", None)
    summary["live_matured_accuracy"] = (
        request.app.state.store.matured_forecast_accuracy(gen.generation_id) if gen else None
    )
    return summary


@app.get(
    "/api/health",
    operation_id="getHealth",
    response_model=HealthReport,
    description="Reports fast per-component health without running heavy ML inference.",
    responses={503: {"model": HealthReport, "description": "One or more critical components are degraded."}},
)
async def get_health(request: Request, response: Response) -> HealthReport | JSONResponse:
    response.headers["Cache-Control"] = "no-store"
    store: PlatformStore = request.app.state.store
    client: httpx.AsyncClient = request.app.state.simulator
    metrics: RequestMetrics = request.app.state.metrics
    sse_state: SSEState = request.app.state.sse_state
    gen: GenerationInfo | None = getattr(request.app.state, "generation", None)

    try:
        store.ping()
        db_health = ComponentHealth(status="ok", detail="SQLite WAL store reachable")
    except sqlite3.Error as exc:
        db_health = ComponentHealth(status="error", detail=str(exc))

    started = time.perf_counter()
    try:
        await read(client, "/v1/health", TypeAdapter(dict), retries=0)
        await read(client, "/v1/instance", INSTANCE, retries=0)
        sim_latency = round((time.perf_counter() - started) * 1000.0, 2)
        sim_health = ComponentHealth(status="ok", detail="Fresh simulator REST reachable", latency_ms=sim_latency)
    except HTTPException as exc:
        sim_latency = round((time.perf_counter() - started) * 1000.0, 2)
        sim_health = ComponentHealth(status="degraded", detail=str(exc.detail), latency_ms=sim_latency)
    except (httpx.HTTPError, ValidationError) as exc:
        sim_latency = round((time.perf_counter() - started) * 1000.0, 2)
        sim_health = ComponentHealth(status="error", detail=str(exc), latency_ms=sim_latency)

    sse_component = ComponentHealth(
        status="ok" if sse_state.status == "connected" else "degraded",
        detail=sse_state.last_error or f"SSE {sse_state.status} ({sse_state.events_received} events)",
    )

    ml_component = ComponentHealth(status="ok", detail="Disabled (core seasonal/OR-Tools mode)")
    ml_client: httpx.AsyncClient | None = getattr(request.app.state, "ml_client", None)
    if ml_client is not None:
        m_start = time.perf_counter()
        try:
            mr = await ml_client.get("/health/ready", timeout=1.0)
            m_lat = round((time.perf_counter() - m_start) * 1000.0, 2)
            mr_body = mr.json() if mr.headers.get("content-type", "").startswith("application/json") else {}
            c_state = str((mr_body.get("chronos2") or {}).get("status", "unknown"))
            i_state = str((mr_body.get("isolation_forest") or {}).get("status", "unknown"))
            both_loaded = mr.status_code == 200 and c_state == "loaded" and i_state == "loaded"
            ml_component = ComponentHealth(
                status="ok" if both_loaded else "degraded",
                detail=f"Chronos-2: {c_state}, Isolation Forest: {i_state}",
                latency_ms=m_lat,
            )
        except Exception as exc:
            ml_component = ComponentHealth(status="degraded", detail=f"ML optional service unreachable ({exc}); fallback ready")

    snap = metrics.snapshot(time.monotonic())
    components = {
        "backend_api": ComponentHealth(status="ok", detail="FastAPI serving requests"),
        "database": db_health,
        "simulator": sim_health,
        "sse_stream": sse_component,
        "prediction_service": ComponentHealth(
            status="ok" if sim_health.status == "ok" else "degraded",
            detail="Seasonal + optional Chronos-2 forecast with profile fallback",
        ),
        "decision_engine": ComponentHealth(
            status="ok" if sim_health.status == "ok" else "degraded",
            detail="OR-Tools constrained optimizer + safe reorder fallback",
        ),
        "ml_service": ml_component,
    }
    healthy = db_health.status == "ok" and sim_health.status == "ok"
    report = HealthReport(
        status="ok" if healthy else "degraded",
        timestamp=utc_now(),
        version=app.version,
        generation_id=gen.generation_id if gen else None,
        circuit_breaker_state=getattr(request.app.state, "circuit_state", "CLOSED"),
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
    gen: GenerationInfo | None = getattr(request.app.state, "generation", None)
    matured = request.app.state.store.matured_forecast_accuracy(gen.generation_id) if gen else None

    sim_data: dict[str, float | int] | None = None
    try:
        sim_metrics = await read(request.app.state.simulator, "/v1/metrics", SIMULATOR_METRICS, retries=0)
        sim_data = sim_metrics.model_dump()
    except (httpx.HTTPError, ValidationError, HTTPException):
        sim_data = None

    return MetricsReport(
        timestamp=utc_now(),
        generation_id=gen.generation_id if gen else None,
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
        cache_hits=int(snap["cache_hits"] or 0),
        cache_misses=int(snap["cache_misses"] or 0),
        cache_hit_ratio=snap["cache_hit_ratio"],
        solver_last_status=metrics.solver_last_status,
        solver_last_ms=metrics.solver_last_ms,
        forecast_source=metrics.forecast_source,
        fallback_reason=metrics.fallback_reason,
        last_recovery_seconds=metrics.last_recovery_seconds,
        matured_accuracy=matured,
        simulator=sim_data,
    )


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics(request: Request) -> Response:
    return Response(content=request.app.state.metrics.prom.render(), media_type="text/plain; version=0.0.4; charset=utf-8")
