import { useEffect, useState } from "react";
import { AlertTriangle, ShieldAlert } from "lucide-react";
import { fetchJson } from "./api";
import type { IncidentsResponse, ShortagesResponse } from "./types";

interface ShortageIncidentsProps {
  generationKey?: string;
  asOfTick: number;
}

export function ShortageIncidents({ generationKey, asOfTick }: ShortageIncidentsProps) {
  const [shortages, setShortages] = useState<ShortagesResponse | null>(null);
  const [incidents, setIncidents] = useState<IncidentsResponse | null>(null);
  const [incFilter, setIncFilter] = useState<"OPEN" | "RESOLVED" | "ALL">("OPEN");

  useEffect(() => {
    let active = true;
    Promise.all([
      fetchJson<ShortagesResponse>("/api/shortages?history_ticks=8&horizon_ticks=96"),
      fetchJson<IncidentsResponse>(`/api/incidents?status=${incFilter}&limit=50`),
    ])
      .then(([sh, inc]) => {
        if (!active) return;
        setShortages(sh);
        setIncidents(inc);
      })
      .catch(() => {
        // Parent dashboard polling surfaces upstream connectivity issues
      });
    return () => {
      active = false;
    };
  }, [generationKey, asOfTick, incFilter]);

  const urgent = (shortages?.shortages ?? []).slice(0, 6);

  return (
    <section className="lower-grid section-block" aria-label="Shortage projection and incident timeline">
      <div className="panel">
        <header className="panel-heading">
          <div>
            <div className="eyebrow">SHORTAGE & SAFETY STOCK RISK</div>
            <h2>Projected station depletion vs outage</h2>
          </div>
          <span className="count-badge">Uncalibrated Heuristic</span>
        </header>
        <p className="muted-inline" style={{ marginTop: 8 }}>
          Separates physical stock depletion (preventable by dispatch) from closed-station outages. Probabilities are uncalibrated scenario heuristics.
        </p>

        <div className="planning-scroll">
          <table className="planning-table">
            <thead>
              <tr>
                <th>#</th>
                <th>Station · Fuel</th>
                <th>Type</th>
                <th>Inv / Safety</th>
                <th>First unmet</th>
                <th>Unmet p50 (p10–p90)</th>
                <th>Risk (Heur. P)</th>
              </tr>
            </thead>
            <tbody>
              {urgent.map((s) => (
                <tr key={`${s.station_id}-${s.fuel_type}`}>
                  <td>{s.urgency_rank}</td>
                  <td>
                    <b>{s.station_name}</b> <small className="muted-inline">{s.fuel_type}</small>
                  </td>
                  <td>
                    <span className={`risk-badge ${s.station_status === "OUTAGE" ? "risk-critical" : "risk-medium"}`}>
                      {s.station_status === "OUTAGE" ? "OUTAGE" : "DEPLETION"}
                    </span>
                  </td>
                  <td>
                    {Math.round(s.current_inventory).toLocaleString()} / {Math.round(s.safety_stock_target).toLocaleString()} L
                  </td>
                  <td>
                    {s.first_unmet_tick != null ? `t=${s.first_unmet_tick} (${s.time_to_stockout_minutes}m)` : "None"}
                  </td>
                  <td>
                    {s.unmet_p50_liters != null ? `${Math.round(s.unmet_p50_liters).toLocaleString()} L` : "0 L"}
                    {s.unmet_p10_liters != null && s.unmet_p90_liters != null
                      ? ` (${Math.round(s.unmet_p10_liters)}–${Math.round(s.unmet_p90_liters)})`
                      : ""}
                  </td>
                  <td>
                    <span className={`risk-badge risk-${s.shortage_risk.toLowerCase()}`}>
                      {s.shortage_risk}
                    </span>{" "}
                    <small className="muted-inline">{Math.round(s.stockout_probability * 100)}%</small>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="panel">
        <header className="panel-heading">
          <div>
            <div className="eyebrow">PERSISTED ANOMALY & INCIDENT LEDGER</div>
            <h2>Rule & Isolation Forest detections</h2>
          </div>
          <div className="badge-row">
            <button
              type="button"
              className={`tab-chip ${incFilter === "OPEN" ? "active" : ""}`}
              onClick={() => setIncFilter("OPEN")}
            >
              Open ({incidents?.open_count ?? 0})
            </button>
            <button
              type="button"
              className={`tab-chip ${incFilter === "RESOLVED" ? "active" : ""}`}
              onClick={() => setIncFilter("RESOLVED")}
            >
              Resolved ({incidents?.resolved_count ?? 0})
            </button>
            <button
              type="button"
              className={`tab-chip ${incFilter === "ALL" ? "active" : ""}`}
              onClick={() => setIncFilter("ALL")}
            >
              All
            </button>
          </div>
        </header>

        <div className="alert-list" style={{ marginTop: 12 }}>
          {!incidents || incidents.incidents.length === 0 ? (
            <div className="empty-healthy">
              <ShieldAlert size={16} /> No {incFilter.toLowerCase()} incidents in this generation.
            </div>
          ) : (
            incidents.incidents.slice(0, 8).map((inc) => (
              <div className="alert-row" key={inc.id}>
                <div className={`alert-icon ${inc.severity === "CRITICAL" ? "critical" : ""}`}>
                  <AlertTriangle size={14} />
                </div>
                <div className="alert-copy">
                  <b>
                    {inc.category} · {inc.entity_id} {inc.fuel_type ? `(${inc.fuel_type})` : ""}
                  </b>
                  <span>
                    {inc.summary} · Source: <b>{inc.source}</b> · {inc.status}
                    {inc.first_tick != null ? ` (t=${inc.first_tick}–${inc.last_tick ?? inc.first_tick})` : ""}
                  </span>
                  {inc.suggestion ? <span className="planning-benefit">{inc.suggestion}</span> : null}
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </section>
  );
}
