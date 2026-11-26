from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from schemas import FuelVolumes, Identifier, Liters, SnapshotModel

Fuel = Literal["DIESEL", "PETROL", "OCTANE"]
FUELS: tuple[Fuel, ...] = ("DIESEL", "PETROL", "OCTANE")
RiskLevel = Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]
ForecastRisk = Literal["CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"]
ConfidenceLevel = Literal["HIGH", "MEDIUM", "LOW"]
ForecastSource = Literal["CHRONOS2", "SEASONAL", "PROFILE_FALLBACK", "REORDER_FALLBACK"]
DetectorSource = Literal["RULE", "ML", "HYBRID"]
PolicyMode = Literal[
    "OR_TOOLS_SEASONAL",
    "OR_TOOLS_CHRONOS2",
    "FORECAST_OPTIMIZER",
    "SAFE_REORDER",
    "REORDER_FALLBACK",
]

FEATURE_SCHEMA_VERSION = "v1.0.0"

# Documented daily demand profiles (liters/day) from simulator integration guide §8.5.
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
    served_liters: Liters | None = None
    unmet_liters: Liters | None = None


class Allocation(SnapshotModel):
    id: int | None = None
    idempotency_key: str | None = None
    source_depot_id: Identifier
    destination_station_id: Identifier
    route_id: Identifier | None = None
    fuel_type: Fuel
    quantity: Liters
    created_tick: int = Field(ge=0)
    departure_tick: int | None = Field(default=None, ge=0)
    expected_arrival_tick: int | None = Field(default=None, ge=0)
    actual_arrival_tick: int | None = Field(default=None, ge=0)
    status: Literal["PENDING", "IN_TRANSIT", "ARRIVED", "FAILED", "CANCELLED"]
    failure_reason: str | None = None


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
    dedup_key: str | None = None
    category: Literal[
        "DEMAND_SPIKE",
        "DEMAND_DROP",
        "STOCKOUT_RISK",
        "ROUTE_DISRUPTED",
        "STATION_OUTAGE",
        "DEPOT_CONSTRAINED",
        "DISPATCH_BOTTLENECK",
        "SUPPLY_DELAYED",
        "SUPPLY_SHORTFALL",
        "UNEXPECTED_DEMAND_ANOMALY",
        "INVENTORY_RESIDUAL_ANOMALY",
    ]
    severity: Literal["CRITICAL", "WARNING"]
    entity_type: Literal["station", "depot", "route", "supply", "system"]
    entity_id: Identifier
    fuel_type: Fuel | None = None
    summary: str
    source: DetectorSource = "RULE"
    status: Literal["OPEN", "RESOLVED"] = "OPEN"
    first_tick: int | None = None
    last_tick: int | None = None
    score: float | None = None
    expected_value: float | None = None
    observed_value: float | None = None
    baseline_value: float | None = None
    z_score: float | None = None
    evidence: list[str] = Field(default_factory=list)
    suggestion: str | None = None


class ForecastPoint(SnapshotModel):
    tick: int = Field(ge=0)
    sim_time: datetime
    p10: Liters | None = None
    p50: Liters
    p90: Liters | None = None
    actual_demand_liters: Liters | None = None
    quantile_method: Literal["CHRONOS2_QUANTILE", "SEASONAL_SIGMA_BAND", "POINT_ONLY"] = "SEASONAL_SIGMA_BAND"
    quality_warnings: list[str] = Field(default_factory=list)


class ForecastSeries(SnapshotModel):
    station_id: Identifier
    station_name: str
    fuel_type: Fuel
    source: ForecastSource
    model_version: str
    observations: int
    first_history_tick: int | None
    last_history_tick: int | None
    history: list[DemandObservation] = Field(default_factory=list)
    forecast: list[ForecastPoint] = Field(default_factory=list)
    fallback_reason: str | None = None
    mae_matured: float | None = None
    wape_matured: float | None = None
    p10_p90_coverage: float | None = None


class ForecastsResponse(SnapshotModel):
    generation_id: str
    as_of_tick: int = Field(ge=0)
    tick_minutes: int = Field(gt=0)
    horizon_ticks: int = Field(gt=0)
    history_ticks: int = Field(gt=0)
    source: ForecastSource
    model_version: str
    feature_version: str = FEATURE_SCHEMA_VERSION
    fallback_reason: str | None = None
    forecast_generated_at: datetime
    age_ticks: int = Field(default=0, ge=0)
    series: list[ForecastSeries] = Field(default_factory=list)


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
    p10_total_liters: Liters | None = None
    p50_total_liters: Liters | None = None
    p90_total_liters: Liters | None = None
    confidence: ConfidenceLevel = "LOW"
    confidence_score: float = 0.0
    shortage_risk: ForecastRisk = "UNKNOWN"
    stockout_probability: float = 0.0
    probability_calibration: Literal["SCENARIO_HEURISTIC", "EMPIRICAL_UNCALIBRATED", "HOLD_OUT_VALIDATED"] = (
        "SCENARIO_HEURISTIC"
    )
    outage_unmet_liters: Liters = 0.0
    depletion_unmet_liters: Liters = 0.0
    first_unmet_tick: int | None = None
    time_to_stockout_minutes: int | None = None
    stockout_duration_ticks: int = 0
    safety_stock_target_liters: Liters = 0.0
    fallback_used: bool = False
    forecast_source: ForecastSource = "SEASONAL"
    model_version: str = "seasonal-v1"
    trajectory_p50: list[Liters] = Field(default_factory=list)


class Recommendation(SnapshotModel):
    plan_id: str = "plan-ephemeral"
    generation_id: str | None = None
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
    policy_mode: PolicyMode = "OR_TOOLS_SEASONAL"
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


class PlanPreviewSummary(SnapshotModel):
    plan_id: str
    generation_id: str
    as_of_tick: int
    horizon_ticks: int
    policy_mode: PolicyMode
    forecast_source: ForecastSource
    model_version: str
    solver_status: str
    solver_runtime_ms: float = 0.0
    config_hash: str
    fallback_reason: str | None = None
    total_allocated_liters: Liters = 0.0
    unmet_before_liters: Liters = 0.0
    unmet_after_liters: Liters = 0.0
    unmet_avoided_liters: Liters = 0.0
    no_action_unmet_liters: Liters = 0.0
    safe_baseline_unmet_liters: Liters = 0.0
    p10_unmet_after_liters: Liters | None = None
    p90_unmet_after_liters: Liters | None = None
    binding_constraints: list[str] = Field(default_factory=list)
    shipments: list[Recommendation] = Field(default_factory=list)


class PlanningReport(SnapshotModel):
    generation_id: str = "default"
    plan_id: str = "plan-ephemeral"
    tick: int
    current_tick: int | None = None
    history_ticks: int
    horizon_ticks: int
    observed_at: datetime | None = None
    forecast_generated_at: datetime | None = None
    age_ticks: int = 0
    cached: bool = False
    degraded: bool = False
    degraded_reason: str | None = None
    forecast_source: ForecastSource = "SEASONAL"
    model_version: str = "seasonal-v1"
    solver_status: str = "OPTIMAL"
    fallback_reason: str | None = None
    forecasts: list[Forecast]
    recommendations: list[Recommendation]
    joint_plan: PlanPreviewSummary | None = None
    policy_mode: PolicyMode = "OR_TOOLS_SEASONAL"
    fallback_active: bool = False
    anomalies: list[Anomaly] = Field(default_factory=list)
    events: list[SimulatorEvent] = Field(default_factory=list)
    supply_arrivals: list[SupplyArrival] = Field(default_factory=list)
    simulator_metrics: SimulatorMetrics | None = None


class ShortageDetail(SnapshotModel):
    station_id: Identifier
    station_name: str
    region_id: Identifier
    fuel_type: Fuel
    station_status: Literal["OPEN", "OUTAGE"]
    current_inventory: Liters
    capacity: Liters
    incoming_committed_liters: Liters
    inventory_position: Liters
    safety_stock_target: Liters
    urgency_rank: int = 0
    shortage_risk: ForecastRisk
    stockout_probability: float
    probability_calibration: str
    first_zero_stock_tick: int | None
    first_unmet_tick: int | None
    time_to_stockout_minutes: int | None
    stockout_duration_ticks: int
    unmet_p10_liters: Liters | None = None
    unmet_p50_liters: Liters | None = None
    unmet_p90_liters: Liters | None = None
    outage_unmet_liters: Liters = 0.0
    depletion_unmet_liters: Liters = 0.0
    projected_trajectory: list[Liters] = Field(default_factory=list)
    explanation: str = ""


class ShortagesResponse(SnapshotModel):
    generation_id: str
    as_of_tick: int
    horizon_ticks: int
    forecast_source: ForecastSource
    shortages: list[ShortageDetail]


class AnomaliesResponse(SnapshotModel):
    generation_id: str
    as_of_tick: int
    detector_source: DetectorSource
    anomaly_model_version: str | None = None
    total: int
    open_count: int
    resolved_count: int
    incidents: list[Anomaly]


class SimulateShipmentInput(SnapshotModel):
    source_depot_id: Identifier
    destination_station_id: Identifier
    route_id: Identifier
    fuel_type: Fuel
    quantity: Liters


class OptimizeRequest(BaseModel):
    model_config = ConfigDict(strict=False)
    history_ticks: int = Field(default=8, ge=1, le=96)
    horizon_ticks: int = Field(default=96, ge=1, le=288)
    policy_override: Literal["AUTO", "SEASONAL", "CHRONOS2", "SAFE_REORDER"] = "AUTO"
    single_action_limit: int | None = Field(default=None, ge=1, le=20)
    max_total_liters: float | None = Field(default=None, gt=0, le=500_000)


class SimulateRequest(BaseModel):
    model_config = ConfigDict(strict=False)
    history_ticks: int = Field(default=8, ge=1, le=96)
    horizon_ticks: int = Field(default=96, ge=1, le=288)
    shipments: list[SimulateShipmentInput] = Field(default_factory=list, max_length=25)


class SimulateResponse(SnapshotModel):
    generation_id: str
    as_of_tick: int
    horizon_ticks: int
    valid: bool
    validation_errors: list[str] = Field(default_factory=list)
    forecast_source: ForecastSource
    total_allocated_liters: Liters
    unmet_before_liters: Liters
    unmet_after_liters: Liters
    unmet_avoided_liters: Liters
    station_impacts: list[dict[str, Any]] = Field(default_factory=list)
    depot_utilization: dict[str, FuelVolumes | dict[str, float]] = Field(default_factory=dict)


class GenerationInfo(SnapshotModel):
    generation_id: str
    sim_instance_id: int | None = None
    scenario_id: str
    scenario_version: str
    seed: int
    last_seen_tick: int
    last_seen_sim_time: datetime
    status: Literal["ACTIVE", "SUPERSEDED", "REBIND_REQUIRED"]
    reason: str | None = None
    started_at: datetime


class DataQualityReport(SnapshotModel):
    generation_id: str
    collected_rows: int
    expected_rows: int
    missing_ticks: int
    duplicate_rows_ignored: int
    future_ticks_rejected: int
    min_tick: int | None = None
    max_tick: int | None = None
