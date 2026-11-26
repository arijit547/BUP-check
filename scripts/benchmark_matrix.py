"""Phase 12 expanded load, uncached solver, running-simulator, sustained, ML latency, Prometheus & resource benchmark."""

from __future__ import annotations

import argparse
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
OUT_PATH = ROOT / "artifacts" / "load" / "benchmark_matrix.json"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def percentiles(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    s = sorted(values)

    def p(q: float) -> float:
        idx = min(len(s) - 1, max(0, int(q * len(s) + 0.9999) - 1))
        return round(s[idx], 2)

    return {
        "mean": round(sum(s) / len(s), 2),
        "p50": p(0.50),
        "p95": p(0.95),
        "p99": p(0.99),
        "max": round(s[-1], 2),
    }


def host_environment() -> dict[str, Any]:
    info: dict[str, Any] = {
        "platform": sys.platform,
        "python_version": sys.version.split()[0],
        "cpu_count": os.cpu_count(),
    }
    try:
        if os.name == "nt":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            info["total_ram_gb"] = round(stat.ullTotalPhys / (1024**3), 2)
            info["avail_ram_gb"] = round(stat.ullAvailPhys / (1024**3), 2)
        else:
            meminfo = Path("/proc/meminfo").read_text(encoding="utf-8")
            for line in meminfo.splitlines():
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    info["total_ram_gb"] = round(kb / (1024**2), 2)
    except Exception as exc:
        info["ram_probe_error"] = f"{type(exc).__name__}: {exc}"

    try:
        git_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        git_dirty = bool(subprocess.check_output(["git", "status", "--short"], cwd=ROOT, text=True).strip())
        info["git_commit"] = git_sha
        info["git_working_tree_dirty"] = git_dirty
    except Exception as exc:
        info["git_probe_error"] = f"{type(exc).__name__}: {exc}"
    return info


def container_metadata(project: str) -> dict[str, Any]:
    try:
        ps = subprocess.run(
            ["docker", "compose", "-p", project, "-f", "compose.yaml", "-f", "compose.ml.yaml", "--profile", "ml", "--profile", "monitoring", "ps", "-q"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        ids = [line.strip() for line in ps.stdout.splitlines() if line.strip()]
        if not ids:
            return {"error": f"No container IDs returned (stderr={ps.stderr.strip()})"}
        insp = subprocess.run(
            ["docker", "inspect", *ids],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        if insp.returncode != 0:
            return {"error": f"docker inspect failed: {insp.stderr.strip()}"}
        out: dict[str, Any] = {}
        for row in json.loads(insp.stdout):
            name = str(row.get("Name", "")).lstrip("/")
            hc = row.get("HostConfig") or {}
            out[name] = {
                "image": (row.get("Config") or {}).get("Image"),
                "image_id": row.get("Image"),
                "memory_limit_bytes": hc.get("Memory") or 0,
                "nano_cpus": hc.get("NanoCpus") or 0,
                "cpu_quota": hc.get("CpuQuota") or 0,
            }
        return out
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def docker_stats(project: str) -> dict[str, Any]:
    try:
        ps = subprocess.run(
            ["docker", "compose", "-p", project, "-f", "compose.yaml", "-f", "compose.ml.yaml", "--profile", "ml", "--profile", "monitoring", "ps", "-q"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        ids = [line.strip() for line in ps.stdout.splitlines() if line.strip()]
        if not ids:
            return {"resource_collection_error": f"No running container IDs found: {ps.stderr.strip()}"}
        st = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}", *ids],
            cwd=ROOT, capture_output=True, text=True, check=False, timeout=20,
        )
        if st.returncode != 0 or not st.stdout.strip():
            return {"resource_collection_error": f"docker stats failed (rc={st.returncode}): {st.stderr.strip()}"}
        out: dict[str, Any] = {}
        for line in st.stdout.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            out[row.get("Name", row.get("ID", "unknown"))] = {
                "cpu": row.get("CPUPerc"),
                "mem": row.get("MemUsage"),
                "mem_perc": row.get("MemPerc"),
            }
        return out
    except Exception as exc:
        return {"resource_collection_error": f"{type(exc).__name__}: {exc}"}


def run_burst(
    base_url: str,
    path_fn,
    concurrency: int,
    duration_s: float,
    *,
    warmup_s: float = 1.5,
    expected_ok_statuses: tuple[int, ...] = (200,),
    collect_provenance: bool = False,
) -> dict[str, Any]:
    # Separated warm-up phase (excluded from measurements)
    if warmup_s > 0:
        w_deadline = time.perf_counter() + warmup_s
        with httpx.Client(base_url=base_url, timeout=15.0) as client:
            i = 0
            while time.perf_counter() < w_deadline:
                p = path_fn(0, i)
                i += 1
                try:
                    if isinstance(p, tuple):
                        client.request(p[0], p[1], json=p[2])
                    else:
                        client.get(p)
                except Exception:
                    pass

    lat: list[float] = []
    errors: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    provenance_samples: list[dict[str, Any]] = []
    started = time.perf_counter()
    deadline = started + duration_s

    def worker(wid: int) -> None:
        i = 0
        with httpx.Client(base_url=base_url, timeout=20.0) as client:
            while time.perf_counter() < deadline:
                path = path_fn(wid, i)
                i += 1
                t0 = time.perf_counter()
                try:
                    if isinstance(path, tuple):
                        method, url, body = path
                        r = client.request(method, url, json=body)
                    else:
                        r = client.get(path)
                    dt = (time.perf_counter() - t0) * 1000.0
                    sc = str(r.status_code)
                    status_counts[sc] = status_counts.get(sc, 0) + 1
                    if r.status_code in expected_ok_statuses:
                        lat.append(dt)
                        if collect_provenance and len(provenance_samples) < 50:
                            provenance_samples.append({
                                "as_of_tick": r.headers.get("x-bup-as-of-tick"),
                                "current_tick": r.headers.get("x-bup-current-tick"),
                                "age_ticks": r.headers.get("x-bup-age-ticks"),
                                "cached": r.headers.get("x-bup-cached"),
                                "degraded": r.headers.get("x-bup-degraded"),
                            })
                    else:
                        k = f"HTTP_{r.status_code}"
                        errors[k] = errors.get(k, 0) + 1
                except Exception as exc:
                    k = type(exc).__name__
                    errors[k] = errors.get(k, 0) + 1

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futs = [pool.submit(worker, w) for w in range(concurrency)]
        for f in futs:
            f.result()

    elapsed = time.perf_counter() - started
    total = len(lat) + sum(errors.values())
    out: dict[str, Any] = {
        "warmup_s": warmup_s,
        "concurrency": concurrency,
        "duration_s": round(elapsed, 2),
        "total_requests": total,
        "successful_requests": len(lat),
        "error_count": sum(errors.values()),
        "errors": errors,
        "status_codes": status_counts,
        "rps": round(total / max(0.01, elapsed), 2),
        "success_rps": round(len(lat) / max(0.01, elapsed), 2),
        "latency_ms": percentiles(lat),
    }
    if collect_provenance and provenance_samples:
        ages = [int(s["age_ticks"]) for s in provenance_samples if s["age_ticks"] is not None]
        out["provenance_summary"] = {
            "samples": len(provenance_samples),
            "cached_count": sum(1 for s in provenance_samples if s["cached"] == "true"),
            "degraded_count": sum(1 for s in provenance_samples if s["degraded"] == "true"),
            "age_ticks_min": min(ages) if ages else None,
            "age_ticks_max": max(ages) if ages else None,
            "sample_last": provenance_samples[-1],
        }
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sustained-seconds", type=int, default=120, help="Duration in seconds for sustained planning run (default: 120s).")
    parser.add_argument("--out", default=str(OUT_PATH))
    args = parser.parse_args()

    sim_port = free_port()
    api_port = free_port()
    dash_port = free_port()
    prom_port = free_port()
    project = f"bup-load-{uuid4().hex[:8]}"
    env = {
        **os.environ,
        "SIMULATOR_PORT": str(sim_port),
        "SNAPSHOT_PORT": str(api_port),
        "DASHBOARD_PORT": str(dash_port),
        "PROMETHEUS_PORT": str(prom_port),
    }
    compose_cmd = [
        "docker", "compose", "-p", project,
        "-f", "compose.yaml", "-f", "compose.ml.yaml", "--profile", "ml", "--profile", "monitoring",
    ]
    report: dict[str, Any] = {
        "command": " ".join([sys.executable, *sys.argv]),
        "environment": host_environment(),
        "scenarios": {},
    }
    try:
        subprocess.run([*compose_cmd, "up", "-d", "--build", "--wait"], cwd=ROOT, env=env, check=True)
        report["containers"] = container_metadata(project)
        sim_url = f"http://127.0.0.1:{sim_port}"
        api_url = f"http://127.0.0.1:{api_port}"
        dash_url = f"http://127.0.0.1:{dash_port}"
        prom_url = f"http://127.0.0.1:{prom_port}"

        with (
            httpx.Client(base_url=sim_url, timeout=30.0, limits=httpx.Limits(max_keepalive_connections=0)) as sim,
            httpx.Client(base_url=api_url, timeout=30.0, limits=httpx.Limits(max_keepalive_connections=0)) as api,
            httpx.Client(base_url=prom_url, timeout=15.0) as prom,
        ):
            for _ in range(45):
                try:
                    if api.get("/api/health").json()["components"]["ml_service"]["status"] == "ok":
                        break
                except Exception:
                    pass
                time.sleep(2)

            # Seed 42 ticks
            inst = sim.get("/v1/instance").json()
            while int(inst["tick"]) < 42:
                inst = sim.post("/admin/step").json()

            # 1-3: Cached paused planning at c=2, 10, 25 (via nginx dashboard proxy, with separated warmup)
            for c in (2, 10, 25):
                res = run_burst(dash_url, lambda _w, _i: "/api/planning?history_ticks=8&horizon_ticks=96", c, 10.0, warmup_s=1.5)
                res["docker_stats"] = docker_stats(project)
                report["scenarios"][f"cached_planning_c{c}"] = res
                print("cached_c", c, res["rps"], res["latency_ms"], res["errors"], flush=True)

            # 4: Uncached cold OR-Tools optimization (rotating horizon_ticks 48..95 bypasses cache hit)
            res_uncached = run_burst(
                api_url,
                lambda w, i: f"/api/planning?history_ticks=8&horizon_ticks={48 + ((w * 7 + i) % 48)}",
                concurrency=2,
                duration_s=10.0,
                warmup_s=1.0,
            )
            res_uncached["docker_stats"] = docker_stats(project)
            report["scenarios"]["uncached_ortools_c2"] = res_uncached
            print("uncached_c2", res_uncached["rps"], res_uncached["latency_ms"], res_uncached["errors"], flush=True)
            time.sleep(2.5)

            # 5: Real amazon/chronos-2 uncached ML inference (warmup 1 call excluded + 12 measured rotating horizons)
            api.get("/api/planning?policy_override=CHRONOS2&history_ticks=16&horizon_ticks=22").raise_for_status()
            ml_lats: list[float] = []
            ml_errs = 0
            last_ml_msg = None
            for i in range(12):
                t0 = time.perf_counter()
                r = api.get(f"/api/planning?policy_override=CHRONOS2&history_ticks=16&horizon_ticks={24 + i}")
                if r.status_code == 200 and r.json().get("forecast_source") == "CHRONOS2":
                    ml_lats.append((time.perf_counter() - t0) * 1000.0)
                else:
                    ml_errs += 1
                    try:
                        b = r.json()
                        last_ml_msg = f"HTTP {r.status_code}: src={b.get('forecast_source')} reason={b.get('fallback_reason')}"
                    except Exception:
                        last_ml_msg = f"HTTP {r.status_code}: {r.text[:200]}"
            report["scenarios"]["ml_chronos2_uncached"] = {
                "warmup_calls": 1,
                "measured_requests": 12,
                "successful_chronos2": len(ml_lats),
                "errors": ml_errs,
                "latency_ms": percentiles(ml_lats),
                "docker_stats": docker_stats(project),
                "last_error": last_ml_msg,
            }
            print("ml_chronos2", report["scenarios"]["ml_chronos2_uncached"], flush=True)

            # 6: Running simulator live streaming + concurrent reads (SIMULATION_SPEED=8), capturing tick lag provenance
            sim.post("/admin/run").raise_for_status()
            res_running = run_burst(
                dash_url,
                lambda _w, _i: "/api/planning?history_ticks=8&horizon_ticks=96",
                concurrency=8,
                duration_s=12.0,
                warmup_s=1.5,
                collect_provenance=True,
            )
            sim.post("/admin/pause").raise_for_status()
            res_running["docker_stats"] = docker_stats(project)
            res_running["metrics_snapshot"] = api.get("/api/metrics").json()
            report["scenarios"]["running_simulator_c8"] = res_running
            print("running_c8", res_running["rps"], res_running["latency_ms"], res_running.get("provenance_summary"), res_running["errors"], flush=True)

            # 7: Sustained planning run (c=10 mixed cached + refresh)
            if args.sustained_seconds > 0:
                res_sustained = run_burst(
                    dash_url,
                    lambda w, i: "/api/planning?history_ticks=8&horizon_ticks=96" if i % 5 != 0 else "/api/snapshot",
                    concurrency=10,
                    duration_s=float(args.sustained_seconds),
                    warmup_s=2.0,
                    collect_provenance=True,
                )
                res_sustained["docker_stats"] = docker_stats(project)
                res_sustained["metrics_snapshot"] = api.get("/api/metrics").json()
                report["scenarios"]["sustained_planning_c10"] = res_sustained
                print("sustained_c10", res_sustained["duration_s"], res_sustained["rps"], res_sustained["latency_ms"], res_sustained["errors"], flush=True)

            # 8: Bounded approval contention (unique approvals + concurrent same-plan replay contention)
            sim.post("/admin/reset").raise_for_status()
            while int(sim.get("/v1/instance").json()["tick"]) < 36:
                sim.post("/admin/step").json()
            api.get("/api/snapshot").raise_for_status()
            batch_plan = api.post("/api/optimize", json={"history_ticks": 8, "horizon_ticks": 96}).json()
            res_plan_cont = run_burst(
                api_url,
                lambda _w, _i: ("PUT", f"/api/plans/{batch_plan['plan_id']}/approval", {}),
                concurrency=4,
                duration_s=5.0,
                warmup_s=0.0,
            )
            sim_allocs_after_batch = len(sim.get("/v1/allocations").json())
            res_plan_cont["expected_batch_allocations"] = len(batch_plan["shipments"])
            res_plan_cont["actual_simulator_allocations"] = sim_allocs_after_batch
            report["scenarios"]["plan_approval_contention_c4"] = res_plan_cont
            print("plan_contention_c4", res_plan_cont["rps"], res_plan_cont["status_codes"], sim_allocs_after_batch, flush=True)

            # 9: Prometheus scrape & signals check
            time.sleep(6.0)  # wait > scrape_interval (5s)
            targets = prom.get("/api/v1/targets").json()
            active_targets = targets.get("data", {}).get("activeTargets", [])
            bup_target = next((t for t in active_targets if t.get("labels", {}).get("job") == "bup-snapshot-api"), None)
            q_req = prom.get("/api/v1/query", params={"query": "bup_http_requests_total"}).json()
            q_sl = prom.get("/api/v1/query", params={"query": "bup_simulator_service_level"}).json()
            metrics_final = api.get("/api/metrics").json()
            report["prometheus_and_observability"] = {
                "target_health": bup_target.get("health") if bup_target else "missing",
                "last_scrape_error": bup_target.get("lastError") if bup_target else None,
                "bup_http_requests_series": len(q_req.get("data", {}).get("result", [])),
                "bup_simulator_service_level_series": len(q_sl.get("data", {}).get("result", [])),
                "api_metrics_final": metrics_final,
            }
            print("prometheus_check", report["prometheus_and_observability"]["target_health"], flush=True)

    finally:
        subprocess.run([*compose_cmd, "down", "-v"], cwd=ROOT, env=env, check=False)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
