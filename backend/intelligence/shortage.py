import math
from statistics import mean

from intelligence.contracts import ForecastRisk, RiskLevel


def project(
    stock: float,
    capacity: float,
    demand: float | list[float],
    arrivals: list[float],
    station_open: bool,
) -> tuple[int | None, list[float], list[float]]:
    """Simulate tick-wise inventory balance: arrivals capped at capacity before positive demand."""
    demands = [demand] * len(arrivals) if isinstance(demand, (int, float)) else demand
    depleted: int | None = None
    unmet: list[float] = []
    room: list[float] = []
    for offset, arriving in enumerate(arrivals):
        tick_demand = max(0.0, float(demands[offset]))
        stock += min(max(0.0, arriving), max(0.0, capacity - stock))
        room.append(max(0.0, capacity - stock))
        served = min(stock, tick_demand) if station_open else 0.0
        unmet.append(round(tick_demand - served, 6))
        stock -= served
        if stock <= 1e-9 and tick_demand > 0 and depleted is None:
            depleted = offset
    return depleted, unmet, room


def project_detailed(
    stock: float,
    capacity: float,
    demand_series: list[float],
    arrivals: list[float],
    station_open: bool,
) -> dict:
    """Return full trajectory distinguishing zero-stock depletion from station outage."""
    horizon = len(arrivals)
    cur = stock
    first_zero_stock: int | None = None
    first_unmet: int | None = None
    stockout_duration = 0
    unmet: list[float] = []
    room: list[float] = []
    trajectory: list[float] = []
    outage_unmet = 0.0
    depletion_unmet = 0.0

    for offset in range(horizon):
        tick_demand = max(0.0, float(demand_series[offset]))
        cur += min(max(0.0, arrivals[offset]), max(0.0, capacity - cur))
        room.append(max(0.0, capacity - cur))
        if station_open:
            served = min(cur, tick_demand)
            tick_unmet = tick_demand - served
            cur -= served
            depletion_unmet += tick_unmet
        else:
            served = 0.0
            tick_unmet = tick_demand
            outage_unmet += tick_unmet

        unmet.append(tick_unmet)
        trajectory.append(round(cur, 3))
        if cur <= 1e-9 and first_zero_stock is None and (tick_demand > 0 or stock <= 1e-9):
            first_zero_stock = offset
        if tick_unmet > 1e-6:
            stockout_duration += 1
            if first_unmet is None:
                first_unmet = offset

    return {
        "depleted_offset": first_unmet if station_open else first_zero_stock,
        "first_zero_stock_offset": first_zero_stock,
        "first_unmet_offset": first_unmet,
        "stockout_duration_ticks": stockout_duration,
        "unmet": unmet,
        "room": room,
        "trajectory": trajectory,
        "outage_unmet_liters": round(outage_unmet, 3),
        "depletion_unmet_liters": round(depletion_unmet, 3),
    }


def compute_safety_stock_target(
    *,
    capacity: float,
    demand_series: list[float],
    demand_std: float,
    min_lead_ticks: int,
    cadence_ticks: int = 6,
    station_open: bool = True,
) -> float:
    """Lead-time + replenishment-cadence demand plus z=1.65 variability buffer."""
    if not station_open or capacity <= 0:
        return 0.0
    window = max(2, min_lead_ticks + cadence_ticks)
    lead_demand = sum(demand_series[:window]) if demand_series else 0.0
    if len(demand_series) < window and demand_series:
        lead_demand = mean(demand_series) * window
    variability_buffer = 1.65 * max(0.0, demand_std) * math.sqrt(window)
    target = max(capacity * 0.35, lead_demand + variability_buffer)
    return round(min(capacity * 0.72, target), 3)


def classify_risk(
    stock: float,
    capacity: float,
    depleted_offset: int | None,
    unmet_total: float,
    *,
    safety_target: float = 0.0,
) -> RiskLevel:
    ratio = stock / capacity if capacity > 0 else 1.0
    if (depleted_offset is not None and depleted_offset <= 12) or ratio < 0.20:
        return "CRITICAL"
    if (depleted_offset is not None and depleted_offset <= 32) or ratio < 0.35:
        return "HIGH"
    if depleted_offset is not None or unmet_total > 0 or stock < safety_target or ratio < 0.55:
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
    """Scenario heuristic score reflecting buffer relative to demand spread (not calibrated empirical probability)."""
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
