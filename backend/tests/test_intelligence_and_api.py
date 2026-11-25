import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path
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
from planning import (  # noqa: E402
    DemandObservation,
    Region,
    SimulatorEvent,
    StationDetail,
    SupplyArrival,
    build_report,
)
from schemas import Depot, FuelVolumes, Route, Simulation, Snapshot, Station  # noqa: E402


def sample_snapshot(
    *,
    tick: int = 10,
    station_diesel: float = 900.0,
    route_status: str = "AVAILABLE",
    depot_status: str = "OPEN",
) -> Snapshot:
    return Snapshot(
        simulation=Simulation(
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
                dispatch_capacity_per_tick=12000,
                inventory=FuelVolumes(DIESEL=40000, PETROL=40000, OCTANE=20000),
                capacity=FuelVolumes(DIESEL=90000, PETROL=70000, OCTANE=45000),
            )
        ],
        stations=[
            Station(
                id="station-mirpur",
                name="Mirpur Fuel Station",
                region_id="region-dhaka",
                status="OPEN",
                inventory=FuelVolumes(DIESEL=station_diesel, PETROL=9000, OCTANE=5000),
                capacity=FuelVolumes(DIESEL=15000, PETROL=14000, OCTANE=9000),
            ),
            Station(
                id="station-tongi",
                name="Tongi Industrial Station",
                region_id="region-dhaka",
                status="OPEN",
                inventory=FuelVolumes(DIESEL=5000, PETROL=6000, OCTANE=3500),
                capacity=FuelVolumes(DIESEL=18000, PETROL=9000, OCTANE=6000),
            ),
        ],
        routes=[
            Route(
                id="route-gazipur-mirpur",
                source_depot_id="depot-gazipur",
                destination_station_id="station-mirpur",
                status=route_status,
                transit_ticks=2,
                max_shipment=5000,
            ),
            Route(
                id="route-gazipur-tongi",
                source_depot_id="depot-gazipur",
                destination_station_id="station-tongi",
                status="AVAILABLE",
                transit_ticks=2,
                max_shipment=7000,
            ),
        ],
    )


class IntelligenceUnitTests(unittest.TestCase):
    def test_insufficient_history_activates_reorder_fallback_and_preserves_contract(self) -> None:
        snap = sample_snapshot(tick=2, station_diesel=1500.0)
        details = [
            StationDetail(id="station-mirpur", region_id="region-dhaka", demand_profile="urban_high", demand_multiplier=1.0),
            StationDetail(id="station-tongi", region_id="region-dhaka", demand_profile="industrial", demand_multiplier=1.0),
        ]
        report = build_report(snap, [], [], history_ticks=8, horizon_ticks=48, station_details=details)

        self.assertTrue(report.fallback_active)
        self.assertEqual(report.policy_mode, "REORDER_FALLBACK")
        mirpur_diesel = next(
            f for f in report.forecasts if f.station_id == "station-mirpur" and f.fuel_type == "DIESEL"
        )
        self.assertIsNone(mirpur_diesel.demand_liters_per_tick)
        self.assertIsNotNone(mirpur_diesel.fallback_demand_liters_per_tick)
        self.assertTrue(mirpur_diesel.fallback_used)
        self.assertEqual(mirpur_diesel.shortage_risk, "CRITICAL")
        self.assertTrue(any(r.policy_mode == "REORDER_FALLBACK" and r.destination_station_id == "station-mirpur" for r in report.recommendations))

    def test_urgency_ranking_prioritizes_imminent_stockout_and_detects_anomalies(self) -> None:
        snap = sample_snapshot(tick=10, station_diesel=900.0)
        details = [
            StationDetail(id="station-mirpur", region_id="region-dhaka", demand_profile="urban_high", demand_multiplier=1.8),
            StationDetail(id="station-tongi", region_id="region-dhaka", demand_profile="industrial", demand_multiplier=1.0),
        ]
        history: list[DemandObservation] = []
        for t in range(2, 10):
            mirpur_val = 190.0 if t == 9 else 100.0
            history.append(DemandObservation(station_id="station-mirpur", fuel_type="DIESEL", tick=t, demand_liters=mirpur_val))
            history.append(DemandObservation(station_id="station-mirpur", fuel_type="PETROL", tick=t, demand_liters=110.0))
            history.append(DemandObservation(station_id="station-mirpur", fuel_type="OCTANE", tick=t, demand_liters=60.0))
            history.append(DemandObservation(station_id="station-tongi", fuel_type="DIESEL", tick=t, demand_liters=150.0))
            history.append(DemandObservation(station_id="station-tongi", fuel_type="PETROL", tick=t, demand_liters=50.0))
            history.append(DemandObservation(station_id="station-tongi", fuel_type="OCTANE", tick=t, demand_liters=25.0))

        events = [
            SimulatorEvent(
                id=1,
                type="demand_spike",
                start_tick=9,
                end_tick=25,
                status="ACTIVE",
                parameters={"station_ids": ["station-mirpur"], "multiplier": 1.8},
            )
        ]
        arrivals = [
            SupplyArrival(
                id="supply-001",
                depot_id="depot-gazipur",
                fuel_type="DIESEL",
                quantity=18000,
                planned_tick=14,
                actual_tick=None,
                status="DELAYED",
            )
        ]
        report = build_report(
            snap,
            history,
            [],
            history_ticks=8,
            horizon_ticks=96,
            station_details=details,
            regions=[Region(id="region-dhaka", name="Dhaka", demand_factor=1.0)],
            supply_arrivals=arrivals,
            events=events,
        )

        self.assertEqual(report.policy_mode, "FORECAST_OPTIMIZER")
        self.assertFalse(report.fallback_active)
        self.assertEqual(report.recommendations[0].destination_station_id, "station-mirpur")
        self.assertEqual(report.recommendations[0].fuel_type, "DIESEL")
        self.assertTrue(report.recommendations[0].rationale)
        categories = {item.category for item in report.anomalies}
        self.assertIn("DEMAND_SPIKE", categories)
        self.assertIn("STOCKOUT_RISK", categories)
        self.assertIn("SUPPLY_DELAYED", categories)


class APIIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["DISABLE_SSE_WATCHER"] = "1"
        os.environ["APPROVAL_DB"] = str(Path(self.tmpdir.name) / "approvals.sqlite3")
        self.stale = False
        self.allocations: list[dict] = []
        self.call_counts: dict[str, int] = {}

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _mock_transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            self.call_counts[path] = self.call_counts.get(path, 0) + 1
            if path == "/v1/health":
                return httpx.Response(200, json={"status": "ok", "database": "ok", "simulation": {"status": "PAUSED", "tick": 10}})
            headers = {"X-Simulator-Stale": "true"} if self.stale else {}
            if path == "/v1/instance":
                return httpx.Response(200, headers=headers, json={
                    "id": 1, "scenario_id": "baseline", "scenario_version": "1.0", "seed": 12345,
                    "sim_time": "2026-01-01T08:00:00+00:00", "tick": 10, "tick_minutes": 15, "status": "PAUSED",
                })
            if path == "/v1/depots":
                return httpx.Response(200, headers=headers, json=[
                    {
                        "id": "depot-gazipur", "name": "Gazipur Depot", "region_id": "region-dhaka", "status": "OPEN",
                        "dispatch_capacity_per_tick": 12000,
                        "capacity": {"DIESEL": 90000, "PETROL": 70000, "OCTANE": 45000},
                        "inventory": {"DIESEL": 60000, "PETROL": 45000, "OCTANE": 26000},
                    }
                ])
            if path == "/v1/stations":
                return httpx.Response(200, headers=headers, json=[
                    {
                        "id": "station-mirpur", "name": "Mirpur Fuel Station", "region_id": "region-dhaka", "status": "OPEN",
                        "demand_profile": "urban_high", "demand_multiplier": 1.0,
                        "capacity": {"DIESEL": 15000, "PETROL": 14000, "OCTANE": 9000},
                        "inventory": {"DIESEL": 1200, "PETROL": 9000, "OCTANE": 5000},
                    }
                ])
            if path == "/v1/routes":
                return httpx.Response(200, headers=headers, json=[
                    {
                        "id": "route-gazipur-mirpur", "source_depot_id": "depot-gazipur",
                        "destination_station_id": "station-mirpur", "transit_ticks": 2,
                        "max_shipment": 7000, "status": "AVAILABLE",
                    }
                ])
            if path == "/v1/regions":
                return httpx.Response(200, headers=headers, json=[{"id": "region-dhaka", "name": "Dhaka", "demand_factor": 1.0}])
            if path == "/v1/supply-arrivals":
                return httpx.Response(200, headers=headers, json=[])
            if path == "/v1/events":
                return httpx.Response(200, headers=headers, json=[])
            if path == "/v1/metrics":
                return httpx.Response(200, headers=headers, json={
                    "served_demand_liters": 5000.0, "unmet_demand_liters": 0.0,
                    "service_level": 1, "allocation_liters": 0.0, "allocation_failures": 0,
                })
            if path == "/v1/demand-history":
                rows = []
                for t in range(2, 10):
                    for fuel, val in (("DIESEL", 110.0), ("PETROL", 110.0), ("OCTANE", 60.0)):
                        rows.append({
                            "id": t * 10, "station_id": "station-mirpur", "fuel_type": fuel,
                            "tick": t, "sim_time": "2026-01-01T08:00:00+00:00",
                            "demand_liters": val, "served_liters": val, "unmet_liters": 0.0,
                        })
                return httpx.Response(200, headers=headers, json=rows)
            if path == "/v1/allocations" and request.method == "GET":
                return httpx.Response(200, headers=headers, json=self.allocations)
            if path == "/v1/allocations" and request.method == "POST":
                body = json.loads(request.content)
                for row in self.allocations:
                    if row["idempotency_key"] == body["idempotency_key"]:
                        return httpx.Response(201, json=row)
                created = {
                    "id": len(self.allocations) + 1,
                    "idempotency_key": body["idempotency_key"],
                    "source_depot_id": body["source_depot_id"],
                    "destination_station_id": body["destination_station_id"],
                    "route_id": body["route_id"],
                    "fuel_type": body["fuel_type"],
                    "quantity": body["quantity"],
                    "created_tick": 10,
                    "departure_tick": None,
                    "expected_arrival_tick": None,
                    "actual_arrival_tick": None,
                    "status": "PENDING",
                    "failure_reason": None,
                }
                self.allocations.append(created)
                return httpx.Response(201, json=created)
            return httpx.Response(404)

        return httpx.MockTransport(handler)

    def test_health_metrics_planning_stale_and_idempotent_approval(self) -> None:
        transport = self._mock_transport()
        with TestClient(app_main.app) as client:
            app_main.app.state.simulator = httpx.AsyncClient(base_url="http://mock", transport=transport)

            health = client.get("/api/health")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["status"], "ok")
            self.assertEqual(health.json()["components"]["database"]["status"], "ok")

            plan = client.get("/api/planning")
            self.assertEqual(plan.status_code, 200)
            body = plan.json()
            self.assertEqual(body["tick"], 10)
            self.assertTrue(len(body["recommendations"]) > 0)

            # Second hit within TTL reuses cache after instance verification (no duplicate demand-history fetch).
            history_calls_before = self.call_counts["/v1/demand-history"]
            plan2 = client.get("/api/planning")
            self.assertEqual(plan2.status_code, 200)
            self.assertEqual(self.call_counts["/v1/demand-history"], history_calls_before)

            # Idempotent approval persistence + conflict check.
            intent = str(uuid4())
            proposal = {
                "source_depot_id": "depot-gazipur",
                "destination_station_id": "station-mirpur",
                "route_id": "route-gazipur-mirpur",
                "fuel_type": "DIESEL",
                "quantity": 3000,
            }
            first = client.put(f"/api/approvals/{intent}", json=proposal)
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json()["state"], "CONFIRMED")
            replay = client.put(f"/api/approvals/{intent}", json=proposal)
            self.assertEqual(replay.json()["shipment"]["id"], first.json()["shipment"]["id"])
            conflict = client.put(f"/api/approvals/{intent}", json={**proposal, "quantity": 2000})
            self.assertEqual(conflict.status_code, 409)

            # Stale fault degrades /api/health, /api/snapshot, and /api/planning immediately.
            self.stale = True
            self.assertEqual(client.get("/api/snapshot").status_code, 503)
            self.assertEqual(client.get("/api/planning").status_code, 503)
            self.assertEqual(client.get("/api/health").status_code, 503)
            self.assertEqual(client.get("/api/metrics").status_code, 200)


if __name__ == "__main__":
    unittest.main()
