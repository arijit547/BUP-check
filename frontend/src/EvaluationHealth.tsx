import { useEffect, useState } from "react";
import { Cpu, RefreshCw } from "lucide-react";
import { fetchJson } from "./api";
import type { EvaluationSummaryResponse, GenerationInfo, ModelsStatusResponse } from "./types";

interface EvaluationHealthProps {
  generationId?: string;
  onRebound?: (next: GenerationInfo) => void;
}

export function EvaluationHealth({ generationId, onRebound }: EvaluationHealthProps) {
  const [models, setModels] = useState<ModelsStatusResponse | null>(null);
  const [evalData, setEvalData] = useState<EvaluationSummaryResponse | null>(null);
  const [rebindMsg, setRebindMsg] = useState<string | null>(null);
  const [rebinding, setRebinding] = useState(false);

  useEffect(() => {
    let active = true;
    Promise.all([
      fetchJson<ModelsStatusResponse>("/api/models"),
      fetchJson<EvaluationSummaryResponse>("/api/evaluation"),
    ])
      .then(([m, e]) => {
        if (!active) return;
        setModels(m);
        setEvalData(e);
      })
      .catch(() => {
        // Non-fatal informational panel
      });
    return () => {
      active = false;
    };
  }, [generationId]);

  async function rebind() {
    setRebinding(true);
    setRebindMsg(null);
    try {
      const next = await fetchJson<GenerationInfo>("/api/generations/rebind", { method: "POST" });
      setRebindMsg(`Bound generation ${next.generation_id.slice(0, 8)} at tick ${next.last_seen_tick}`);
      onRebound?.(next);
    } catch (err) {
      setRebindMsg(err instanceof Error ? err.message : "Rebind failed");
    } finally {
      setRebinding(false);
    }
  }

  const agg = evalData?.policy_comparison?.aggregate ?? null;
  const forecastDatasets = evalData?.forecast_evaluation?.datasets ?? [];
  const resilienceSummary = evalData?.resilience_summary ?? null;
  const sustainedBench = evalData?.benchmark_summary?.scenarios?.sustained_planning_c10 ?? null;
  const mlChronosBench = evalData?.benchmark_summary?.scenarios?.ml_chronos2_uncached ?? null;
  const matured = evalData?.live_matured_accuracy;
  const mlStatObj =
    typeof models?.ml_service_status === "object" && models.ml_service_status !== null
      ? models.ml_service_status
      : null;
  const chronosState = mlStatObj?.chronos2
    ? typeof mlStatObj.chronos2 === "object"
      ? mlStatObj.chronos2.status
      : mlStatObj.chronos2
    : null;
  const iforestState = mlStatObj?.isolation_forest
    ? typeof mlStatObj.isolation_forest === "object"
      ? mlStatObj.isolation_forest.status
      : mlStatObj.isolation_forest
    : null;
  const mlStatusText = mlStatObj
    ? `${String(mlStatObj.status ?? "unknown")}${chronosState || iforestState ? ` (Chronos-2: ${String(chronosState ?? "unknown")}, IForest: ${String(iforestState ?? "unknown")})` : ""}`
    : typeof models?.ml_service_status === "string"
      ? models.ml_service_status
      : models?.ml_service_configured
        ? "checking"
        : "core mode (disabled)";
  const evalSourceLabel =
    evalData?.artifact_source === "runtime_artifacts"
      ? "Runtime Eval Artifacts"
      : evalData?.artifact_source === "curated_evidence"
        ? "Curated Release Evidence"
        : "Not Yet Measured";

  return (
    <section className="panel section-block" aria-label="Models, generation and benchmark evaluation">
      <header className="panel-heading">
        <div>
          <div className="eyebrow">MODEL REGISTRY & BENCHMARK EVALUATION</div>
          <h2>Pretrained ML artifacts, generation ledger & policy metrics</h2>
        </div>
        <div className="badge-row">
          <span className="count-badge">{evalSourceLabel}</span>
          {generationId ? (
            <span className="count-badge">Gen {generationId.slice(0, 8)}</span>
          ) : null}
          <button type="button" className="refresh-button" onClick={rebind} disabled={rebinding}>
            <RefreshCw size={13} />
            <span>{rebinding ? "Rebinding..." : "Rebind Generation"}</span>
          </button>
        </div>
      </header>

      {rebindMsg ? <p className="muted-inline" style={{ marginTop: 8 }}>{rebindMsg}</p> : null}

      <div className="obs-strip">
        <span>
          <Cpu size={13} /> Default Policy: <b>{models?.default_forecast_policy ?? "OR_TOOLS_SEASONAL"}</b>
        </span>
        <span>Experimental Candidate: <b>{models?.experimental_ml_policy ?? "CHRONOS2"}</b></span>
        <span>
          ML Service: <b>{mlStatusText}</b>
          {models?.last_ml_latency_ms != null ? ` · ${models.last_ml_latency_ms.toFixed(0)} ms` : ""}
        </span>
        {resilienceSummary ? (
          <span>
            Resilience Matrix: <b>{resilienceSummary.passed}/{resilienceSummary.total} passed</b>
          </span>
        ) : null}
        {sustainedBench ? (
          <span>
            Sustained Load (c=10): <b>{sustainedBench.rps?.toFixed(1)} req/s</b> · p95 <b>{sustainedBench.latency_ms?.p95?.toFixed(1)} ms</b> · {sustainedBench.error_count ?? 0} errors
          </span>
        ) : null}
        {mlChronosBench ? (
          <span>
            Chronos-2 Live Load: p95 <b>{mlChronosBench.latency_ms?.p95?.toFixed(0)} ms</b> · {mlChronosBench.error_count ?? 0} errors
          </span>
        ) : null}
        {matured && (matured.matured_points ?? 0) > 0 ? (
          <span>
            Live Matured ({matured.matured_points} pts): <b>MAE {matured.mae}</b> · <b>Cov {matured.p10_p90_coverage}</b>
          </span>
        ) : null}
      </div>

      {models?.last_ml_error ? (
        <p className="warning-note">ML Service Notice: {models.last_ml_error}</p>
      ) : null}

      {evalData?.status === "not_yet_measured" ? (
        <p className="warning-note">
          Evaluation artifacts have not been measured in this workspace yet. Run{" "}
          <code>scripts/eval_forecasts.py</code> and <code>scripts/eval_policies.py</code> or verify curated files in{" "}
          <code>docs/evidence/</code>.
        </p>
      ) : null}

      {agg ? (
        <div className="planning-scroll" style={{ marginTop: 12 }}>
          <table className="planning-table">
            <thead>
              <tr>
                <th>Policy Variant</th>
                <th>Budget Tier</th>
                <th>Mean SL</th>
                <th>Measured Window SL</th>
                <th>Depletion / Outage Unmet</th>
                <th>Allocated (L)</th>
                <th>ML / Fallback Steps</th>
                <th>Decisions</th>
                <th>Mean Decision Latency</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(agg).map(([name, m]) => (
                <tr key={name}>
                  <th>{name}</th>
                  <td>{m.budget_tier ?? "—"}</td>
                  <td><b>{(m.mean_service_level * 100).toFixed(3)}%</b></td>
                  <td>
                    {m.measured_window_service_level != null
                      ? `${(m.measured_window_service_level * 100).toFixed(3)}%`
                      : "—"}
                  </td>
                  <td>
                    {m.depletion_unmet_liters != null && m.outage_unmet_liters != null
                      ? `${m.depletion_unmet_liters.toLocaleString()} L depl / ${m.outage_unmet_liters.toLocaleString()} L outage`
                      : `${m.total_unmet_liters.toLocaleString()} L`}
                  </td>
                  <td>{m.total_allocated_liters.toLocaleString()} L</td>
                  <td>
                    {m.chronos2_steps != null || m.fallback_steps != null
                      ? `${m.chronos2_steps ?? 0} ML / ${m.fallback_steps ?? 0} fb`
                      : "—"}
                  </td>
                  <td>{m.total_decisions} ({m.total_failures} fail)</td>
                  <td>{m.mean_decision_latency_ms.toFixed(1)} ms</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {forecastDatasets.length > 0 ? (
        <div className="planning-scroll" style={{ marginTop: 12 }}>
          <table className="planning-table">
            <thead>
              <tr>
                <th>Forecast Dataset</th>
                <th>Horizon</th>
                <th>Rolling WAPE / MAE</th>
                <th>Seasonal WAPE / MAE / Cov</th>
                <th>Chronos-2 WAPE / MAE / Cov</th>
              </tr>
            </thead>
            <tbody>
              {forecastDatasets.flatMap((ds) =>
                ["4", "16", "96"]
                  .filter((h) => ds.horizons?.[h])
                  .map((h) => {
                    const row = ds.horizons[h];
                    const rm = row?.rolling_mean;
                    const se = row?.seasonal;
                    const ch = row?.chronos2;
                    return (
                      <tr key={`${ds.dataset}-${h}`}>
                        <th>{ds.dataset}</th>
                        <td>{h} ticks</td>
                        <td>
                          {rm?.wape != null ? `${(rm.wape * 100).toFixed(2)}% / ${rm.mae?.toFixed(2)}` : "—"}
                        </td>
                        <td>
                          {se?.wape != null
                            ? `${(se.wape * 100).toFixed(2)}% / ${se.mae?.toFixed(2)} / ${((se.p10_p90_coverage ?? 0) * 100).toFixed(1)}%`
                            : "—"}
                        </td>
                        <td>
                          {ch?.wape != null
                            ? `${(ch.wape * 100).toFixed(2)}% / ${ch.mae?.toFixed(2)} / ${((ch.p10_p90_coverage ?? 0) * 100).toFixed(1)}%`
                            : "—"}
                        </td>
                      </tr>
                    );
                  }),
              )}
            </tbody>
          </table>
        </div>
      ) : null}

      {!agg && forecastDatasets.length === 0 ? (
        <p className="muted-inline" style={{ marginTop: 10 }}>
          Offline Chronos-2 & Isolation Forest manifest loaded; multi-scenario policy comparison appears once evaluation suite completes.
        </p>
      ) : null}
    </section>
  );
}
