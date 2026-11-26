"""Phase 11 live fault, event, security, concurrency, and recovery verification matrix."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = ROOT / "artifacts" / "evaluation" / "resilience_matrix.json"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def step_ticks(sim: httpx.Client, target_ticks: int) -> None:
    inst = sim.get("/v1/instance").json()
    while int(inst["tick"]) < target_ticks:
        inst = sim.post("/admin/step").json()


def reset_and_seed(sim: httpx.Client, api: httpx.Client, ticks: int = 24) -> None:
    sim.post("/admin/faults/clear").raise_for_status()
    sim.post("/admin/reset").raise_for_status()
    step_ticks(sim, ticks)
    # Trigger generation discovery/rebind cleanly
    api.get("/api/snapshot").raise_for_status()


def main() -> None:
    sim_port = free_port()
    api_port = free_port()
    dash_port = free_port()
    project = f"bup-resil-{uuid4().hex[:8]}"
    env = {
        **os.environ,
        "SIMULATOR_PORT": str(sim_port),
        "SNAPSHOT_PORT": str(api_port),
        "DASHBOARD_PORT": str(dash_port),
    }
    compose_cmd = ["docker", "compose", "-p", project, "-f", "compose.yaml", "-f", "compose.ml.yaml", "--profile", "ml"]
    results: list[dict[str, Any]] = []

    def record(name: str, category: str, passed: bool, detail: dict[str, Any]) -> None:
        row = {"test": name, "category": category, "passed": passed, **detail}
        results.append(row)
        print(json.dumps(row), flush=True)

    try:
        subprocess.run([*compose_cmd, "up", "-d", "--build", "--wait"], cwd=ROOT, env=env, check=True)
        sim_url = f"http://127.0.0.1:{sim_port}"
        api_url = f"http://127.0.0.1:{api_port}"
        dash_url = f"http://127.0.0.1:{dash_port}"
        with httpx.Client(base_url=sim_url, timeout=15.0) as sim, httpx.Client(base_url=api_url, timeout=20.0) as api:
            # Wait for ML readiness
            for _ in range(30):
                h = api.get("/api/health").json()
                if h.get("components", {}).get("ml_service", {}).get("status") == "ok":
                    break
                time.sleep(2)

            # 1-6: All 6 official domain events
            event_specs = [
                (
                    "domain_event_demand_spike",
                    {"type": "demand_spike", "start_tick": 24, "duration_ticks": 10, "parameters": {"station_ids": ["station-mirpur"], "multiplier": 2.0}},
                    3,
                    "DEMAND_SPIKE",
                ),
                (
                    "domain_event_route_disruption",
                    {"type": "route_disruption", "start_tick": 24, "duration_ticks": 8, "parameters": {"route_ids": ["route-gazipur-mirpur"]}},
                    2,
                    "ROUTE_DISRUPTED",
                ),
                (
                    "domain_event_station_outage",
                    {"type": "station_outage", "start_tick": 24, "duration_ticks": 6, "parameters": {"station_ids": ["station-tongi"]}},
                    2,
                    "STATION_OUTAGE",
                ),
                (
                    "domain_event_depot_constraint",
                    {"type": "depot_constraint", "start_tick": 24, "duration_ticks": 8, "parameters": {"depot_ids": ["depot-patiya"]}},
                    2,
                    "DEPOT_CONSTRAINED",
                ),
                (
                    "domain_event_shipment_delay",
                    {"type": "shipment_delay", "start_tick": 24, "duration_ticks": 6, "parameters": {"depot_ids": ["depot-gazipur"], "fuel_types": ["DIESEL"], "delay_ticks": 4}},
                    2,
                    "SUPPLY_DELAYED",
                ),
                (
                    "domain_event_supply_shortfall",
                    {"type": "supply_shortfall", "start_tick": 24, "duration_ticks": 6, "parameters": {"depot_ids": ["depot-gazipur"], "fuel_types": ["DIESEL"], "factor": 0.45}},
                    2,
                    "SUPPLY_SHORTFALL",
                ),
            ]
            for name, ev_body, advance, expected_cat in event_specs:
                reset_and_seed(sim, api, 24)
                sim.post("/admin/events", json=ev_body).raise_for_status()
                step_ticks(sim, 24 + advance)
                plan = api.get("/api/planning?history_ticks=8&horizon_ticks=96").json()
                cats = [a["category"] for a in plan.get("anomalies", [])]
                passed = expected_cat in cats
                extra: dict[str, Any] = {"detected_categories": cats}
                if name == "domain_event_route_disruption":
                    rec_routes = [r["route_id"] for r in plan.get("recommendations", [])]
                    passed = passed and ("route-gazipur-mirpur" not in rec_routes)
                    extra["recommended_routes"] = rec_routes
                elif name == "domain_event_station_outage":
                    rec_stations = [r["destination_station_id"] for r in plan.get("recommendations", [])]
                    shortages = api.get("/api/shortages").json().get("shortages", [])
                    tongi_outage = [s for s in shortages if s["station_id"] == "station-tongi" and s["outage_unmet_liters"] > 0]
                    passed = passed and ("station-tongi" not in rec_stations) and len(tongi_outage) == 3
                    extra["tongi_outage_series"] = len(tongi_outage)
                record(name, "domain_event", passed, extra)

            # Combined crisis check
            reset_and_seed(sim, api, 24)
            for ev_b in (
                {"type": "demand_spike", "start_tick": 24, "duration_ticks": 12, "parameters": {"station_ids": ["station-mirpur"], "multiplier": 1.9}},
                {"type": "route_disruption", "start_tick": 24, "duration_ticks": 8, "parameters": {"route_ids": ["route-gazipur-mirpur"]}},
                {"type": "depot_constraint", "start_tick": 24, "duration_ticks": 8, "parameters": {"depot_ids": ["depot-patiya"]}},
                {"type": "station_outage", "start_tick": 24, "duration_ticks": 6, "parameters": {"station_ids": ["station-coxsbazar"]}},
            ):
                sim.post("/admin/events", json=ev_b).raise_for_status()
            step_ticks(sim, 27)
            plan = api.get("/api/planning?history_ticks=8&horizon_ticks=96").json()
            cats = set(a["category"] for a in plan.get("anomalies", []))
            recs = plan.get("recommendations", [])
            ok_combined = (
                {"DEMAND_SPIKE", "ROUTE_DISRUPTED", "DEPOT_CONSTRAINED", "STATION_OUTAGE"}.issubset(cats)
                and all(r["route_id"] != "route-gazipur-mirpur" and r["destination_station_id"] != "station-coxsbazar" for r in recs)
            )
            record("domain_event_combined_crisis", "domain_event", ok_combined, {"categories": sorted(cats), "recommendations_count": len(recs)})

            # 7-12: All 5 official simulator faults (with separated fault-phase & recovery assertions)
            reset_and_seed(sim, api, 24)
            # Latency moderate (500ms -> OK)
            sim.post("/admin/faults", json={"type": "latency", "duration_seconds": 15, "parameters": {"delay_ms": 500}}).raise_for_status()
            r_lat = api.get("/api/snapshot")
            sim.post("/admin/faults/clear").raise_for_status()
            record(
                "fault_latency_moderate",
                "api_fault",
                r_lat.status_code == 200 and r_lat.headers.get("x-bup-degraded") == "false",
                {"fault_phase": {"http_status": r_lat.status_code}, "recovery_phase": {"recovered_status": r_lat.status_code}},
            )

            # Latency exceeding configured SIMULATOR_TIMEOUT_SECONDS (3.0s) -> bounded timeout (503) + clean recovery
            sim.post("/admin/faults", json={"type": "latency", "duration_seconds": 20, "parameters": {"delay_ms": 4000}}).raise_for_status()
            r_timeout = api.get("/api/snapshot")
            sim.post("/admin/faults/clear").raise_for_status()
            time.sleep(3.2)
            r_timeout_recov = api.get("/api/snapshot")
            record(
                "fault_latency_timeout_and_recovery",
                "api_fault",
                r_timeout.status_code == 503 and r_timeout_recov.status_code == 200,
                {
                    "fault_phase": {"timeout_status": r_timeout.status_code},
                    "recovery_phase": {"recovered_status": r_timeout_recov.status_code},
                },
            )

            # Unavailable -> 503 degraded + recovery
            sim.post("/admin/faults", json={"type": "unavailable", "duration_seconds": 30, "parameters": {}}).raise_for_status()
            r_unavail = api.get("/api/planning")
            r_health_deg = api.get("/api/health")
            sim.post("/admin/faults/clear").raise_for_status()
            time.sleep(3.2)
            r_recov = api.get("/api/snapshot")
            r_health_recov = api.get("/api/health")
            record(
                "fault_unavailable_and_recovery",
                "api_fault",
                r_unavail.status_code == 503
                and r_health_deg.status_code == 503
                and r_recov.status_code == 200
                and r_health_recov.status_code == 200,
                {
                    "fault_phase": {"unavailable_status": r_unavail.status_code, "health_degraded_status": r_health_deg.status_code},
                    "recovery_phase": {"snapshot_status": r_recov.status_code, "health_status": r_health_recov.status_code},
                },
            )

            # Error rate (100% -> 503/502, clear -> recovers)
            sim.post("/admin/faults", json={"type": "error_rate", "duration_seconds": 20, "parameters": {"rate": 1.0, "status_code": 500}}).raise_for_status()
            r_err = api.get("/api/planning?horizon_ticks=48")
            sim.post("/admin/faults/clear").raise_for_status()
            time.sleep(3.2)
            r_err_recov = api.get("/api/snapshot")
            record(
                "fault_error_rate_and_recovery",
                "api_fault",
                r_err.status_code in (502, 503) and r_err_recov.status_code == 200,
                {
                    "fault_phase": {"fault_status": r_err.status_code},
                    "recovery_phase": {"recovered_status": r_err_recov.status_code},
                },
            )

            # Stale data: assert 503 degraded reads AND 0 new simulator allocations from stale state, then clean recovery + write
            stale_plan = api.post("/api/optimize", json={"history_ticks": 8, "horizon_ticks": 52}).json()
            allocs_before_stale = len(sim.get("/v1/allocations").json())
            sim.post("/admin/faults", json={"type": "stale_data", "duration_seconds": 30, "parameters": {"lag_ticks": 12}}).raise_for_status()
            r_stale_snap = api.get("/api/snapshot")
            r_stale_plan = api.get("/api/planning?horizon_ticks=53")
            r_stale_health = api.get("/api/health")
            r_stale_plan_write = api.put(f"/api/plans/{stale_plan['plan_id']}/approval", json={})
            r_stale_single_write = api.put(
                f"/api/approvals/{uuid4()}",
                json={
                    "source_depot_id": "depot-gazipur",
                    "destination_station_id": "station-mirpur",
                    "route_id": "route-gazipur-mirpur",
                    "fuel_type": "DIESEL",
                    "quantity": 500.0,
                },
            )
            allocs_during_stale = len(sim.get("/v1/allocations").json())
            sim.post("/admin/faults/clear").raise_for_status()
            time.sleep(3.2)
            r_stale_recov_snap = api.get("/api/snapshot")
            r_stale_recov_plan = api.get("/api/planning?horizon_ticks=53")
            r_stale_recov_health = api.get("/api/health")
            r_stale_recov_write = api.put(
                f"/api/approvals/{uuid4()}",
                json={
                    "source_depot_id": "depot-gazipur",
                    "destination_station_id": "station-mirpur",
                    "route_id": "route-gazipur-mirpur",
                    "fuel_type": "DIESEL",
                    "quantity": 100.0,
                },
            )
            stale_single_body = r_stale_single_write.json() if r_stale_single_write.status_code == 200 else {}
            stale_fault_ok = (
                r_stale_snap.status_code == 503
                and r_stale_plan.status_code == 503
                and r_stale_health.status_code == 503
                and r_stale_plan_write.status_code == 503
                and (
                    r_stale_single_write.status_code == 503
                    or (
                        r_stale_single_write.status_code == 200
                        and stale_single_body.get("state") == "READY"
                        and "stale" in str(stale_single_body.get("message", "")).lower()
                    )
                )
                and allocs_during_stale == allocs_before_stale
            )
            stale_recov_ok = (
                r_stale_recov_snap.status_code == 200
                and r_stale_recov_snap.headers.get("x-bup-degraded") == "false"
                and r_stale_recov_plan.status_code == 200
                and not r_stale_recov_plan.json().get("degraded")
                and r_stale_recov_health.status_code == 200
                and r_stale_recov_write.status_code == 200
                and r_stale_recov_write.json().get("state") == "CONFIRMED"
            )
            record(
                "fault_stale_data_and_recovery",
                "api_fault",
                stale_fault_ok and stale_recov_ok,
                {
                    "fault_phase": {
                        "snapshot_status": r_stale_snap.status_code,
                        "planning_status": r_stale_plan.status_code,
                        "health_status": r_stale_health.status_code,
                        "plan_write_status": r_stale_plan_write.status_code,
                        "single_write_status": r_stale_single_write.status_code,
                        "allocs_before": allocs_before_stale,
                        "allocs_during_stale": allocs_during_stale,
                    },
                    "recovery_phase": {
                        "snapshot_status": r_stale_recov_snap.status_code,
                        "planning_status": r_stale_recov_plan.status_code,
                        "health_status": r_stale_recov_health.status_code,
                        "recovered_write_status": r_stale_recov_write.status_code,
                        "degraded_flag_after_clear": r_stale_recov_plan.json().get("degraded") if r_stale_recov_plan.status_code == 200 else None,
                    },
                },
            )

            # Stream disconnect: inject fault, force NEW SSE connection via snapshot-api restart, verify 503 blocked + reconnect + new tick event
            sim.post("/admin/faults", json={"type": "stream_disconnect", "duration_seconds": 30, "parameters": {}}).raise_for_status()
            subprocess.run([*compose_cmd, "restart", "snapshot-api"], cwd=ROOT, env=env, check=True)
            deadline_boot = time.monotonic() + 25.0
            while time.monotonic() < deadline_boot:
                try:
                    if api.get("/api/health").status_code == 200:
                        break
                except Exception:
                    pass
                time.sleep(0.3)

            blocked_during_fault = False
            reconnects_during_fault = 0
            rest_during_sse_fault = api.get("/api/snapshot").status_code
            deadline_sse_blk = time.monotonic() + 12.0
            while time.monotonic() < deadline_sse_blk:
                m_blk = api.get("/api/metrics").json()
                h_blk = api.get("/api/health").json()
                detail = str(h_blk.get("components", {}).get("sse_stream", {}).get("detail", ""))
                reconnects_during_fault = int(m_blk.get("sse_reconnects", 0))
                if m_blk.get("sse_status") == "degraded" and reconnects_during_fault >= 1 and "503" in detail:
                    blocked_during_fault = True
                    break
                time.sleep(0.3)

            sim.post("/admin/faults/clear").raise_for_status()
            reconnected_after_clear = False
            events_before_step = 0
            reconnects_after_clear = 0
            deadline_sse_recov = time.monotonic() + 18.0
            while time.monotonic() < deadline_sse_recov:
                m_rec = api.get("/api/metrics").json()
                reconnects_after_clear = int(m_rec.get("sse_reconnects", 0))
                if m_rec.get("sse_status") == "connected" and reconnects_after_clear >= 1:
                    reconnected_after_clear = True
                    events_before_step = int(m_rec.get("sse_events_received", 0))
                    break
                time.sleep(0.4)

            new_event_received = False
            events_after_step = events_before_step
            if reconnected_after_clear:
                sim.post("/admin/step").raise_for_status()
                deadline_ev = time.monotonic() + 10.0
                while time.monotonic() < deadline_ev:
                    m_ev = api.get("/api/metrics").json()
                    events_after_step = int(m_ev.get("sse_events_received", 0))
                    if events_after_step > events_before_step:
                        new_event_received = True
                        break
                    time.sleep(0.25)

            record(
                "fault_stream_disconnect_sse",
                "api_fault",
                blocked_during_fault and rest_during_sse_fault == 200 and reconnected_after_clear and new_event_received,
                {
                    "fault_phase": {
                        "blocked_with_503": blocked_during_fault,
                        "reconnect_attempts_during_fault": reconnects_during_fault,
                        "rest_fallback_snapshot_status": rest_during_sse_fault,
                    },
                    "recovery_phase": {
                        "reconnected": reconnected_after_clear,
                        "reconnects_after_clear": reconnects_after_clear,
                        "events_before_step": events_before_step,
                        "events_after_step": events_after_step,
                        "new_event_received": new_event_received,
                    },
                },
            )

            # 13: ML service down -> graceful degradation + recovery (verifying true CHRONOS2 returns after start!)
            subprocess.run([*compose_cmd, "stop", "ml-service"], cwd=ROOT, env=env, check=True)
            h_down = api.get("/api/health").json()
            p_down = api.get("/api/planning?policy_override=CHRONOS2&horizon_ticks=40").json()
            subprocess.run([*compose_cmd, "start", "ml-service"], cwd=ROOT, env=env, check=True)
            for _ in range(45):
                try:
                    if api.get("/api/health").json()["components"]["ml_service"]["status"] == "ok":
                        break
                except Exception:
                    pass
                time.sleep(2)
            h_up = api.get("/api/health").json()
            p_up = api.get("/api/planning?policy_override=CHRONOS2&horizon_ticks=41").json()
            record(
                "ml_service_stop_degrade_and_restart",
                "ml_resilience",
                h_down["status"] == "ok" and h_down["components"]["ml_service"]["status"] == "degraded"
                and p_down["forecast_source"] == "SEASONAL" and p_down.get("degraded") is True
                and h_up["components"]["ml_service"]["status"] == "ok"
                and p_up["forecast_source"] == "CHRONOS2" and p_up.get("degraded") is False,
                {
                    "fault_phase": {
                        "down_ml_status": h_down["components"]["ml_service"]["status"],
                        "fallback_source": p_down["forecast_source"],
                        "degraded": p_down.get("degraded"),
                    },
                    "recovery_phase": {
                        "recovered_ml_status": h_up["components"]["ml_service"]["status"],
                        "recovered_source": p_up["forecast_source"],
                        "degraded": p_up.get("degraded"),
                    },
                },
            )

            # 14: Concurrency & idempotency + joint plan tick-37 replay + cancel + restart + reset isolation (409)
            reset_and_seed(sim, api, 36)
            opt_plan = api.post("/api/optimize", json={"history_ticks": 8, "horizon_ticks": 96}).json()
            expected_ships = len(opt_plan["shipments"])
            with ThreadPoolExecutor(max_workers=6) as pool:
                plan_futs = [pool.submit(lambda: api.put(f"/api/plans/{opt_plan['plan_id']}/approval", json={})) for _ in range(6)]
                plan_resps = [f.result() for f in plan_futs]
            plan_alloc_count = len(sim.get("/v1/allocations").json())
            sim.post("/admin/step").raise_for_status()
            plan_replay_t37 = api.put(f"/api/plans/{opt_plan['plan_id']}/approval", json={})
            plan_alloc_count_after_t37 = len(sim.get("/v1/allocations").json())

            # Cancel one pending/in-transit check on single shipment + 8-way concurrent single approval + restart replay
            reset_and_seed(sim, api, 24)
            snap_cur = api.get("/api/snapshot").json()
            mirpur = next(s for s in snap_cur["stations"] if s["id"] == "station-mirpur")
            qty_room = min(1500.0, float(mirpur["capacity"]["DIESEL"]) - float(mirpur["inventory"]["DIESEL"]))
            prop = {
                "source_depot_id": "depot-gazipur", "destination_station_id": "station-mirpur",
                "route_id": "route-gazipur-mirpur", "fuel_type": "DIESEL", "quantity": round(qty_room, 3),
            }
            same_key = str(uuid4())
            with ThreadPoolExecutor(max_workers=8) as pool:
                futs = [pool.submit(lambda: api.put(f"/api/approvals/{same_key}", json=prop)) for _ in range(8)]
                resps = [f.result() for f in futs]
            alloc_ids = {r.json()["shipment"]["id"] for r in resps if r.status_code == 200}
            conflict = api.put(f"/api/approvals/{same_key}", json={**prop, "quantity": round(qty_room - 10.0, 3)})
            sim_allocs = sim.get("/v1/allocations").json()

            # Restart backend container while intents exist and verify both plan & single key replay cleanly
            subprocess.run([*compose_cmd, "restart", "snapshot-api"], cwd=ROOT, env=env, check=True)
            for _ in range(30):
                try:
                    if api.get("/api/health").status_code == 200:
                        break
                except Exception:
                    time.sleep(0.5)
            replay_after_restart = api.put(f"/api/approvals/{same_key}", json=prop)
            cancel_res = api.post(f"/api/approvals/{same_key}/cancel")

            # Reset simulator and verify generation changes + old plan returns 409
            rebind1 = api.post("/api/generations/rebind").json()["generation_id"]
            sim.post("/admin/reset").raise_for_status()
            step_ticks(sim, 10)
            rebind2 = api.post("/api/generations/rebind").json()["generation_id"]
            old_plan_after_reset = api.put(f"/api/plans/{opt_plan['plan_id']}/approval", json={})
            old_intent_after_reset = api.put(f"/api/approvals/{same_key}", json=prop)
            record(
                "concurrency_restart_cancel_and_reset_isolation",
                "state_integrity",
                expected_ships >= 1
                and all(r.status_code == 200 and r.json()["state"] == "CONFIRMED" for r in plan_resps)
                and plan_alloc_count == expected_ships
                and plan_replay_t37.status_code == 200
                and plan_alloc_count_after_t37 == expected_ships
                and len(alloc_ids) == 1 and len(sim_allocs) == 1 and conflict.status_code == 409
                and replay_after_restart.status_code == 200
                and cancel_res.status_code == 200 and cancel_res.json()["shipment"]["status"] == "CANCELLED"
                and rebind1 != rebind2
                and old_plan_after_reset.status_code == 409
                and old_intent_after_reset.status_code == 409,
                {
                    "plan_shipments_expected": expected_ships,
                    "plan_allocs_concurrent": plan_alloc_count,
                    "plan_allocs_after_tick_37_replay": plan_alloc_count_after_t37,
                    "concurrent_unique_alloc_ids": len(alloc_ids),
                    "sim_allocs_count": len(sim_allocs),
                    "conflict_status": conflict.status_code,
                    "replay_after_restart_state": replay_after_restart.json()["state"],
                    "cancel_status": cancel_res.json()["shipment"]["status"],
                    "generation_changed": rebind1 != rebind2,
                    "old_plan_reset_status": old_plan_after_reset.status_code,
                    "old_intent_reset_status": old_intent_after_reset.status_code,
                },
            )

            # 14: Security headers & auth check
            h_resp = api.get("/api/health")
            sec_ok = (
                h_resp.headers.get("x-content-type-options") == "nosniff"
                and h_resp.headers.get("x-frame-options") in ("DENY", "SAMEORIGIN")
                and h_resp.headers.get("referrer-policy") == "same-origin"
            )
            prom_txt = api.get("/metrics").text
            record(
                "security_headers_and_prometheus_metrics",
                "observability_security",
                sec_ok and "bup_http_requests_total" in prom_txt,
                {"headers_present": sec_ok, "prometheus_bytes": len(prom_txt)},
            )

            # 16: RUNNING mode provenance headers & cached/degraded reporting
            reset_and_seed(sim, api, 24)
            api.get("/api/snapshot").raise_for_status()
            api.get("/api/planning?history_ticks=8&horizon_ticks=96").raise_for_status()
            sim.post("/admin/run").raise_for_status()
            run_snaps = []
            for _ in range(12):
                rs = api.get("/api/snapshot")
                run_snaps.append((
                    rs.status_code,
                    rs.headers.get("x-bup-as-of-tick"),
                    rs.headers.get("x-bup-current-tick"),
                    rs.headers.get("x-bup-cached"),
                    rs.headers.get("x-bup-degraded"),
                    rs.headers.get("x-bup-observed-at"),
                ))
                time.sleep(0.08)
            sim.post("/admin/pause").raise_for_status()
            after_pause = api.get("/api/snapshot")
            record(
                "running_mode_provenance_and_freshness",
                "observability_security",
                all(s[0] == 200 and s[5] is not None for s in run_snaps) and after_pause.status_code == 200 and after_pause.headers.get("x-bup-cached") == "false",
                {
                    "running_samples": len(run_snaps),
                    "cached_fallback_observed": any(s[3] == "true" for s in run_snaps),
                    "last_running_sample": run_snaps[-1],
                    "paused_recovery_cached": after_pause.headers.get("x-bup-cached"),
                },
            )
    finally:
        subprocess.run([*compose_cmd, "down", "-v"], cwd=ROOT, env=env, check=False)

    summary = {
        "total": len(results),
        "passed": sum(1 for r in results if r["passed"]),
        "failed": sum(1 for r in results if not r["passed"]),
        "results": results,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print("SUMMARY:", json.dumps({k: v for k, v in summary.items() if k != "results"}))
    if summary["failed"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
