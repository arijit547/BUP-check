import { useEffect, useState } from "react";
import { useSWRConfig } from "swr";
import { AlertTriangle, CheckCircle2, Play, RefreshCw, SlidersHorizontal, XCircle } from "lucide-react";
import { fetchJson } from "./api";
import type {
  Fuel,
  JointPlanSummary,
  LatestPlanResponse,
  PlanApprovalResult,
  SimulateResponse,
} from "./types";

interface PlannerRoute {
  id: string;
  source_depot_id: string;
  destination_station_id: string;
  status: string;
  transit_ticks: number;
  max_shipment: number;
}

interface AllocationPlannerProps {
  routes: PlannerRoute[];
  tick: number;
  disabled?: boolean;
  onActionComplete?: () => void;
}

const PLAN_STORAGE_KEY = "bup.planner.active-plan-id";
const SAFE_PLAN_ID_RE = /^plan-[a-zA-Z0-9_-]{3,120}$/;

function sanitizePlanId(id: string | null | undefined): string | null {
  if (typeof id !== "string") return null;
  const matched = SAFE_PLAN_ID_RE.exec(id);
  return matched ? matched[0] : null;
}

export function AllocationPlanner({ routes, tick, disabled = false, onActionComplete }: AllocationPlannerProps) {
  const { mutate } = useSWRConfig();
  const [policy, setPolicy] = useState<"AUTO" | "CHRONOS2" | "SAFE_REORDER">("AUTO");
  const [horizonTicks, setHorizonTicks] = useState(96);
  const [singleAction, setSingleAction] = useState(false);
  const [plan, setPlan] = useState<JointPlanSummary | null>(null);
  const [approvalResult, setApprovalResult] = useState<PlanApprovalResult | null>(null);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [rationale, setRationale] = useState("");
  const [planBusy, setPlanBusy] = useState(false);
  const [planError, setPlanError] = useState<string | null>(null);
  const [batchError, setBatchError] = useState<string | null>(null);
  const [batchBusy, setBatchBusy] = useState(false);
  const [staleGenNotice, setStaleGenNotice] = useState<string | null>(null);

  const openRoutes = routes.filter((r) => r.status === "AVAILABLE");
  const [simRouteId, setSimRouteId] = useState(openRoutes[0]?.id ?? routes[0]?.id ?? "");
  const [simFuel, setSimFuel] = useState<Fuel>("DIESEL");
  const [simQty, setSimQty] = useState(3000);
  const [simResult, setSimResult] = useState<SimulateResponse | null>(null);
  const [simBusy, setSimBusy] = useState(false);

  // Restore server-stored active plan and approval outcomes on mount / browser refresh / multi-tab load
  useEffect(() => {
    let active = true;
    async function restoreLatest() {
      try {
        const latest = await fetchJson<LatestPlanResponse>("/api/plans/latest");
        if (!active) return;
        const storedId = sanitizePlanId(sessionStorage.getItem(PLAN_STORAGE_KEY));
        if (storedId && (!latest.plan || latest.plan.plan_id !== storedId)) {
          try {
            const savedPlan = await fetchJson<JointPlanSummary>(`/api/plans/${encodeURIComponent(storedId)}`);
            if (!active) return;
            const checkedId = sanitizePlanId(savedPlan.plan_id);
            if (savedPlan.generation_id === latest.generation_id && checkedId) {
              setPlan(savedPlan);
              setStaleGenNotice(null);
              try {
                const appRes = await fetchJson<PlanApprovalResult>(
                  `/api/plans/${encodeURIComponent(checkedId)}/approval`,
                );
                if (active) setApprovalResult(appRes);
              } catch {
                if (active) setApprovalResult(null);
              }
              return;
            }
          } catch {
            // Fall through to latest plan
          }
        }
        if (latest.plan) {
          setPlan(latest.plan);
          setApprovalResult(latest.approval);
          setStaleGenNotice(null);
          const safePlanId = sanitizePlanId(latest.plan.plan_id);
          if (safePlanId) {
            sessionStorage.setItem(PLAN_STORAGE_KEY, safePlanId);
          } else {
            sessionStorage.removeItem(PLAN_STORAGE_KEY);
          }
        }
      } catch {
        // Non-fatal restore attempt
      }
    }
    void restoreLatest();
    return () => {
      active = false;
    };
  }, []);

  // Poll approval shipment status progression cleanly when a plan has submitted items and tick moves
  useEffect(() => {
    if (!plan) return;
    let active = true;
    const planId = sanitizePlanId(plan.plan_id);
    if (!planId) return;
    async function syncApprovalStatus() {
      if (!planId) return;
      try {
        const res = await fetchJson<PlanApprovalResult>(`/api/plans/${encodeURIComponent(planId)}/approval`);
        if (active) setApprovalResult(res);
      } catch {
        // No batch approval yet (404) or transient read failure
      }
      try {
        const latest = await fetchJson<LatestPlanResponse>("/api/plans/latest");
        if (active && latest.generation_id !== plan?.generation_id) {
          setStaleGenNotice(
            `Displayed plan (${planId}) belongs to an earlier simulator generation. Run Optimizer for the active world.`,
          );
        } else if (active) {
          setStaleGenNotice(null);
        }
      } catch {
        // Ignore transient status poll failures
      }
    }
    if (approvalResult) {
      void syncApprovalStatus();
    }
    return () => {
      active = false;
    };
  }, [tick, plan?.plan_id]);

  async function runOptimize() {
    setPlanBusy(true);
    setPlanError(null);
    setBatchError(null);
    setStaleGenNotice(null);
    setReviewOpen(false);
    try {
      const res = await fetchJson<JointPlanSummary>("/api/optimize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          history_ticks: 8,
          horizon_ticks: horizonTicks,
          policy_override: policy,
          single_action_limit: singleAction ? 1 : null,
        }),
      });
      setPlan(res);
      setApprovalResult(null);
      const safePlanId = sanitizePlanId(res.plan_id);
      if (safePlanId) {
        sessionStorage.setItem(PLAN_STORAGE_KEY, safePlanId);
      } else {
        sessionStorage.removeItem(PLAN_STORAGE_KEY);
      }
    } catch (err) {
      setPlanError(err instanceof Error ? err.message : "Optimization failed");
    } finally {
      setPlanBusy(false);
    }
  }

  async function submitPlanApproval(reconcileOnly = false) {
    if (!plan || plan.shipments.length === 0 || disabled) return;
    const safePlanId = sanitizePlanId(plan.plan_id);
    if (!safePlanId) {
      setBatchError("Invalid plan identifier.");
      return;
    }
    setBatchBusy(true);
    setBatchError(null);
    try {
      const res = await fetchJson<PlanApprovalResult>(
        `/api/plans/${encodeURIComponent(safePlanId)}/approval`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            decision_rationale: rationale.trim() || null,
            expected_config_hash: plan.config_hash,
            reconcile_only: reconcileOnly,
          }),
        },
      );
      setApprovalResult(res);
      setReviewOpen(false);
      await Promise.all([
        mutate("/api/approvals"),
        mutate("/api/snapshot"),
        mutate("/api/planning"),
      ]);
      onActionComplete?.();
    } catch (err) {
      setBatchError(err instanceof Error ? err.message : "Batch plan approval failed");
      try {
        const res = await fetchJson<PlanApprovalResult>(
          `/api/plans/${encodeURIComponent(safePlanId)}/approval`,
        );
        setApprovalResult(res);
      } catch {
        // Keep current view
      }
    } finally {
      setBatchBusy(false);
    }
  }

  async function refreshApprovalView() {
    if (!plan) return;
    const safePlanId = sanitizePlanId(plan.plan_id);
    if (!safePlanId) return;
    try {
      const res = await fetchJson<PlanApprovalResult>(
        `/api/plans/${encodeURIComponent(safePlanId)}/approval`,
      );
      setApprovalResult(res);
      setBatchError(null);
    } catch (err) {
      setBatchError(err instanceof Error ? err.message : "Could not refresh plan approval status");
    }
  }

  async function runSimulation() {
    const route = routes.find((r) => r.id === simRouteId) ?? openRoutes[0];
    if (!route) return;
    setSimBusy(true);
    try {
      const res = await fetchJson<SimulateResponse>("/api/simulate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          history_ticks: 8,
          horizon_ticks: horizonTicks,
          shipments: [
            {
              source_depot_id: route.source_depot_id,
              destination_station_id: route.destination_station_id,
              route_id: route.id,
              fuel_type: simFuel,
              quantity: simQty,
            },
          ],
        }),
      });
      setSimResult(res);
    } finally {
      setSimBusy(false);
    }
  }

  const allConfirmed =
    !!approvalResult &&
    approvalResult.total_shipments > 0 &&
    approvalResult.confirmed_count === approvalResult.total_shipments;
  const planBlocked = Boolean(staleGenNotice);

  return (
    <section className="panel section-block" aria-label="Joint optimization and what-if planner">
      <header className="panel-heading">
        <div>
          <div className="eyebrow">OR-TOOLS JOINT OPTIMIZATION &amp; COUNTERFACTUAL SIMULATOR</div>
          <h2>Policy optimizer, before/after comparison &amp; what-if check (tick {tick})</h2>
        </div>
        <div className="badge-row">
          <label className="inline-control">
            <span>Policy</span>
            <select aria-label="Optimizer policy" value={policy} onChange={(e) => setPolicy(e.target.value as "AUTO" | "CHRONOS2" | "SAFE_REORDER")}>
              <option value="AUTO">OR-Tools + Seasonal</option>
              <option value="CHRONOS2">OR-Tools + Chronos-2</option>
              <option value="SAFE_REORDER">Safe Reorder Baseline</option>
            </select>
          </label>
          <label className="inline-control">
            <span>Horizon</span>
            <select aria-label="Optimizer horizon ticks" value={horizonTicks} onChange={(e) => setHorizonTicks(Number(e.target.value))}>
              <option value={32}>32 ticks</option>
              <option value={96}>96 ticks</option>
              <option value={144}>144 ticks</option>
            </select>
          </label>
          <label className="inline-control">
            <input type="checkbox" checked={singleAction} onChange={(e) => setSingleAction(e.target.checked)} />
            <span>Single shipment limit</span>
          </label>
          <button type="button" className="refresh-button" onClick={runOptimize} disabled={planBusy}>
            <Play size={13} />
            <span>{planBusy ? "Solving..." : "Run Optimizer"}</span>
          </button>
        </div>
      </header>

      {planError ? <p className="error-note">{planError}</p> : null}
      {staleGenNotice ? (
        <div className="warning-note" role="alert" style={{ marginTop: 10 }}>
          <AlertTriangle size={15} />
          <span>{staleGenNotice}</span>
        </div>
      ) : null}

      {plan ? (
        <div style={{ marginTop: 12 }}>
          <div className="plan-compare-grid">
            <div className="compare-card">
              <small>NO-ACTION UNMET</small>
              <b>{Math.round(plan.no_action_unmet_liters).toLocaleString()} L</b>
            </div>
            <div className="compare-card">
              <small>SAFE REORDER UNMET</small>
              <b>{Math.round(plan.safe_baseline_unmet_liters).toLocaleString()} L</b>
            </div>
            <div className="compare-card">
              <small>JOINT PLAN UNMET ({plan.policy_mode})</small>
              <b className="planning-benefit">{Math.round(plan.unmet_after_liters).toLocaleString()} L</b>
            </div>
            <div className="compare-card">
              <small>TOTAL AVOIDED / DISPATCHED</small>
              <b>
                {Math.round(plan.unmet_avoided_liters).toLocaleString()} L avoided ({Math.round(plan.total_allocated_liters).toLocaleString()} L)
              </b>
            </div>
          </div>

          <div className="obs-strip">
            <span>Plan ID: <b>{plan.plan_id}</b> (as of tick {plan.as_of_tick})</span>
            <span>Solver: <b>{plan.solver_status}</b> ({plan.solver_runtime_ms.toFixed(1)} ms)</span>
            <span>Source: <b>{plan.forecast_source}</b></span>
            {plan.p10_unmet_after_liters != null && plan.p90_unmet_after_liters != null ? (
              <span>Sensitivity p10–p90 unmet: <b>{Math.round(plan.p10_unmet_after_liters)}–{Math.round(plan.p90_unmet_after_liters)} L</b></span>
            ) : null}
            <span>Constraints: <b>{plan.binding_constraints.slice(0, 2).join("; ")}</b></span>
          </div>

          {plan.fallback_reason ? <p className="warning-note">{plan.fallback_reason}</p> : null}

          {plan.shipments.length > 0 ? (
            <div style={{ marginTop: 12 }}>
              <div className="panel-heading" style={{ marginBottom: 6 }}>
                <b style={{ fontSize: 12 }}>
                  Proposed joint shipments ({plan.shipments.length} line items · reviewed server-side)
                </b>
                <div className="badge-row">
                  {approvalResult ? (
                    <button
                      type="button"
                      className="refresh-button"
                      onClick={() => void refreshApprovalView()}
                      disabled={batchBusy}
                    >
                      <RefreshCw size={12} />
                      <span>Refresh batch status</span>
                    </button>
                  ) : null}
                  {approvalResult?.reconcile_required ? (
                    <button
                      type="button"
                      className="refresh-button"
                      disabled={disabled || batchBusy || planBlocked}
                      onClick={() => void submitPlanApproval(true)}
                    >
                      {batchBusy ? "Reconciling..." : `Reconcile unresolved (${approvalResult.unknown_count})`}
                    </button>
                  ) : null}
                  {!approvalResult ? (
                    <button
                      type="button"
                      className="refresh-button approve-button"
                      disabled={disabled || batchBusy || planBlocked}
                      onClick={() => setReviewOpen(true)}
                    >
                      Review joint batch ({plan.shipments.length} shipments)
                    </button>
                  ) : allConfirmed ? (
                    <button
                      type="button"
                      className="refresh-button approve-button"
                      disabled={disabled || batchBusy || planBlocked}
                      onClick={() => void submitPlanApproval(false)}
                    >
                      {batchBusy ? "Checking..." : `All ${approvalResult.confirmed_count} confirmed (replay safe)`}
                    </button>
                  ) : approvalResult.requires_new_plan ? (
                    <button
                      type="button"
                      className="refresh-button approve-button"
                      disabled={planBusy}
                      onClick={runOptimize}
                    >
                      Generate updated plan
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="refresh-button approve-button"
                      disabled={disabled || batchBusy || planBlocked}
                      onClick={() => setReviewOpen(true)}
                    >
                      {batchBusy
                        ? "Submitting..."
                        : `Resume / reconcile remaining (${approvalResult.unknown_count + approvalResult.unsent_count})`}
                    </button>
                  )}
                </div>
              </div>

              {reviewOpen && !allConfirmed ? (
                <div className="approval-review" role="region" aria-label="Joint batch approval confirmation" style={{ marginBottom: 10 }}>
                  <div style={{ flex: 1 }}>
                    <b>Confirm immutable joint plan `{plan.plan_id}` (generated at tick {plan.as_of_tick})</b>
                    <div className="muted-inline" style={{ marginTop: 4 }}>
                      Approving dispatches the exact reviewed quantities and routes below using persistent server-owned intent IDs.
                      If simulator tick moved or constraints changed, unsafe items stop before dispatch.
                    </div>
                    <div style={{ marginTop: 8 }}>
                      <input
                        aria-label="Operator batch decision note"
                        type="text"
                        placeholder="Optional decision note / rationale..."
                        value={rationale}
                        onChange={(e) => setRationale(e.target.value)}
                        style={{ width: "100%", maxWidth: 420, height: 30, fontSize: 12, padding: "0 8px" }}
                      />
                    </div>
                  </div>
                  <button type="button" className="refresh-button" onClick={() => setReviewOpen(false)} disabled={batchBusy}>
                    Cancel
                  </button>
                  <button
                    type="button"
                    className="refresh-button approve-button"
                    disabled={disabled || batchBusy || planBlocked}
                    onClick={() => void submitPlanApproval(false)}
                  >
                    {batchBusy ? "Submitting..." : `Confirm & Submit Batch (${plan.shipments.length})`}
                  </button>
                </div>
              ) : null}

              {batchError ? (
                <div className="banner error-banner" role="alert" style={{ marginBottom: 8 }}>
                  <AlertTriangle size={15} />
                  <span>{batchError}</span>
                </div>
              ) : null}

              {approvalResult ? (
                <div
                  className={
                    approvalResult.state === "CONFIRMED"
                      ? "planning-benefit"
                      : "warning-note"
                  }
                  role="status"
                  style={{ marginBottom: 8 }}
                >
                  <b>
                    Batch status: {approvalResult.state} — {approvalResult.confirmed_count}/{approvalResult.total_shipments} confirmed
                    {approvalResult.rejected_count > 0 ? `, ${approvalResult.rejected_count} rejected` : ""}
                    {approvalResult.unknown_count > 0 ? `, ${approvalResult.unknown_count} unknown` : ""}
                    {approvalResult.unsent_count > 0 ? `, ${approvalResult.unsent_count} unsent` : ""}
                  </b>
                  {approvalResult.stopped_reason ? (
                    <div style={{ marginTop: 2 }}>{approvalResult.stopped_reason}</div>
                  ) : null}
                </div>
              ) : null}

              <section className="planning-scroll" aria-label="Joint plan line-item review">
                <table className="planning-table">
                  <thead>
                    <tr>
                      <th>#</th>
                      <th>Depot → Destination</th>
                      <th>Route</th>
                      <th>Fuel</th>
                      <th>Quantity</th>
                      <th>ETA</th>
                      <th>Status / Simulator ID</th>
                    </tr>
                  </thead>
                  <tbody>
                    {plan.shipments.map((s, idx) => {
                      const item = approvalResult?.items.find((it) => it.item_index === idx);
                      return (
                        <tr key={`${plan.plan_id}-${idx}`}>
                          <td>{idx + 1}</td>
                          <th scope="row">
                            {s.source_depot_name} → {s.destination_station_name}
                          </th>
                          <td>{s.route_id}</td>
                          <td>{s.fuel_type}</td>
                          <td>{s.quantity_liters.toLocaleString()} L</td>
                          <td>{s.eta_ticks} ticks ({s.eta_minutes} min)</td>
                          <td>
                            {item ? (
                              <span>
                                <b>{item.shipment ? `#${item.shipment.id} · ${item.shipment.status}` : item.state}</b>
                                {item.message ? <div className="muted-inline">{item.message}</div> : null}
                              </span>
                            ) : (
                              <span className="muted-inline">Ready for review</span>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </section>
            </div>
          ) : (
            <p className="muted-inline" style={{ marginTop: 10 }}>
              No dispatch needed in this plan snapshot.
            </p>
          )}
        </div>
      ) : (
        <p className="muted-inline" style={{ marginTop: 10 }}>
          Click <b>Run Optimizer</b> to compare joint OR-Tools dispatch against No-Action and Safe Reorder baselines with live constraint checks.
        </p>
      )}

      <div style={{ marginTop: 16, paddingTop: 12, borderTop: "1px solid var(--line)" }}>
        <div className="badge-row" style={{ justifyContent: "space-between" }}>
          <b style={{ fontSize: 12, display: "inline-flex", alignItems: "center", gap: 6 }}>
            <SlidersHorizontal size={13} /> Read-only counterfactual shipment preview
          </b>
          <div className="badge-row">
            <select aria-label="Counterfactual route" value={simRouteId} onChange={(e) => setSimRouteId(e.target.value)} style={{ height: 30, fontSize: 11 }}>
              {routes.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.id} ({r.status}, {r.transit_ticks}t)
                </option>
              ))}
            </select>
            <select aria-label="Counterfactual fuel" value={simFuel} onChange={(e) => setSimFuel(e.target.value as Fuel)} style={{ height: 30, fontSize: 11 }}>
              <option value="DIESEL">DIESEL</option>
              <option value="PETROL">PETROL</option>
              <option value="OCTANE">OCTANE</option>
            </select>
            <input
              aria-label="Counterfactual quantity"
              type="number"
              min={10}
              max={20000}
              step={100}
              value={simQty}
              onChange={(e) => setSimQty(Number(e.target.value))}
              style={{ width: 90, height: 30, fontSize: 11 }}
            />
            <button type="button" className="refresh-button" onClick={runSimulation} disabled={simBusy}>
              {simBusy ? "Simulating..." : "Simulate"}
            </button>
          </div>
        </div>
        {simResult ? (
          <div className={`preview-result ${simResult.valid ? "preview-valid" : "preview-invalid"}`} style={{ marginTop: 10 }}>
            {simResult.valid ? <CheckCircle2 size={16} /> : <XCircle size={16} />}
            <div>
              <b>{simResult.valid ? "Feasible proposal" : `Invalid: ${simResult.validation_errors.join("; ")}`}</b>
              <p>
                Projected unmet demand: {simResult.unmet_before_liters.toLocaleString()} L →{" "}
                {simResult.unmet_after_liters.toLocaleString()} L (<b>{simResult.unmet_avoided_liters.toLocaleString()} L avoided</b>) · Source {simResult.forecast_source}
              </p>
            </div>
          </div>
        ) : null}
      </div>
    </section>
  );
}
