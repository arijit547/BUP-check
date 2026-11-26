"""Fair matched-budget multi-scenario policy evaluator against unmodified official simulator."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = ROOT / "artifacts" / "evaluation" / "policy_comparison.json"
FUELS = ("DIESEL", "PETROL", "OCTANE")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def reorder_heuristic_batch(
    snapshot: dict[str, Any],
    allocations: list[dict[str, Any]],
    *,
    max_shipments: int | None = None,
    max_total_liters: float | None = None,
) -> list[dict[str, Any]]:
    """Pure snapshot <35%->60% threshold reorder baseline honoring multi-pick stock/room/dispatch/budget limits."""
    tick = int(snapshot["simulation"]["tick"])
    depots = {d["id"]: d for d in snapshot["depots"]}
    depot_stock = {(d["id"], f): float(d["inventory"][f]) for d in snapshot["depots"] for f in FUELS}
    used_dispatch: dict[str, float] = {}
    for a in allocations:
        if a["status"] in ("PENDING", "IN_TRANSIT") and int(a["created_tick"]) == tick:
            used_dispatch[a["source_depot_id"]] = used_dispatch.get(a["source_depot_id"], 0.0) + float(a["quantity"])
    dispatch_left = {
        d["id"]: max(0.0, float(d["dispatch_capacity_per_tick"]) - used_dispatch.get(d["id"], 0.0))
        for d in snapshot["depots"]
    }
    station_room = {
        (s["id"], f): max(0.0, float(s["capacity"][f]) - float(s["inventory"][f]))
        for s in snapshot["stations"]
        for f in FUELS
    }

    candidates: list[dict[str, Any]] = []
    for station in snapshot["stations"]:
        if station["status"] != "OPEN":
            continue
        for fuel in FUELS:
            stock = float(station["inventory"][fuel])
            capacity = float(station["capacity"][fuel])
            ratio = stock / capacity if capacity > 0 else 1.0
            if ratio >= 0.35:
                continue
            desired = max(0.0, capacity * 0.6 - stock)
            for route in snapshot["routes"]:
                if route["status"] != "AVAILABLE" or route["destination_station_id"] != station["id"]:
                    continue
                depot = depots.get(route["source_depot_id"])
                if not depot or depot["status"] not in ("OPEN", "CONSTRAINED"):
                    continue
                candidates.append(
                    {
                        "source_depot_id": depot["id"],
                        "destination_station_id": station["id"],
                        "route_id": route["id"],
                        "fuel_type": fuel,
                        "desired": desired,
                        "route_max": float(route["max_shipment"]),
                        "ratio": ratio,
                        "eta": int(route["transit_ticks"]),
                    }
                )

    candidates.sort(key=lambda c: (c["ratio"], c["eta"], c["route_id"], c["fuel_type"]))
    desired_left: dict[tuple[str, str], float] = {}
    selected: list[dict[str, Any]] = []
    liters_used = 0.0

    for c in candidates:
        if max_shipments is not None and len(selected) >= max_shipments:
            break
        key = (c["destination_station_id"], c["fuel_type"])
        rem_des = desired_left.setdefault(key, c["desired"])
        rem_budget = (max_total_liters - liters_used) if max_total_liters is not None else float("inf")
        limit = min(
            rem_des,
            rem_budget,
            depot_stock[(c["source_depot_id"], c["fuel_type"])],
            dispatch_left[c["source_depot_id"]],
            c["route_max"],
            station_room[key],
        )
        qty = math.floor(max(0.0, limit) * 1000.0) / 1000.0
        if qty < 1.0:
            continue
        desired_left[key] = max(0.0, rem_des - qty)
        depot_stock[(c["source_depot_id"], c["fuel_type"])] -= qty
        dispatch_left[c["source_depot_id"]] -= qty
        station_room[key] -= qty
        liters_used += qty
        selected.append(
            {
                "source_depot_id": c["source_depot_id"],
                "destination_station_id": c["destination_station_id"],
                "route_id": c["route_id"],
                "fuel_type": c["fuel_type"],
                "quantity": qty,
            }
        )
    return selected


def seed_history(sim: httpx.Client, ticks: int = 42) -> None:
    inst = sim.get("/v1/instance").json()
    while int(inst["tick"]) < ticks:
        sim.post("/admin/step").raise_for_status()
        inst = sim.get("/v1/instance").json()


SCENARIOS: dict[str, dict[str, Any]] = {
    "spike_route_disruption": {
        "seed_ticks": 42,
        "end_tick": 150,
        "cadence": 6,
        "events": [
            {"type": "demand_spike", "start_tick": 42, "duration_ticks": 16, "parameters": {"station_ids": ["station-mirpur"], "multiplier": 1.8}},
            {"type": "route_disruption", "start_tick": 42, "duration_ticks": 12, "parameters": {"route_ids": ["route-gazipur-mirpur"]}},
        ],
    },
    "multi_station_combined_crisis": {
        "seed_ticks": 42,
        "end_tick": 150,
        "cadence": 6,
        "events": [
            {"type": "demand_spike", "start_tick": 44, "duration_ticks": 16, "parameters": {"station_ids": ["station-mirpur"], "multiplier": 1.7}},
            {"type": "route_disruption", "start_tick": 44, "duration_ticks": 10, "parameters": {"route_ids": ["route-gazipur-mirpur"]}},
            {"type": "depot_constraint", "start_tick": 50, "duration_ticks": 14, "parameters": {"depot_ids": ["depot-patiya"]}},
            {"type": "supply_shortfall", "start_tick": 52, "duration_ticks": 6, "parameters": {"depot_ids": ["depot-gazipur"], "fuel_types": ["DIESEL"], "factor": 0.6}},
            {"type": "demand_spike", "start_tick": 56, "duration_ticks": 16, "parameters": {"station_ids": ["station-coxsbazar"], "multiplier": 1.6}},
            {"type": "shipment_delay", "start_tick": 60, "duration_ticks": 10, "parameters": {"depot_ids": ["depot-patiya"], "fuel_types": ["PETROL", "DIESEL"], "delay_ticks": 3}},
        ],
    },
    "outage_and_recovery": {
        "seed_ticks": 42,
        "end_tick": 150,
        "cadence": 6,
        "events": [
            {"type": "station_outage", "start_tick": 46, "duration_ticks": 8, "parameters": {"station_ids": ["station-tongi"]}},
            {"type": "route_disruption", "start_tick": 48, "duration_ticks": 10, "parameters": {"route_ids": ["route-patiya-karnaphuli"]}},
            {"type": "demand_spike", "start_tick": 52, "duration_ticks": 18, "parameters": {"station_ids": ["station-karnaphuli"], "multiplier": 1.8}},
        ],
    },
    "heldout_late_crisis": {
        "seed_ticks": 60,
        "end_tick": 180,
        "cadence": 6,
        "events": [
            {"type": "demand_spike", "start_tick": 62, "duration_ticks": 18, "parameters": {"station_ids": ["station-karnaphuli", "station-tongi"], "multiplier": 1.65}},
            {"type": "depot_constraint", "start_tick": 64, "duration_ticks": 16, "parameters": {"depot_ids": ["depot-gazipur"]}},
            {"type": "route_disruption", "start_tick": 68, "duration_ticks": 12, "parameters": {"route_ids": ["route-gazipur-tongi"]}},
            {"type": "shipment_delay", "start_tick": 72, "duration_ticks": 10, "parameters": {"depot_ids": ["depot-patiya"], "fuel_types": ["DIESEL"], "delay_ticks": 3}},
        ],
    },
}

BUDGETS: dict[str, dict[str, Any]] = {
    "single_1_7k": {"max_shipments": 1, "max_total_liters": 7000.0, "description": "Single shipment (<= 1 truck, <= 7,000 L/step)"},
    "batch_4_15k": {"max_shipments": 4, "max_total_liters": 15000.0, "description": "Matched batch (<= 4 shipments, <= 15,000 L/step)"},
    "unconstrained": {"max_shipments": None, "max_total_liters": None, "description": "Unconstrained per-step capacity limit only"},
}

POLICIES: list[dict[str, Any]] = [
    # 1) Single-shipment equal budget (<=1 shipment, <=7,000 L)
    {"id": "reorder_single_1_7k", "budget_tier": "single_1_7k", "mode": "reorder", "max_shipments": 1, "max_total_liters": 7000.0},
    {"id": "safe_reorder_single_1_7k", "budget_tier": "single_1_7k", "mode": "planning", "override": "SAFE_REORDER", "max_shipments": 1, "max_total_liters": 7000.0},
    {"id": "ortools_seasonal_single_1_7k", "budget_tier": "single_1_7k", "mode": "planning", "override": "AUTO", "max_shipments": 1, "max_total_liters": 7000.0},
    {"id": "ortools_chronos2_single_1_7k", "budget_tier": "single_1_7k", "mode": "planning", "override": "CHRONOS2", "max_shipments": 1, "max_total_liters": 7000.0},
    # 2) Equal-budget multi-shipment (<=4 shipments, <=15,000 L/step)
    {"id": "reorder_batch_4_15k", "budget_tier": "batch_4_15k", "mode": "reorder", "max_shipments": 4, "max_total_liters": 15000.0},
    {"id": "safe_reorder_batch_4_15k", "budget_tier": "batch_4_15k", "mode": "planning", "override": "SAFE_REORDER", "max_shipments": 4, "max_total_liters": 15000.0},
    {"id": "ortools_seasonal_batch_4_15k", "budget_tier": "batch_4_15k", "mode": "planning", "override": "AUTO", "max_shipments": 4, "max_total_liters": 15000.0},
    {"id": "ortools_chronos2_batch_4_15k", "budget_tier": "batch_4_15k", "mode": "planning", "override": "CHRONOS2", "max_shipments": 4, "max_total_liters": 15000.0},
    # 3) Unconstrained multi-shipment comparison (capacity-only)
    {"id": "reorder_batch_unconstrained", "budget_tier": "unconstrained", "mode": "reorder", "max_shipments": None, "max_total_liters": None},
    {"id": "ortools_seasonal_batch_unconstrained", "budget_tier": "unconstrained", "mode": "planning", "override": "AUTO", "max_shipments": None, "max_total_liters": None},
    {"id": "ortools_chronos2_batch_unconstrained", "budget_tier": "unconstrained", "mode": "planning", "override": "CHRONOS2", "max_shipments": None, "max_total_liters": None},
]


def _decompose_demand_windows(sim: httpx.Client, seed_ticks: int) -> dict[str, float]:
    stations = sim.get("/v1/stations").json()
    events = sim.get("/v1/events").json()
    outage_windows: list[tuple[int, int, set[str]]] = []
    for ev in events:
        if ev.get("type") == "station_outage":
            sids = set((ev.get("parameters") or {}).get("station_ids") or [])
            outage_windows.append((int(ev["start_tick"]), int(ev["end_tick"]), sids))

    warmup_unmet = 0.0
    measured_served = 0.0
    measured_unmet = 0.0
    outage_unmet = 0.0
    for st in stations:
        sid = st["id"]
        rows = sim.get(f"/v1/demand-history?station_id={sid}&limit=2000").json()
        for r in rows:
            t = int(r["tick"])
            served = float(r["served_liters"])
            unmet = float(r["unmet_liters"])
            if t < seed_ticks:
                warmup_unmet += unmet
            else:
                measured_served += served
                measured_unmet += unmet
                in_outage = any(
                    st_t <= t < en_t and (not sids or sid in sids)
                    for st_t, en_t, sids in outage_windows
                )
                if in_outage and math.isclose(served, 0.0, abs_tol=1e-9) and unmet > 0.0:
                    outage_unmet += unmet

    depletion_unmet = max(0.0, measured_unmet - outage_unmet)
    meas_denom = measured_served + measured_unmet
    return {
        "warmup_unmet_liters": round(warmup_unmet, 3),
        "measured_served_liters": round(measured_served, 3),
        "measured_unmet_liters": round(measured_unmet, 3),
        "measured_service_level": round(measured_served / meas_denom, 6) if meas_denom > 0 else 1.0,
        "outage_unmet_liters": round(outage_unmet, 3),
        "depletion_unmet_liters": round(depletion_unmet, 3),
    }


def run_policy_trial(sim_url: str, api_url: str, scenario_name: str, policy: dict[str, Any]) -> dict[str, Any]:
    spec = SCENARIOS[scenario_name]
    with httpx.Client(base_url=sim_url, timeout=15.0) as sim, httpx.Client(base_url=api_url, timeout=30.0) as api:
        sim.post("/admin/reset").raise_for_status()
        seed_history(sim, spec["seed_ticks"])
        start = sim.get("/v1/instance").json()
        for ev in spec["events"]:
            sim.post("/admin/events", json=ev).raise_for_status()

        decisions_confirmed = 0
        decisions_rejected = 0
        latencies_ms: list[float] = []
        decision_steps = 0
        chronos_steps = 0
        fallback_steps = 0
        fallback_reasons: set[str] = set()

        for _tick in range(int(start["tick"]), int(spec["end_tick"])):
            nxt = sim.post("/admin/step").json()
            tick_now = int(nxt["tick"])
            if (tick_now - int(start["tick"]) - 1) % int(spec["cadence"]) != 0 or tick_now + int(spec["cadence"]) > int(spec["end_tick"]):
                continue

            decision_steps += 1
            t0 = time.perf_counter()
            if policy["mode"] == "reorder":
                snap = api.get("/api/snapshot").json()
                existing_allocs = sim.get("/v1/allocations").json()
                picks = reorder_heuristic_batch(
                    snap,
                    existing_allocs,
                    max_shipments=policy["max_shipments"],
                    max_total_liters=policy["max_total_liters"],
                )
                latencies_ms.append((time.perf_counter() - t0) * 1000.0)
                for pick in picks:
                    r = api.put(f"/api/approvals/{uuid4()}", json=pick)
                    if r.status_code == 200 and r.json().get("state") == "CONFIRMED":
                        decisions_confirmed += 1
                    else:
                        decisions_rejected += 1
            else:
                params: dict[str, Any] = {
                    "history_ticks": 16,
                    "horizon_ticks": 96,
                    "policy_override": policy["override"],
                }
                if policy["max_shipments"] is not None:
                    params["single_action_limit"] = policy["max_shipments"]
                if policy["max_total_liters"] is not None:
                    params["max_total_liters"] = policy["max_total_liters"]
                plan = api.get("/api/planning?" + urlencode(params)).json()
                latencies_ms.append((time.perf_counter() - t0) * 1000.0)

                src = plan.get("forecast_source")
                if policy["override"] == "CHRONOS2":
                    if src == "CHRONOS2":
                        chronos_steps += 1
                    else:
                        fallback_steps += 1
                        if plan.get("fallback_reason"):
                            fallback_reasons.add(str(plan["fallback_reason"]))
                elif plan.get("fallback_active"):
                    fallback_steps += 1
                    if plan.get("fallback_reason"):
                        fallback_reasons.add(str(plan["fallback_reason"]))

                plan_id = plan.get("plan_id")
                if plan.get("recommendations"):
                    batch_resp = api.put(f"/api/plans/{plan_id}/approval", json={})
                    if batch_resp.status_code == 200:
                        br = batch_resp.json()
                        decisions_confirmed += int(br.get("confirmed_count", 0))
                        decisions_rejected += int(br.get("rejected_count", 0)) + int(br.get("unknown_count", 0))
                    else:
                        decisions_rejected += len(plan["recommendations"])

        metrics = sim.get("/v1/metrics").json()
        decomp = _decompose_demand_windows(sim, int(spec["seed_ticks"]))
        return {
            "scenario": scenario_name,
            "policy": policy["id"],
            "budget_tier": policy["budget_tier"],
            "seed": start.get("seed", 12345),
            "seed_ticks": spec["seed_ticks"],
            "end_tick": spec["end_tick"],
            "cadence": spec["cadence"],
            "decision_steps": decision_steps,
            "chronos2_steps": chronos_steps,
            "fallback_steps": fallback_steps,
            "fallback_reasons": sorted(fallback_reasons),
            "decisions_confirmed": decisions_confirmed,
            "decision_failures": decisions_rejected,
            "avg_decision_latency_ms": round(sum(latencies_ms) / max(1, len(latencies_ms)), 2),
            "p95_decision_latency_ms": round(
                sorted(latencies_ms)[min(len(latencies_ms) - 1, int(0.95 * len(latencies_ms)))], 2
            ) if latencies_ms else 0.0,
            **decomp,
            **metrics,
        }


def main() -> None:
    sim_port = free_port()
    api_port = free_port()
    dash_port = free_port()
    project = f"bup-eval-{uuid4().hex[:8]}"
    env = {
        **os.environ,
        "SIMULATOR_PORT": str(sim_port),
        "SNAPSHOT_PORT": str(api_port),
        "DASHBOARD_PORT": str(dash_port),
    }
    compose_cmd = [
        "docker", "compose", "-p", project,
        "-f", "compose.yaml", "-f", "compose.ml.yaml", "--profile", "ml",
    ]
    rows: list[dict[str, Any]] = []
    try:
        subprocess.run([*compose_cmd, "up", "-d", "--build", "--wait"], cwd=ROOT, env=env, check=True)
        sim_url = f"http://127.0.0.1:{sim_port}"
        api_url = f"http://127.0.0.1:{api_port}"
        for _ in range(45):
            try:
                h = httpx.get(f"{api_url}/api/health", timeout=5.0).json()
                if h.get("components", {}).get("ml_service", {}).get("status") == "ok":
                    break
            except Exception:
                pass
            time.sleep(2)

        for scen in SCENARIOS:
            for pol in POLICIES:
                row = run_policy_trial(sim_url, api_url, scen, pol)
                rows.append(row)
                print(json.dumps(row), flush=True)
    finally:
        subprocess.run([*compose_cmd, "down", "-v"], cwd=ROOT, env=env, check=False)

    by_policy: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_policy.setdefault(row["policy"], []).append(row)

    agg: dict[str, Any] = {}
    for pol_cfg in POLICIES:
        pol = pol_cfg["id"]
        p_rows = by_policy.get(pol, [])
        if not p_rows:
            continue
        agg[pol] = {
            "budget_tier": pol_cfg["budget_tier"],
            "max_shipments": pol_cfg["max_shipments"],
            "max_total_liters": pol_cfg["max_total_liters"],
            "mean_service_level": round(sum(r["service_level"] for r in p_rows) / len(p_rows), 6),
            "measured_window_service_level": round(
                sum(r["measured_served_liters"] for r in p_rows)
                / max(1e-6, sum(r["measured_served_liters"] + r["measured_unmet_liters"] for r in p_rows)),
                6,
            ),
            "total_unmet_liters": round(sum(r["unmet_demand_liters"] for r in p_rows), 3),
            "outage_unmet_liters": round(sum(r["outage_unmet_liters"] for r in p_rows), 3),
            "depletion_unmet_liters": round(sum(r["depletion_unmet_liters"] for r in p_rows), 3),
            "total_allocated_liters": round(sum(r["allocation_liters"] for r in p_rows), 3),
            "total_decisions": sum(r["decisions_confirmed"] for r in p_rows),
            "total_failures": sum(r["decision_failures"] for r in p_rows),
            "chronos2_steps": sum(r["chronos2_steps"] for r in p_rows),
            "fallback_steps": sum(r["fallback_steps"] for r in p_rows),
            "mean_decision_latency_ms": round(sum(r["avg_decision_latency_ms"] for r in p_rows) / len(p_rows), 2),
        }

    payload = {
        "methodology": {
            "simulator_image": "asifmahmoud414/bup-fuel-supply-simulator:1.0.0",
            "seed": 12345,
            "note": (
                "Single seed (12345) deterministic simulator across 4 varied scenario schedules including heldout_late_crisis. "
                "Comparisons hold scenario, initial warmup, measurement window, cadence, shipment limits, and liter budgets identical per tier. "
                "Earlier v1 historical unconstrained comparison (99.49% vs 97.61% with 797 vs 70 dispatches) is retained for context."
            ),
            "historical_context": {
                "ortools_seasonal_batch_797_dispatches": {"mean_service_level": 0.994912, "total_unmet_liters": 3017.126, "decisions": 797},
                "original_reorder_single_70_dispatches": {"mean_service_level": 0.976098, "total_unmet_liters": 16470.692, "decisions": 70},
            },
        },
        "budgets": BUDGETS,
        "scenarios": SCENARIOS,
        "trials": rows,
        "aggregate": agg,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print("\nAGGREGATE:\n" + json.dumps(agg, indent=2))


if __name__ == "__main__":
    main()
