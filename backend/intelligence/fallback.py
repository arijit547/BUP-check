from statistics import mean

from intelligence.collector import hour_factor
from intelligence.contracts import (
    FUELS,
    PROFILE_DAILY_LITERS,
    DemandObservation,
    Fuel,
    Recommendation,
    StationDetail,
)
from intelligence.shortage import classify_risk, compute_safety_stock_target, project
from schemas import Snapshot


def fallback_demand_rate(
    samples: list[tuple[int, float | DemandObservation]],
    station_detail: StationDetail | None,
    region_factor: float,
    fuel: Fuel,
    sim_hour: int,
    tick_minutes: int,
) -> float:
    """Compute cold-start / low-confidence rate using official simulator profile + hour + region + multiplier."""
    if samples:
        vals = [val.demand_liters if isinstance(val, DemandObservation) else float(val) for _, val in samples]
        base = mean(vals)
        multiplier = station_detail.demand_multiplier if station_detail else 1.0
        return round(base * max(1.0, multiplier), 3)
    profile = station_detail.demand_profile if station_detail else "urban_high"
    daily = PROFILE_DAILY_LITERS.get(profile, PROFILE_DAILY_LITERS["urban_high"])[fuel]
    ticks_per_day = max(1.0, (24 * 60) / max(1, tick_minutes))
    multiplier = station_detail.demand_multiplier if station_detail else 1.0
    rate = (daily / ticks_per_day) * region_factor * hour_factor(profile, sim_hour) * multiplier
    return round(rate, 3)


def _urgency_discount(unmet_before: list[float], unmet_after: list[float]) -> float:
    tau = 16.0
    score = 0.0
    for offset, (b, a) in enumerate(zip(unmet_before, unmet_after)):
        avoided = max(0.0, b - a)
        if avoided > 0:
            score += avoided / (1.0 + offset / tau)
    return round(score, 3)


def build_safe_reorder_recommendations(
    *,
    snapshot: Snapshot,
    incoming: dict[tuple[str, Fuel], list[float]],
    dispatch_used: dict[str, float],
    demand_series_by_key: dict[tuple[str, Fuel], list[float]],
    std_by_key: dict[tuple[str, Fuel], float],
    plan_id: str,
    generation_id: str,
    reason: str,
    single_action_limit: int | None = None,
    max_total_liters: float | None = None,
) -> list[Recommendation]:
    """Safe constrained reorder baseline / fallback honoring depot stock, dispatch, route max, and storage limits."""
    tick = snapshot.simulation.tick
    tick_minutes = snapshot.simulation.tick_minutes
    horizon_ticks = len(next(iter(incoming.values()))) if incoming else 96
    depots = {d.id: d for d in snapshot.depots}
    depot_stock = {(d.id, f): float(getattr(d.inventory, f)) for d in snapshot.depots for f in FUELS}
    depot_dispatch_left = {
        d.id: max(0.0, float(d.dispatch_capacity_per_tick) - dispatch_used.get(d.id, 0.0))
        for d in snapshot.depots
    }
    station_room = {
        (s.id, f): max(0.0, float(getattr(s.capacity, f)) - float(getattr(s.inventory, f)))
        for s in snapshot.stations
        for f in FUELS
    }

    candidates = []
    routes_by_station: dict[str, list] = {}
    for route in snapshot.routes:
        if route.status == "AVAILABLE" and route.transit_ticks < horizon_ticks:
            routes_by_station.setdefault(route.destination_station_id, []).append(route)

    for station in snapshot.stations:
        if station.status != "OPEN":
            continue
        st_routes = routes_by_station.get(station.id, [])
        if not st_routes:
            continue
        min_lead = min(r.transit_ticks for r in st_routes)
        for fuel in FUELS:
            key = (station.id, fuel)
            stock = float(getattr(station.inventory, fuel))
            capacity = float(getattr(station.capacity, fuel))
            if capacity <= 0:
                continue
            arrivals = incoming.get(key, [0.0] * horizon_ticks)
            series = demand_series_by_key.get(key, [0.0] * horizon_ticks)
            d_std = std_by_key.get(key, 0.0)
            target = max(
                capacity * 0.60,
                compute_safety_stock_target(
                    capacity=capacity,
                    demand_series=series,
                    demand_std=d_std,
                    min_lead_ticks=min_lead,
                    cadence_ticks=6,
                    station_open=True,
                ),
            )
            inv_position = stock + sum(arrivals)
            ratio = inv_position / capacity
            depleted, unmet_before, room = project(stock, capacity, series, arrivals, True)
            if ratio >= 0.38 and depleted is None and inv_position >= target * 0.85:
                continue
            for route in st_routes:
                depot = depots.get(route.source_depot_id)
                if depot is None or depot.status not in ("OPEN", "CONSTRAINED"):
                    continue
                eta = route.transit_ticks
                desired = max(0.0, target - stock)
                candidates.append((ratio, eta, route.id, station, depot, fuel, route, desired, arrivals, series, unmet_before, room, depleted, target))

    candidates.sort(key=lambda item: (item[0], item[1], item[2], item[5]))
    desired_left: dict[tuple[str, Fuel], float] = {}
    liters_used = 0.0
    recs: list[Recommendation] = []
    for ratio, eta, _rid, station, depot, fuel, route, desired, arrivals, series, unmet_before, room, depleted, target in candidates:
        if single_action_limit is not None and len(recs) >= single_action_limit:
            break
        key = (station.id, fuel)
        rem_desired = desired_left.setdefault(key, desired)
        budget_rem = (max_total_liters - liters_used) if max_total_liters is not None else float("inf")
        capacity = float(getattr(station.capacity, fuel))
        stock = float(getattr(station.inventory, fuel))
        limit = min(
            rem_desired,
            budget_rem,
            depot_stock[(depot.id, fuel)],
            depot_dispatch_left[depot.id],
            route.max_shipment,
            station_room[key],
            room[eta],
        )
        quantity = int(max(0.0, limit) * 1000) / 1000
        if quantity < 1.0:
            continue
        desired_left[key] = max(0.0, rem_desired - quantity)
        liters_used += quantity
        depot_stock[(depot.id, fuel)] -= quantity
        depot_dispatch_left[depot.id] -= quantity
        station_room[key] -= quantity

        with_ship = arrivals.copy()
        with_ship[eta] += quantity
        after_depleted, unmet_after, _ = project(stock, capacity, series, with_ship, True)
        base_unmet = round(sum(unmet_before), 3)
        after_unmet = round(sum(unmet_after), 3)
        avoided = round(max(0.0, base_unmet - after_unmet), 3)
        risk_b = classify_risk(stock, capacity, depleted, base_unmet, safety_target=target)
        risk_a = classify_risk(min(capacity, stock + quantity), capacity, after_depleted, after_unmet, safety_target=target)
        score = _urgency_discount(unmet_before, unmet_after) + max(0.0, (0.60 - ratio) * 1000.0)
        alternatives = [r.id for r in routes_by_station.get(station.id, []) if r.id != route.id]
        recs.append(
            Recommendation(
                plan_id=plan_id,
                generation_id=generation_id,
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
                unmet_before_liters=base_unmet,
                unmet_after_liters=after_unmet,
                unmet_avoided_liters=avoided,
                policy_mode="REORDER_FALLBACK" if "fallback" in reason.lower() or "insufficient" in reason.lower() else "SAFE_REORDER",
                priority_score=round(score, 3),
                stockout_tick_before=tick + depleted if depleted is not None else None,
                stockout_tick_after=tick + after_depleted if after_depleted is not None else None,
                risk_before=risk_b,
                risk_after=risk_a,
                confidence="LOW" if "insufficient" in reason.lower() else "MEDIUM",
                rationale=(
                    f"{reason} Preventive/safety replenishment of {quantity:,.0f} L toward {target:,.0f} L target; "
                    f"arrives tick {tick + eta} ({eta * tick_minutes} min)."
                ),
                signals=[
                    f"Inventory position ratio {ratio * 100:.1f}% vs {target:,.0f} L safety target",
                    f"Expected demand rate {mean(series):.1f} L/tick",
                ],
                constraints=[
                    f"Route max {route.max_shipment:,.0f} L",
                    f"Depot stock {getattr(depot.inventory, fuel):,.0f} L",
                    f"Station immediate room {getattr(station.capacity, fuel) - stock:,.0f} L",
                ],
                alternative_routes=alternatives,
            )
        )
    return recs
