import { useState } from "react";
import useSWR from "swr";
import { fetchJson } from "./api";
import { type Proposal, type useApprovals } from "./Approvals";
import {
  type Anomaly,
  IntelligenceContextPanel,
  type SimulatorEvent,
  type SupplyArrival,
} from "./ObservabilityPanel";

type Forecast = {
  station_id: string;
  station_name: string;
  fuel_type: string;
  observations: number;
  first_history_tick: number | null;
  last_history_tick: number | null;
  demand_liters_per_tick: number | null;
  stockout_tick: number | null;
  unmet_liters: number | null;
  projected_demand_liters_per_tick: number | null;
  fallback_demand_liters_per_tick: number | null;
  demand_std_liters: number | null;
  confidence: "HIGH" | "MEDIUM" | "LOW";
  confidence_score: number;
  shortage_risk: "CRITICAL" | "HIGH" | "MEDIUM" | "LOW" | "UNKNOWN";
  stockout_probability: number;
  fallback_used: boolean;
};

type Recommendation = {
  route_id: string;
  source_depot_id: string;
  source_depot_name: string;
  destination_station_id: string;
  destination_station_name: string;
  fuel_type: string;
  quantity_liters: number;
  arrival_tick: number;
  eta_ticks: number;
  eta_minutes: number;
  unmet_before_liters: number;
  unmet_after_liters: number;
  unmet_avoided_liters: number;
  policy_mode: "FORECAST_OPTIMIZER" | "REORDER_FALLBACK";
  priority_score: number;
  stockout_tick_before: number | null;
  stockout_tick_after: number | null;
  risk_before: "CRITICAL" | "HIGH" | "MEDIUM" | "LOW";
  risk_after: "CRITICAL" | "HIGH" | "MEDIUM" | "LOW";
  confidence: "HIGH" | "MEDIUM" | "LOW";
  rationale: string;
  signals: string[];
  constraints: string[];
  alternative_routes: string[];
};

type PlanningReport = {
  tick: number;
  history_ticks: number;
  horizon_ticks: number;
  forecasts: Forecast[];
  recommendations: Recommendation[];
  policy_mode: "FORECAST_OPTIMIZER" | "REORDER_FALLBACK";
  fallback_active: boolean;
  anomalies: Anomaly[];
  events: SimulatorEvent[];
  supply_arrivals: SupplyArrival[];
};

const liters = new Intl.NumberFormat("en-US", { maximumFractionDigits: 3 });

function proposalOf(item: Recommendation): Proposal {
  return {
    source_depot_id: item.source_depot_id,
    destination_station_id: item.destination_station_id,
    route_id: item.route_id,
    fuel_type: item.fuel_type,
    quantity: item.quantity_liters,
  };
}

export default function PlanningPanel({ approvals, snapshotUnavailable }: {
  approvals: ReturnType<typeof useApprovals>;
  snapshotUnavailable: boolean;
}) {
  const [selected, setSelected] = useState<Recommendation | null>(null);
  const { data, error, isLoading, mutate } = useSWR(
    "/api/planning",
    async (url: string) => ({
      ...await fetchJson<PlanningReport>(url),
      receivedAt: new Date().toLocaleTimeString(),
    }),
    { refreshInterval: 10_000, errorRetryCount: 3 },
  );
  const stale = error || snapshotUnavailable || approvals.simulatorError;
  const disabled = !!stale || approvals.busy || !!approvals.outbox || !approvals.ready;

  return (
    <>
      <section className="panel planning-panel" aria-labelledby="planning-title">
        <div className="panel-heading">
          <h2 id="planning-title">Demand forecast and recommendations</h2>
          {data && (
            <div className="badge-row">
              {data.fallback_active && <span className="count-badge warning-count">FALLBACK ACTIVE</span>}
              <span className="count-badge">{data.policy_mode.replaceAll("_", " ")} · AS OF TICK {data.tick}</span>
            </div>
          )}
        </div>
        {isLoading && <p role="status">Loading demand history and projections…</p>}
        {stale && (
          <div className="banner error-banner" role="alert">
            <span>{data ? `Stale forecast · last validated at ${data.receivedAt}. ` : ""}
              {error?.message ?? "Waiting for fresh simulator data."}</span>
            <button onClick={() => void mutate()}>Retry</button>
          </div>
        )}
        {data && (
          <>
            <p className="planning-note">
              Rolling average of the last {data.history_ticks} completed ticks with hour-of-day and crisis-event adjustments.
              Projection horizon {data.horizon_ticks} ticks.
            </p>
            <div className="planning-scroll" tabIndex={0} role="region" aria-label="Station and fuel forecasts">
              <table className="planning-table">
                <thead>
                  <tr>
                    <th>Station / fuel</th>
                    <th>History ticks</th>
                    <th>Demand per tick</th>
                    <th>Stockout tick</th>
                    <th>Unmet demand</th>
                    <th>Risk / confidence</th>
                  </tr>
                </thead>
                <tbody>
                  {data.forecasts.map(forecast => (
                    <tr key={forecast.station_id + forecast.fuel_type}>
                      <th scope="row">{forecast.station_name} · {forecast.fuel_type}</th>
                      <td>
                        {forecast.observations ? `${forecast.first_history_tick}–${forecast.last_history_tick}` : "—"}
                        {" "}({forecast.observations}/{data.history_ticks})
                      </td>
                      {forecast.demand_liters_per_tick === null || forecast.unmet_liters === null ? (
                        <td colSpan={3}>
                          Forecast unavailable · insufficient history
                          {forecast.fallback_demand_liters_per_tick !== null && (
                            <span className="muted-inline"> · Fallback est. {liters.format(forecast.fallback_demand_liters_per_tick)} L/tick</span>
                          )}
                        </td>
                      ) : (
                        <>
                          <td>
                            {liters.format(forecast.demand_liters_per_tick)} L
                            {forecast.projected_demand_liters_per_tick !== null && (
                              <span className="muted-inline"> (proj. {liters.format(forecast.projected_demand_liters_per_tick)} L)</span>
                            )}
                          </td>
                          <td>{forecast.stockout_tick ?? "None in horizon"}</td>
                          <td>{liters.format(forecast.unmet_liters)} L</td>
                        </>
                      )}
                      <td>
                        <span className={`risk-badge risk-${forecast.shortage_risk.toLowerCase()}`}>
                          {forecast.shortage_risk}
                        </span>
                        <span className="muted-inline"> · {forecast.confidence} ({Math.round(forecast.stockout_probability * 100)}%)</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {data.forecasts.length === 0 && <p>No stations in the simulator snapshot.</p>}
            </div>

            <h3>Recommended shipment previews</h3>
            <p className="planning-note">Ranked by urgency-weighted unmet demand avoided over the {data.horizon_ticks}-tick horizon.</p>
            {selected && (
              <div className="approval-review">
                <div>
                  <b>Review shipment</b><br />
                  {selected.source_depot_name} → {selected.destination_station_name}
                  {" · "}{liters.format(selected.quantity_liters)} L {selected.fuel_type}
                  {" · "}Risk {selected.risk_before} → {selected.risk_after}
                  <div className="rationale-box">
                    <span>{selected.rationale}</span>
                    <small>Signals: {selected.signals.join(" · ")}</small>
                    <small>Constraints: {selected.constraints.join(" · ")}</small>
                    {selected.alternative_routes.length > 0 && (
                      <small>Alternatives: {selected.alternative_routes.join(", ")}</small>
                    )}
                  </div>
                </div>
                <button className="refresh-button" onClick={() => setSelected(null)}>Back</button>
                <button className="refresh-button approve-button"
                  disabled={disabled || approvals.pending(proposalOf(selected))}
                  onClick={() => {
                    void approvals.approve(proposalOf(selected));
                    setSelected(null);
                  }}>Approve shipment</button>
              </div>
            )}
            {data.recommendations.length === 0 ? (
              <p>No feasible shipment reduces projected unmet demand with the history available.</p>
            ) : (
              <div className="planning-scroll" tabIndex={0} role="region" aria-label="Ranked shipment previews">
                <table className="planning-table">
                  <thead>
                    <tr>
                      <th>Route / fuel</th>
                      <th>Quantity</th>
                      <th>ETA</th>
                      <th>Unmet before → after</th>
                      <th>Unmet avoided</th>
                      <th>Risk &amp; rationale</th>
                      <th>Approval</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.recommendations.map(item => (
                      <tr key={item.route_id + item.fuel_type}>
                        <th scope="row">{item.source_depot_name} → {item.destination_station_name} · {item.fuel_type}</th>
                        <td>{liters.format(item.quantity_liters)} L</td>
                        <td>Tick {item.arrival_tick} · {item.eta_ticks} ticks ({item.eta_minutes} min)</td>
                        <td>{liters.format(item.unmet_before_liters)} → {liters.format(item.unmet_after_liters)} L</td>
                        <td className="planning-benefit">{liters.format(item.unmet_avoided_liters)} L</td>
                        <td className="shipment-details">
                          <span className={`risk-badge risk-${item.risk_before.toLowerCase()}`}>{item.risk_before}</span>
                          {" → "}
                          <span className={`risk-badge risk-${item.risk_after.toLowerCase()}`}>{item.risk_after}</span>
                          <div className="muted-inline">{item.policy_mode === "REORDER_FALLBACK" ? "Fallback policy" : `${item.confidence} conf.`}</div>
                        </td>
                        <td><button className="refresh-button"
                          disabled={disabled || approvals.pending(proposalOf(item))}
                          onClick={() => setSelected(item)}>Review</button></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </section>
      {data && (
        <IntelligenceContextPanel
          anomalies={data.anomalies ?? []}
          events={data.events ?? []}
          supplyArrivals={data.supply_arrivals ?? []}
        />
      )}
    </>
  );
}
