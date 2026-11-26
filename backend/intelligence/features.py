from datetime import datetime, timedelta
import math
from statistics import mean, pstdev
from typing import Any

from intelligence.collector import hour_factor
from intelligence.contracts import (
    FEATURE_SCHEMA_VERSION,
    Allocation,
    DemandObservation,
    Fuel,
    SimulatorEvent,
    StationDetail,
)
from schemas import Snapshot


def spike_multiplier_at(
    events: list[SimulatorEvent],
    station_id: str,
    region_id: str,
    target_tick: int,
) -> float:
    """Return combined demand_spike multiplier active at target_tick from known events."""
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


def build_causal_features(
    *,
    snapshot: Snapshot,
    station_id: str,
    fuel: Fuel,
    station_detail: StationDetail | None,
    region_factor: float,
    samples: list[tuple[int, DemandObservation]],
    allocations: list[Allocation],
    events: list[SimulatorEvent],
    horizon_ticks: int,
) -> dict[str, Any]:
    """Construct causal, leak-free features for issue tick snapshot.simulation.tick."""
    tick = snapshot.simulation.tick
    sim_time = snapshot.simulation.sim_time
    tick_minutes = snapshot.simulation.tick_minutes
    profile = station_detail.demand_profile if station_detail else "urban_high"
    station = next(s for s in snapshot.stations if s.id == station_id)
    stock = getattr(station.inventory, fuel)
    capacity = getattr(station.capacity, fuel)
    demand_mult = station_detail.demand_multiplier if station_detail else 1.0

    normalized_values: list[float] = []
    raw_values: list[float] = []
    for s_tick, obs in samples:
        s_time = sim_time - timedelta(minutes=(tick - s_tick) * tick_minutes)
        h_f = hour_factor(profile, s_time.hour)
        s_f = spike_multiplier_at(events, station_id, station.region_id, s_tick)
        raw_values.append(obs.demand_liters)
        normalized_values.append(obs.demand_liters / max(0.01, h_f * s_f))

    def lag(k: int) -> float | None:
        return raw_values[-k] if len(raw_values) >= k else None

    roll_8 = mean(raw_values[-8:]) if raw_values else None
    roll_32 = mean(raw_values[-32:]) if raw_values else None
    roll_std_8 = pstdev(raw_values[-8:]) if len(raw_values[-8:]) > 1 else 0.0

    inflight = sum(
        a.quantity
        for a in allocations
        if a.destination_station_id == station_id
        and a.fuel_type == fuel
        and a.status in ("PENDING", "IN_TRANSIT")
    )
    min_lead_ticks = min(
        (r.transit_ticks for r in snapshot.routes if r.destination_station_id == station_id and r.status == "AVAILABLE"),
        default=None,
    )

    future_covariates: list[dict[str, Any]] = []
    for offset in range(horizon_ticks):
        f_tick = tick + offset
        f_time = sim_time + timedelta(minutes=offset * tick_minutes)
        angle = 2.0 * math.pi * ((f_time.hour * 60 + f_time.minute) / 1440.0)
        h_f = hour_factor(profile, f_time.hour)
        # Only events known at issue time (or current multiplier at offset 0 if event missing)
        s_f = spike_multiplier_at(events, station_id, station.region_id, f_tick)
        if offset == 0 and s_f == 1.0 and demand_mult != 1.0:
            s_f = demand_mult
        future_covariates.append({
            "tick": f_tick,
            "sim_time": f_time.isoformat(),
            "hour": f_time.hour,
            "sin_hour": round(math.sin(angle), 5),
            "cos_hour": round(math.cos(angle), 5),
            "hour_factor": h_f,
            "known_spike_multiplier": round(s_f, 4),
            "region_factor": region_factor,
        })

    return {
        "feature_version": FEATURE_SCHEMA_VERSION,
        "issue_tick": tick,
        "station_id": station_id,
        "fuel_type": fuel,
        "profile": profile,
        "region_factor": region_factor,
        "demand_multiplier": demand_mult,
        "inventory_ratio": round(stock / capacity, 4) if capacity > 0 else 1.0,
        "inflight_liters": inflight,
        "min_lead_ticks": min_lead_ticks,
        "outage_active": station.status == "OUTAGE",
        "rolling_mean_8": round(roll_8, 3) if roll_8 is not None else None,
        "rolling_mean_32": round(roll_32, 3) if roll_32 is not None else None,
        "rolling_std_8": round(roll_std_8, 3),
        "normalized_base_rate": round(mean(normalized_values), 3) if normalized_values else None,
        "lags": {"t_minus_1": lag(1), "t_minus_2": lag(2), "t_minus_4": lag(4), "t_minus_8": lag(8)},
        "future_covariates": future_covariates,
    }
