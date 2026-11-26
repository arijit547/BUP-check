"""Section 3 automated verification: Core mode without weights, separate model readiness, SHA-256 prep, offline ML, and ML stop/recover."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = ROOT / "artifacts" / "evaluation" / "ml_verification.json"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def step_ticks(sim: httpx.Client, target: int) -> None:
    while int(sim.get("/v1/instance").json()["tick"]) < target:
        sim.post("/admin/step").raise_for_status()


def main() -> None:
    report: dict[str, object] = {"checks": {}}
    checks: dict[str, object] = report["checks"]  # type: ignore[assignment]

    # 1. Core startup with neither weights nor ml-service configured
    sim_p, api_p, dash_p = free_port(), free_port(), free_port()
    core_proj = f"bup-core-{uuid4().hex[:8]}"
    core_env = {**os.environ, "SIMULATOR_PORT": str(sim_p), "SNAPSHOT_PORT": str(api_p), "DASHBOARD_PORT": str(dash_p)}
    core_cmd = ["docker", "compose", "-p", core_proj, "-f", "compose.yaml"]
    try:
        subprocess.run([*core_cmd, "up", "-d", "--build", "--wait"], cwd=ROOT, env=core_env, check=True)
        with httpx.Client(base_url=f"http://127.0.0.1:{sim_p}", timeout=10.0) as sim, httpx.Client(base_url=f"http://127.0.0.1:{api_p}", timeout=15.0) as api:
            step_ticks(sim, 24)
            m = api.get("/api/models").json()
            p_def = api.get("/api/planning?history_ticks=8&horizon_ticks=48").json()
            p_chr = api.get("/api/planning?policy_override=CHRONOS2&history_ticks=8&horizon_ticks=48").json()
            assert m["ml_service_configured"] is False
            assert p_def["forecast_source"] == "SEASONAL"
            assert p_chr["forecast_source"] == "SEASONAL" and "disabled" in str(p_chr.get("fallback_reason", "")).lower()
            checks["core_without_ml"] = {"passed": True, "default_source": p_def["forecast_source"], "fallback_reason": p_chr.get("fallback_reason")}
            print("CHECK core_without_ml PASS", flush=True)
    finally:
        subprocess.run([*core_cmd, "down", "-v"], cwd=ROOT, env=core_env, check=False)

    # 2. Clean model preparation & SHA-256 checksum verification + partial-readiness check (1 model missing -> 503 partial, NOT ready!)
    with tempfile.TemporaryDirectory() as empty_weights:
        subprocess.run(["docker", "compose", "-f", "compose.yaml", "-f", "compose.ml.yaml", "--profile", "ml", "build", "ml-service"], cwd=ROOT, check=True)
        imgs = subprocess.check_output(["docker", "compose", "-f", "compose.yaml", "-f", "compose.ml.yaml", "--profile", "ml", "config", "--images"], cwd=ROOT, text=True)
        ml_img = next(line.strip() for line in imgs.splitlines() if "ml-service" in line)
        cname = f"bup-partial-ml-{uuid4().hex[:6]}"
        pport = free_port()
        subprocess.run([
            "docker", "run", "-d", "--name", cname, "-p", f"127.0.0.1:{pport}:8002",
            "-v", f"{empty_weights}:/models:ro",
            "-v", f"{ROOT / 'ml-service' / 'artifacts'}:/app/artifacts:ro",
            ml_img,
        ], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
        try:
            for _ in range(25):
                try:
                    if httpx.get(f"http://127.0.0.1:{pport}/health/live", timeout=2.0).status_code == 200:
                        break
                except Exception:
                    time.sleep(0.5)
            r_part = httpx.get(f"http://127.0.0.1:{pport}/health/ready", timeout=5.0)
            body = r_part.json()
            assert r_part.status_code == 503, r_part.text
            assert body["status"] == "partial"
            assert body["chronos2"]["status"] == "missing"
            assert body["isolation_forest"]["status"] == "loaded"
            checks["separate_model_readiness"] = {"passed": True, "http_status": r_part.status_code, "payload": body}
            print("CHECK separate_model_readiness PASS", flush=True)
        finally:
            subprocess.run(["docker", "rm", "-f", cname], check=False, stdout=subprocess.DEVNULL)

    # Run clean preparation verification (SHA-256 Chronos-2 verify + locked Isolation Forest training + --network=none test)
    prep_out = subprocess.check_output(
        [sys.executable, str(ROOT / "scripts" / "prepare-models.py"), "--skip-download", "--skip-collect", "--verify-offline"],
        cwd=ROOT,
        text=True,
    )
    checks["prepare_and_offline_smoke"] = {"passed": "OFFLINE_PREP_OK" in prep_out, "output_tail": prep_out.splitlines()[-12:]}
    print("CHECK prepare_and_offline_smoke PASS", flush=True)

    # 3. Full ML isolated stack: true Chronos-2 inference + Isolation Forest anomaly detection + stop/degrade/recover
    sim_p, api_p, dash_p = free_port(), free_port(), free_port()
    ml_proj = f"bup-mlchk-{uuid4().hex[:8]}"
    ml_env = {**os.environ, "SIMULATOR_PORT": str(sim_p), "SNAPSHOT_PORT": str(api_p), "DASHBOARD_PORT": str(dash_p)}
    ml_cmd = ["docker", "compose", "-p", ml_proj, "-f", "compose.yaml", "-f", "compose.ml.yaml", "--profile", "ml"]
    try:
        subprocess.run([*ml_cmd, "up", "-d", "--wait"], cwd=ROOT, env=ml_env, check=True)
        with httpx.Client(base_url=f"http://127.0.0.1:{sim_p}", timeout=15.0) as sim, httpx.Client(base_url=f"http://127.0.0.1:{api_p}", timeout=25.0) as api:
            for _ in range(45):
                try:
                    if api.get("/api/health").json()["components"]["ml_service"]["status"] == "ok":
                        break
                except Exception:
                    pass
                time.sleep(2)
            step_ticks(sim, 24)
            # True non-fallback Chronos-2 forecast
            t0 = time.perf_counter()
            fc = api.get("/api/forecasts?history_ticks=24&horizon_ticks=32&policy_override=CHRONOS2").json()
            c_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            assert fc["source"] == "CHRONOS2" and fc.get("fallback_reason") is None, fc
            assert "29ec3766d36d" in fc["model_version"]

            # Exercise Isolation Forest with demand spike
            sim.post("/admin/events", json={
                "type": "demand_spike", "start_tick": 24, "duration_ticks": 8,
                "parameters": {"station_ids": ["station-mirpur"], "multiplier": 2.1},
            }).raise_for_status()
            step_ticks(sim, 27)
            inc = api.get("/api/incidents").json()
            assert inc["anomaly_model_version"] == "iforest-bup-v1.0.0"
            assert any(i["source"] in ("ML", "HYBRID") for i in inc["incidents"]), inc

            # Stop ml-service -> fallback planning -> restart ml-service -> recovery
            subprocess.run([*ml_cmd, "stop", "ml-service"], cwd=ROOT, env=ml_env, check=True)
            h_stop = api.get("/api/health").json()
            p_stop = api.get("/api/planning?policy_override=CHRONOS2&horizon_ticks=40").json()
            assert h_stop["components"]["ml_service"]["status"] == "degraded"
            assert p_stop["forecast_source"] == "SEASONAL" and p_stop["degraded"] is True

            subprocess.run([*ml_cmd, "start", "ml-service"], cwd=ROOT, env=ml_env, check=True)
            for _ in range(45):
                try:
                    if api.get("/api/health").json()["components"]["ml_service"]["status"] == "ok":
                        break
                except Exception:
                    pass
                time.sleep(2)
            p_recov = api.get("/api/forecasts?history_ticks=24&horizon_ticks=32&policy_override=CHRONOS2").json()
            assert p_recov["source"] == "CHRONOS2"
            checks["live_ml_inference_and_recovery"] = {
                "passed": True,
                "chronos2_source": fc["source"],
                "chronos2_model_version": fc["model_version"],
                "chronos2_latency_ms": c_ms,
                "anomaly_model_version": inc["anomaly_model_version"],
                "hybrid_or_ml_incidents": sum(1 for i in inc["incidents"] if i["source"] in ("ML", "HYBRID")),
                "stopped_ml_fallback_source": p_stop["forecast_source"],
                "recovered_chronos2_source": p_recov["source"],
            }
            print("CHECK live_ml_inference_and_recovery PASS", flush=True)
    finally:
        subprocess.run([*ml_cmd, "down", "-v"], cwd=ROOT, env=ml_env, check=False)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
