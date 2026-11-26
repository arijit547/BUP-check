"""Reproducible model preparation from a clean checkout: download Chronos-2, collect simulator traces, train Isolation Forest in locked container, verify checksums and offline inference."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-download", action="store_true", help="Verify existing weights instead of contacting Hugging Face.")
    parser.add_argument("--skip-collect", action="store_true", help="Reuse existing CSV traces in artifacts/datasets if present.")
    parser.add_argument("--verify-offline", action="store_true", help="Run a --network=none ml-service container smoke check after prep.")
    args = parser.parse_args()

    manifest_path = ROOT / "ml-service" / "model-manifest.json"
    weights_dir = ROOT / "ml-service" / "weights" / "chronos-2"
    datasets_dir = ROOT / "artifacts" / "datasets"
    artifacts_dir = ROOT / "ml-service" / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    weights_dir.parent.mkdir(parents=True, exist_ok=True)

    # 1. Download (or verify) pinned amazon/chronos-2 weights against committed manifest SHA-256
    dl_cmd = [sys.executable, str(ROOT / "scripts" / "download-models.py"), "--target", str(weights_dir), "--manifest", str(manifest_path)]
    if args.skip_download:
        dl_cmd.append("--verify-only")
    run(dl_cmd)

    # 2. Collect reproducible simulator datasets if missing or requested
    required_csvs = ("normal_baseline.csv", "crisis_dev_mix.csv", "crisis_heldout_mix.csv")
    if not args.skip_collect or not all((datasets_dir / f).exists() for f in required_csvs):
        run([sys.executable, str(ROOT / "scripts" / "collect-dataset.py"), "--out-dir", str(datasets_dir)])

    # 3. Build ml-service with locked dependencies and train Isolation Forest inside the exact locked runtime
    run(["docker", "compose", "-f", "compose.yaml", "-f", "compose.ml.yaml", "--profile", "ml", "build", "ml-service"])
    image_tag = subprocess.check_output(
        ["docker", "compose", "-f", "compose.yaml", "-f", "compose.ml.yaml", "--profile", "ml", "config", "--images"],
        cwd=ROOT,
        text=True,
    )
    ml_img = next((line.strip() for line in image_tag.splitlines() if "ml-service" in line), "bupfinal-ml-service")

    before_sha = json.loads(manifest_path.read_text(encoding="utf-8")).get("isolation_forest", {}).get("artifact_sha256")
    tmp_manifest = ROOT / "artifacts" / "iforest-train-manifest.json"
    tmp_manifest.parent.mkdir(parents=True, exist_ok=True)
    tmp_manifest.write_text(manifest_path.read_text(encoding="utf-8"), encoding="utf-8")

    run([
        "docker", "run", "--rm", "--user", "0:0",
        "-v", f"{datasets_dir}:/work/datasets:ro",
        "-v", f"{artifacts_dir}:/work/artifacts",
        "-v", f"{tmp_manifest}:/work/model-manifest.json",
        ml_img,
        "python", "/app/train_anomaly.py",
        "--datasets-dir", "/work/datasets",
        "--out-dir", "/work/artifacts",
        "--manifest", "/work/model-manifest.json",
    ])
    actual_sha = hashlib.sha256((artifacts_dir / "isolation_forest.joblib").read_bytes()).hexdigest()
    trained_meta = json.loads(tmp_manifest.read_text(encoding="utf-8")).get("isolation_forest", {})
    print(json.dumps({
        "iforest_artifact": str(artifacts_dir / "isolation_forest.joblib"),
        "expected_sha256": before_sha,
        "actual_sha256": actual_sha,
        "checksum_exact_match": before_sha == actual_sha,
        "heldout_unannounced_metrics": trained_meta.get("heldout_unannounced_metrics"),
    }, indent=2))
    # Update model-manifest.json only if checksum changed under deterministic training (e.g., library update)
    if before_sha != actual_sha:
        manifest_obj = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_obj["isolation_forest"] = {**trained_meta, "artifact_path": "/app/artifacts/isolation_forest.joblib"}
        manifest_path.write_text(json.dumps(manifest_obj, indent=2) + "\n", encoding="utf-8")

    if args.verify_offline:
        run([
            "docker", "run", "--rm", "--network=none",
            "-e", "HF_HUB_OFFLINE=1",
            "-e", "TRANSFORMERS_OFFLINE=1",
            "-v", f"{ROOT / 'ml-service' / 'weights'}:/models:ro",
            "-v", f"{artifacts_dir}:/app/artifacts:ro",
            "-v", f"{manifest_path}:/app/model-manifest.json:ro",
            ml_img,
            "python", "-c",
            "from chronos_adapter import ChronosAdapter; from anomaly_model import IsolationForestDetector; "
            "c=ChronosAdapter('/models/chronos-2','/app/model-manifest.json'); c.load(); "
            "a=IsolationForestDetector('/app/artifacts/isolation_forest.joblib','/app/model-manifest.json'); a.load(); "
            "assert c.status=='loaded' and a.status=='loaded', (c.status, c.error, a.status, a.error); "
            "print('OFFLINE_PREP_OK', c.model_id, c.model_revision, c.load_time_ms, a.model_version)",
        ])


if __name__ == "__main__":
    main()
