from intelligence.contracts import ConfidenceLevel, RiskLevel


def build_recommendation_explanation(
    *,
    depot_name: str,
    station_name: str,
    fuel: str,
    quantity: float,
    arrival_tick: int,
    eta_ticks: int,
    eta_minutes: int,
    unmet_before: float,
    unmet_after: float,
    unmet_avoided: float,
    risk_before: RiskLevel,
    risk_after: RiskLevel,
    safety_target: float,
    inv_after: float,
    rolling_mean: float,
    projected_avg: float,
    stockout_tick_before: int | None,
    demand_multiplier: float,
    route_max: float,
    dispatch_remaining: float,
    station_room_now: float,
    room_at_arrival: float,
    confidence: ConfidenceLevel,
    forecast_source: str,
) -> tuple[str, list[str], list[str]]:
    if unmet_avoided > 0:
        rationale = (
            f"Reduces projected unmet {fuel.lower()} demand by {unmet_avoided:,.1f} L "
            f"({risk_before} → {risk_after} risk); arrives tick {arrival_tick} ({eta_minutes} min)."
        )
    else:
        rationale = (
            f"Preventive safety-stock dispatch of {quantity:,.0f} L toward {safety_target:,.0f} L target "
            f"({risk_before} → {risk_after}); arrives tick {arrival_tick} ({eta_minutes} min)."
        )

    signals = [
        f"Source {forecast_source} ({confidence} conf.): mean {rolling_mean:.1f} L/tick, proj. {projected_avg:.1f} L/tick",
        f"Stockout before: tick {stockout_tick_before}" if stockout_tick_before is not None else f"Projected reserve after ETA {inv_after:,.0f} L vs {safety_target:,.0f} L target",
    ]
    if abs(demand_multiplier - 1.0) > 0.01:
        signals.append(f"Active demand multiplier {demand_multiplier:.2f}x")

    constraints = [
        f"Route max {route_max:,.0f} L",
        f"{depot_name} dispatch remaining {dispatch_remaining:,.0f} L",
        f"{station_name} immediate room {station_room_now:,.0f} L (arrival room {room_at_arrival:,.0f} L)",
    ]
    return rationale, signals, constraints
