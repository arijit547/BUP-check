"""Collect reproducible training and held-out scenario datasets from isolated simulator containers."""
import argparse
import csv
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
VALID_SCENARIOS = ("normal_baseline", "crisis_dev_mix", "crisis_heldout_mix")


def _resolve_cli_path(raw: str) -> Path:
    if "\x00" in raw or ".." in Path(raw).parts:
        raise ValueError(f"Unsafe path argument: {raw}")
    resolved = Path(raw).resolve()
    allowed = (ROOT, Path.cwd().resolve(), Path(tempfile.gettempdir()).resolve())
    if not any(resolved == root or resolved.is_relative_to(root) for root in allowed):
        raise ValueError(f"Path outside allowed directories: {resolved}")
    return resolved

IMAGE = "asifmahmoud414/bup-fuel-supply-simulator:1.0.0@sha256:7067050693f49d377d91ca91f2faa6e63f673f69a69429d4693e78e9c0598e92"

SCENARIOS = {
    "normal_baseline": {
        "ticks": 288,  # 3 full simulated days at 15 min/tick
        "events": [],
        "split": "train",
        "description": "Normal baseline operation with no injected disruptions for normal behavior fitting.",
    },
    "crisis_dev_mix": {
        "ticks": 192,
        "events": [
            {"type": "demand_spike", "start_tick": 48, "duration_ticks": 16, "parameters": {"station_ids": ["station-mirpur"], "multiplier": 1.8}},
            {"type": "route_disruption", "start_tick": 60, "duration_ticks": 12, "parameters": {"route_ids": ["route-gazipur-mirpur"]}},
            {"type": "depot_constraint", "start_tick": 100, "duration_ticks": 14, "parameters": {"depot_ids": ["depot-patiya"]}},
            {"type": "shipment_delay", "start_tick": 120, "duration_ticks": 8, "parameters": {"depot_ids": ["depot-gazipur"], "fuel_types": ["DIESEL"], "delay_ticks": 4}},
        ],
        "split": "validation",
        "description": "Development scenario with demand spike, route disruption, depot constraint, and shipment delay.",
    },
    "crisis_heldout_mix": {
        "ticks": 192,
        "events": [
            {"type": "demand_spike", "start_tick": 44, "duration_ticks": 18, "parameters": {"region_ids": ["region-chattogram"], "multiplier": 1.7}},
            {"type": "station_outage", "start_tick": 72, "duration_ticks": 8, "parameters": {"station_ids": ["station-tongi"]}},
            {"type": "supply_shortfall", "start_tick": 90, "duration_ticks": 6, "parameters": {"depot_ids": ["depot-patiya"], "fuel_types": ["DIESEL"], "factor": 0.5}},
            {"type": "route_disruption", "start_tick": 110, "duration_ticks": 14, "parameters": {"route_ids": ["route-patiya-karnaphuli"]}},
        ],
        "split": "test_heldout",
        "description": "Held-out evaluation scenario with regional demand spike, station outage, shortfall, and highway route disruption.",
    },
}


def req(base: str, path: str, method: str = "GET", body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    r = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(r, timeout=10) as resp:
        return json.loads(resp.read())


def collect_scenario(name: str, cfg: dict, out_dir: Path) -> dict:
    safe_name = next((s for s in VALID_SCENARIOS if s == name), None)
    if safe_name is None or not re.fullmatch(r"[a-z0-9_]+", safe_name):
        raise ValueError(f"Unsupported scenario name: {name}")
    safe_dir = _resolve_cli_path(str(out_dir))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    container = f"bup-collect-{safe_name}-{port}"
    base = f"http://127.0.0.1:{port}"
    subprocess.run([
        "docker", "run", "-d", "--name", container, "-p", f"127.0.0.1:{port}:8000",
        "-e", "SIMULATOR_START_MODE=paused", "-e", "TICK_MINUTES=15", IMAGE,
    ], check=True, stdout=subprocess.DEVNULL)

    try:
        for _ in range(40):
            try:
                if req(base, "/v1/health")["status"] == "ok":
                    break
            except Exception:
                time.sleep(0.5)

        inst = req(base, "/v1/instance")
        regions = {r["id"]: r for r in req(base, "/v1/regions")}
        for ev in cfg["events"]:
            req(base, "/admin/events", "POST", ev)

        snapshot_rows = []
        cur0 = req(base, "/v1/instance")
        snapshot_rows.append({
            "tick": cur0["tick"],
            "sim_time": cur0["sim_time"],
            "depots": {d["id"]: d for d in req(base, "/v1/depots")},
            "stations": {s["id"]: s for s in req(base, "/v1/stations")},
            "routes": {r["id"]: r for r in req(base, "/v1/routes")},
            "arrivals": req(base, "/v1/supply-arrivals"),
            "events": req(base, "/v1/events"),
        })
        total_ticks = cfg["ticks"]
        for _ in range(total_ticks):
            req(base, "/admin/step", "POST")
            cur = req(base, "/v1/instance")
            depots = {d["id"]: d for d in req(base, "/v1/depots")}
            stations = req(base, "/v1/stations")
            routes = req(base, "/v1/routes")
            arrivals = req(base, "/v1/supply-arrivals")
            events = req(base, "/v1/events")
            snapshot_rows.append({
                "tick": cur["tick"],
                "sim_time": cur["sim_time"],
                "depots": depots,
                "stations": {s["id"]: s for s in stations},
                "routes": {r["id"]: r for r in routes},
                "arrivals": arrivals,
                "events": events,
            })

        # Fetch all bounded demand-history rows per station (total_ticks * 3 <= 2000)
        station_list = req(base, "/v1/stations")
        demand_rows = []
        for st in station_list:
            rows = req(base, f"/v1/demand-history?station_id={st['id']}&limit=2000")
            for row in rows:
                row["scenario"] = name
                row["split"] = cfg["split"]
                row["demand_profile"] = st["demand_profile"]
                row["region_id"] = st["region_id"]
                row["region_factor"] = regions[st["region_id"]]["demand_factor"]
                demand_rows.append(row)

        demand_rows.sort(key=lambda r: (r["tick"], r["station_id"], r["fuel_type"]))

        # Join demand for tick t strictly with pre-step snapshot at tick t (as-of issue time).
        snap_by_tick = {s["tick"]: s for s in snapshot_rows}
        csv_path = (safe_dir / f"{safe_name}.csv").resolve()
        if not csv_path.is_relative_to(safe_dir):
            raise ValueError(f"CSV output escapes out_dir: {csv_path}")
        fieldnames = [
            "scenario", "split", "seed", "tick", "sim_time", "station_id", "region_id",
            "region_factor", "demand_profile", "fuel_type", "demand_liters", "served_liters",
            "unmet_liters", "station_status", "demand_multiplier", "inventory_before_liters",
            "inventory_after_liters", "capacity_liters", "active_event_types",
        ]
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for row in demand_rows:
                pre = snap_by_tick.get(row["tick"])
                post = snap_by_tick.get(row["tick"] + 1)
                st_pre = pre["stations"].get(row["station_id"]) if pre else None
                st_post = post["stations"].get(row["station_id"]) if post else None
                active_evs = [
                    e["type"] for e in (post["events"] if post else [])
                    if e["start_tick"] <= row["tick"] < e["end_tick"]
                ]
                writer.writerow({
                    "scenario": row["scenario"],
                    "split": row["split"],
                    "seed": inst["seed"],
                    "tick": row["tick"],
                    "sim_time": row["sim_time"],
                    "station_id": row["station_id"],
                    "region_id": row["region_id"],
                    "region_factor": row["region_factor"],
                    "demand_profile": row["demand_profile"],
                    "fuel_type": row["fuel_type"],
                    "demand_liters": row["demand_liters"],
                    "served_liters": row["served_liters"],
                    "unmet_liters": row["unmet_liters"],
                    "station_status": st_post["status"] if st_post else (st_pre["status"] if st_pre else "OPEN"),
                    "demand_multiplier": st_post["demand_multiplier"] if st_post else (st_pre["demand_multiplier"] if st_pre else 1.0),
                    "inventory_before_liters": st_pre["inventory"][row["fuel_type"]] if st_pre else "",
                    "inventory_after_liters": st_post["inventory"][row["fuel_type"]] if st_post else "",
                    "capacity_liters": st_pre["capacity"][row["fuel_type"]] if st_pre else "",
                    "active_event_types": "|".join(sorted(set(active_evs))),
                })

        snap_path = (safe_dir / f"{safe_name}_snapshots.json").resolve()
        if not snap_path.is_relative_to(safe_dir):
            raise ValueError(f"Snapshot output escapes out_dir: {snap_path}")
        snap_path.write_text(json.dumps(snapshot_rows, indent=2), encoding="utf-8")
        return {
            "scenario": safe_name,
            "split": cfg["split"],
            "description": cfg["description"],
            "seed": inst["seed"],
            "ticks": total_ticks,
            "demand_rows": len(demand_rows),
            "events": cfg["events"],
            "csv_path": str(csv_path),
            "snapshots_path": str(snap_path),
            "simulator_metrics": req(base, "/v1/metrics"),
        }
    finally:
        subprocess.run(["docker", "rm", "-f", container], check=False, stdout=subprocess.DEVNULL)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="artifacts/datasets")
    parser.add_argument("--scenarios", nargs="*", default=list(VALID_SCENARIOS))
    args = parser.parse_args()
    out_dir = _resolve_cli_path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "simulator_image": IMAGE,
        "note": "Fixed seed scenario traces in unmodified official simulator; held-out evaluations vary injected crisis types, timing, and intensity.",
        "scenarios": [],
    }
    selected = []
    for raw_key in args.scenarios:
        key = next((s for s in VALID_SCENARIOS if s == raw_key), None)
        if key is None:
            raise ValueError(f"Unknown scenario {raw_key!r}; expected one of {VALID_SCENARIOS}")
        selected.append(key)
    for key in selected:
        print(f"Collecting scenario {key}...")
        info = collect_scenario(key, SCENARIOS[key], out_dir)
        manifest["scenarios"].append(info)
        print(f"  collected {info['demand_rows']} rows -> {info['csv_path']}")
    manifest_out = (out_dir / "manifest.json").resolve()
    if not manifest_out.is_relative_to(out_dir):
        raise ValueError(f"Manifest output escapes out_dir: {manifest_out}")
    manifest_out.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
