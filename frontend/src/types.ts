export type Fuel = "DIESEL" | "PETROL" | "OCTANE";
export type RiskLevel = "CRITICAL" | "HIGH" | "MEDIUM" | "LOW";
export type ForecastRisk = RiskLevel | "UNKNOWN";

export interface AuthStatus {
  auth_required: boolean;
  authenticated: boolean;
  username: string | null;
  role: "operator" | "viewer" | null;
}

export interface DemandObservation {
  station_id: string;
  fuel_type: Fuel;
  tick: number;
  demand_liters: number;
  served_liters?: number | null;
  unmet_liters?: number | null;
}

export interface ForecastPoint {
  tick: number;
  sim_time: string;
  p10: number | null;
  p50: number;
  p90: number | null;
  actual_demand_liters?: number | null;
  quantile_method: "CHRONOS2_QUANTILE" | "SEASONAL_SIGMA_BAND" | "POINT_ONLY";
  quality_warnings: string[];
}

export interface ForecastSeries {
  station_id: string;
  station_name: string;
  fuel_type: Fuel;
  source: "CHRONOS2" | "SEASONAL" | "PROFILE_FALLBACK" | "REORDER_FALLBACK";
  model_version: string;
  observations: number;
  first_history_tick: number | null;
  last_history_tick: number | null;
  history: DemandObservation[];
  forecast: ForecastPoint[];
  fallback_reason: string | null;
  mae_matured: number | null;
  wape_matured: number | null;
  p10_p90_coverage: number | null;
}

export interface ForecastsResponse {
  generation_id: string;
  as_of_tick: number;
  tick_minutes: number;
  horizon_ticks: number;
  history_ticks: number;
  source: string;
  model_version: string;
  feature_version: string;
  fallback_reason: string | null;
  forecast_generated_at: string;
  age_ticks: number;
  series: ForecastSeries[];
}

export interface ShortageDetail {
  station_id: string;
  station_name: string;
  region_id: string;
  fuel_type: Fuel;
  station_status: "OPEN" | "OUTAGE";
  current_inventory: number;
  capacity: number;
  incoming_committed_liters: number;
  inventory_position: number;
  safety_stock_target: number;
  urgency_rank: number;
  shortage_risk: ForecastRisk;
  stockout_probability: number;
  probability_calibration: string;
  first_zero_stock_tick: number | null;
  first_unmet_tick: number | null;
  time_to_stockout_minutes: number | null;
  stockout_duration_ticks: number;
  unmet_p10_liters: number | null;
  unmet_p50_liters: number | null;
  unmet_p90_liters: number | null;
  outage_unmet_liters: number;
  depletion_unmet_liters: number;
  projected_trajectory: number[];
  explanation: string;
}

export interface ShortagesResponse {
  generation_id: string;
  as_of_tick: number;
  horizon_ticks: number;
  forecast_source: string;
  shortages: ShortageDetail[];
}

export interface IncidentItem {
  id: string;
  dedup_key?: string | null;
  category: string;
  severity: "CRITICAL" | "WARNING";
  entity_type: string;
  entity_id: string;
  fuel_type: Fuel | null;
  summary: string;
  source: "RULE" | "ML" | "HYBRID";
  status: "OPEN" | "RESOLVED";
  first_tick: number | null;
  last_tick: number | null;
  score: number | null;
  expected_value: number | null;
  observed_value: number | null;
  baseline_value: number | null;
  z_score: number | null;
  evidence: string[];
  suggestion: string | null;
}

export interface IncidentsResponse {
  generation_id: string;
  as_of_tick: number;
  detector_source: "RULE" | "ML" | "HYBRID";
  anomaly_model_version: string | null;
  total: number;
  open_count: number;
  resolved_count: number;
  incidents: IncidentItem[];
}

export interface PlanRecommendation {
  plan_id: string;
  generation_id: string | null;
  route_id: string;
  source_depot_id: string;
  source_depot_name: string;
  destination_station_id: string;
  destination_station_name: string;
  fuel_type: Fuel;
  quantity_liters: number;
  arrival_tick: number;
  eta_ticks: number;
  eta_minutes: number;
  unmet_before_liters: number;
  unmet_after_liters: number;
  unmet_avoided_liters: number;
  policy_mode: string;
  priority_score: number;
  stockout_tick_before: number | null;
  stockout_tick_after: number | null;
  risk_before: RiskLevel;
  risk_after: RiskLevel;
  confidence: "HIGH" | "MEDIUM" | "LOW";
  rationale: string;
  signals: string[];
  constraints: string[];
  alternative_routes: string[];
}

export interface JointPlanSummary {
  plan_id: string;
  generation_id: string;
  as_of_tick: number;
  horizon_ticks: number;
  policy_mode: string;
  forecast_source: string;
  model_version: string;
  solver_status: string;
  solver_runtime_ms: number;
  config_hash: string;
  fallback_reason: string | null;
  total_allocated_liters: number;
  unmet_before_liters: number;
  unmet_after_liters: number;
  unmet_avoided_liters: number;
  no_action_unmet_liters: number;
  safe_baseline_unmet_liters: number;
  p10_unmet_after_liters: number | null;
  p90_unmet_after_liters: number | null;
  binding_constraints: string[];
  shipments: PlanRecommendation[];
}

export interface SimulateResponse {
  generation_id: string;
  as_of_tick: number;
  horizon_ticks: number;
  valid: boolean;
  validation_errors: string[];
  forecast_source: string;
  total_allocated_liters: number;
  unmet_before_liters: number;
  unmet_after_liters: number;
  unmet_avoided_liters: number;
  station_impacts: Array<{
    route_id: string;
    station_id: string;
    fuel_type: Fuel;
    quantity: number;
    eta_ticks: number;
  }>;
}

export interface GenerationInfo {
  generation_id: string;
  sim_instance_id: number | null;
  scenario_id: string;
  scenario_version: string;
  seed: number;
  last_seen_tick: number;
  last_seen_sim_time: string;
  status: "ACTIVE" | "SUPERSEDED" | "REBIND_REQUIRED";
  reason: string | null;
  started_at: string;
}

export interface ModelsStatusResponse {
  default_forecast_policy: string;
  experimental_ml_policy: string;
  ml_service_configured: boolean;
  ml_service_status:
    | string
    | {
        status?: string;
        chronos2?: string | { status?: string; [key: string]: unknown };
        isolation_forest?: string | { status?: string; [key: string]: unknown };
        [key: string]: unknown;
      }
    | null;
  last_ml_latency_ms: number | null;
  last_ml_error: string | null;
  manifest: Record<string, unknown> | null;
}

export interface ForecastHorizonModelMetrics {
  points?: number;
  mae?: number;
  rmse?: number;
  wape?: number;
  p10_p90_coverage?: number;
}

export interface PolicyAggregateMetric {
  budget_tier?: string;
  max_shipments?: number | null;
  max_total_liters?: number | null;
  mean_service_level: number;
  measured_window_service_level?: number;
  total_unmet_liters: number;
  outage_unmet_liters?: number;
  depletion_unmet_liters?: number;
  total_allocated_liters: number;
  total_decisions: number;
  total_failures: number;
  chronos2_steps?: number;
  fallback_steps?: number;
  mean_decision_latency_ms: number;
}

export interface EvaluationSummaryResponse {
  forecast_evaluation: {
    model_id?: string;
    model_revision?: string;
    load_time_ms?: number;
    datasets?: Array<{
      dataset: string;
      horizons: Record<string, Record<string, ForecastHorizonModelMetrics>>;
    }>;
  } | null;
  policy_comparison: {
    methodology?: Record<string, unknown>;
    trials?: Array<Record<string, unknown>>;
    aggregate?: Record<string, PolicyAggregateMetric>;
  } | null;
  model_manifest: Record<string, unknown> | null;
  resilience_summary?: {
    total: number;
    passed: number;
    failed: number;
  } | null;
  benchmark_summary?: {
    scenarios: Record<
      string,
      {
        rps?: number;
        duration_s?: number;
        error_count?: number;
        latency_ms?: { p50?: number; p95?: number; p99?: number };
      }
    >;
    prometheus_target_health?: string | null;
  } | null;
  ml_verification?: {
    checks?: Record<string, { passed?: boolean }>;
  } | null;
  artifact_source?: string;
  live_matured_accuracy: {
    matured_points?: number;
    mae?: number | null;
    rmse?: number | null;
    wape?: number | null;
    p10_p90_coverage?: number | null;
  } | null;
  status: string;
}

export interface PlanApprovalItemResult {
  item_index: number;
  intent_id: string;
  proposal: {
    source_depot_id: string;
    destination_station_id: string;
    route_id: string;
    fuel_type: Fuel;
    quantity: number;
    idempotency_key: string;
  };
  eta_ticks: number | null;
  state: "UNSENT" | "READY" | "UNKNOWN" | "CONFIRMED" | "REJECTED";
  message: string | null;
  shipment: {
    id: number;
    status: string;
    failure_reason: string | null;
    expected_arrival_tick?: number | null;
    actual_arrival_tick?: number | null;
  } | null;
  updated_at: string | null;
}

export interface PlanApprovalResult {
  plan_id: string;
  generation_id: string;
  as_of_tick: number;
  current_tick: number | null;
  actor: string;
  decision_rationale: string | null;
  state: "CONFIRMED" | "PARTIAL" | "STOPPED" | "UNKNOWN";
  stopped_reason: string | null;
  reconcile_required: boolean;
  requires_new_plan: boolean;
  total_shipments: number;
  confirmed_count: number;
  rejected_count: number;
  unknown_count: number;
  unsent_count: number;
  items: PlanApprovalItemResult[];
  created_at: string | null;
  updated_at: string | null;
}

export interface LatestPlanResponse {
  generation_id: string;
  plan: JointPlanSummary | null;
  approval: PlanApprovalResult | null;
}
