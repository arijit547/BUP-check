import useSWR from "swr";
import { fetchJson } from "./api";
import { Activity, AlertTriangle, CheckCircle2, Gauge, Truck } from "lucide-react";

type ComponentHealth = {
  status: "ok" | "degraded" | "error";
  detail: string | null;
  latency_ms: number | null;
};

type HealthReport = {
  status: "ok" | "degraded";
  timestamp: string;
  version: string;
  components: Record<string, ComponentHealth>;
  p95_latency_ms: number | null;
  error_rate_percent: number;
};

type MetricsReport = {
  total_requests: number;
  error_count: number;
  error_rate_percent: number;
  requests_per_minute: number;
  p50_latency_ms: number | null;
  p95_latency_ms: number | null;
  p99_latency_ms: number | null;
  fallback_activations: number;
  active_anomalies: number;
  sse_status: string;
  sse_events_received: number;
  sse_reconnects: number;
  simulator: {
    served_demand_liters: number;
    unmet_demand_liters: number;
    service_level: number;
    allocation_liters: number;
    allocation_failures: number;
  } | null;
};

export type Anomaly = {
  id: string;
  category: string;
  severity: "CRITICAL" | "WARNING";
  entity_type: string;
  entity_id: string;
  fuel_type: string | null;
  summary: string;
};

export type SimulatorEvent = {
  id: number;
  type: string;
  start_tick: number;
  end_tick: number;
  status: "SCHEDULED" | "ACTIVE" | "RESOLVED";
};

export type SupplyArrival = {
  id: string;
  depot_id: string;
  fuel_type: string;
  quantity: number;
  planned_tick: number;
  actual_tick: number | null;
  status: "SCHEDULED" | "DELAYED" | "ARRIVED";
};

const componentLabels: Record<string, string> = {
  backend_api: "Backend API",
  database: "Approval DB",
  simulator: "Fuel Simulator",
  sse_stream: "SSE Stream",
  prediction_service: "Prediction Service",
  decision_engine: "Decision Engine",
};

const num = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });

export function HealthPanel() {
  const health = useSWR("/api/health", async (url: string) => {
    const response = await fetch(url, { cache: "no-store" });
    return response.json() as Promise<HealthReport>;
  }, { refreshInterval: 10_000 });
  const metrics = useSWR("/api/metrics", fetchJson<MetricsReport>, { refreshInterval: 10_000 });

  const components = Object.entries(health.data?.components ?? {});
  const sim = metrics.data?.simulator;

  return (
    <section className="panel health-panel" aria-label="System health and observability">
      <div className="panel-heading">
        <div>
          <div className="eyebrow">OBSERVABILITY & SERVICE STATUS</div>
          <h2>System health and metrics</h2>
        </div>
        {health.data && (
          <span className={`count-badge ${health.data.status === "ok" ? "health-ok-badge" : "warning-count"}`}>
            SYSTEM {health.data.status.toUpperCase()}
          </span>
        )}
      </div>
      <div className="health-grid">
        {components.map(([key, item]) => (
          <div className="health-chip" key={key}>
            <span className={`health-dot ${item.status}`} />
            <div>
              <b>{componentLabels[key] ?? key}</b>
              <small>{item.status.toUpperCase()}{item.latency_ms !== null ? ` · ${item.latency_ms} ms` : ""}</small>
            </div>
          </div>
        ))}
      </div>
      {metrics.data && (
        <div className="obs-strip">
          <span><Gauge size={14} /> p95 latency: <b>{metrics.data.p95_latency_ms !== null ? `${metrics.data.p95_latency_ms} ms` : "—"}</b></span>
          <span><Activity size={14} /> Error rate: <b>{metrics.data.error_rate_percent}%</b> ({metrics.data.error_count}/{metrics.data.total_requests})</span>
          <span><CheckCircle2 size={14} /> Service level: <b>{sim ? `${(sim.service_level * 100).toFixed(2)}%` : "—"}</b></span>
          <span><AlertTriangle size={14} /> Anomalies: <b>{metrics.data.active_anomalies}</b> · Fallbacks: <b>{metrics.data.fallback_activations}</b></span>
        </div>
      )}
    </section>
  );
}

export function IntelligenceContextPanel({
  anomalies,
  events,
  supplyArrivals,
}: {
  anomalies: Anomaly[];
  events: SimulatorEvent[];
  supplyArrivals: SupplyArrival[];
}) {
  const upcoming = supplyArrivals.filter(item => item.status !== "ARRIVED").slice(0, 6);
  return (
    <section className="lower-grid" aria-label="Anomalies and incoming supply">
      <div className="panel">
        <div className="panel-heading">
          <div>
            <div className="eyebrow">DETECTION LAYER</div>
            <h2>Anomalies &amp; crisis events</h2>
          </div>
          <span className={`count-badge ${anomalies.length ? "warning-count" : ""}`}>{anomalies.length} detected</span>
        </div>
        {anomalies.length === 0 ? (
          <div className="empty-healthy">
            <CheckCircle2 size={18} />
            <span>No demand spikes, route disruptions, or bottlenecks detected.</span>
          </div>
        ) : (
          <div className="alert-list">
            {anomalies.slice(0, 8).map(item => (
              <div className="alert-row" key={item.id}>
                <div className={`alert-icon ${item.severity === "CRITICAL" ? "critical" : ""}`}>
                  <AlertTriangle size={16} />
                </div>
                <div className="alert-copy">
                  <b>{item.category.replaceAll("_", " ")}</b>
                  <span>{item.summary}</span>
                </div>
                <span className={`level ${item.severity === "CRITICAL" ? "critical-text" : ""}`}>{item.severity}</span>
              </div>
            ))}
          </div>
        )}
        {events.length > 0 && (
          <div className="mini-events">
            <b>Simulator events:</b>{" "}
            {events.slice(0, 4).map(ev => `${ev.type} (${ev.status}, ticks ${ev.start_tick}–${ev.end_tick})`).join(" · ")}
          </div>
        )}
      </div>

      <div className="panel">
        <div className="panel-heading">
          <div>
            <div className="eyebrow">DEPOT RESUPPLY SCHEDULE</div>
            <h2>Incoming supply arrivals</h2>
          </div>
          <span className="count-badge">{upcoming.length} pending</span>
        </div>
        {upcoming.length === 0 ? (
          <div className="empty-healthy">
            <Truck size={18} />
            <span>No pending depot supply arrivals.</span>
          </div>
        ) : (
          <div className="alert-list">
            {upcoming.map(item => (
              <div className="alert-row" key={item.id}>
                <div className={`alert-icon ${item.status === "DELAYED" ? "critical" : ""}`}>
                  <Truck size={16} />
                </div>
                <div className="alert-copy">
                  <b>{item.depot_id} · {item.fuel_type}</b>
                  <span>{num.format(item.quantity)} L · Planned tick {item.planned_tick}</span>
                </div>
                <span className={`level ${item.status === "DELAYED" ? "critical-text" : ""}`}>{item.status}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
