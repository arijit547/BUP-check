# BUP Fuel Supply Operations — Decision & Optimization Methodology

## 1. Demand Forecasting & Uncertainty Modeling

Each `(station_id, fuel_type)` series is regularized onto a gap-aware tick timeline ending at `tick - 1` (preventing look-ahead leakage from the open current tick):
- **Primary Seasonal Model (`SEASONAL`):** Removes station profile diurnal variation (`hour_factor(profile, hour)`) and known active/past spike multipliers from observed demand to estimate normalized base demand rate $\bar{d}_{\text{norm}}$, then projects each future tick $t + \tau$ using future hour factor and known active/scheduled multiplier, bounded by normalized residual variance $\sigma$ (`p10`, `p50`, `p90`).
- **Pretrained ML Candidate (`CHRONOS2`):** Passes historical observations and causal horizon covariates to local `amazon/chronos-2` (`ml-service`) predicting native non-negative monotone `[p10, p50, p90]` quantile trajectories.
- **Cold-Start / Insufficient History Fallback (`PROFILE_FALLBACK`):** Activates whenever fewer than `history_ticks` exist in the current generation; uses official simulator daily profile tables scaled by region factor, hour of day, and active demand multiplier.

## 2. Shortage Projection & Safety Stock Target

Inventory evolves tick by tick over `horizon_ticks` ($\tau = 0 \dots H - 1$):
1. Incoming shipments scheduled via `IN_TRANSIT` (`expected_arrival_tick`) or `PENDING` (`route.transit_ticks`) arrive before tick demand, clamped at station tank capacity.
2. If `station.status == "OUTAGE"`, served demand is `0` and unmet demand accumulates strictly into `outage_unmet_liters` (distinguishing unpreventable outages from stock depletion).
3. If `station.status == "OPEN"`, served demand is `min(stock, demand_t)` and shortfall accumulates into `depletion_unmet_liters`.
4. **Dynamic Safety Stock Target ([shortage.py](../backend/intelligence/shortage.py)):**
   $$\text{Safety Target} = \min\!\Big(\text{cap},\; \sum_{\tau=0}^{L+\text{cadence}-1} \hat{d}_\tau + 1.65\,\sigma \sqrt{L + \text{cadence}}\Big)$$
   where $L$ is the minimum open corridor lead time (`transit_ticks`) and $\text{cadence}=6$ ticks.

## 3. Constrained Allocation Optimization (Google OR-Tools CBC)

Formulated in [optimizer.py](../backend/intelligence/optimizer.py) using continuous dispatch quantities $x_{r,f} \ge 0$ and binary dispatch indicators $y_{r,f} \in \{0, 1\}$ per available `(route r, fuel f)` pair:

### Hard Constraints (Verified Against Simulator Invariants)
- **Depot Eligibility & Stock:** $\sum_{r \in \text{out}(d)} x_{r,f} \le I_{d,f}^{\text{depot}}$ for `status in ("OPEN", "CONSTRAINED")`.
- **Depot Dispatch Capacity Remaining:** $\sum_{r \in \text{out}(d), f} x_{r,f} \le C_d^{\text{dispatch}} - U_d^{\text{tick}}$ where $U_d^{\text{tick}}$ sums existing allocations with `created_tick == tick`.
- **Route Open & Lot Limits:** $y_{r,f} \cdot q_{\min} \le x_{r,f} \le y_{r,f} \cdot \min(\text{max\_ship}_r,\; \text{room}_0,\; \text{room}_{\text{eta}})$ for routes with `status == "AVAILABLE"` and open destination station (`status == "OPEN"`).
- **Immediate Station Storage Limit (Simulator Rule):** $\sum_{r \in \text{in}(s)} x_{r,f} \le \text{cap}_{s,f} - I_{s,f}^{\text{now}}$ so concurrent route picks never exceed destination tank room at submission.
- **Step Shipment & Liter Budgets (Optional):** $\sum_{r,f} y_{r,f} \le K$ (`single_action_limit`) and $\sum_{r,f} x_{r,f} \le B$ (`max_total_liters`) when bounded budgets apply.
- **Horizon Inventory Balance & Unmet Demand:** $I_{s,f,t} = I_{s,f,t-1} + A_{s,f,t} + \sum_{r:\eta_r=t} x_{r,f} - (d_{s,f,t} - u_{s,f,t})$ across $t = 0 \dots H - 1$, $0 \le I_{s,f,t} \le \text{cap}_{s,f}$.

### Multi-Objective Cost Minimization
1. **Minimize Urgency-Weighted Unmet Demand:** $\sum u_{s,f,t} \cdot 100 (1 + \frac{16}{t+4})$ ensures imminent shortages strictly outrank distant shortages.
2. **Minimax Fairness & Worst-Case Reserve Deficit:** Penalizes `max_unmet_ratio` across all open `(station, fuel)` pairs plus normalized worst-case tank deficit ratio `W_{s,f}` (`3200.0`) so smaller tanks (e.g., `OCTANE` `6,000–9,000 L`) are never starved behind large `DIESEL` tanks (`14,000–18,000 L`).
3. **Post-Arrival Safety Reserve Checkpoints:** Capacity-normalized penalties on shortfall below target reserve over post-arrival checkpoints.
4. **Truck Load Consolidation & Corridor Tie-Break:** Fixed setup penalty per active truck (`140.0 * y`) favors full tanker loads over fragmented drips, plus tiny transit tie-breaker (`0.002 * eta * x`).

### Post-Solve Verification, Safe Reorder Fallback & Joint Approval
- Every OR-Tools plan is floored to simulator 3-decimal precision, persisted immutably with its `plan_id` in SQLite WAL, and passed through `validate_plan_independently()` before surfacing.
- If CBC times out (`solve_timeout_ms=2000`), is infeasible, or fails validation, the engine deterministically falls back to `build_safe_reorder_recommendations()` (`SAFE_REORDER`).
- Joint batches execute sequentially through `PUT /api/plans/{plan_id}/approval` ([approvals.py](../backend/approvals.py)): a fresh simulator snapshot and generation check run before committing the first item; confirmed items are reconciled without resending; `UNKNOWN` post-timeout outcomes are reconciled/retried with their deterministic UUID5 `intent_id`; and any rejection, unresolved failure, or mid-batch generation drift halts immediately with remaining items left `UNSENT` (`requires_new_plan=true`).
- Joint before/after unmet impact (`unmet_before_liters`, `unmet_after_liters`, `p10/p90` sensitivity bounds) and custom user what-if trials (`POST /api/simulate`) share the exact `simulate_joint_impact()` engine.
