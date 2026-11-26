import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import random
import sqlite3
import sys
import tempfile
import unittest
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import main as app_main  # noqa: E402
from intelligence.collector import hour_factor, regularized_series  # noqa: E402
from intelligence.contracts import (  # noqa: E402
    Allocation,
    Anomaly,
    DemandObservation,
    Region,
    SimulatorEvent,
    StationDetail,
    SupplyArrival,
)
from intelligence.features import build_causal_features, spike_multiplier_at  # noqa: E402
from intelligence.optimizer import optimize_allocations, validate_plan_independently  # noqa: E402
from intelligence.shortage import compute_safety_stock_target, project, project_detailed  # noqa: E402
from planning import build_report  # noqa: E402
from schemas import Depot, FuelVolumes, Route, Simulation, Snapshot, Station  # noqa: E402
from security import create_pbkdf2_hash, verify_pbkdf2_password  # noqa: E402
from storage import PlatformStore  # noqa: E402


def make_world(
    *,
    tick: int = 24,
    mirpur_diesel: float = 1200.0,
    station_status: str = "OPEN",
    route_status: str = "AVAILABLE",
    depot_status: str = "OPEN",
    depot_diesel: float = 50000.0,
) -> Snapshot:
    return Snapshot(
        simulation=Simulation(
            id=1,
            scenario_id="baseline",
            scenario_version="1.0",
            seed=12345,
            tick=tick,
            tick_minutes=15,
            sim_time=datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
            status="PAUSED",
        ),
        depots=[
            Depot(
                id="depot-gazipur",
                name="Gazipur Depot",
                region_id="region-dhaka",
                status=depot_status,
                dispatch_capacity_per_tick=12000.0,
                inventory=FuelVolumes(DIESEL=depot_diesel, PETROL=45000.0, OCTANE=26000.0),
                capacity=FuelVolumes(DIESEL=90000.0, PETROL=70000.0, OCTANE=45000.0),
            )
        ],
        stations=[
            Station(
                id="station-mirpur",
                name="Mirpur Fuel Station",
                region_id="region-dhaka",
                status=station_status,
                inventory=FuelVolumes(DIESEL=mirpur_diesel, PETROL=9000.0, OCTANE=5000.0),
                capacity=FuelVolumes(DIESEL=15000.0, PETROL=14000.0, OCTANE=9000.0),
            ),
            Station(
                id="station-tongi",
                name="Tongi Industrial Station",
                region_id="region-dhaka",
                status="OPEN",
                inventory=FuelVolumes(DIESEL=11000.0, PETROL=6000.0, OCTANE=3500.0),
                capacity=FuelVolumes(DIESEL=18000.0, PETROL=9000.0, OCTANE=6000.0),
            ),
        ],
        routes=[
            Route(
                id="route-gazipur-mirpur",
                source_depot_id="depot-gazipur",
                destination_station_id="station-mirpur",
                status=route_status,
                transit_ticks=2,
                max_shipment=7000.0,
            ),
            Route(
                id="route-gazipur-tongi",
                source_depot_id="depot-gazipur",
                destination_station_id="station-tongi",
                status="AVAILABLE",
                transit_ticks=2,
                max_shipment=6500.0,
            ),
        ],
    )


class CollectorFeatureShortageTests(unittest.TestCase):
    def test_persistent_store_dedup_gap_and_future_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PlatformStore(str(Path(tmp) / "test.sqlite3"))
            sim_t = datetime(2026, 1, 1, 2, 0, tzinfo=UTC)
            gen = store.create_generation(
                sim_instance_id=1, scenario_id="baseline", scenario_version="1.0",
                seed=12345, tick=10, sim_time=sim_t,
            )
            obs = [
                DemandObservation(station_id="station-mirpur", fuel_type="DIESEL", tick=7, demand_liters=90.0),
                DemandObservation(station_id="station-mirpur", fuel_type="DIESEL", tick=7, demand_liters=90.0),
                DemandObservation(station_id="station-mirpur", fuel_type="DIESEL", tick=9, demand_liters=95.0),
                DemandObservation(station_id="station-mirpur", fuel_type="DIESEL", tick=12, demand_liters=999.0),
            ]
            dq = store.upsert_observations(gen.generation_id, obs, {"station-mirpur": "region-dhaka"}, 15, 10, sim_t)
            self.assertEqual(dq.collected_rows, 2)
            self.assertEqual(dq.duplicate_rows_ignored, 1)
            self.assertEqual(dq.future_ticks_rejected, 1)
            loaded = store.load_observations(gen.generation_id)
            self.assertEqual([o.tick for o in loaded], [7, 9])

            reg, gaps = regularized_series(
                {o.tick: o for o in loaded},
                start_tick=7, end_tick_exclusive=10, current_tick=10, tick_minutes=15, current_sim_time=sim_t,
            )
            self.assertEqual(gaps, 1)
            self.assertIsNone(reg[1]["demand_liters"])
            self.assertTrue(reg[1]["missing"])

    def test_causal_features_no_double_spike_normalization(self) -> None:
        snap = make_world(tick=20)
        detail = StationDetail(id="station-mirpur", region_id="region-dhaka", demand_profile="urban_high", demand_multiplier=1.8)
        events = [
            SimulatorEvent(id=1, type="demand_spike", start_tick=16, end_tick=28, status="ACTIVE",
                           parameters={"station_ids": ["station-mirpur"], "multiplier": 1.8}),
        ]
        base_samples = []
        for t in range(12, 20):
            dt = snap.simulation.sim_time - timedelta(minutes=(20 - t) * 15)
            hf = hour_factor("urban_high", dt.hour)
            mult = 1.8 if t >= 16 else 1.0
            base_samples.append((t, DemandObservation(station_id="station-mirpur", fuel_type="DIESEL", tick=t, demand_liters=100.0 * hf * mult)))

        feat = build_causal_features(
            snapshot=snap, station_id="station-mirpur", fuel="DIESEL",
            station_detail=detail, region_factor=1.0, samples=base_samples,
            allocations=[], events=events, horizon_ticks=8,
        )
        self.assertAlmostEqual(feat["normalized_base_rate"], 100.0, places=1)
        self.assertEqual(len(feat["future_covariates"]), 8)
        self.assertAlmostEqual(feat["future_covariates"][0]["known_spike_multiplier"], 1.8, places=2)

    def test_outage_vs_depletion_and_conservation(self) -> None:
        det_out = project_detailed(5000.0, 15000.0, [100.0] * 10, [0.0] * 10, station_open=False)
        self.assertEqual(det_out["outage_unmet_liters"], 1000.0)
        self.assertEqual(det_out["depletion_unmet_liters"], 0.0)

        det_dep = project_detailed(350.0, 15000.0, [100.0] * 10, [0.0, 0.0, 500.0] + [0.0] * 7, station_open=True)
        self.assertEqual(det_dep["depletion_unmet_liters"], 150.0)
        self.assertEqual(det_dep["outage_unmet_liters"], 0.0)
        det_cov = project_detailed(350.0, 15000.0, [100.0] * 10, [0.0, 0.0, 700.0] + [0.0] * 7, station_open=True)
        self.assertEqual(det_cov["depletion_unmet_liters"], 0.0)


class OptimizerInvariantAndPropertyTests(unittest.TestCase):
    def test_seeded_randomized_feasible_plans_satisfy_all_constraints(self) -> None:
        rng = random.Random(2026)
        for trial in range(15):
            snap = make_world(
                tick=40 + trial,
                mirpur_diesel=float(rng.randint(200, 14000)),
                depot_diesel=float(rng.randint(1000, 50000)),
                route_status="DISRUPTED" if trial % 5 == 0 else "AVAILABLE",
                station_status="OUTAGE" if trial % 7 == 0 else "OPEN",
            )
            history = []
            for t in range(32 + trial, 40 + trial):
                for sid in ("station-mirpur", "station-tongi"):
                    for fuel in ("DIESEL", "PETROL", "OCTANE"):
                        history.append(DemandObservation(
                            station_id=sid, fuel_type=fuel, tick=t,
                            demand_liters=float(rng.randint(40, 220)),
                        ))
            details = [
                StationDetail(id="station-mirpur", region_id="region-dhaka", demand_profile="urban_high", demand_multiplier=1.0),
                StationDetail(id="station-tongi", region_id="region-dhaka", demand_profile="industrial", demand_multiplier=1.0),
            ]
            report = build_report(snap, history, [], history_ticks=8, horizon_ticks=48, station_details=details)
            routes_map = {r.id: r for r in snap.routes}
            chosen = [(routes_map[r.route_id], r.fuel_type, r.quantity_liters) for r in report.recommendations]
            incoming = {(s.id, f): [0.0] * 48 for s in snap.stations for f in ("DIESEL", "PETROL", "OCTANE")}
            dem = {(s.id, f): [100.0] * 48 for s in snap.stations for f in ("DIESEL", "PETROL", "OCTANE")}
            ok, errs = validate_plan_independently(
                shipments=chosen, snapshot=snap, dispatch_used={}, incoming=incoming, demand_series_by_key=dem,
            )
            self.assertTrue(ok, f"Trial {trial} violated constraints: {errs}")

    def test_single_action_limit_honored(self) -> None:
        snap = make_world(tick=20, mirpur_diesel=1000.0)
        history = []
        for t in range(12, 20):
            for sid in ("station-mirpur", "station-tongi"):
                for fuel in ("DIESEL", "PETROL", "OCTANE"):
                    history.append(DemandObservation(station_id=sid, fuel_type=fuel, tick=t, demand_liters=150.0))
        report = build_report(snap, history, [], history_ticks=8, horizon_ticks=48, single_action_limit=1)
        self.assertLessEqual(len(report.recommendations), 1)
        budget_report = build_report(snap, history, [], history_ticks=8, horizon_ticks=48, max_total_liters=3500.0)
        self.assertLessEqual(sum(r.quantity_liters for r in budget_report.recommendations), 3500.0 + 1e-6)


class ResilienceGenerationAuthAndApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["DISABLE_SSE_WATCHER"] = "1"
        os.environ["APPROVAL_DB"] = str(Path(self.tmpdir.name) / "test.sqlite3")
        os.environ["ML_SERVICE_URL"] = ""
        os.environ["OPERATOR_AUTH_MODE"] = "local_open"
        os.environ["OPERATOR_PASSWORD_HASH"] = ""
        os.environ["OPERATOR_TOKEN_SHA256"] = ""
        self.tick = 20
        self.allocations: list[dict] = []
        self.fail_post_timeout = False
        self.ml_down = False

    def tearDown(self) -> None:
        self.tmpdir.cleanup()
        os.environ["OPERATOR_AUTH_MODE"] = "local_open"
        os.environ["OPERATOR_PASSWORD_HASH"] = ""

    def _sim_transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/v1/health":
                return httpx.Response(200, json={"status": "ok", "database": "ok", "simulation": {"status": "PAUSED", "tick": self.tick}})
            if path == "/v1/instance":
                return httpx.Response(200, json={
                    "id": 1, "scenario_id": "baseline", "scenario_version": "1.0", "seed": 12345,
                    "sim_time": (datetime(2026, 1, 1, 0, 0, tzinfo=UTC) + timedelta(minutes=15 * self.tick)).isoformat(),
                    "tick": self.tick, "tick_minutes": 15, "status": "PAUSED",
                })
            if path == "/v1/depots":
                return httpx.Response(200, json=[{
                    "id": "depot-gazipur", "name": "Gazipur Depot", "region_id": "region-dhaka", "status": "OPEN",
                    "dispatch_capacity_per_tick": 12000,
                    "capacity": {"DIESEL": 90000, "PETROL": 70000, "OCTANE": 45000},
                    "inventory": {"DIESEL": 60000, "PETROL": 45000, "OCTANE": 26000},
                }])
            if path == "/v1/stations":
                return httpx.Response(200, json=[{
                    "id": "station-mirpur", "name": "Mirpur Fuel Station", "region_id": "region-dhaka", "status": "OPEN",
                    "demand_profile": "urban_high", "demand_multiplier": 1.0,
                    "capacity": {"DIESEL": 15000, "PETROL": 14000, "OCTANE": 9000},
                    "inventory": {"DIESEL": 1200, "PETROL": 9000, "OCTANE": 5000},
                }])
            if path == "/v1/routes":
                return httpx.Response(200, json=[{
                    "id": "route-gazipur-mirpur", "source_depot_id": "depot-gazipur",
                    "destination_station_id": "station-mirpur", "transit_ticks": 2,
                    "max_shipment": 7000, "status": "AVAILABLE",
                }])
            if path == "/v1/regions":
                return httpx.Response(200, json=[{"id": "region-dhaka", "name": "Dhaka", "demand_factor": 1.0}])
            if path == "/v1/supply-arrivals":
                return httpx.Response(200, json=[])
            if path == "/v1/events":
                return httpx.Response(200, json=[])
            if path == "/v1/metrics":
                return httpx.Response(200, json={
                    "served_demand_liters": 5000.0, "unmet_demand_liters": 0.0,
                    "service_level": 1.0, "allocation_liters": 0.0, "allocation_failures": 0,
                })
            if path == "/v1/demand-history":
                rows = []
                for t in range(max(0, self.tick - 12), self.tick):
                    for fuel in ("DIESEL", "PETROL", "OCTANE"):
                        rows.append({
                            "id": t * 10, "station_id": "station-mirpur", "fuel_type": fuel,
                            "tick": t, "sim_time": (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=15 * t)).isoformat(),
                            "demand_liters": 110.0, "served_liters": 110.0, "unmet_liters": 0.0,
                        })
                return httpx.Response(200, json=rows)
            if path == "/v1/allocations" and request.method == "GET":
                return httpx.Response(200, json=self.allocations)
            if path == "/v1/allocations" and request.method == "POST":
                body = json.loads(request.content)
                for row in self.allocations:
                    if row["idempotency_key"] == body["idempotency_key"]:
                        return httpx.Response(201, json=row)
                created = {
                    "id": len(self.allocations) + 1, "idempotency_key": body["idempotency_key"],
                    "source_depot_id": body["source_depot_id"], "destination_station_id": body["destination_station_id"],
                    "route_id": body["route_id"], "fuel_type": body["fuel_type"], "quantity": body["quantity"],
                    "created_tick": self.tick, "departure_tick": None, "expected_arrival_tick": None,
                    "actual_arrival_tick": None, "status": "PENDING", "failure_reason": None,
                }
                self.allocations.append(created)
                if self.fail_post_timeout:
                    # Simulate dropped HTTP response after official simulator accepted the shipment!
                    raise httpx.ReadTimeout("Simulated response drop after simulator commit")
                return httpx.Response(201, json=created)
            if path.endswith("/cancel") and request.method == "POST":
                aid = int(path.split("/")[-2])
                for row in self.allocations:
                    if row["id"] == aid:
                        row["status"] = "CANCELLED"
                        return httpx.Response(200, json=row)
                return httpx.Response(404, json={"detail": {"code": "ALLOCATION_NOT_FOUND", "message": "Unknown"}})
            return httpx.Response(404)

        return httpx.MockTransport(handler)

    def test_timeout_after_commit_recovery_cancel_reset_and_new_endpoints(self) -> None:
        with TestClient(app_main.app) as client:
            app_main.app.state.simulator = httpx.AsyncClient(base_url="http://mock", transport=self._sim_transport())
            app_main.app.state.generation = None

            # Verify new APIs work cleanly
            self.assertEqual(client.get("/api/forecasts").status_code, 200)
            self.assertEqual(client.get("/api/shortages").status_code, 200)
            self.assertEqual(client.get("/api/anomalies").status_code, 200)
            self.assertEqual(client.get("/api/models").status_code, 200)
            self.assertEqual(client.get("/api/evaluation").status_code, 200)
            self.assertEqual(client.get("/metrics").status_code, 200)

            opt = client.post("/api/optimize", json={"history_ticks": 8, "horizon_ticks": 48})
            self.assertEqual(opt.status_code, 200)
            plan = opt.json()
            self.assertIn(plan["solver_status"], ("OPTIMAL", "FEASIBLE"))

            sim_res = client.post("/api/simulate", json={
                "history_ticks": 8, "horizon_ticks": 48,
                "shipments": [{
                    "source_depot_id": "depot-gazipur", "destination_station_id": "station-mirpur",
                    "route_id": "route-gazipur-mirpur", "fuel_type": "DIESEL", "quantity": 3000,
                }],
            })
            self.assertEqual(sim_res.status_code, 200)
            self.assertTrue(sim_res.json()["valid"])

            # Timeout AFTER commit recovers exact simulator allocation ID without duplicating
            self.fail_post_timeout = True
            intent = str(uuid4())
            proposal = {
                "source_depot_id": "depot-gazipur", "destination_station_id": "station-mirpur",
                "route_id": "route-gazipur-mirpur", "fuel_type": "DIESEL", "quantity": 2500,
            }
            rec = client.put(f"/api/approvals/{intent}", json=proposal)
            self.assertEqual(rec.status_code, 200)
            self.assertEqual(rec.json()["state"], "CONFIRMED")
            self.assertEqual(len(self.allocations), 1)
            self.fail_post_timeout = False

            # Explicit operator cancellation of PENDING shipment
            canceled = client.post(f"/api/approvals/{intent}/cancel")
            self.assertEqual(canceled.status_code, 200)
            self.assertEqual(canceled.json()["shipment"]["status"], "CANCELLED")

            # Simulate reset -> tick regression creates new generation and isolates prior pending retries
            gen_before = app_main.app.state.generation.generation_id
            old_intent = str(uuid4())
            with app_main.app.state.store._connect() as conn, conn:
                conn.execute(
                    "INSERT INTO approvals (intent_id, payload, state, generation_id) VALUES (?, ?, 'UNKNOWN', ?)",
                    (old_intent, json.dumps({**proposal, "idempotency_key": f"bup-{old_intent}"}), gen_before),
                )
            self.tick = 0
            self.allocations.clear()
            app_main.clear_planning_cache(app_main.app)
            self.assertEqual(client.get("/api/snapshot").status_code, 200)
            gen_after = app_main.app.state.generation.generation_id
            self.assertNotEqual(gen_before, gen_after)

            blocked = client.put(f"/api/approvals/{old_intent}", json=proposal)
            self.assertEqual(blocked.status_code, 409)

    def test_incident_debounce_and_operator_auth_enforcement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PlatformStore(str(Path(tmp) / "inc.sqlite3"))
            gen = store.create_generation(
                sim_instance_id=1, scenario_id="baseline", scenario_version="1.0",
                seed=1, tick=10, sim_time=datetime(2026, 1, 1, tzinfo=UTC),
            )
            a = Anomaly(
                id="route-1", dedup_key="route-1", category="ROUTE_DISRUPTED",
                severity="CRITICAL", entity_type="route", entity_id="route-gazipur-mirpur",
                summary="Disrupted",
            )
            store.sync_incidents(gen.generation_id, 10, [a])
            # 1 clear tick is debounced -> still OPEN
            store.sync_incidents(gen.generation_id, 11, [])
            items1, open1, res1 = store.list_incidents(gen.generation_id)
            self.assertEqual((open1, res1), (1, 0))
            # 2nd clear tick -> RESOLVED
            store.sync_incidents(gen.generation_id, 12, [])
            items2, open2, res2 = store.list_incidents(gen.generation_id)
            self.assertEqual((open2, res2), (0, 1))

        # Auth check
        pw_hash = create_pbkdf2_hash("secret-test-pass", iterations=50000)
        self.assertTrue(verify_pbkdf2_password("secret-test-pass", pw_hash))
        self.assertFalse(verify_pbkdf2_password("wrong", pw_hash))

        os.environ["OPERATOR_AUTH_MODE"] = "required"
        os.environ["OPERATOR_PASSWORD_HASH"] = pw_hash
        with TestClient(app_main.app) as client:
            app_main.app.state.simulator = httpx.AsyncClient(base_url="http://mock", transport=self._sim_transport())
            proposal = {
                "source_depot_id": "depot-gazipur", "destination_station_id": "station-mirpur",
                "route_id": "route-gazipur-mirpur", "fuel_type": "DIESEL", "quantity": 1000,
            }
            unauth = client.put(f"/api/approvals/{uuid4()}", json=proposal)
            self.assertEqual(unauth.status_code, 401)
            bad_login = client.post("/api/auth/login", json={"username": "operator", "password": "wrong"})
            self.assertEqual(bad_login.status_code, 401)
            good_login = client.post("/api/auth/login", json={"username": "operator", "password": "secret-test-pass"})
            self.assertEqual(good_login.status_code, 200)
            ok_write = client.put(f"/api/approvals/{uuid4()}", json=proposal)
            self.assertEqual(ok_write.status_code, 200)


class JointPlanApprovalAndFreshnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "plan_test.sqlite3")
        os.environ["DISABLE_SSE_WATCHER"] = "1"
        os.environ["APPROVAL_DB"] = self.db_path
        os.environ["ML_SERVICE_URL"] = ""
        os.environ["OPERATOR_AUTH_MODE"] = "local_open"
        os.environ["OPERATOR_PASSWORD_HASH"] = ""
        os.environ["OPERATOR_TOKEN_SHA256"] = ""
        self.tick = 36
        self.sim_status = "PAUSED"
        self.instance_calls = 0
        self.stale_fault = False
        self.disrupt_routes: set[str] = set()
        self.allocations: list[dict] = []
        self.fail_before_commit_count = 0
        self.fail_after_commit_count = 0
        self.fail_alloc_get_once = False

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _sim_transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            headers = {"X-Simulator-Stale": "true"} if (self.stale_fault and path != "/v1/health") else {}
            if path == "/v1/health":
                return httpx.Response(200, json={"status": "ok", "database": "ok", "simulation": {"status": self.sim_status, "tick": self.tick}})
            if path == "/v1/instance":
                self.instance_calls += 1
                tick_val = self.tick + (self.instance_calls % 2) if self.sim_status == "RUNNING" else self.tick
                return httpx.Response(
                    200,
                    headers=headers,
                    json={
                        "id": 1, "scenario_id": "baseline", "scenario_version": "1.0", "seed": 12345,
                        "sim_time": (datetime(2026, 1, 1, 0, 0, tzinfo=UTC) + timedelta(minutes=15 * tick_val)).isoformat(),
                        "tick": tick_val, "tick_minutes": 15, "status": self.sim_status,
                    },
                )
            if path == "/v1/depots":
                return httpx.Response(200, headers=headers, json=[
                    {
                        "id": "depot-gazipur", "name": "Gazipur Depot", "region_id": "region-dhaka", "status": "OPEN",
                        "dispatch_capacity_per_tick": 12000,
                        "capacity": {"DIESEL": 90000, "PETROL": 70000, "OCTANE": 45000},
                        "inventory": {"DIESEL": 60000, "PETROL": 45000, "OCTANE": 26000},
                    },
                    {
                        "id": "depot-patiya", "name": "Patiya Depot", "region_id": "region-chattogram", "status": "OPEN",
                        "dispatch_capacity_per_tick": 11000,
                        "capacity": {"DIESEL": 85000, "PETROL": 65000, "OCTANE": 40000},
                        "inventory": {"DIESEL": 55000, "PETROL": 42000, "OCTANE": 24000},
                    },
                ])
            if path == "/v1/stations":
                return httpx.Response(200, headers=headers, json=[
                    {
                        "id": "station-mirpur", "name": "Mirpur Fuel Station", "region_id": "region-dhaka", "status": "OPEN",
                        "demand_profile": "urban_high", "demand_multiplier": 1.0,
                        "capacity": {"DIESEL": 15000, "PETROL": 14000, "OCTANE": 9000},
                        "inventory": {"DIESEL": 1200, "PETROL": 1500, "OCTANE": 900},
                    },
                    {
                        "id": "station-tongi", "name": "Tongi Industrial Station", "region_id": "region-dhaka", "status": "OPEN",
                        "demand_profile": "industrial", "demand_multiplier": 1.0,
                        "capacity": {"DIESEL": 18000, "PETROL": 9000, "OCTANE": 6000},
                        "inventory": {"DIESEL": 1400, "PETROL": 1200, "OCTANE": 700},
                    },
                    {
                        "id": "station-karnaphuli", "name": "Karnaphuli Highway Station", "region_id": "region-chattogram", "status": "OPEN",
                        "demand_profile": "highway", "demand_multiplier": 1.0,
                        "capacity": {"DIESEL": 14000, "PETROL": 15000, "OCTANE": 9000},
                        "inventory": {"DIESEL": 1300, "PETROL": 1400, "OCTANE": 900},
                    },
                ])
            if path == "/v1/routes":
                routes = [
                    ("route-gazipur-mirpur", "depot-gazipur", "station-mirpur", 2, 7000),
                    ("route-gazipur-tongi", "depot-gazipur", "station-tongi", 2, 6500),
                    ("route-patiya-karnaphuli", "depot-patiya", "station-karnaphuli", 2, 7000),
                ]
                return httpx.Response(200, headers=headers, json=[
                    {
                        "id": rid, "source_depot_id": d, "destination_station_id": s,
                        "transit_ticks": tt, "max_shipment": ms,
                        "status": "DISRUPTED" if rid in self.disrupt_routes else "AVAILABLE",
                    }
                    for rid, d, s, tt, ms in routes
                ])
            if path == "/v1/regions":
                return httpx.Response(200, headers=headers, json=[
                    {"id": "region-dhaka", "name": "Dhaka", "demand_factor": 1.0},
                    {"id": "region-chattogram", "name": "Chattogram", "demand_factor": 1.08},
                ])
            if path == "/v1/supply-arrivals":
                return httpx.Response(200, headers=headers, json=[])
            if path == "/v1/events":
                return httpx.Response(200, headers=headers, json=[])
            if path == "/v1/metrics":
                return httpx.Response(200, headers=headers, json={
                    "served_demand_liters": 9000.0, "unmet_demand_liters": 0.0,
                    "service_level": 1.0, "allocation_liters": 0.0, "allocation_failures": 0,
                })
            if path == "/v1/demand-history":
                rows = []
                for t in range(max(0, self.tick - 12), self.tick):
                    for sid in ("station-mirpur", "station-tongi", "station-karnaphuli"):
                        for fuel in ("DIESEL", "PETROL", "OCTANE"):
                            rows.append({
                                "id": t * 100, "station_id": sid, "fuel_type": fuel,
                                "tick": t, "sim_time": (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=15 * t)).isoformat(),
                                "demand_liters": 120.0, "served_liters": 120.0, "unmet_liters": 0.0,
                            })
                return httpx.Response(200, headers=headers, json=rows)
            if path == "/v1/allocations" and request.method == "GET":
                if self.fail_alloc_get_once:
                    self.fail_alloc_get_once = False
                    return httpx.Response(503, json={"error": {"code": "FAULT_INJECTED", "message": "Transient"}})
                return httpx.Response(200, headers=headers, json=self.allocations)
            if path == "/v1/allocations" and request.method == "POST":
                if self.fail_before_commit_count > 0:
                    self.fail_before_commit_count -= 1
                    raise httpx.ReadTimeout("Timeout before simulator commit")
                body = json.loads(request.content)
                for row in self.allocations:
                    if row["idempotency_key"] == body["idempotency_key"]:
                        return httpx.Response(200, json=row)
                created = {
                    "id": len(self.allocations) + 1, "idempotency_key": body["idempotency_key"],
                    "source_depot_id": body["source_depot_id"], "destination_station_id": body["destination_station_id"],
                    "route_id": body["route_id"], "fuel_type": body["fuel_type"], "quantity": body["quantity"],
                    "created_tick": self.tick, "departure_tick": None, "expected_arrival_tick": None,
                    "actual_arrival_tick": None, "status": "PENDING", "failure_reason": None,
                }
                self.allocations.append(created)
                if self.fail_after_commit_count > 0:
                    self.fail_after_commit_count -= 1
                    self.fail_alloc_get_once = True
                    raise httpx.ReadTimeout("Timeout after simulator commit")
                return httpx.Response(201, json=created)
            if path.endswith("/cancel") and request.method == "POST":
                aid = int(path.split("/")[-2])
                for row in self.allocations:
                    if row["id"] == aid:
                        row["status"] = "CANCELLED"
                        return httpx.Response(200, json=row)
                return httpx.Response(404)
            return httpx.Response(404)

        return httpx.MockTransport(handler)

    def test_batch_replay_after_tick_restart_retention_and_simultaneous_submits(self) -> None:
        with TestClient(app_main.app) as client:
            app_main.app.state.simulator = httpx.AsyncClient(base_url="http://mock", transport=self._sim_transport())
            app_main.app.state.generation = None

            opt = client.post("/api/optimize", json={"history_ticks": 8, "horizon_ticks": 48})
            self.assertEqual(opt.status_code, 200)
            plan = opt.json()
            n_ships = len(plan["shipments"])
            self.assertGreaterEqual(n_ships, 3)

            # Reject tampered plan_id + uuid4 on legacy endpoint
            first_ship = plan["shipments"][0]
            bad_alt = client.put(
                f"/api/approvals/{uuid4()}?plan_id={plan['plan_id']}",
                json={
                    "source_depot_id": first_ship["source_depot_id"],
                    "destination_station_id": first_ship["destination_station_id"],
                    "route_id": first_ship["route_id"],
                    "fuel_type": first_ship["fuel_type"],
                    "quantity": first_ship["quantity_liters"] + 50.0,
                },
            )
            self.assertEqual(bad_alt.status_code, 409)

            # Concurrent simultaneous submits on the exact same plan_id produce strictly n_ships allocations
            async def run_concurrent():
                transport = httpx.ASGITransport(app=app_main.app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                    return await asyncio.gather(*(ac.put(f"/api/plans/{plan['plan_id']}/approval", json={}) for _ in range(5)))

            resps = asyncio.run(run_concurrent())
            for r in resps:
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["state"], "CONFIRMED")
                self.assertEqual(r.json()["confirmed_count"], n_ships)
            self.assertEqual(len(self.allocations), n_ships)
            first_ids = [item["shipment"]["id"] for item in resps[0].json()["items"]]

            # Step tick (36 -> 37), mark allocations IN_TRANSIT, replay same plan_id -> zero duplicates!
            self.tick = 37
            for a in self.allocations:
                a["status"] = "IN_TRANSIT"
                a["departure_tick"] = 37
                a["expected_arrival_tick"] = 39
            replay = client.put(f"/api/plans/{plan['plan_id']}/approval", json={})
            self.assertEqual(replay.status_code, 200)
            rjson = replay.json()
            self.assertEqual(rjson["state"], "CONFIRMED")
            self.assertEqual([item["shipment"]["id"] for item in rjson["items"]], first_ids)
            self.assertEqual([item["shipment"]["status"] for item in rjson["items"]], ["IN_TRANSIT"] * n_ships)
            self.assertEqual(len(self.allocations), n_ships)

            # Retention cleanup of > 105 subsequent plans never deletes an approved plan
            store = app_main.app.state.store
            for i in range(105):
                extra = dict(plan)
                extra["plan_id"] = f"plan-extra-{i}"
                store.record_plan(app_main.PlanPreviewSummary.model_validate(extra))
            self.assertIsNotNone(store.get_plan(plan["plan_id"]))

        # Simulate backend restart with fresh TestClient on the same SQLite DB
        with TestClient(app_main.app) as client2:
            app_main.app.state.simulator = httpx.AsyncClient(base_url="http://mock", transport=self._sim_transport())
            latest = client2.get("/api/plans/latest")
            self.assertEqual(latest.status_code, 200)
            self.assertEqual(latest.json()["plan"]["plan_id"], plan["plan_id"])
            self.assertEqual(latest.json()["approval"]["confirmed_count"], n_ships)
            after_restart = client2.put(f"/api/plans/{plan['plan_id']}/approval", json={})
            self.assertEqual(after_restart.status_code, 200)
            self.assertEqual(len(self.allocations), n_ships)

    def test_timeout_before_and_after_commit_partial_route_change_and_reset_block(self) -> None:
        with TestClient(app_main.app) as client:
            app_main.app.state.simulator = httpx.AsyncClient(base_url="http://mock", transport=self._sim_transport())
            app_main.app.state.generation = None

            plan = client.post("/api/optimize", json={"history_ticks": 8, "horizon_ticks": 48}).json()
            n_ships = len(plan["shipments"])
            self.assertGreaterEqual(n_ships, 3)

            # 1) Fail BEFORE commit on the very first attempt x3 -> shipment 0 UNKNOWN, remaining UNSENT (0 allocs in sim)
            self.fail_before_commit_count = 3
            res1 = client.put(f"/api/plans/{plan['plan_id']}/approval", json={})
            self.assertEqual(res1.status_code, 200)
            b1 = res1.json()
            self.assertEqual(b1["state"], "UNKNOWN")
            self.assertTrue(b1["reconcile_required"])
            self.assertEqual(b1["unknown_count"], 1)
            self.assertEqual(b1["unsent_count"], n_ships - 1)
            self.assertEqual(len(self.allocations), 0)

            # Reconcile only recovers/submits item 0 safely without touching remaining UNSENT items
            rec1 = client.put(f"/api/plans/{plan['plan_id']}/approval", json={"reconcile_only": True})
            self.assertEqual(rec1.status_code, 200)
            r1 = rec1.json()
            self.assertEqual(r1["confirmed_count"], 1)
            self.assertEqual(r1["unsent_count"], n_ships - 1)
            self.assertEqual(len(self.allocations), 1)

            # 2) Fail AFTER commit (plus transient 503 on GET) on shipment 1 -> UNKNOWN while sim ALREADY has 2 allocs
            self.fail_after_commit_count = 2
            res2 = client.put(f"/api/plans/{plan['plan_id']}/approval", json={})
            self.assertEqual(res2.status_code, 200)
            b2 = res2.json()
            self.assertEqual(b2["state"], "UNKNOWN")
            self.assertEqual(b2["confirmed_count"], 1)
            self.assertEqual(b2["unknown_count"], 1)
            self.assertEqual(b2["unsent_count"], n_ships - 2)
            self.assertEqual(len(self.allocations), 2)

            # 3) Disrupt route for item 2 -> item 1 UNKNOWN reconciles to CONFIRMED cleanly, then item 2 REJECTED halts batch!
            target_route = plan["shipments"][2]["route_id"]
            self.disrupt_routes.add(target_route)
            res3 = client.put(f"/api/plans/{plan['plan_id']}/approval", json={})
            self.assertEqual(res3.status_code, 200)
            b3 = res3.json()
            self.assertEqual(b3["state"], "STOPPED")
            self.assertEqual(b3["confirmed_count"], 2)
            self.assertEqual(b3["rejected_count"], 1)
            self.assertEqual(b3["unsent_count"], n_ships - 3)
            self.assertTrue(b3["requires_new_plan"])
            self.assertEqual(len(self.allocations), 2)

            # 4) Reset simulator -> old plan blocked with 409 Conflict
            self.tick = 0
            self.allocations.clear()
            self.disrupt_routes.clear()
            app_main.clear_planning_cache(app_main.app)
            self.assertEqual(client.get("/api/snapshot").status_code, 200)
            blocked = client.put(f"/api/plans/{plan['plan_id']}/approval", json={})
            self.assertEqual(blocked.status_code, 409)

    def test_provenance_headers_stale_and_running_grace(self) -> None:
        with TestClient(app_main.app) as client:
            app_main.app.state.simulator = httpx.AsyncClient(base_url="http://mock", transport=self._sim_transport())
            app_main.app.state.generation = None

            # Healthy snapshot & planning
            snap_r = client.get("/api/snapshot")
            self.assertEqual(snap_r.status_code, 200)
            self.assertEqual(snap_r.headers.get("x-bup-cached"), "false")
            self.assertEqual(snap_r.headers.get("x-bup-degraded"), "false")
            self.assertEqual(snap_r.headers.get("x-bup-age-ticks"), "0")

            plan_r = client.get("/api/planning?history_ticks=8&horizon_ticks=24")
            self.assertEqual(plan_r.status_code, 200)
            self.assertFalse(plan_r.json()["cached"])
            self.assertFalse(plan_r.json()["degraded"])

            # RUNNING mode mid-tick change serves cached coherent snapshot/plan clearly labeled CACHED + DEGRADED
            self.sim_status = "RUNNING"
            snap_run = client.get("/api/snapshot")
            self.assertEqual(snap_run.status_code, 200)
            self.assertEqual(snap_run.headers.get("x-bup-cached"), "true")
            self.assertEqual(snap_run.headers.get("x-bup-degraded"), "true")

            plan_run = client.get("/api/planning?history_ticks=8&horizon_ticks=24")
            self.assertEqual(plan_run.status_code, 200)
            self.assertTrue(plan_run.json()["cached"])
            self.assertTrue(plan_run.json()["degraded"])

            # Stale fault rejects fresh snapshot and blocks approval writes
            self.sim_status = "PAUSED"
            self.stale_fault = True
            app_main.clear_planning_cache(app_main.app)
            app_main.clear_coherent_planning(app_main.app)
            self.assertEqual(client.get("/api/snapshot").status_code, 503)
            opt_plan = app_main.app.state.store.get_latest_plan(app_main.app.state.generation.generation_id)
            if opt_plan:
                stale_write = client.put(f"/api/plans/{opt_plan.plan_id}/approval", json={})
                self.assertEqual(stale_write.status_code, 503)

    def test_evaluation_summary_curated_fallback_and_not_yet_measured(self) -> None:
        from intelligence.evaluation import load_evaluation_summary

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            empty_eval = root / "eval"
            empty_evid = root / "evid"
            empty_load = root / "load"
            empty_eval.mkdir()
            empty_evid.mkdir()
            empty_load.mkdir()
            manifest = root / "model-manifest.json"
            manifest.write_text('{"chronos2": {"model_id": "amazon/chronos-2"}}', encoding="utf-8")

            # Static manifest alone must NOT mark status="measured" when no measurements exist
            unmeasured = load_evaluation_summary(
                eval_dir=str(empty_eval),
                manifest_path=str(manifest),
                evidence_dir=str(empty_evid),
                load_dir=str(empty_load),
            )
            self.assertEqual(unmeasured["status"], "not_yet_measured")
            self.assertEqual(unmeasured["artifact_source"], "not_yet_measured")
            self.assertIsNotNone(unmeasured["model_manifest"])

            # Historical v1 traces alone must not mark matched policy benchmark as measured
            (empty_evid / "reorder-policy.json").write_text('{"policy": "reorder-policy"}', encoding="utf-8")
            legacy_only = load_evaluation_summary(
                eval_dir=str(empty_eval),
                manifest_path=str(manifest),
                evidence_dir=str(empty_evid),
                load_dir=str(empty_load),
            )
            self.assertEqual(legacy_only["status"], "not_yet_measured")
            self.assertIsNotNone(legacy_only["historical_baseline_traces"])

            # Curated evidence JSONs load cleanly when runtime artifacts dir is empty (clean checkout)
            (empty_evid / "policy_comparison.json").write_text(
                json.dumps({"aggregate": {"reorder_batch_4_15k": {"mean_service_level": 0.994912}}}),
                encoding="utf-8",
            )
            curated = load_evaluation_summary(
                eval_dir=str(empty_eval),
                manifest_path=str(manifest),
                evidence_dir=str(empty_evid),
                load_dir=str(empty_load),
            )
            self.assertEqual(curated["status"], "measured")
            self.assertEqual(curated["artifact_source"], "curated_evidence")
            self.assertAlmostEqual(
                curated["policy_comparison"]["aggregate"]["reorder_batch_4_15k"]["mean_service_level"],
                0.994912,
            )

    def test_horizon_bounds_enforced_internally(self) -> None:
        from datetime import UTC, datetime
        from intelligence.forecasting import build_seasonal_forecast_points
        from planning import build_forecasts_response, build_report

        with self.assertRaises(ValueError):
            build_seasonal_forecast_points(
                samples=[(0, 10.0)],
                tick=1,
                horizon_ticks=289,
                tick_minutes=15,
                sim_time=datetime.now(UTC),
                station_id="s1",
                region_id="r1",
                station_detail=None,
                events=[],
            )
        with self.assertRaises(ValueError):
            build_report(make_world(), [], [], 8, 289)
        with self.assertRaises(ValueError):
            build_forecasts_response(make_world(), [], 8, 0, generation_id="g1")


if __name__ == "__main__":
    unittest.main()
