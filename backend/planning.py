from collections import defaultdict
from datetime import timedelta
import math
from statistics import mean, pstdev
from typing import Any, Literal

from pydantic import Field

from schemas import FuelVolumes, Identifier, Liters, Snapshot, SnapshotModel

Fuel = Literal["DIESEL", "PETROL", "OCTANE"]
FUELS: tuple[Fuel, ...] = ("DIESEL", "PETROL", "OCTANE")
RiskLevel = Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]
ForecastRisk = Literal["CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"]
ConfidenceLevel = Literal["HIGH", "MEDIUM", "LOW"]

# Documented daily demand profiles (liters/day) from simulator guide §8.5 for tick-0 fallback.
PROFILE_DAILY_LITERS: dict[str, dict[Fuel, float]] = {
    "urban_high": {"DIESEL": 8500.0, "PETROL": 10500.0, "OCTANE": 5600.0},
    "industrial": {"DIESEL": 14000.0, "PETROL": 4500.0, "OCTANE": 2200.0},
    "highway": {"DIESEL": 10500.0, "PETROL": 11000.0, "OCTANE": 6200.0},
    "regional": {"DIESEL": 7200.0, "PETROL": 7600.0, "OCTANE": 3600.0},
}


class DemandObservation(SnapshotModel):
    station_id: Identifier
    fuel_type: Fuel
    tick: int = Field(ge=0)
    demand_liters: Liters


class Allocation(SnapshotModel):
    source_depot_id: Identifier
    destination_station_id: Identifier
    route_id: Identifier | None = None
    fuel_type: Fuel
    quantity: Liters
    created_tick: int = Field(ge=0)
    expected_arrival_tick: int | None = Field(default=None, ge=0)
    status: Literal["PENDING", "IN_TRANSIT", "ARRIVED", "FAILED", "CANCELLED"]


class StationDetail(SnapshotModel):
    id: Identifier
    region_id: Identifier
    demand_profile: str = "urban_high"
    demand_multiplier: float = Field(default=1.0, gt=0)


class Region(SnapshotModel):
    id: Identifier
    name: str
    demand_factor: float = Field(gt=0)


class SupplyArrival(SnapshotModel):
    id: Identifier
    depot_id: Identifier
    fuel_type: Fuel
    quantity: Liters
    planned_tick: int = Field(ge=0)
    actual_tick: int | None = Field(default=None, ge=0)
    status: Literal["SCHEDULED", "DELAYED", "ARRIVED"]


class SimulatorEvent(SnapshotModel):
    id: int
    type: str
    start_tick: int = Field(ge=0)
    end_tick: int = Field(ge=0)
    status: Literal["SCHEDULED", "ACTIVE", "RESOLVED"]
    parameters: dict[str, Any] = Field(default_factory=dict)


class SimulatorMetrics(SnapshotModel):
    served_demand_liters: Liters
    unmet_demand_liters: Liters
    service_level: float = Field(ge=0)
    allocation_liters: Liters
    allocation_failures: int = Field(ge=0)


class Anomaly(SnapshotModel):
    id: str
    category: Literal[
        "DEMAND_SPIKE",
        "DEMAND_DROP",
        "STOCKOUT_RISK",
        "ROUTE_DISRUPTED",
        "STATION_OUTAGE",
        "DEPOT_CONSTRAINED",
        "DISPATCH_BOTTLENECK",
        "SUPPLY_DELAYED",
    ]
    severity: Literal["CRITICAL", "WARNING"]
    entity_type: Literal["station", "depot", "route", "supply"]
    entity_id: Identifier
    fuel_type: Fuel | None = None
    summary: str
    observed_value: float | None = None
    baseline_value: float | None = None
    z_score: float | None = None


class Forecast(SnapshotModel):
    station_id: Identifier
    station_name: str
    fuel_type: Fuel
    observations: int
    first_history_tick: int | None
    last_history_tick: int | None
    demand_liters_per_tick: Liters | None
    stockout_tick: int | None
    unmet_liters: Liters | None
    projected_demand_liters_per_tick: Liters | None = None
    fallback_demand_liters_per_tick: Liters | None = None
    demand_std_liters: Liters | None = None
    confidence: ConfidenceLevel = "LOW"
    confidence_score: float = 0.0
    shortage_risk: ForecastRisk = "UNKNOWN"
    stockout_probability: float = 0.0
    fallback_used: bool = False


class Recommendation(SnapshotModel):
    route_id: Identifier
    source_depot_id: Identifier
    source_depot_name: str
    destination_station_id: Identifier
    destination_station_name: str
    fuel_type: Fuel
    quantity_liters: Liters
    arrival_tick: int
    eta_ticks: int
    eta_minutes: int
    unmet_before_liters: Liters
    unmet_after_liters: Liters
    unmet_avoided_liters: Liters
    policy_mode: Literal["FORECAST_OPTIMIZER", "REORDER_FALLBACK"] = "FORECAST_OPTIMIZER"
    priority_score: float = 0.0
    stockout_tick_before: int | None = None
    stockout_tick_after: int | None = None
    risk_before: RiskLevel = "MEDIUM"
    risk_after: RiskLevel = "LOW"
    confidence: ConfidenceLevel = "MEDIUM"
    rationale: str = ""
    signals: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    alternative_routes: list[str] = Field(default_factory=list)


class PlanningReport(SnapshotModel):
    tick: int
    history_ticks: int
    horizon_ticks: int
    forecasts: list[Forecast]
    recommendations: list[Recommendation]
    policy_mode: Literal["FORECAST_OPTIMIZER", "REORDER_FALLBACK"] = "FORECAST_OPTIMIZER"
    fallback_active: bool = False
    anomalies: list[Anomaly] = Field(default_factory=list)
    events: list[SimulatorEvent] = Field(default_factory=list)
    supply_arrivals: list[SupplyArrival] = Field(default_factory=list)
    simulator_metrics: SimulatorMetrics | None = None


def hour_factor(profile: str, hour: int) -> float:
    """Return documented simulator hour-of-day factor (§8.6)."""
    if profile == "industrial":
        return 1.55 if 6 <= hour <= 17 else 0.45
    if profile == "highway":
        return 1.35 if (6 <= hour <= 9 or 16 <= hour <= 20) else 0.75
    if profile == "urban_high":
        return 1.45 if (7 <= hour <= 9 or 16 <= hour <= 20) else 0.70
    if profile == "regional":
        return 1.25 if 7 <= hour <= 20 else 0.65
    return 1.0


def spike_multiplier_at(
    events: list[SimulatorEvent],
    station_id: str,
    region_id: str,
    target_tick: int,
) -> float:
    """Return combined demand_spike multiplier active at target_tick."""
    multiplier = 1.0
    for event in events:
        if event.type != "demand_spike":
            continue
        if not (event.start_tick <= target_tick < event.end_tick):
            continue
        params = event.parameters or {}
        station_ids = params.get("station_ids") or []
        region_ids = params.get("region_ids") or []
        applies_station = not station_ids or station_id in station_ids
        applies_region = not region_ids or region_id in region_ids
        if applies_station and applies_region:
            factor = params.get("multiplier", 1.5)
            if isinstance(factor, (int, float)) and factor > 0:
                multiplier *= float(factor)
    return multiplier


def project(
    stock: float,
    capacity: float,
    demand: float | list[float],
    arrivals: list[float],
    station_open: bool,
) -> tuple[int | None, list[float], list[float]]:
    """Return the first depleted offset, unmet demand, and room before demand."""
    demands = [demand] * len(arrivals) if isinstance(demand, (int, float)) else demand
    depleted = None
    unmet: list[float] = []
    room: list[float] = []
    for offset, arriving in enumerate(arrivals):
        tick_demand = demands[offset]
        stock += min(arriving, max(0.0, capacity - stock))
        room.append(max(0.0, capacity - stock))
        served = min(stock, tick_demand) if station_open else 0.0
        unmet.append(tick_demand - served)
        stock -= served
        if stock == 0 and tick_demand > 0 and depleted is None:
            depleted = offset
    return depleted, unmet, room


def classify_risk(
    stock: float,
    capacity: float,
    depleted_offset: int | None,
    unmet_total: float,
) -> RiskLevel:
    ratio = stock / capacity if capacity > 0 else 1.0
    if (depleted_offset is not None and depleted_offset <= 12) or ratio < 0.20:
        return "CRITICAL"
    if (depleted_offset is not None and depleted_offset <= 32) or ratio < 0.35:
        return "HIGH"
    if depleted_offset is not None or unmet_total > 0 or ratio < 0.55:
        return "MEDIUM"
    return "LOW"


def stockout_probability(
    stock: float,
    demand_series: list[float],
    demand_std: float,
    arrivals: list[float],
    depleted_offset: int | None,
    station_open: bool,
) -> float:
    if not station_open:
        return 0.0
    total_arrivals = sum(arrivals)
    expected_demand = sum(demand_series)
    horizon_std = max(1e-6, demand_std * math.sqrt(max(1, len(demand_series))))
    net_buffer = stock + total_arrivals - expected_demand
    z = net_buffer / horizon_std
    prob = 1.0 / (1.0 + math.exp(max(-20.0, min(20.0, z))))
    if depleted_offset is not None:
        prob = max(prob, 0.75 if depleted_offset <= 16 else 0.55)
    return round(max(0.0, min(1.0, prob)), 3)


def confidence_for_window(
    observations: int,
    history_ticks: int,
    values: list[float],
) -> tuple[ConfidenceLevel, float, float]:
    if not values:
        return "LOW", 0.2, 0.0
    std = pstdev(values) if len(values) > 1 else 0.0
    avg = mean(values)
    cv = std / avg if avg > 0 else 0.0
    coverage = min(1.0, observations / max(1, history_ticks))
    score = round(max(0.1, min(0.99, coverage * (1.0 - min(0.6, cv)))), 3)
    if observations >= history_ticks and score >= 0.72:
        return "HIGH", score, round(std, 3)
    if observations >= max(2, history_ticks // 2) and score >= 0.45:
        return "MEDIUM", score, round(std, 3)
    return "LOW", score, round(std, 3)


def fallback_demand_rate(
    samples: list[tuple[int, float]],
    station_detail: StationDetail | None,
    region_factor: float,
    fuel: Fuel,
    sim_hour: int,
    tick_minutes: int,
) -> float:
    if samples:
        base = mean(liters for _, liters in samples)
        multiplier = station_detail.demand_multiplier if station_detail else 1.0
        return round(base * max(1.0, multiplier), 3)
    profile = station_detail.demand_profile if station_detail else "urban_high"
    daily = PROFILE_DAILY_LITERS.get(profile, PROFILE_DAILY_LITERS["urban_high"])[fuel]
    ticks_per_day = max(1.0, (24 * 60) / max(1, tick_minutes))
    multiplier = station_detail.demand_multiplier if station_detail else 1.0
    rate = (daily / ticks_per_day) * region_factor * hour_factor(profile, sim_hour) * multiplier
    return round(rate, 3)


def build_demand_series(
    samples: list[tuple[int, float]],
    tick: int,
    horizon_ticks: int,
    tick_minutes: int,
    sim_time: Any,
    station_id: str,
    region_id: str,
    station_detail: StationDetail | None,
    events: list[SimulatorEvent],
) -> tuple[float, list[float]]:
    """Return raw rolling mean and spike/seasonality-adjusted horizon demand series."""
    rolling_mean = mean(liters for _, liters in samples)
    profile = station_detail.demand_profile if station_detail else "urban_high"

    # Normalize each historical tick by its hour factor and active spike multiplier.
    normalized: list[float] = []
    for sample_tick, liters in samples:
        sample_time = sim_time - timedelta(minutes=(tick - sample_tick) * tick_minutes)
        h_factor = hour_factor(profile, sample_time.hour)
        s_factor = spike_multiplier_at(events, station_id, region_id, sample_tick)
        normalized.append(liters / max(0.01, h_factor * s_factor))

    base_rate = mean(normalized) if normalized else rolling_mean
    current_mult = station_detail.demand_multiplier if station_detail else 1.0

    series: list[float] = []
    for offset in range(horizon_ticks):
        future_tick = tick + offset
        future_time = sim_time + timedelta(minutes=offset * tick_minutes)
        h_factor = hour_factor(profile, future_time.hour)
        s_factor = spike_multiplier_at(events, station_id, region_id, future_tick)
        if offset == 0 and s_factor == 1.0 and current_mult != 1.0:
            s_factor = current_mult
        series.append(round(base_rate * h_factor * s_factor, 3))
    return rolling_mean, series


def detect_anomalies(
    snapshot: Snapshot,
    observed: dict[tuple[str, Fuel], dict[int, float]],
    station_details: dict[str, StationDetail],
    forecasts: list[Forecast],
    dispatch_used: dict[str, float],
    supply_arrivals: list[SupplyArrival],
    events: list[SimulatorEvent],
) -> list[Anomaly]:
    anomalies: list[Anomaly] = []
    tick = snapshot.simulation.tick
    station_names = {station.id: station.name for station in snapshot.stations}
    depot_names = {depot.id: depot.name for depot in snapshot.depots}

    for station in snapshot.stations:
        if station.status == "OUTAGE":
            anomalies.append(Anomaly(
                id=f"outage-{station.id}",
                category="STATION_OUTAGE",
                severity="CRITICAL",
                entity_type="station",
                entity_id=station.id,
                summary=f"{station.name} is in OUTAGE; demand is not being served.",
            ))
        detail = station_details.get(station.id)
        for fuel in FUELS:
            samples = sorted(observed[station.id, fuel].items())
            if len(samples) >= 4:
                latest_tick, latest = samples[-1]
                prior = [val for _, val in samples[:-1]]
                base = mean(prior)
                std = pstdev(prior) if len(prior) > 1 else 0.0
                z = (latest - base) / std if std > 1e-6 else None
                ratio = latest / base if base > 0 else 1.0
                mult = detail.demand_multiplier if detail else 1.0
                if (latest_tick == tick - 1) and ((z is not None and z >= 2.0) or ratio >= 1.35 or mult >= 1.25):
                    severity = "CRITICAL" if ((z is not None and z >= 2.8) or ratio >= 1.6 or mult >= 1.5) else "WARNING"
                    anomalies.append(Anomaly(
                        id=f"spike-{station.id}-{fuel}",
                        category="DEMAND_SPIKE",
                        severity=severity,
                        entity_type="station",
                        entity_id=station.id,
                        fuel_type=fuel,
                        summary=(
                            f"{station.name} {fuel} demand spiked to {latest:.1f} L/tick "
                            f"vs {base:.1f} L baseline (multiplier {mult:.2f}x)."
                        ),
                        observed_value=round(latest, 3),
                        baseline_value=round(base, 3),
                        z_score=round(z, 2) if z is not None else None,
                    ))
                elif (latest_tick == tick - 1) and ((z is not None and z <= -2.2) or ratio <= 0.45):
                    anomalies.append(Anomaly(
                        id=f"drop-{station.id}-{fuel}",
                        category="DEMAND_DROP",
                        severity="WARNING",
                        entity_type="station",
                        entity_id=station.id,
                        fuel_type=fuel,
                        summary=f"{station.name} {fuel} demand dropped sharply to {latest:.1f} L/tick vs {base:.1f} L.",
                        observed_value=round(latest, 3),
                        baseline_value=round(base, 3),
                        z_score=round(z, 2) if z is not None else None,
                    ))

    for forecast in forecasts:
        if forecast.shortage_risk in ("CRITICAL", "HIGH"):
            eta_msg = (
                f"projected stockout at tick {forecast.stockout_tick}"
                if forecast.stockout_tick is not None
                else "low inventory reserve"
            )
            anomalies.append(Anomaly(
                id=f"risk-{forecast.station_id}-{forecast.fuel_type}",
                category="STOCKOUT_RISK",
                severity="CRITICAL" if forecast.shortage_risk == "CRITICAL" else "WARNING",
                entity_type="station",
                entity_id=forecast.station_id,
                fuel_type=forecast.fuel_type,
                summary=f"{forecast.station_name} {forecast.fuel_type}: {eta_msg} ({forecast.shortage_risk}).",
                observed_value=forecast.unmet_liters,
            ))

    for route in snapshot.routes:
        if route.status == "DISRUPTED":
            dest_name = station_names.get(route.destination_station_id, route.destination_station_id)
            anomalies.append(Anomaly(
                id=f"route-{route.id}",
                category="ROUTE_DISRUPTED",
                severity="CRITICAL",
                entity_type="route",
                entity_id=route.id,
                summary=f"{route.id} to {dest_name} is DISRUPTED; rerouting required.",
            ))

    for depot in snapshot.depots:
        if depot.status == "CONSTRAINED":
            anomalies.append(Anomaly(
                id=f"depot-{depot.id}",
                category="DEPOT_CONSTRAINED",
                severity="WARNING",
                entity_type="depot",
                entity_id=depot.id,
                summary=f"{depot.name} is CONSTRAINED; monitor dispatch limits and resupplies.",
            ))
        if depot.dispatch_capacity_per_tick > 0:
            utilization = dispatch_used[depot.id] / depot.dispatch_capacity_per_tick
            if utilization >= 0.75:
                anomalies.append(Anomaly(
                    id=f"dispatch-{depot.id}",
                    category="DISPATCH_BOTTLENECK",
                    severity="CRITICAL" if utilization >= 0.95 else "WARNING",
                    entity_type="depot",
                    entity_id=depot.id,
                    summary=(
                        f"{depot.name} dispatch capacity {utilization * 100:.0f}% utilized "
                        f"({dispatch_used[depot.id]:.0f}/{depot.dispatch_capacity_per_tick:.0f} L)."
                    ),
                    observed_value=round(dispatch_used[depot.id], 3),
                    baseline_value=depot.dispatch_capacity_per_tick,
                ))

    for arrival in supply_arrivals:
        if arrival.status == "DELAYED":
            depot_name = depot_names.get(arrival.depot_id, arrival.depot_id)
            anomalies.append(Anomaly(
                id=f"supply-{arrival.id}",
                category="SUPPLY_DELAYED",
                severity="WARNING",
                entity_type="supply",
                entity_id=arrival.id,
                fuel_type=arrival.fuel_type,
                summary=(
                    f"Incoming {arrival.quantity:.0f} L {arrival.fuel_type} to {depot_name} "
                    f"delayed (planned tick {arrival.planned_tick})."
                ),
                observed_value=float(arrival.planned_tick),
            ))

    # Deduplicate by id, critical first.
    anomalies.sort(key=lambda item: (0 if item.severity == "CRITICAL" else 1, item.id))
    return anomalies


def urgency_score(unmet_before: list[float], unmet_after: list[float]) -> float:
    """Discount future unmet demand avoided so imminent stockouts outrank distant ones."""
    tau = 16.0
    score = 0.0
    for offset, (before_val, after_val) in enumerate(zip(unmet_before, unmet_after)):
        avoided = max(0.0, before_val - after_val)
        if avoided > 0:
            score += avoided / (1.0 + offset / tau)
    return round(score, 3)


def build_report(
    snapshot: Snapshot,
    history: list[DemandObservation],
    allocations: list[Allocation],
    history_ticks: int,
    horizon_ticks: int,
    *,
    station_details: list[StationDetail] | None = None,
    regions: list[Region] | None = None,
    supply_arrivals: list[SupplyArrival] | None = None,
    events: list[SimulatorEvent] | None = None,
    simulator_metrics: SimulatorMetrics | None = None,
) -> PlanningReport:
    tick = snapshot.simulation.tick
    sim_time = snapshot.simulation.sim_time
    tick_minutes = snapshot.simulation.tick_minutes
    details_map = {item.id: item for item in (station_details or [])}
    region_factors = {item.id: item.demand_factor for item in (regions or [])}
    event_list = events or []
    arrival_list = supply_arrivals or []

    observed: dict[tuple[str, Fuel], dict[int, float]] = defaultdict(dict)
    for entry in history:
        if tick - history_ticks <= entry.tick < tick:
            observed[entry.station_id, entry.fuel_type][entry.tick] = entry.demand_liters

    routes_by_id = {route.id: route for route in snapshot.routes}
    incoming = defaultdict(lambda: [0.0] * horizon_ticks)
    dispatch_used: dict[str, float] = defaultdict(float)
    for allocation in allocations:
        if allocation.status in ("PENDING", "IN_TRANSIT") and allocation.created_tick == tick:
            dispatch_used[allocation.source_depot_id] += allocation.quantity
        eta = allocation.expected_arrival_tick
        if allocation.status == "IN_TRANSIT" and eta is not None and tick <= eta < tick + horizon_ticks:
            incoming[allocation.destination_station_id, allocation.fuel_type][eta - tick] += allocation.quantity
        elif allocation.status == "PENDING" and allocation.route_id and allocation.route_id in routes_by_id:
            pending_eta = routes_by_id[allocation.route_id].transit_ticks
            if pending_eta < horizon_ticks:
                incoming[allocation.destination_station_id, allocation.fuel_type][pending_eta] += allocation.quantity

    depots = {depot.id: depot for depot in snapshot.depots}
    routes_by_station = defaultdict(list)
    for route in snapshot.routes:
        if route.status == "AVAILABLE" and route.transit_ticks < horizon_ticks:
            routes_by_station[route.destination_station_id].append(route)

    forecasts: list[Forecast] = []
    recommendations: list[Recommendation] = []
    fallback_active = False

    for station in snapshot.stations:
        detail = details_map.get(station.id)
        region_factor = region_factors.get(station.region_id, 1.0)
        station_routes = routes_by_station[station.id]
        for fuel in FUELS:
            key = station.id, fuel
            samples = sorted(observed[key].items())
            values = [liters for _, liters in samples]
            stock = getattr(station.inventory, fuel)
            capacity = getattr(station.capacity, fuel)
            arrivals = incoming[key]
            station_open = station.status == "OPEN"
            conf_level, conf_score, demand_std = confidence_for_window(len(samples), history_ticks, values)

            if len(samples) < history_ticks:
                fallback_active = True
                fb_rate = fallback_demand_rate(samples, detail, region_factor, fuel, sim_time.hour, tick_minutes)
                fb_depleted, fb_unmet, fb_room = project(stock, capacity, fb_rate, arrivals, station_open)
                fb_baseline = round(sum(fb_unmet), 3)
                fb_risk = classify_risk(stock, capacity, fb_depleted, fb_baseline)
                fb_prob = stockout_probability(
                    stock, [fb_rate] * horizon_ticks, demand_std or fb_rate * 0.12, arrivals, fb_depleted, station_open,
                )
                forecasts.append(Forecast(
                    station_id=station.id,
                    station_name=station.name,
                    fuel_type=fuel,
                    observations=len(samples),
                    first_history_tick=samples[0][0] if samples else None,
                    last_history_tick=samples[-1][0] if samples else None,
                    demand_liters_per_tick=None,
                    stockout_tick=None,
                    unmet_liters=None,
                    projected_demand_liters_per_tick=None,
                    fallback_demand_liters_per_tick=fb_rate,
                    demand_std_liters=demand_std if samples else None,
                    confidence="LOW",
                    confidence_score=conf_score,
                    shortage_risk=fb_risk,
                    stockout_probability=fb_prob,
                    fallback_used=True,
                ))

                ratio = stock / capacity if capacity > 0 else 1.0
                if station_open and ratio < 0.35:
                    target_fill = max(0.0, capacity * 0.6 - stock)
                    for route in station_routes:
                        depot = depots.get(route.source_depot_id)
                        if depot is None:
                            continue
                        eta = route.transit_ticks
                        limit = min(
                            getattr(depot.inventory, fuel),
                            route.max_shipment,
                            depot.dispatch_capacity_per_tick - dispatch_used[depot.id],
                            capacity - stock,
                            fb_room[eta],
                            target_fill,
                        )
                        quantity = int(max(0.0, limit) * 1000) / 1000
                        if quantity <= 0:
                            continue
                        with_shipment = arrivals.copy()
                        with_shipment[eta] += quantity
                        after_depleted, remaining_unmet, _ = project(
                            stock, capacity, fb_rate, with_shipment, station_open,
                        )
                        after = round(sum(remaining_unmet), 3)
                        avoided = round(max(0.0, fb_baseline - after), 3)
                        after_risk = classify_risk(
                            min(capacity, stock + quantity), capacity, after_depleted, after,
                        )
                        score = urgency_score(fb_unmet, remaining_unmet) + (1000.0 * (0.35 - ratio))
                        alternatives = [r.id for r in station_routes if r.id != route.id]
                        recommendations.append(Recommendation(
                            route_id=route.id,
                            source_depot_id=depot.id,
                            source_depot_name=depot.name,
                            destination_station_id=station.id,
                            destination_station_name=station.name,
                            fuel_type=fuel,
                            quantity_liters=quantity,
                            arrival_tick=tick + eta,
                            eta_ticks=eta,
                            eta_minutes=eta * tick_minutes,
                            unmet_before_liters=fb_baseline,
                            unmet_after_liters=after,
                            unmet_avoided_liters=avoided,
                            policy_mode="REORDER_FALLBACK",
                            priority_score=round(score, 3),
                            stockout_tick_before=tick + fb_depleted if fb_depleted is not None else None,
                            stockout_tick_after=tick + after_depleted if after_depleted is not None else None,
                            risk_before=fb_risk,
                            risk_after=after_risk,
                            confidence="LOW",
                            rationale=(
                                f"History incomplete ({len(samples)}/{history_ticks}); safety reorder fallback "
                                f"triggered at {ratio * 100:.0f}% stock toward 60% target."
                            ),
                            signals=[
                                f"Fallback safety rule: {ratio * 100:.0f}% stocked (< 35%)",
                                f"Profile baseline estimate {fb_rate:.1f} L/tick",
                            ],
                            constraints=[
                                f"Route limit {route.max_shipment:,.0f} L",
                                f"Depot stock {getattr(depot.inventory, fuel):,.0f} L",
                            ],
                            alternative_routes=alternatives,
                        ))
                continue

            rolling_mean, demand_series = build_demand_series(
                samples, tick, horizon_ticks, tick_minutes, sim_time,
                station.id, station.region_id, detail, event_list,
            )
            depleted, unmet, room = project(stock, capacity, demand_series, arrivals, station_open)
            baseline = round(sum(unmet), 3)
            projected_avg = round(mean(demand_series), 3)
            risk = classify_risk(stock, capacity, depleted, baseline)
            prob = stockout_probability(stock, demand_series, demand_std, arrivals, depleted, station_open)
            stockout_tick = tick + depleted if depleted is not None else None

            forecasts.append(Forecast(
                station_id=station.id,
                station_name=station.name,
                fuel_type=fuel,
                observations=len(samples),
                first_history_tick=samples[0][0],
                last_history_tick=samples[-1][0],
                demand_liters_per_tick=rolling_mean,
                stockout_tick=stockout_tick,
                unmet_liters=baseline,
                projected_demand_liters_per_tick=projected_avg,
                fallback_demand_liters_per_tick=None,
                demand_std_liters=demand_std,
                confidence=conf_level,
                confidence_score=conf_score,
                shortage_risk=risk,
                stockout_probability=prob,
                fallback_used=False,
            ))
            if not station_open or baseline <= 0:
                continue

            for route in station_routes:
                depot = depots.get(route.source_depot_id)
                if depot is None:
                    continue
                eta = route.transit_ticks
                limit = min(
                    getattr(depot.inventory, fuel),
                    route.max_shipment,
                    depot.dispatch_capacity_per_tick - dispatch_used[depot.id],
                    capacity - stock,
                    room[eta],
                    sum(unmet[eta:]),
                )
                # Floor to the simulator's inventory precision, within every limit.
                quantity = int(max(0.0, limit) * 1000) / 1000
                if quantity == 0:
                    continue
                with_shipment = arrivals.copy()
                with_shipment[eta] += quantity
                after_depleted, remaining_unmet, _ = project(
                    stock, capacity, demand_series, with_shipment, station_open,
                )
                after = round(sum(remaining_unmet), 3)
                avoided = round(baseline - after, 3)
                if avoided <= 0:
                    continue
                after_stockout = tick + after_depleted if after_depleted is not None else None
                after_risk = classify_risk(min(capacity, stock + quantity), capacity, after_depleted, after)
                score = urgency_score(unmet, remaining_unmet)
                alternatives = [r.id for r in station_routes if r.id != route.id]
                signals = [
                    f"{history_ticks}-tick rolling mean {rolling_mean:.1f} L/tick (projected {projected_avg:.1f} L/tick)",
                    f"Stockout projected at tick {stockout_tick}" if stockout_tick is not None else "No full stockout in horizon",
                ]
                if detail and detail.demand_multiplier != 1.0:
                    signals.append(f"Active demand multiplier {detail.demand_multiplier:.2f}x")
                constraints = [
                    f"Route max {route.max_shipment:,.0f} L",
                    f"Depot dispatch remaining {depot.dispatch_capacity_per_tick - dispatch_used[depot.id]:,.0f} L",
                    f"Station room at arrival {room[eta]:,.0f} L",
                ]
                recommendations.append(Recommendation(
                    route_id=route.id,
                    source_depot_id=depot.id,
                    source_depot_name=depot.name,
                    destination_station_id=station.id,
                    destination_station_name=station.name,
                    fuel_type=fuel,
                    quantity_liters=quantity,
                    arrival_tick=tick + eta,
                    eta_ticks=eta,
                    eta_minutes=eta * tick_minutes,
                    unmet_before_liters=baseline,
                    unmet_after_liters=after,
                    unmet_avoided_liters=avoided,
                    policy_mode="FORECAST_OPTIMIZER",
                    priority_score=score,
                    stockout_tick_before=stockout_tick,
                    stockout_tick_after=after_stockout,
                    risk_before=risk,
                    risk_after=after_risk,
                    confidence=conf_level,
                    rationale=(
                        f"Avoids {avoided:,.1f} L unmet demand ({risk} → {after_risk} risk); "
                        f"arrives at tick {tick + eta} ({eta * tick_minutes} min)."
                    ),
                    signals=signals,
                    constraints=constraints,
                    alternative_routes=alternatives,
                ))

    recommendations.sort(
        key=lambda item: (-item.priority_score, -item.unmet_avoided_liters, item.eta_ticks, item.route_id, item.fuel_type),
    )
    anomalies = detect_anomalies(
        snapshot, observed, details_map, forecasts, dispatch_used, arrival_list, event_list,
    )
    mode: Literal["FORECAST_OPTIMIZER", "REORDER_FALLBACK"] = (
        "REORDER_FALLBACK" if fallback_active and all(f.demand_liters_per_tick is None for f in forecasts) else "FORECAST_OPTIMIZER"
    )
    return PlanningReport(
        tick=tick,
        history_ticks=history_ticks,
        horizon_ticks=horizon_ticks,
        forecasts=forecasts,
        recommendations=recommendations,
        policy_mode=mode,
        fallback_active=fallback_active,
        anomalies=anomalies,
        events=event_list,
        supply_arrivals=arrival_list,
        simulator_metrics=simulator_metrics,
    )
