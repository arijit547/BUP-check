from collections import defaultdict
from datetime import UTC, datetime, timedelta
from statistics import mean
from typing import Any

from intelligence.anomaly import detect_rule_anomalies, merge_ml_anomalies
from intelligence.collector import hour_factor, index_observations_by_series, regularized_series
from intelligence.contracts import (
    FUELS,
    Allocation,
    Anomaly,
    ConfidenceLevel,
    DemandObservation,
    Forecast,
    ForecastPoint,
    ForecastRisk,
    ForecastSeries,
    ForecastSource,
    ForecastsResponse,
    Fuel,
    PlanPreviewSummary,
    PlanningReport,
    PolicyMode,
    Recommendation,
    Region,
    ShortageDetail,
    ShortagesResponse,
    SimulatorEvent,
    SimulatorMetrics,
    StationDetail,
    SupplyArrival,
)
from intelligence.fallback import fallback_demand_rate
from intelligence.features import build_causal_features, spike_multiplier_at
from intelligence.forecasting import build_demand_series, build_seasonal_forecast_points, confidence_for_window
from intelligence.optimizer import optimize_allocations, simulate_joint_impact, urgency_score, validate_plan_independently
from intelligence.shortage import classify_risk, compute_safety_stock_target, project, project_detailed, stockout_probability
from schemas import Snapshot

__all__ = [
    "Allocation",
    "Anomaly",
    "DemandObservation",
    "Forecast",
    "ForecastPoint",
    "ForecastRisk",
    "ForecastSeries",
    "ForecastsResponse",
    "Fuel",
    "FUELS",
    "PlanPreviewSummary",
    "PlanningReport",
    "Recommendation",
    "Region",
    "ShortageDetail",
    "ShortagesResponse",
    "SimulatorEvent",
    "SimulatorMetrics",
    "StationDetail",
    "SupplyArrival",
    "build_demand_series",
    "build_report",
    "classify_risk",
    "confidence_for_window",
    "fallback_demand_rate",
    "hour_factor",
    "project",
    "spike_multiplier_at",
    "stockout_probability",
    "urgency_score",
]


def _extract_incoming_and_dispatch(
    *,
    snapshot: Snapshot,
    allocations: list[Allocation],
    horizon_ticks: int,
) -> tuple[dict[tuple[str, Fuel], list[float]], dict[str, float]]:
    tick = snapshot.simulation.tick
    routes_by_id = {route.id: route for route in snapshot.routes}
    incoming: dict[tuple[str, Fuel], list[float]] = defaultdict(lambda: [0.0] * horizon_ticks)
    dispatch_used: dict[str, float] = defaultdict(float)
    for allocation in allocations:
        if allocation.status in ("PENDING", "IN_TRANSIT") and allocation.created_tick == tick:
            dispatch_used[allocation.source_depot_id] += allocation.quantity
        eta = allocation.expected_arrival_tick
        if allocation.status == "IN_TRANSIT" and eta is not None and tick <= eta < tick + horizon_ticks:
            incoming[(allocation.destination_station_id, allocation.fuel_type)][eta - tick] += allocation.quantity
        elif allocation.status == "PENDING" and allocation.route_id and allocation.route_id in routes_by_id:
            pending_eta = routes_by_id[allocation.route_id].transit_ticks
            if pending_eta < horizon_ticks:
                incoming[(allocation.destination_station_id, allocation.fuel_type)][pending_eta] += allocation.quantity
    return incoming, dispatch_used


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
    generation_id: str = "default",
    chronos_predictions: dict[tuple[str, Fuel], list[ForecastPoint]] | None = None,
    chronos_model_version: str | None = None,
    ml_anomaly_scores: list[dict[str, Any]] | None = None,
    policy_override: str = "AUTO",
    single_action_limit: int | None = None,
    max_total_liters: float | None = None,
    ml_fallback_reason: str | None = None,
) -> PlanningReport:
    tick = snapshot.simulation.tick
    sim_time = snapshot.simulation.sim_time
    tick_minutes = snapshot.simulation.tick_minutes
    details_map = {item.id: item for item in (station_details or [])}
    region_factors = {item.id: item.demand_factor for item in (regions or [])}
    event_list = events or []
    arrival_list = supply_arrivals or []

    observed = index_observations_by_series(history, current_tick=tick, history_ticks=history_ticks)
    incoming, dispatch_used = _extract_incoming_and_dispatch(
        snapshot=snapshot, allocations=allocations, horizon_ticks=horizon_ticks
    )

    routes_by_station = defaultdict(list)
    for route in snapshot.routes:
        if route.status == "AVAILABLE" and route.transit_ticks < horizon_ticks:
            routes_by_station[route.destination_station_id].append(route)

    forecasts: list[Forecast] = []
    demand_series_by_key: dict[tuple[str, Fuel], list[float]] = {}
    demand_p10_by_key: dict[tuple[str, Fuel], list[float]] = {}
    demand_p90_by_key: dict[tuple[str, Fuel], list[float]] = {}
    std_by_key: dict[tuple[str, Fuel], float] = {}
    fallback_active = bool(ml_fallback_reason)
    has_any_full_history = False

    use_chronos = bool(chronos_predictions) and policy_override in ("AUTO", "CHRONOS2")
    active_source: ForecastSource = "CHRONOS2" if (chronos_predictions and policy_override == "CHRONOS2") else "SEASONAL"
    active_model_ver = chronos_model_version if (active_source == "CHRONOS2" and chronos_model_version) else "seasonal-v2.0"

    for station in snapshot.stations:
        detail = details_map.get(station.id)
        region_factor = region_factors.get(station.region_id, 1.0)
        station_open = station.status == "OPEN"
        st_routes = routes_by_station[station.id]
        min_lead = min((r.transit_ticks for r in st_routes), default=3)

        for fuel in FUELS:
            key = (station.id, fuel)
            samples = sorted(observed[key].items())
            values = [obs.demand_liters for _, obs in samples]
            stock = float(getattr(station.inventory, fuel))
            capacity = float(getattr(station.capacity, fuel))
            arrivals = incoming[key]
            conf_level, conf_score, demand_std = confidence_for_window(len(samples), history_ticks, values)
            std_by_key[key] = demand_std

            if len(samples) < history_ticks:
                fallback_active = True
                fb_rate = fallback_demand_rate(samples, detail, region_factor, fuel, sim_time.hour, tick_minutes)
                fb_series = [fb_rate] * horizon_ticks
                demand_series_by_key[key] = fb_series
                demand_p10_by_key[key] = [round(max(0.0, fb_rate * 0.8), 3)] * horizon_ticks
                demand_p90_by_key[key] = [round(fb_rate * 1.2, 3)] * horizon_ticks
                fb_detail = project_detailed(stock, capacity, fb_series, arrivals, station_open)
                fb_baseline = round(sum(fb_detail["unmet"]), 3)
                fb_safety = compute_safety_stock_target(
                    capacity=capacity, demand_series=fb_series, demand_std=demand_std or fb_rate * 0.12,
                    min_lead_ticks=min_lead, cadence_ticks=6, station_open=station_open,
                )
                fb_risk = classify_risk(stock, capacity, fb_detail["depleted_offset"], fb_baseline, safety_target=fb_safety)
                fb_prob = stockout_probability(
                    stock, fb_series, demand_std or fb_rate * 0.12, arrivals, fb_detail["depleted_offset"], station_open,
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
                    p10_total_liters=round(sum(demand_p10_by_key[key]), 3),
                    p50_total_liters=round(sum(fb_series), 3),
                    p90_total_liters=round(sum(demand_p90_by_key[key]), 3),
                    confidence="LOW",
                    confidence_score=conf_score,
                    shortage_risk=fb_risk,
                    stockout_probability=fb_prob,
                    probability_calibration="SCENARIO_HEURISTIC",
                    outage_unmet_liters=fb_detail["outage_unmet_liters"],
                    depletion_unmet_liters=fb_detail["depletion_unmet_liters"],
                    first_unmet_tick=None,
                    time_to_stockout_minutes=None,
                    stockout_duration_ticks=0,
                    safety_stock_target_liters=fb_safety,
                    fallback_used=True,
                    forecast_source="PROFILE_FALLBACK",
                    model_version="profile-fallback-v1",
                    trajectory_p50=fb_detail["trajectory"],
                ))
                continue

            has_any_full_history = True
            rolling_mean, norm_std, seas_pts = build_seasonal_forecast_points(
                samples=samples, tick=tick, horizon_ticks=horizon_ticks,
                tick_minutes=tick_minutes, sim_time=sim_time,
                station_id=station.id, region_id=station.region_id,
                station_detail=detail, events=event_list,
            )

            series_source: ForecastSource = "SEASONAL"
            series_model_ver = "seasonal-v2.0"
            chosen_pts = seas_pts
            if chronos_predictions and key in chronos_predictions and policy_override == "CHRONOS2":
                c_pts = chronos_predictions[key]
                if len(c_pts) == horizon_ticks:
                    chosen_pts = c_pts
                    series_source = "CHRONOS2"
                    series_model_ver = chronos_model_version or "amazon/chronos-2"

            p50_series = [pt.p50 for pt in chosen_pts]
            p10_series = [pt.p10 if pt.p10 is not None else pt.p50 for pt in chosen_pts]
            p90_series = [pt.p90 if pt.p90 is not None else pt.p50 for pt in chosen_pts]
            demand_series_by_key[key] = p50_series
            demand_p10_by_key[key] = p10_series
            demand_p90_by_key[key] = p90_series

            det = project_detailed(stock, capacity, p50_series, arrivals, station_open)
            baseline = round(sum(det["unmet"]), 3)
            projected_avg = round(mean(p50_series), 3)
            safety_target = compute_safety_stock_target(
                capacity=capacity, demand_series=p50_series, demand_std=demand_std,
                min_lead_ticks=min_lead, cadence_ticks=6, station_open=station_open,
            )
            risk = classify_risk(stock, capacity, det["depleted_offset"], baseline, safety_target=safety_target)
            prob = stockout_probability(stock, p50_series, demand_std, arrivals, det["depleted_offset"], station_open)
            stockout_tick = (tick + det["depleted_offset"]) if det["depleted_offset"] is not None else None
            first_unmet_tick = (tick + det["first_unmet_offset"]) if det["first_unmet_offset"] is not None else None
            tts_min = (det["first_unmet_offset"] * tick_minutes) if det["first_unmet_offset"] is not None else None

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
                p10_total_liters=round(sum(p10_series), 3),
                p50_total_liters=round(sum(p50_series), 3),
                p90_total_liters=round(sum(p90_series), 3),
                confidence=conf_level,
                confidence_score=conf_score,
                shortage_risk=risk,
                stockout_probability=prob,
                probability_calibration="SCENARIO_HEURISTIC",
                outage_unmet_liters=det["outage_unmet_liters"],
                depletion_unmet_liters=det["depletion_unmet_liters"],
                first_unmet_tick=first_unmet_tick,
                time_to_stockout_minutes=tts_min,
                stockout_duration_ticks=det["stockout_duration_ticks"],
                safety_stock_target_liters=safety_target,
                fallback_used=False,
                forecast_source=series_source,
                model_version=series_model_ver,
                trajectory_p50=det["trajectory"],
            ))

    if not has_any_full_history:
        effective_source: ForecastSource = "REORDER_FALLBACK"
        chosen_policy: PolicyMode = "REORDER_FALLBACK"
        reason = ml_fallback_reason or "History incomplete across stations; preventive safety-stock fallback active."
    elif policy_override == "SAFE_REORDER":
        effective_source = active_source
        chosen_policy = "SAFE_REORDER"
        reason = "Safe reorder baseline mode selected."
    elif policy_override == "CHRONOS2" and chronos_predictions:
        effective_source = "CHRONOS2"
        chosen_policy = "OR_TOOLS_CHRONOS2"
        reason = None
    else:
        effective_source = "SEASONAL"
        chosen_policy = "OR_TOOLS_SEASONAL"
        reason = ml_fallback_reason

    recommendations, joint_plan = optimize_allocations(
        snapshot=snapshot,
        generation_id=generation_id,
        forecasts=forecasts,
        incoming=incoming,
        dispatch_used=dispatch_used,
        demand_series_by_key=demand_series_by_key,
        demand_p10_by_key=demand_p10_by_key,
        demand_p90_by_key=demand_p90_by_key,
        std_by_key=std_by_key,
        station_details=details_map,
        forecast_source=effective_source,
        model_version=active_model_ver,
        policy_mode=chosen_policy,
        fallback_reason=reason,
        single_action_limit=single_action_limit,
        max_total_liters=max_total_liters,
    )

    rule_anomalies = detect_rule_anomalies(
        snapshot, observed, details_map, forecasts, dispatch_used, arrival_list, event_list,
    )
    anomalies = merge_ml_anomalies(
        rule_anomalies, ml_anomaly_scores or [], snapshot=snapshot, station_details=details_map, events=event_list,
    )

    now_utc = datetime.now(UTC)
    return PlanningReport(
        generation_id=generation_id,
        plan_id=joint_plan.plan_id,
        tick=tick,
        current_tick=tick,
        history_ticks=history_ticks,
        horizon_ticks=horizon_ticks,
        observed_at=now_utc,
        forecast_generated_at=now_utc,
        age_ticks=0,
        cached=False,
        degraded=bool(ml_fallback_reason),
        degraded_reason=ml_fallback_reason,
        forecast_source=effective_source,
        model_version=active_model_ver,
        solver_status=joint_plan.solver_status,
        fallback_reason=joint_plan.fallback_reason,
        forecasts=forecasts,
        recommendations=recommendations,
        joint_plan=joint_plan,
        policy_mode=(
            "FORECAST_OPTIMIZER"
            if joint_plan.policy_mode == "OR_TOOLS_SEASONAL"
            else joint_plan.policy_mode
        ),
        fallback_active=fallback_active or joint_plan.solver_status != "OPTIMAL",
        anomalies=anomalies,
        events=event_list,
        supply_arrivals=arrival_list,
        simulator_metrics=simulator_metrics,
    )


def build_forecasts_response(
    snapshot: Snapshot,
    history: list[DemandObservation],
    history_ticks: int,
    horizon_ticks: int,
    *,
    generation_id: str,
    station_details: list[StationDetail] | None = None,
    regions: list[Region] | None = None,
    events: list[SimulatorEvent] | None = None,
    chronos_predictions: dict[tuple[str, Fuel], list[ForecastPoint]] | None = None,
    chronos_model_version: str | None = None,
    policy_override: str = "AUTO",
    fallback_reason: str | None = None,
    matured_accuracy: dict[str, Any] | None = None,
) -> ForecastsResponse:
    tick = snapshot.simulation.tick
    sim_time = snapshot.simulation.sim_time
    tick_minutes = snapshot.simulation.tick_minutes
    details_map = {item.id: item for item in (station_details or [])}
    region_factors = {item.id: item.demand_factor for item in (regions or [])}
    event_list = events or []
    observed = index_observations_by_series(history, current_tick=tick, history_ticks=history_ticks)

    series_out: list[ForecastSeries] = []
    any_chronos = False
    for station in snapshot.stations:
        detail = details_map.get(station.id)
        r_fac = region_factors.get(station.region_id, 1.0)
        for fuel in FUELS:
            key = (station.id, fuel)
            samples = sorted(observed[key].items())
            obs_list = [obs for _, obs in samples]
            if len(samples) < history_ticks:
                fb = fallback_demand_rate(samples, detail, r_fac, fuel, sim_time.hour, tick_minutes)
                pts = [
                    ForecastPoint(
                        tick=tick + off,
                        sim_time=sim_time + timedelta(minutes=off * tick_minutes),
                        p10=round(fb * 0.8, 3),
                        p50=fb,
                        p90=round(fb * 1.2, 3),
                        quantile_method="SEASONAL_SIGMA_BAND",
                        quality_warnings=["insufficient_history_profile_fallback"],
                    )
                    for off in range(horizon_ticks)
                ]
                s_src: ForecastSource = "PROFILE_FALLBACK"
                s_ver = "profile-fallback-v1"
                s_reason = fallback_reason or f"Insufficient history ({len(samples)}/{history_ticks} ticks)"
            else:
                _, _, pts = build_seasonal_forecast_points(
                    samples=samples, tick=tick, horizon_ticks=horizon_ticks,
                    tick_minutes=tick_minutes, sim_time=sim_time,
                    station_id=station.id, region_id=station.region_id,
                    station_detail=detail, events=event_list,
                )
                s_src = "SEASONAL"
                s_ver = "seasonal-v2.0"
                s_reason = fallback_reason
                if chronos_predictions and key in chronos_predictions and policy_override == "CHRONOS2":
                    c_pts = chronos_predictions[key]
                    if len(c_pts) == horizon_ticks:
                        pts = c_pts
                        s_src = "CHRONOS2"
                        s_ver = chronos_model_version or "amazon/chronos-2"
                        any_chronos = True

            series_out.append(ForecastSeries(
                station_id=station.id,
                station_name=station.name,
                fuel_type=fuel,
                source=s_src,
                model_version=s_ver,
                observations=len(samples),
                first_history_tick=samples[0][0] if samples else None,
                last_history_tick=samples[-1][0] if samples else None,
                history=obs_list,
                forecast=pts,
                fallback_reason=s_reason,
                mae_matured=matured_accuracy.get("mae") if matured_accuracy else None,
                wape_matured=matured_accuracy.get("wape") if matured_accuracy else None,
                p10_p90_coverage=matured_accuracy.get("p10_p90_coverage") if matured_accuracy else None,
            ))

    top_source: ForecastSource = "CHRONOS2" if any_chronos else ("SEASONAL" if any(s.observations >= history_ticks for s in series_out) else "PROFILE_FALLBACK")
    top_ver = (chronos_model_version or "amazon/chronos-2") if any_chronos else ("seasonal-v2.0" if top_source == "SEASONAL" else "profile-fallback-v1")
    return ForecastsResponse(
        generation_id=generation_id,
        as_of_tick=tick,
        tick_minutes=tick_minutes,
        horizon_ticks=horizon_ticks,
        history_ticks=history_ticks,
        source=top_source,
        model_version=top_ver,
        fallback_reason=fallback_reason,
        forecast_generated_at=sim_time,
        age_ticks=0,
        series=series_out,
    )


def build_shortages_response(
    *,
    snapshot: Snapshot,
    report: PlanningReport,
    allocations: list[Allocation],
) -> ShortagesResponse:
    tick = snapshot.simulation.tick
    incoming, _ = _extract_incoming_and_dispatch(
        snapshot=snapshot, allocations=allocations, horizon_ticks=report.horizon_ticks
    )
    in_total: dict[tuple[str, Fuel], float] = {k: sum(v) for k, v in incoming.items()}
    fc_map = {(f.station_id, f.fuel_type): f for f in report.forecasts}
    stations_map = {s.id: s for s in snapshot.stations}
    items: list[ShortageDetail] = []

    for f in report.forecasts:
        st = stations_map[f.station_id]
        cur = float(getattr(st.inventory, f.fuel_type))
        cap = float(getattr(st.capacity, f.fuel_type))
        inc = in_total.get((st.id, f.fuel_type), 0.0)
        unmet_p50 = f.unmet_liters if f.unmet_liters is not None else f.depletion_unmet_liters + f.outage_unmet_liters
        explanation = (
            f"OUTAGE: {f.outage_unmet_liters:,.1f} L unserved during outage."
            if st.status == "OUTAGE"
            else (
                f"Projected first unmet demand at tick {f.first_unmet_tick} ({f.time_to_stockout_minutes} min); "
                f"safety target {f.safety_stock_target_liters:,.0f} L."
                if f.first_unmet_tick is not None
                else f"No stockout in horizon; inventory position {cur + inc:,.0f} L vs {f.safety_stock_target_liters:,.0f} L safety target."
            )
        )
        items.append(ShortageDetail(
            station_id=st.id,
            station_name=st.name,
            region_id=st.region_id,
            fuel_type=f.fuel_type,
            station_status=st.status,
            current_inventory=cur,
            capacity=cap,
            incoming_committed_liters=round(inc, 3),
            inventory_position=round(cur + inc, 3),
            safety_stock_target=f.safety_stock_target_liters,
            shortage_risk=f.shortage_risk,
            stockout_probability=f.stockout_probability,
            probability_calibration=f.probability_calibration,
            first_zero_stock_tick=f.stockout_tick,
            first_unmet_tick=f.first_unmet_tick,
            time_to_stockout_minutes=f.time_to_stockout_minutes,
            stockout_duration_ticks=f.stockout_duration_ticks,
            unmet_p10_liters=f.p10_total_liters,
            unmet_p50_liters=unmet_p50,
            unmet_p90_liters=f.p90_total_liters,
            outage_unmet_liters=f.outage_unmet_liters,
            depletion_unmet_liters=f.depletion_unmet_liters,
            projected_trajectory=f.trajectory_p50,
            explanation=explanation,
        ))

    risk_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "UNKNOWN": 4}
    items.sort(key=lambda i: (risk_order.get(i.shortage_risk, 9), i.first_unmet_tick if i.first_unmet_tick is not None else 999999, -i.unmet_p50_liters))
    for rank, item in enumerate(items, start=1):
        item.urgency_rank = rank
    return ShortagesResponse(
        generation_id=report.generation_id,
        as_of_tick=tick,
        horizon_ticks=report.horizon_ticks,
        forecast_source=report.forecast_source,
        shortages=items,
    )
