from collections import defaultdict
from datetime import datetime, timedelta
from statistics import mean
from typing import Any

from intelligence.contracts import DemandObservation, Fuel, FUELS, StationDetail


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


def index_observations_by_series(
    observations: list[DemandObservation],
    *,
    current_tick: int,
    history_ticks: int | None = None,
) -> dict[tuple[str, Fuel], dict[int, DemandObservation]]:
    """Index unique completed observations up to current_tick - 1, preserving latest entry without zero-filling gaps."""
    indexed: dict[tuple[str, Fuel], dict[int, DemandObservation]] = defaultdict(dict)
    min_tick = (current_tick - history_ticks) if history_ticks is not None else 0
    for entry in observations:
        if entry.tick >= current_tick and current_tick > 0:
            continue
        if entry.tick < min_tick:
            continue
        indexed[(entry.station_id, entry.fuel_type)][entry.tick] = entry
    return indexed


def regularized_series(
    obs_by_tick: dict[int, DemandObservation],
    *,
    start_tick: int,
    end_tick_exclusive: int,
    current_tick: int,
    tick_minutes: int,
    current_sim_time: datetime,
    station_detail: StationDetail | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Build regularly spaced time series over [start_tick, end_tick_exclusive), marking gaps explicitly."""
    rows: list[dict[str, Any]] = []
    gap_count = 0
    profile = station_detail.demand_profile if station_detail else "urban_high"
    for tick in range(start_tick, end_tick_exclusive):
        t_time = current_sim_time - timedelta(minutes=(current_tick - tick) * tick_minutes)
        obs = obs_by_tick.get(tick)
        if obs is None:
            gap_count += 1
        rows.append({
            "tick": tick,
            "sim_time": t_time.isoformat(),
            "demand_liters": float(obs.demand_liters) if obs is not None else None,
            "hour": t_time.hour,
            "hour_factor": hour_factor(profile, t_time.hour),
            "missing": obs is None,
        })
    return rows, gap_count
