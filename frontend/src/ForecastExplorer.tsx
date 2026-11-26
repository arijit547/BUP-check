import { useEffect, useState } from "react";
import { Activity } from "lucide-react";
import { fetchJson } from "./api";
import type { ForecastSeries, ForecastsResponse } from "./types";

interface ForecastExplorerProps {
  generationKey?: string;
  asOfTick: number;
}

function buildLinePath(xs: number[], ys: number[]): string {
  return xs
    .map((x, idx) => `${idx === 0 ? "M" : "L"}${x.toFixed(1)},${ys[idx].toFixed(1)}`)
    .join(" ");
}

export function ForecastExplorer({ generationKey, asOfTick }: ForecastExplorerProps) {
  const [historyTicks, setHistoryTicks] = useState(24);
  const [horizonTicks, setHorizonTicks] = useState(32);
  const [policyOverride, setPolicyOverride] = useState<"AUTO" | "CHRONOS2">("AUTO");
  const [data, setData] = useState<ForecastsResponse | null>(null);
  const [selectedKey, setSelectedKey] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    fetchJson<ForecastsResponse>(
      `/api/forecasts?history_ticks=${historyTicks}&horizon_ticks=${horizonTicks}&policy_override=${policyOverride}`,
    )
      .then((res) => {
        if (!active) return;
        setData(res);
        if (res.series.length > 0 && !selectedKey) {
          setSelectedKey(`${res.series[0].station_id}::${res.series[0].fuel_type}`);
        }
      })
      .catch((err) => {
        if (!active) return;
        setError(err instanceof Error ? err.message : "Failed to load forecasts");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [generationKey, asOfTick, historyTicks, horizonTicks, policyOverride]);

  const series: ForecastSeries | undefined =
    data?.series.find((s) => `${s.station_id}::${s.fuel_type}` === selectedKey) ?? data?.series[0];

  const width = 760;
  const height = 220;
  const padL = 48;
  const padR = 16;
  const padT = 16;
  const padB = 28;
  const plotW = width - padL - padR;
  const plotH = height - padT - padB;

  let bandPath = "";
  let p50Path = "";
  let histPath = "";
  let minTick = 0;
  let maxTick = 1;
  let maxVal = 100;

  if (series && (series.history.length > 0 || series.forecast.length > 0)) {
    const allTicks = [
      ...series.history.map((h) => h.tick),
      ...series.forecast.map((f) => f.tick),
    ];
    minTick = Math.min(...allTicks);
    maxTick = Math.max(minTick + 1, ...allTicks);
    const allVals = [
      ...series.history.map((h) => h.demand_liters),
      ...series.forecast.flatMap((f) => [f.p10 ?? f.p50, f.p50, f.p90 ?? f.p50]),
    ];
    maxVal = Math.max(10, ...allVals) * 1.12;

    const mapX = (t: number) => padL + ((t - minTick) / (maxTick - minTick)) * plotW;
    const mapY = (v: number) => padT + plotH - (Math.max(0, v) / maxVal) * plotH;

    if (series.history.length > 0) {
      histPath = buildLinePath(
        series.history.map((h) => mapX(h.tick)),
        series.history.map((h) => mapY(h.demand_liters)),
      );
    }

    if (series.forecast.length > 0) {
      const fxs = series.forecast.map((f) => mapX(f.tick));
      p50Path = buildLinePath(
        fxs,
        series.forecast.map((f) => mapY(f.p50)),
      );
      const upper = series.forecast.map((f, idx) => `${idx === 0 ? "M" : "L"}${fxs[idx].toFixed(1)},${mapY(f.p90 ?? f.p50).toFixed(1)}`);
      const lower = [...series.forecast]
        .reverse()
        .map((f, rIdx) => {
          const origIdx = series.forecast.length - 1 - rIdx;
          return `L${fxs[origIdx].toFixed(1)},${mapY(f.p10 ?? f.p50).toFixed(1)}`;
        });
      bandPath = [...upper, ...lower, "Z"].join(" ");
    }
  }

  return (
    <section className="panel section-block" aria-label="Demand forecast explorer">
      <header className="panel-heading">
        <div>
          <div className="eyebrow">TIME-SERIES DEMAND INTELLIGENCE</div>
          <h2>Demand forecast & uncertainty quantiles (p10–p90)</h2>
        </div>
        <div className="badge-row">
          <label className="inline-control">
            Series
            <select
              aria-label="Forecast station and fuel"
              value={selectedKey}
              onChange={(e) => setSelectedKey(e.target.value)}
            >
              {data?.series.map((s) => {
                const k = `${s.station_id}::${s.fuel_type}`;
                return (
                  <option key={k} value={k}>
                    {s.station_name} · {s.fuel_type}
                  </option>
                );
              })}
            </select>
          </label>
          <label className="inline-control">
            Model
            <select
              aria-label="Forecast policy override"
              value={policyOverride}
              onChange={(e) => setPolicyOverride(e.target.value as "AUTO" | "CHRONOS2")}
            >
              <option value="AUTO">Seasonal Baseline</option>
              <option value="CHRONOS2">Chronos-2 Candidate</option>
            </select>
          </label>
          <label className="inline-control">
            History
            <select
              aria-label="History window ticks"
              value={historyTicks}
              onChange={(e) => setHistoryTicks(Number(e.target.value))}
            >
              <option value={8}>8 ticks</option>
              <option value={24}>24 ticks</option>
              <option value={48}>48 ticks</option>
            </select>
          </label>
          <label className="inline-control">
            Horizon
            <select
              aria-label="Forecast horizon ticks"
              value={horizonTicks}
              onChange={(e) => setHorizonTicks(Number(e.target.value))}
            >
              <option value={16}>16 ticks</option>
              <option value={32}>32 ticks</option>
              <option value={96}>96 ticks</option>
            </select>
          </label>
        </div>
      </header>

      {error ? <p className="error-note">{error}</p> : null}

      {series ? (
        <div className="forecast-body">
          <div className="obs-strip">
            <span><Activity size={13} /> Source: <b>{series.source}</b> ({series.model_version})</span>
            <span>Observations: <b>{series.observations}/{historyTicks} ticks</b></span>
            {series.mae_matured != null ? (
              <span>Matured MAE: <b>{series.mae_matured.toFixed(1)} L/tick</b></span>
            ) : null}
            {series.p10_p90_coverage != null ? (
              <span>Live p10–p90 coverage: <b>{(series.p10_p90_coverage * 100).toFixed(1)}%</b></span>
            ) : null}
            {loading ? <span className="muted-inline">Refreshing...</span> : null}
          </div>

          {series.fallback_reason ? (
            <div className="warning-note">
              <span>{series.fallback_reason}</span>
            </div>
          ) : null}

          <svg viewBox={`0 0 ${width} ${height}`} className="forecast-svg" role="img" aria-label="Demand history and quantile forecast chart">
            <line x1={padL} y1={padT + plotH} x2={padL + plotW} y2={padT + plotH} stroke="#d5ddd7" />
            <line x1={padL} y1={padT} x2={padL} y2={padT + plotH} stroke="#d5ddd7" />
            <text x={4} y={padT + 8} fontSize="10" fill="#6f7d75">{Math.round(maxVal)} L</text>
            <text x={4} y={padT + plotH} fontSize="10" fill="#6f7d75">0 L</text>
            <text x={padL} y={height - 6} fontSize="10" fill="#6f7d75">t={minTick}</text>
            <text x={width - padR - 42} y={height - 6} fontSize="10" fill="#6f7d75">t={maxTick}</text>

            {bandPath ? <path d={bandPath} fill="rgba(21, 107, 82, 0.14)" /> : null}
            {histPath ? <path d={histPath} fill="none" stroke="#2d3b36" strokeWidth="2" /> : null}
            {p50Path ? <path d={p50Path} fill="none" stroke="#156b52" strokeWidth="2.3" strokeDasharray="5 3" /> : null}
          </svg>

          <div className="fuel-legend">
            <span><i className="legend-dot" style={{ background: "#2d3b36" }} /> Observed demand (L/tick)</span>
            <span><i className="legend-dot" /> Forecast p50</span>
            <span><i className="legend-dot" style={{ background: "rgba(21,107,82,0.35)" }} /> Uncertainty band (p10–p90)</span>
          </div>
        </div>
      ) : (
        <p className="muted-inline">No demand history loaded yet.</p>
      )}
    </section>
  );
}
