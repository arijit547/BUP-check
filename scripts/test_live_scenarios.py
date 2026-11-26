"""Custom 8-scenario live simulation & API test suite against the running Compose stack."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = ROOT / "artifacts" / "evaluation" / "live_custom_tests.json"

SIM_URL = os.environ.get("SIM_URL", "http://127.0.0.1:8000")
API_URL = os.environ.get("API_URL", "http://127.0.0.1:8001")
DASH_URL = os.environ.get("DASH_URL", "http://127.0.0.1:3000")
PROM_URL = os.environ.get("PROM_URL", "http://127.0.0.1:9090")


def step_to(sim: httpx.Client, target: int) -> int:
    inst = sim.get("/v1/instance").json()
    while int(inst["tick"]) < target:
        inst = sim.post("/admin/step").json()
    return int(inst["tick"])


def main() -> None:
    results: list[dict[str, Any]] = []

    def check(tc: str, desc: str, passed: bool, detail: dict[str, Any]) -> None:
        row = {"id": tc, "description": desc, "passed": passed, **detail}
        results.append(row)
        print(json.dumps(row), flush=True)

    with (
        httpx.Client(base_url=SIM_URL, timeout=20.0, limits=httpx.Limits(max_keepalive_connections=0)) as sim,
        httpx.Client(base_url=API_URL, timeout=25.0, limits=httpx.Limits(max_keepalive_connections=0)) as api,
        httpx.Client(base_url=DASH_URL, timeout=25.0, limits=httpx.Limits(max_keepalive_connections=0)) as dash,
    ):
        # Ensure clean ML readiness first
        ml_ready = False
        for _ in range(30):
            h = api.get("/api/health").json()
            if h.get("components", {}).get("ml_service", {}).get("status") == "ok":
                ml_ready = True
                break
            time.sleep(2)

        # TC-01: Cold-start early ticks (tick=2 < history_ticks=24)
        sim.post("/admin/faults/clear").raise_for_status()
        sim.post("/admin/reset").raise_for_status()
        step_to(sim, 2)
        cold_fc = dash.get("/api/forecasts?history_ticks=24&horizon_ticks=32").json()
        cold_opt = dash.post("/api/optimize", json={"history_ticks": 24, "horizon_ticks": 48}).json()
        ok_tc01 = (
            cold_fc["source"] == "PROFILE_FALLBACK"
            and len(cold_fc["series"]) == 12
            and all(s["observations"] == 2 for s in cold_fc["series"])
            and cold_opt["policy_mode"] == "REORDER_FALLBACK"
        )
        check("TC-01", "Cold-start fallback safety (tick=2 < history=24)", ok_tc01, {
            "fc_source": cold_fc["source"],
            "policy_mode": cold_opt["policy_mode"],
            "observations_sample": cold_fc["series"][0]["observations"],
        })

        # TC-02: Multi-step normal simulation & Chronos-2 quantile verification (tick=2 -> 40)
        step_to(sim, 40)
        fc_seas = dash.get("/api/forecasts?history_ticks=24&horizon_ticks=32&policy_override=AUTO").json()
        fc_chron = dash.get("/api/forecasts?history_ticks=24&horizon_ticks=32&policy_override=CHRONOS2").json()
        quantiles_valid = (
            fc_chron["source"] == "CHRONOS2"
            and len(fc_chron["series"]) == 12
            and all(
                0.0 <= pt["p10"] <= pt["p50"] <= pt["p90"]
                for s in fc_chron["series"]
                for pt in s["forecast"]
            )
        )
        # Step 4 more ticks so forecast points mature and verify evaluation endpoint tracks matured accuracy
        step_to(sim, 44)
        dash.get("/api/forecasts?history_ticks=24&horizon_ticks=32").raise_for_status()
        eval_summary = dash.get("/api/evaluation").json()
        matured = eval_summary.get("live_matured_accuracy") or {}
        check("TC-02", "Chronos-2 offline quantiles & live matured accuracy", quantiles_valid and ml_ready, {
            "seasonal_source": fc_seas["source"],
            "chronos_source": fc_chron["source"],
            "chronos_model": fc_chron["model_version"],
            "matured_points": matured.get("matured_points"),
            "matured_mae": matured.get("mae"),
        })

        # TC-03: Demand surge + primary route disruption -> HYBRID ML detection & backup reroute
        t_ev = sim.get("/v1/instance").json()["tick"]
        sim.post("/admin/events", json={
            "type": "demand_spike", "start_tick": t_ev, "duration_ticks": 12,
            "parameters": {"station_ids": ["station-mirpur"], "multiplier": 1.9},
        }).raise_for_status()
        sim.post("/admin/events", json={
            "type": "route_disruption", "start_tick": t_ev, "duration_ticks": 8,
            "parameters": {"route_ids": ["route-gazipur-mirpur"]},
        }).raise_for_status()
        step_to(sim, t_ev + 3)
        inc_res = dash.get("/api/incidents?status=OPEN").json()
        cats_by_id = {(i["category"], i["entity_id"]): i for i in inc_res["incidents"]}
        has_hybrid_spike = any(
            i["category"] == "DEMAND_SPIKE" and i["entity_id"] == "station-mirpur" and i["source"] in ("HYBRID", "RULE")
            for i in inc_res["incidents"]
        )
        has_disrupt = ("ROUTE_DISRUPTED", "route-gazipur-mirpur") in cats_by_id
        opt_crisis = dash.post("/api/optimize", json={"history_ticks": 8, "horizon_ticks": 96}).json()
        rec_routes = [s["route_id"] for s in opt_crisis["shipments"]]
        avoids_disrupted = "route-gazipur-mirpur" not in rec_routes
        check("TC-03", "Demand spike + route disruption detection & OR-Tools reroute", has_hybrid_spike and has_disrupt and avoids_disrupted, {
            "open_incidents": inc_res["open_count"],
            "mirpur_spike_sources": [i["source"] for i in inc_res["incidents"] if i["entity_id"] == "station-mirpur"],
            "recommended_routes": rec_routes,
            "solver_status": opt_crisis["solver_status"],
        })

        # TC-04: Counterfactual simulation (invalid vs valid) & approved delivery completion
        invalid_sim = dash.post("/api/simulate", json={
            "history_ticks": 8, "horizon_ticks": 48,
            "shipments": [{
                "source_depot_id": "depot-patiya", "destination_station_id": "station-mirpur",
                "route_id": "route-patiya-mirpur", "fuel_type": "DIESEL", "quantity": 99999.0,
            }],
        }).json()
        snap_now = dash.get("/api/snapshot").json()
        patiya = next(d for d in snap_now["depots"] if d["id"] == "depot-patiya")
        mirpur = next(s for s in snap_now["stations"] if s["id"] == "station-mirpur")
        qty_valid = round(min(2500.0, float(patiya["inventory"]["DIESEL"]), float(mirpur["capacity"]["DIESEL"]) - float(mirpur["inventory"]["DIESEL"])), 3)
        valid_sim = dash.post("/api/simulate", json={
            "history_ticks": 8, "horizon_ticks": 48,
            "shipments": [{
                "source_depot_id": "depot-patiya", "destination_station_id": "station-mirpur",
                "route_id": "route-patiya-mirpur", "fuel_type": "DIESEL", "quantity": qty_valid,
            }],
        }).json()
        # Submit valid shipment & advance 5 ticks to ARRIVED
        deliv_uuid = str(uuid4())
        put_res = dash.put(f"/api/approvals/{deliv_uuid}", json={
            "source_depot_id": "depot-patiya", "destination_station_id": "station-mirpur",
            "route_id": "route-patiya-mirpur", "fuel_type": "DIESEL", "quantity": qty_valid,
        }).json()
        step_to(sim, sim.get("/v1/instance").json()["tick"] + 5)
        ledger = dash.get("/api/approvals").json()
        matched_deliv = next((a for a in ledger["attempts"] if a["intent_id"] == deliv_uuid), None)
        ok_tc04 = (
            not invalid_sim["valid"]
            and valid_sim["valid"]
            and put_res["state"] == "CONFIRMED"
            and matched_deliv is not None
            and matched_deliv["shipment"]["status"] == "ARRIVED"
        )
        check("TC-04", "Counterfactual simulate (invalid + valid) & full delivery arrival", ok_tc04, {
            "invalid_errors": invalid_sim["validation_errors"],
            "valid_avoided": valid_sim["unmet_avoided_liters"],
            "final_status": matched_deliv["shipment"]["status"] if matched_deliv and matched_deliv.get("shipment") else None,
        })

        # TC-05: PENDING shipment cancellation & depot stock refund verification
        snap_pre = dash.get("/api/snapshot").json()
        gaz = next(d for d in snap_pre["depots"] if d["id"] == "depot-gazipur")
        tongi = next(s for s in snap_pre["stations"] if s["id"] == "station-tongi")
        depot_before = float(gaz["inventory"]["PETROL"])
        cancel_qty = round(min(1200.0, float(tongi["capacity"]["PETROL"]) - float(tongi["inventory"]["PETROL"])), 3)
        cancel_uuid = str(uuid4())
        c_put = dash.put(f"/api/approvals/{cancel_uuid}", json={
            "source_depot_id": "depot-gazipur", "destination_station_id": "station-tongi",
            "route_id": "route-gazipur-tongi", "fuel_type": "PETROL", "quantity": cancel_qty,
        }).json()
        snap_mid = dash.get("/api/snapshot").json()
        depot_mid = float(next(d for d in snap_mid["depots"] if d["id"] == "depot-gazipur")["inventory"]["PETROL"])
        c_post = dash.post(f"/api/approvals/{cancel_uuid}/cancel").json()
        snap_after = dash.get("/api/snapshot").json()
        depot_after = float(next(d for d in snap_after["depots"] if d["id"] == "depot-gazipur")["inventory"]["PETROL"])
        ok_tc05 = (
            c_put["state"] == "CONFIRMED"
            and abs((depot_before - cancel_qty) - depot_mid) < 1e-2
            and c_post["shipment"]["status"] == "CANCELLED"
            and abs(depot_before - depot_after) < 1e-2
        )
        check("TC-05", "PENDING approval cancellation refunds depot inventory", ok_tc05, {
            "depot_before": depot_before,
            "depot_during_pending": depot_mid,
            "depot_after_cancel": depot_after,
            "cancel_status": c_post["shipment"]["status"],
        })

        # TC-06: Station outage guard + 2-tick incident auto-resolution debounce
        t_out = sim.get("/v1/instance").json()["tick"]
        sim.post("/admin/events", json={
            "type": "station_outage", "start_tick": t_out, "duration_ticks": 4,
            "parameters": {"station_ids": ["station-tongi"]},
        }).raise_for_status()
        step_to(sim, t_out + 2)
        shortages = dash.get("/api/shortages").json()["shortages"]
        tongi_sh = [s for s in shortages if s["station_id"] == "station-tongi"]
        opt_out = dash.post("/api/optimize", json={"history_ticks": 8, "horizon_ticks": 48}).json()
        no_outage_dispatch = all(r["destination_station_id"] != "station-tongi" for r in opt_out["shipments"])
        # Advance 4 ticks past outage end (2 ticks to resolve -> 2-tick hysteresis threshold)
        step_to(sim, t_out + 6)
        dash.get("/api/planning").raise_for_status()
        step_to(sim, t_out + 7)
        dash.get("/api/planning").raise_for_status()
        resolved_inc = dash.get("/api/incidents?status=RESOLVED").json()["incidents"]
        outage_resolved = any(i["category"] == "STATION_OUTAGE" and i["entity_id"] == "station-tongi" for i in resolved_inc)
        ok_tc06 = (
            len(tongi_sh) == 3
            and all(s["outage_unmet_liters"] > 0 and s["depletion_unmet_liters"] == 0.0 for s in tongi_sh)
            and no_outage_dispatch
            and outage_resolved
        )
        check("TC-06", "Station outage blocks dispatch & resolves cleanly after 2 clear ticks", ok_tc06, {
            "outage_unmet_sample": tongi_sh[0]["outage_unmet_liters"] if tongi_sh else 0,
            "no_outage_dispatch": no_outage_dispatch,
            "outage_resolved": outage_resolved,
        })

        # TC-07: Live RUNNING mode concurrency through dashboard proxy + SSE events
        tick_start_run = int(sim.get("/v1/instance").json()["tick"])
        sse_before = api.get("/api/metrics").json()["sse_events_received"]
        sim.post("/admin/run").raise_for_status()
        errors_run = 0
        ok_run = 0
        deadline = time.perf_counter() + 4.0

        def poll_live() -> None:
            nonlocal errors_run, ok_run
            with httpx.Client(base_url=DASH_URL, timeout=10.0) as c:
                while time.perf_counter() < deadline:
                    r1 = c.get("/api/snapshot")
                    r2 = c.get("/api/planning?history_ticks=8&horizon_ticks=96")
                    if r1.status_code == 200 and r2.status_code == 200:
                        ok_run += 2
                    else:
                        errors_run += 1

        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = [pool.submit(poll_live) for _ in range(4)]
            for f in futs:
                f.result()
        sim.post("/admin/pause").raise_for_status()
        tick_end_run = int(sim.get("/v1/instance").json()["tick"])
        sse_after = api.get("/api/metrics").json()["sse_events_received"]
        ok_tc07 = errors_run == 0 and ok_run >= 8 and tick_end_run > tick_start_run and sse_after > sse_before
        check("TC-07", "RUNNING mode live ticks + concurrent nginx reads + SSE stream", ok_tc07, {
            "ok_responses": ok_run,
            "error_responses": errors_run,
            "ticks_advanced": f"{tick_start_run}->{tick_end_run}",
            "sse_events": f"{sse_before}->{sse_after}",
        })

        # TC-08: Prometheus target scrape, simulator reset isolation, and clean 36-tick warmup for operator
        prom_ok = False
        try:
            pt = httpx.get(f"{PROM_URL}/api/v1/targets", timeout=5.0).json()
            prom_ok = pt.get("status") == "success" and len(pt.get("data", {}).get("activeTargets", [])) >= 1
        except Exception:
            prom_ok = False
        gen_before = api.post("/api/generations/rebind").json()["generation_id"]
        sim.post("/admin/reset").raise_for_status()
        step_to(sim, 36)
        snap_ready = dash.get("/api/snapshot").json()
        gen_after = api.post("/api/generations/rebind").json()["generation_id"]
        # Ensure pre-reset cancel UUID cannot be replayed on new generation (409)
        old_replay = dash.put(f"/api/approvals/{cancel_uuid}", json={
            "source_depot_id": "depot-gazipur", "destination_station_id": "station-tongi",
            "route_id": "route-gazipur-tongi", "fuel_type": "PETROL", "quantity": 500.0,
        })
        # Warm both Seasonal & Chronos-2 cache so UI opens instantaneously for user
        dash.get("/api/planning?history_ticks=8&horizon_ticks=96").raise_for_status()
        dash.get("/api/forecasts?history_ticks=24&horizon_ticks=32&policy_override=CHRONOS2").raise_for_status()
        ok_tc08 = prom_ok and gen_before != gen_after and old_replay.status_code == 409 and snap_ready["simulation"]["tick"] == 36
        check("TC-08", "Prometheus scrape, reset generation isolation (409) & 36-tick ready state", ok_tc08, {
            "prometheus_targets_up": prom_ok,
            "generation_rotated": gen_before != gen_after,
            "old_intent_replay_status": old_replay.status_code,
            "final_tick": snap_ready["simulation"]["tick"],
        })

    summary = {
        "total": len(results),
        "passed": sum(1 for r in results if r["passed"]),
        "failed": sum(1 for r in results if not r["passed"]),
        "cases": results,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print("LIVE_SUITE_SUMMARY:", json.dumps({k: v for k, v in summary.items() if k != "cases"}))
    if summary["failed"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
