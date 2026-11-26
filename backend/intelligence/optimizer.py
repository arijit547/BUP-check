from dataclasses import dataclass
import hashlib
import math
from statistics import mean
import time
from uuid import uuid4

from ortools.linear_solver import pywraplp

from intelligence.contracts import (
    FUELS,
    ConfidenceLevel,
    Forecast,
    ForecastSource,
    Fuel,
    PlanPreviewSummary,
    PolicyMode,
    Recommendation,
    StationDetail,
)
from intelligence.explain import build_recommendation_explanation
from intelligence.fallback import build_safe_reorder_recommendations
from intelligence.shortage import classify_risk, compute_safety_stock_target, project
from schemas import Route, Snapshot


def urgency_score(unmet_before: list[float], unmet_after: list[float]) -> float:
    """Discount future unmet demand avoided so imminent stockouts outrank distant ones."""
    tau = 16.0
    score = 0.0
    for offset, (before_val, after_val) in enumerate(zip(unmet_before, unmet_after)):
        avoided = max(0.0, before_val - after_val)
        if avoided > 0:
            score += avoided / (1.0 + offset / tau)
    return round(score, 3)


def validate_plan_independently(
    *,
    shipments: list[tuple[Route, Fuel, float]],
    snapshot: Snapshot,
    dispatch_used: dict[str, float],
    incoming: dict[tuple[str, Fuel], list[float]],
    demand_series_by_key: dict[tuple[str, Fuel], list[float]],
    max_total_liters: float | None = None,
) -> tuple[bool, list[str]]:
    """Verify every rounded shipment independently against all simulator & arrival invariants."""
    errors: list[str] = []
    depots = {d.id: d for d in snapshot.depots}
    stations = {s.id: s for s in snapshot.stations}
    routes = {r.id: r for r in snapshot.routes}
    horizon = len(next(iter(incoming.values()))) if incoming else 96

    depot_fuel_sum: dict[tuple[str, Fuel], float] = {}
    depot_dispatch_sum: dict[str, float] = {}
    station_fuel_sum: dict[tuple[str, Fuel], float] = {}

    for route, fuel, qty in shipments:
        if qty <= 0 or abs(qty - round(qty, 3)) > 1e-9:
            errors.append(f"Invalid precision or non-positive qty {qty} on {route.id}")
        r = routes.get(route.id)
        if r is None or r.status != "AVAILABLE":
            errors.append(f"Route {route.id} unavailable")
        d = depots.get(route.source_depot_id)
        s = stations.get(route.destination_station_id)
        if d is None or d.status not in ("OPEN", "CONSTRAINED"):
            errors.append(f"Depot {route.source_depot_id} unavailable")
        if s is None or s.status != "OPEN":
            errors.append(f"Station {route.destination_station_id} unavailable")
        if qty > route.max_shipment + 1e-6:
            errors.append(f"Route max exceeded on {route.id}: {qty} > {route.max_shipment}")
        if route.transit_ticks >= horizon:
            errors.append(f"Arrival outside horizon on {route.id}")
        depot_fuel_sum[(route.source_depot_id, fuel)] = depot_fuel_sum.get((route.source_depot_id, fuel), 0.0) + qty
        depot_dispatch_sum[route.source_depot_id] = depot_dispatch_sum.get(route.source_depot_id, 0.0) + qty
        station_fuel_sum[(route.destination_station_id, fuel)] = station_fuel_sum.get((route.destination_station_id, fuel), 0.0) + qty

    for (did, fuel), total in depot_fuel_sum.items():
        if total > getattr(depots[did].inventory, fuel) + 1e-6:
            errors.append(f"Depot {did} {fuel} inventory exceeded: {total}")
    for did, total in depot_dispatch_sum.items():
        if total + dispatch_used.get(did, 0.0) > depots[did].dispatch_capacity_per_tick + 1e-6:
            errors.append(f"Depot {did} dispatch capacity exceeded: {total}")
    for (sid, fuel), total in station_fuel_sum.items():
        st = stations[sid]
        cur = float(getattr(st.inventory, fuel))
        cap = float(getattr(st.capacity, fuel))
        if cur + total > cap + 1e-6:
            errors.append(f"Station {sid} {fuel} immediate storage capacity exceeded: {cur + total} > {cap}")
    if max_total_liters is not None and sum(q for _, _, q in shipments) > max_total_liters + 1e-6:
        errors.append(f"Total liter budget exceeded: {sum(q for _, _, q in shipments)} > {max_total_liters}")
    return len(errors) == 0, errors


def simulate_joint_impact(
    *,
    shipments: list[tuple[Route, Fuel, float]],
    snapshot: Snapshot,
    incoming: dict[tuple[str, Fuel], list[float]],
    demand_series_by_key: dict[tuple[str, Fuel], list[float]],
    demand_p10_by_key: dict[tuple[str, Fuel], list[float]],
    demand_p90_by_key: dict[tuple[str, Fuel], list[float]],
) -> dict[str, float | None]:
    extra: dict[tuple[str, Fuel], list[float]] = {}
    horizon = len(next(iter(incoming.values()))) if incoming else 96
    for route, fuel, qty in shipments:
        k = (route.destination_station_id, fuel)
        vec = extra.setdefault(k, [0.0] * horizon)
        if route.transit_ticks < horizon:
            vec[route.transit_ticks] += qty

    unmet_before = 0.0
    unmet_after = 0.0
    p10_after = 0.0
    p90_after = 0.0
    has_quantiles = True
    for st in snapshot.stations:
        st_open = st.status == "OPEN"
        for fuel in FUELS:
            k = (st.id, fuel)
            stock = float(getattr(st.inventory, fuel))
            cap = float(getattr(st.capacity, fuel))
            arr = incoming.get(k, [0.0] * horizon)
            dem = demand_series_by_key.get(k, [0.0] * horizon)
            _, u_b, _ = project(stock, cap, dem, arr, st_open)
            combined = [a + b for a, b in zip(arr, extra.get(k, [0.0] * horizon))]
            _, u_a, _ = project(stock, cap, dem, combined, st_open)
            unmet_before += sum(u_b)
            unmet_after += sum(u_a)

            if k in demand_p10_by_key and k in demand_p90_by_key:
                _, u10, _ = project(stock, cap, demand_p10_by_key[k], combined, st_open)
                _, u90, _ = project(stock, cap, demand_p90_by_key[k], combined, st_open)
                p10_after += sum(u10)
                p90_after += sum(u90)
            else:
                has_quantiles = False

    return {
        "unmet_before": round(unmet_before, 3),
        "unmet_after": round(unmet_after, 3),
        "unmet_avoided": round(max(0.0, unmet_before - unmet_after), 3),
        "p10_unmet_after": round(p10_after, 3) if has_quantiles else None,
        "p90_unmet_after": round(p90_after, 3) if has_quantiles else None,
    }


def optimize_allocations(
    *,
    snapshot: Snapshot,
    generation_id: str,
    forecasts: list[Forecast],
    incoming: dict[tuple[str, Fuel], list[float]],
    dispatch_used: dict[str, float],
    demand_series_by_key: dict[tuple[str, Fuel], list[float]],
    demand_p10_by_key: dict[tuple[str, Fuel], list[float]],
    demand_p90_by_key: dict[tuple[str, Fuel], list[float]],
    std_by_key: dict[tuple[str, Fuel], float],
    station_details: dict[str, StationDetail],
    forecast_source: ForecastSource,
    model_version: str,
    policy_mode: PolicyMode,
    fallback_reason: str | None = None,
    single_action_limit: int | None = None,
    max_total_liters: float | None = None,
    solve_timeout_ms: int = 2000,
) -> tuple[list[Recommendation], PlanPreviewSummary]:
    """Solve constrained global dispatch with OR-Tools CBC, validate independently, and simulate joint plan."""
    started = time.perf_counter()
    tick = snapshot.simulation.tick
    tick_minutes = snapshot.simulation.tick_minutes
    horizon_ticks = len(next(iter(incoming.values()))) if incoming else 96
    plan_horizon = horizon_ticks
    plan_id = f"plan-{generation_id[:8]}-{tick}-{uuid4().hex[:8]}"
    cfg_hash = hashlib.sha256(
        f"{generation_id}:{tick}:{horizon_ticks}:{policy_mode}:{forecast_source}:{single_action_limit}:{max_total_liters}".encode()
    ).hexdigest()[:16]

    depots = {d.id: d for d in snapshot.depots}
    stations = {s.id: s for s in snapshot.stations}
    forecasts_by_key = {(f.station_id, f.fuel_type): f for f in forecasts}

    # Build baseline comparisons (no-action & safe reorder)
    no_action_stats = simulate_joint_impact(
        shipments=[], snapshot=snapshot, incoming=incoming,
        demand_series_by_key=demand_series_by_key,
        demand_p10_by_key=demand_p10_by_key, demand_p90_by_key=demand_p90_by_key,
    )
    safe_recs = build_safe_reorder_recommendations(
        snapshot=snapshot, incoming=incoming, dispatch_used=dispatch_used,
        demand_series_by_key=demand_series_by_key, std_by_key=std_by_key,
        plan_id=plan_id, generation_id=generation_id,
        reason=fallback_reason or "Safe safety-stock baseline",
        single_action_limit=single_action_limit,
        max_total_liters=max_total_liters,
    )
    routes_map = {r.id: r for r in snapshot.routes}
    safe_stats = simulate_joint_impact(
        shipments=[(routes_map[r.route_id], r.fuel_type, r.quantity_liters) for r in safe_recs if r.route_id in routes_map],
        snapshot=snapshot, incoming=incoming,
        demand_series_by_key=demand_series_by_key,
        demand_p10_by_key=demand_p10_by_key, demand_p90_by_key=demand_p90_by_key,
    )

    if policy_mode in ("SAFE_REORDER", "REORDER_FALLBACK"):
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        summary = PlanPreviewSummary(
            plan_id=plan_id, generation_id=generation_id, as_of_tick=tick,
            horizon_ticks=horizon_ticks, policy_mode=policy_mode,
            forecast_source=forecast_source, model_version=model_version,
            solver_status="SAFE_REORDER", solver_runtime_ms=elapsed_ms,
            config_hash=cfg_hash, fallback_reason=fallback_reason,
            total_allocated_liters=round(sum(r.quantity_liters for r in safe_recs), 3),
            unmet_before_liters=float(no_action_stats["unmet_before"] or 0.0),
            unmet_after_liters=float(safe_stats["unmet_after"] or 0.0),
            unmet_avoided_liters=float(safe_stats["unmet_avoided"] or 0.0),
            no_action_unmet_liters=float(no_action_stats["unmet_before"] or 0.0),
            safe_baseline_unmet_liters=float(safe_stats["unmet_after"] or 0.0),
            p10_unmet_after_liters=safe_stats["p10_unmet_after"],
            p90_unmet_after_liters=safe_stats["p90_unmet_after"],
            binding_constraints=["Policy configured to safe safety-stock reorder"],
            shipments=safe_recs,
        )
        return safe_recs, summary

    # Solve constrained MIP using OR-Tools CBC
    solver = pywraplp.Solver.CreateSolver("CBC")
    if solver is None:
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        summary = PlanPreviewSummary(
            plan_id=plan_id, generation_id=generation_id, as_of_tick=tick,
            horizon_ticks=horizon_ticks, policy_mode="SAFE_REORDER",
            forecast_source=forecast_source, model_version=model_version,
            solver_status="SOLVER_UNAVAILABLE_FALLBACK", solver_runtime_ms=elapsed_ms,
            config_hash=cfg_hash, fallback_reason="OR-Tools CBC solver unavailable; using safe reorder.",
            total_allocated_liters=round(sum(r.quantity_liters for r in safe_recs), 3),
            unmet_before_liters=float(no_action_stats["unmet_before"] or 0.0),
            unmet_after_liters=float(safe_stats["unmet_after"] or 0.0),
            unmet_avoided_liters=float(safe_stats["unmet_avoided"] or 0.0),
            no_action_unmet_liters=float(no_action_stats["unmet_before"] or 0.0),
            safe_baseline_unmet_liters=float(safe_stats["unmet_after"] or 0.0),
            binding_constraints=["Fallback: solver unavailable"],
            shipments=safe_recs,
        )
        return safe_recs, summary

    solver.SetTimeLimit(solve_timeout_ms)
    x_vars: dict[tuple[str, Fuel], pywraplp.Variable] = {}
    y_vars: dict[tuple[str, Fuel], pywraplp.Variable] = {}
    eligible_routes = [
        r for r in snapshot.routes
        if r.status == "AVAILABLE"
        and r.transit_ticks < plan_horizon
        and r.source_depot_id in depots
        and r.destination_station_id in stations
        and depots[r.source_depot_id].status in ("OPEN", "CONSTRAINED")
        and stations[r.destination_station_id].status == "OPEN"
    ]

    for route in eligible_routes:
        depot = depots[route.source_depot_id]
        station = stations[route.destination_station_id]
        for fuel in FUELS:
            depot_inv = float(getattr(depot.inventory, fuel))
            st_cap = float(getattr(station.capacity, fuel))
            st_inv = float(getattr(station.inventory, fuel))
            arr = incoming.get((station.id, fuel), [0.0] * horizon_ticks)
            dem = demand_series_by_key.get((station.id, fuel), [0.0] * horizon_ticks)
            _, _, room = project(st_inv, st_cap, dem, arr, True)
            upper = min(route.max_shipment, depot_inv, max(0.0, depot.dispatch_capacity_per_tick - dispatch_used.get(depot.id, 0.0)), max(0.0, st_cap - st_inv), room[route.transit_ticks])
            if upper < 10.0:
                continue
            xv = solver.NumVar(0.0, upper, f"x_{route.id}_{fuel}")
            yv = solver.BoolVar(f"y_{route.id}_{fuel}")
            min_lot = min(250.0, upper * 0.5)
            solver.Add(xv <= upper * yv)
            solver.Add(xv >= min_lot * yv)
            x_vars[(route.id, fuel)] = xv
            y_vars[(route.id, fuel)] = yv

    if single_action_limit is not None and y_vars:
        solver.Add(solver.Sum(y_vars.values()) <= single_action_limit)
    if max_total_liters is not None and x_vars:
        solver.Add(solver.Sum(x_vars.values()) <= float(max_total_liters))

    # Depot inventory and dispatch constraints
    for depot in snapshot.depots:
        d_vars_all = [x_vars[(r.id, f)] for r in eligible_routes if r.source_depot_id == depot.id for f in FUELS if (r.id, f) in x_vars]
        if d_vars_all:
            rem_dispatch = max(0.0, depot.dispatch_capacity_per_tick - dispatch_used.get(depot.id, 0.0))
            solver.Add(solver.Sum(d_vars_all) <= rem_dispatch)
        for fuel in FUELS:
            df_vars = [x_vars[(r.id, fuel)] for r in eligible_routes if r.source_depot_id == depot.id and (r.id, fuel) in x_vars]
            if df_vars:
                solver.Add(solver.Sum(df_vars) <= float(getattr(depot.inventory, fuel)))

    # Immediate destination capacity constraint across all routes into same (station, fuel)
    for station in snapshot.stations:
        for fuel in FUELS:
            sf_vars = [x_vars[(r.id, fuel)] for r in eligible_routes if r.destination_station_id == station.id and (r.id, fuel) in x_vars]
            if sf_vars:
                room_now = max(0.0, float(getattr(station.capacity, fuel)) - float(getattr(station.inventory, fuel)))
                solver.Add(solver.Sum(sf_vars) <= room_now)

    # Horizon inventory balance, safety deficit, and minimax unmet ratio
    objective = solver.Objective()
    max_deficit_ratio = solver.NumVar(0.0, 10.0, "max_unmet_ratio")
    objective.SetCoefficient(max_deficit_ratio, 5000.0)

    safety_targets: dict[tuple[str, Fuel], float] = {}
    min_leads = {
        s.id: min((r.transit_ticks for r in eligible_routes if r.destination_station_id == s.id), default=3)
        for s in snapshot.stations
    }

    for station in snapshot.stations:
        if station.status != "OPEN":
            continue
        for fuel in FUELS:
            k = (station.id, fuel)
            cap = float(getattr(station.capacity, fuel))
            cur_inv = float(getattr(station.inventory, fuel))
            arr = incoming.get(k, [0.0] * horizon_ticks)
            dem = demand_series_by_key.get(k, [0.0] * horizon_ticks)
            safety = compute_safety_stock_target(
                capacity=cap,
                demand_series=dem,
                demand_std=std_by_key.get(k, 0.0),
                min_lead_ticks=min_leads[station.id],
                cadence_ticks=6,
                station_open=True,
            )
            safety_targets[k] = safety

            inv_vars = [solver.NumVar(0.0, cap, f"I_{station.id}_{fuel}_{t}") for t in range(plan_horizon)]
            total_dem = max(1.0, sum(dem[:plan_horizon]))
            unmet_vars = []
            # Early-time weighted unmet demand normalized by tank size so small tanks are never starved
            norm_cap = max(1000.0, cap)
            cap_scale = min(2.0, max(0.7, 12000.0 / norm_cap))
            for t in range(plan_horizon):
                d_t = max(0.0, dem[t])
                u_t = solver.NumVar(0.0, d_t, f"U_{station.id}_{fuel}_{t}")
                unmet_vars.append(u_t)
                objective.SetCoefficient(u_t, (120.0 + 1800.0 / (t + 3.0)) * cap_scale)
                prev_inv = cur_inv if t == 0 else inv_vars[t - 1]
                arr_t = arr[t]
                incoming_x = [
                    x_vars[(r.id, fuel)]
                    for r in eligible_routes
                    if r.destination_station_id == station.id and r.transit_ticks == t and (r.id, fuel) in x_vars
                ]
                ship_in = solver.Sum(incoming_x) if incoming_x else 0.0
                # Station inventory receives supply before demand, served = d_t - u_t
                solver.Add(inv_vars[t] == prev_inv + arr_t + ship_in - (d_t - u_t))

            # Fairness constraint on cumulative unmet share
            solver.Add(max_deficit_ratio * total_dem >= solver.Sum(unmet_vars))

            # Safety stock target across post-arrival lookahead checkpoints (prevents under-filling)
            start_check = min(plan_horizon - 1, min_leads[station.id] + 2)
            checkpoints = list(range(start_check, min(plan_horizon, start_check + 28), 4))
            if not checkpoints:
                checkpoints = [plan_horizon - 1]
            # Per-checkpoint weight dominates consolidation cost for real safety replenishment (> 0.6/L)
            weight_per_cp = round((9600.0 / norm_cap) / len(checkpoints), 4)
            cadence_dem = sum(dem[: min(plan_horizon, min_leads[station.id] + 18)])
            target_reserve = min(cap * 0.82, max(safety, cap * 0.68, cadence_dem))
            for cp_t in checkpoints:
                q_s = solver.NumVar(0.0, cap, f"Q_{station.id}_{fuel}_{cp_t}")
                solver.Add(inv_vars[cp_t] + q_s >= target_reserve)
                objective.SetCoefficient(q_s, weight_per_cp)

            # Worst-case minimum post-arrival reserve ratio penalty so starving fuel gets priority
            worst_deficit = solver.NumVar(0.0, 1.0, f"W_{station.id}_{fuel}")
            for cp_t in checkpoints[:4]:
                solver.Add(worst_deficit * max(1.0, cap) >= target_reserve - inv_vars[cp_t])
            objective.SetCoefficient(worst_deficit, 3600.0)

    for (rid, fuel), yv in y_vars.items():
        # Minimal tie-break setup cost so CBC avoids zero-benefit micro-splits without suppressing refills
        objective.SetCoefficient(yv, 2.0)

    for (rid, fuel), xv in x_vars.items():
        eta = routes_map[rid].transit_ticks
        # Tiny proxy cost avoiding unnecessary hoarding & preferring faster corridors
        objective.SetCoefficient(xv, 0.002 * eta)

    objective.SetMinimization()
    status_code = solver.Solve()
    status_names = {
        pywraplp.Solver.OPTIMAL: "OPTIMAL",
        pywraplp.Solver.FEASIBLE: "FEASIBLE",
        pywraplp.Solver.INFEASIBLE: "INFEASIBLE",
        pywraplp.Solver.UNBOUNDED: "UNBOUNDED",
        pywraplp.Solver.ABNORMAL: "ABNORMAL",
        pywraplp.Solver.NOT_SOLVED: "TIMEOUT",
    }
    solver_status = status_names.get(status_code, "UNKNOWN")

    if status_code not in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        summary = PlanPreviewSummary(
            plan_id=plan_id, generation_id=generation_id, as_of_tick=tick,
            horizon_ticks=horizon_ticks, policy_mode="SAFE_REORDER",
            forecast_source=forecast_source, model_version=model_version,
            solver_status=f"{solver_status}_FALLBACK", solver_runtime_ms=elapsed_ms,
            config_hash=cfg_hash, fallback_reason=f"OR-Tools solver returned {solver_status}; falling back to safe reorder.",
            total_allocated_liters=round(sum(r.quantity_liters for r in safe_recs), 3),
            unmet_before_liters=float(no_action_stats["unmet_before"] or 0.0),
            unmet_after_liters=float(safe_stats["unmet_after"] or 0.0),
            unmet_avoided_liters=float(safe_stats["unmet_avoided"] or 0.0),
            no_action_unmet_liters=float(no_action_stats["unmet_before"] or 0.0),
            safe_baseline_unmet_liters=float(safe_stats["unmet_after"] or 0.0),
            binding_constraints=[f"Solver {solver_status}; active safe fallback"],
            shipments=safe_recs,
        )
        return safe_recs, summary

    chosen: list[tuple[Route, Fuel, float]] = []
    for (rid, fuel), xv in x_vars.items():
        qty = round(math.floor(max(0.0, xv.solution_value()) * 1000.0) / 1000.0, 3)
        if qty >= 1.0:
            chosen.append((routes_map[rid], fuel, qty))

    valid, v_errors = validate_plan_independently(
        shipments=chosen, snapshot=snapshot, dispatch_used=dispatch_used,
        incoming=incoming, demand_series_by_key=demand_series_by_key,
        max_total_liters=max_total_liters,
    )
    if not valid:
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        summary = PlanPreviewSummary(
            plan_id=plan_id, generation_id=generation_id, as_of_tick=tick,
            horizon_ticks=horizon_ticks, policy_mode="SAFE_REORDER",
            forecast_source=forecast_source, model_version=model_version,
            solver_status="POST_SOLVE_VALIDATION_FAILED", solver_runtime_ms=elapsed_ms,
            config_hash=cfg_hash, fallback_reason="; ".join(v_errors),
            total_allocated_liters=round(sum(r.quantity_liters for r in safe_recs), 3),
            unmet_before_liters=float(no_action_stats["unmet_before"] or 0.0),
            unmet_after_liters=float(safe_stats["unmet_after"] or 0.0),
            unmet_avoided_liters=float(safe_stats["unmet_avoided"] or 0.0),
            no_action_unmet_liters=float(no_action_stats["unmet_before"] or 0.0),
            safe_baseline_unmet_liters=float(safe_stats["unmet_after"] or 0.0),
            binding_constraints=v_errors,
            shipments=safe_recs,
        )
        return safe_recs, summary

    # Convert validated OR-Tools solution into rich Recommendation objects
    recs: list[Recommendation] = []
    binding_constraints: list[str] = []
    for route, fuel, qty in chosen:
        depot = depots[route.source_depot_id]
        station = stations[route.destination_station_id]
        detail = station_details.get(station.id)
        k = (station.id, fuel)
        arr = incoming.get(k, [0.0] * horizon_ticks)
        dem = demand_series_by_key.get(k, [0.0] * horizon_ticks)
        stock = float(getattr(station.inventory, fuel))
        cap = float(getattr(station.capacity, fuel))
        dep_before, unmet_b, room = project(stock, cap, dem, arr, True)
        with_ship = arr.copy()
        with_ship[route.transit_ticks] += qty
        dep_after, unmet_a, _ = project(stock, cap, dem, with_ship, True)

        base_u = round(sum(unmet_b), 3)
        after_u = round(sum(unmet_a), 3)
        avoided = round(max(0.0, base_u - after_u), 3)
        safety = safety_targets.get(k, cap * 0.45)
        fc = forecasts_by_key.get(k)
        rolling_mean = fc.demand_liters_per_tick if (fc and fc.demand_liters_per_tick is not None) else mean(dem)
        proj_avg = mean(dem)
        conf: ConfidenceLevel = fc.confidence if fc else "MEDIUM"
        risk_b = classify_risk(stock, cap, dep_before, base_u, safety_target=safety)
        risk_a = classify_risk(min(cap, stock + qty), cap, dep_after, after_u, safety_target=safety)
        inv_pos = stock + sum(arr)
        post_arrival_ratio = (stock + sum(arr[:route.transit_ticks + 6])) / max(1.0, cap)
        imminent_bonus = (2500.0 / (1.0 + dep_before / 8.0)) if dep_before is not None else 0.0
        score = (
            urgency_score(unmet_b, unmet_a) * 3.0
            + imminent_bonus
            + max(0.0, (max(safety / max(1.0, cap), 0.68) - post_arrival_ratio)) * 3000.0
            + max(0.0, 0.50 - (inv_pos / max(1.0, cap))) * 1500.0
        )

        if abs(qty - route.max_shipment) < 1.0:
            binding_constraints.append(f"{route.id} max_shipment cap ({route.max_shipment:,.0f} L)")
        if abs(qty - (cap - stock)) < 1.0:
            binding_constraints.append(f"{station.name} immediate tank cap ({cap:,.0f} L)")

        alternatives = [r.id for r in eligible_routes if r.destination_station_id == station.id and r.id != route.id]
        rationale, signals, constraints = build_recommendation_explanation(
            depot_name=depot.name,
            station_name=station.name,
            fuel=fuel,
            quantity=qty,
            arrival_tick=tick + route.transit_ticks,
            eta_ticks=route.transit_ticks,
            eta_minutes=route.transit_ticks * tick_minutes,
            unmet_before=base_u,
            unmet_after=after_u,
            unmet_avoided=avoided,
            risk_before=risk_b,
            risk_after=risk_a,
            safety_target=safety,
            inv_after=min(cap, stock + qty),
            rolling_mean=rolling_mean,
            projected_avg=proj_avg,
            stockout_tick_before=tick + dep_before if dep_before is not None else None,
            demand_multiplier=detail.demand_multiplier if detail else 1.0,
            route_max=route.max_shipment,
            dispatch_remaining=max(0.0, depot.dispatch_capacity_per_tick - dispatch_used.get(depot.id, 0.0)),
            station_room_now=max(0.0, cap - stock),
            room_at_arrival=room[route.transit_ticks],
            confidence=conf,
            forecast_source=forecast_source,
        )
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
                quantity_liters=qty,
                arrival_tick=tick + route.transit_ticks,
                eta_ticks=route.transit_ticks,
                eta_minutes=route.transit_ticks * tick_minutes,
                unmet_before_liters=base_u,
                unmet_after_liters=after_u,
                unmet_avoided_liters=avoided,
                policy_mode=policy_mode,
                priority_score=round(score, 3),
                stockout_tick_before=tick + dep_before if dep_before is not None else None,
                stockout_tick_after=tick + dep_after if dep_after is not None else None,
                risk_before=risk_b,
                risk_after=risk_a,
                confidence=conf,
                rationale=rationale,
                signals=signals,
                constraints=constraints,
                alternative_routes=alternatives,
            )
        )

    recs.sort(key=lambda item: (-item.priority_score, -item.unmet_avoided_liters, item.eta_ticks, item.route_id, item.fuel_type))
    joint_stats = simulate_joint_impact(
        shipments=chosen, snapshot=snapshot, incoming=incoming,
        demand_series_by_key=demand_series_by_key,
        demand_p10_by_key=demand_p10_by_key, demand_p90_by_key=demand_p90_by_key,
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
    summary = PlanPreviewSummary(
        plan_id=plan_id,
        generation_id=generation_id,
        as_of_tick=tick,
        horizon_ticks=horizon_ticks,
        policy_mode=policy_mode,
        forecast_source=forecast_source,
        model_version=model_version,
        solver_status=solver_status,
        solver_runtime_ms=elapsed_ms,
        config_hash=cfg_hash,
        fallback_reason=fallback_reason,
        total_allocated_liters=round(sum(r.quantity_liters for r in recs), 3),
        unmet_before_liters=float(joint_stats["unmet_before"] or 0.0),
        unmet_after_liters=float(joint_stats["unmet_after"] or 0.0),
        unmet_avoided_liters=float(joint_stats["unmet_avoided"] or 0.0),
        no_action_unmet_liters=float(no_action_stats["unmet_before"] or 0.0),
        safe_baseline_unmet_liters=float(safe_stats["unmet_after"] or 0.0),
        p10_unmet_after_liters=joint_stats["p10_unmet_after"],
        p90_unmet_after_liters=joint_stats["p90_unmet_after"],
        binding_constraints=sorted(set(binding_constraints)) or ["No active bottlenecks hit"],
        shipments=recs,
    )
    return recs, summary
