from datetime import timedelta
import math
from statistics import mean, pstdev
from typing import Any

from intelligence.collector import hour_factor
from intelligence.contracts import ConfidenceLevel, DemandObservation, ForecastPoint, ForecastSource, Fuel, SimulatorEvent, StationDetail
from intelligence.features import spike_multiplier_at


MAX_FORECAST_HORIZON_TICKS = 288


def _bounded_horizon(horizon_ticks: int) -> int:
    horizon = int(horizon_ticks)
    if horizon < 1 or horizon > MAX_FORECAST_HORIZON_TICKS:
        raise ValueError(f"horizon_ticks must be within 1..{MAX_FORECAST_HORIZON_TICKS}, got {horizon_ticks}")
    return min(MAX_FORECAST_HORIZON_TICKS, max(1, horizon))


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


def build_seasonal_forecast_points(
    *,
    samples: list[tuple[int, float | DemandObservation]],
    tick: int,
    horizon_ticks: int,
    tick_minutes: int,
    sim_time: Any,
    station_id: str,
    region_id: str,
    station_detail: StationDetail | None,
    events: list[SimulatorEvent],
) -> tuple[float, float, list[ForecastPoint]]:
    """Return raw rolling mean, normalized residual std, and p10/p50/p90 seasonal forecast points."""
    numeric_samples = [
        (t, val.demand_liters if isinstance(val, DemandObservation) else float(val))
        for t, val in samples
    ]
    rolling_mean = mean(liters for _, liters in numeric_samples)
    profile = station_detail.demand_profile if station_detail else "urban_high"

    normalized: list[float] = []
    for sample_tick, liters in numeric_samples:
        sample_time = sim_time - timedelta(minutes=(tick - sample_tick) * tick_minutes)
        h_factor = hour_factor(profile, sample_time.hour)
        s_factor = spike_multiplier_at(events, station_id, region_id, sample_tick)
        normalized.append(liters / max(0.01, h_factor * s_factor))

    base_rate = mean(normalized) if normalized else rolling_mean
    norm_std = pstdev(normalized) if len(normalized) > 1 else max(0.05 * base_rate, 1.0)
    current_mult = station_detail.demand_multiplier if station_detail else 1.0

    points: list[ForecastPoint] = []
    bounded_horizon = _bounded_horizon(horizon_ticks)
    for offset in range(bounded_horizon):
        future_tick = tick + offset
        future_time = sim_time + timedelta(minutes=offset * tick_minutes)
        h_factor = hour_factor(profile, future_time.hour)
        s_factor = spike_multiplier_at(events, station_id, region_id, future_tick)
        if offset == 0 and math.isclose(s_factor, 1.0, abs_tol=1e-9) and not math.isclose(current_mult, 1.0, abs_tol=1e-9):
            s_factor = current_mult
        scale = h_factor * s_factor
        p50 = round(max(0.0, base_rate * scale), 3)
        band = 1.2816 * norm_std * scale
        p10 = round(max(0.0, p50 - band), 3)
        p90 = round(max(p50, p50 + band), 3)
        points.append(
            ForecastPoint(
                tick=future_tick,
                sim_time=future_time,
                p10=p10,
                p50=p50,
                p90=p90,
                quantile_method="SEASONAL_SIGMA_BAND",
                quality_warnings=[],
            )
        )
    return rolling_mean, round(norm_std, 3), points


def build_demand_series(
    samples: list[tuple[int, float | DemandObservation]],
    tick: int,
    horizon_ticks: int,
    tick_minutes: int,
    sim_time: Any,
    station_id: str,
    region_id: str,
    station_detail: StationDetail | None,
    events: list[SimulatorEvent],
) -> tuple[float, list[float]]:
    rolling_mean, _, pts = build_seasonal_forecast_points(
        samples=samples, tick=tick, horizon_ticks=horizon_ticks,
        tick_minutes=tick_minutes, sim_time=sim_time,
        station_id=station_id, region_id=region_id,
        station_detail=station_detail, events=events,
    )
    return rolling_mean, [pt.p50 for pt in pts]
