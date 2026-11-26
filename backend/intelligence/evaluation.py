import json
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_curated_or_runtime(
    filename: str,
    primary_dir: Path,
    evidence_dir: Path,
) -> tuple[dict[str, Any] | None, str | None]:
    primary = _load_json(primary_dir / filename)
    if primary is not None:
        return primary, "runtime_artifacts"
    curated = _load_json(evidence_dir / filename)
    if curated is not None:
        return curated, "curated_evidence"
    return None, None


def _summarize_resilience(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not raw:
        return None
    return {
        "total": raw.get("total", 0),
        "passed": raw.get("passed", 0),
        "failed": raw.get("failed", 0),
    }


def _summarize_benchmark(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not raw:
        return None
    scenarios = raw.get("scenarios") or {}
    compact: dict[str, Any] = {}
    for key, val in scenarios.items():
        if not isinstance(val, dict):
            continue
        compact[key] = {
            "rps": val.get("rps"),
            "duration_s": val.get("duration_s"),
            "error_count": val.get("error_count", val.get("errors")),
            "latency_ms": val.get("latency_ms"),
        }
    prom = raw.get("prometheus_and_observability") or {}
    return {
        "scenarios": compact,
        "prometheus_target_health": prom.get("target_health"),
    }


def load_evaluation_summary(
    *,
    eval_dir: str = "artifacts/evaluation",
    manifest_path: str = "ml-service/model-manifest.json",
    evidence_dir: str = "docs/evidence",
    load_dir: str = "artifacts/load",
) -> dict[str, Any]:
    """Read real measured evaluation, model manifest, and policy artifacts (never fabricate values)."""
    edir = Path(eval_dir)
    ev_dir = Path(evidence_dir)
    ldir = Path(load_dir)

    forecast_eval, forecast_src = _load_curated_or_runtime("forecast_eval.json", edir, ev_dir)
    policy_eval, policy_src = _load_curated_or_runtime("policy_comparison.json", edir, ev_dir)
    resilience_raw, _ = _load_curated_or_runtime("resilience_matrix.json", edir, ev_dir)
    ml_verification, _ = _load_curated_or_runtime("ml_verification.json", edir, ev_dir)
    benchmark_raw, _ = _load_curated_or_runtime("benchmark_matrix.json", ldir, ev_dir)
    manifest = _load_json(Path(manifest_path))

    fp = _load_json(ev_dir / "forecast-policy.json")
    rp = _load_json(ev_dir / "reorder-policy.json")
    historical_traces = [p for p in (fp, rp) if p is not None]

    has_measured_policy = bool(policy_eval and policy_eval.get("aggregate"))
    has_measured_eval = bool(forecast_eval or has_measured_policy)
    artifact_source = (
        policy_src
        or forecast_src
        or ("not_yet_measured" if not has_measured_eval else "unknown")
    )

    return {
        "forecast_evaluation": forecast_eval,
        "policy_comparison": policy_eval,
        "model_manifest": manifest,
        "resilience_summary": _summarize_resilience(resilience_raw),
        "benchmark_summary": _summarize_benchmark(benchmark_raw),
        "ml_verification": ml_verification,
        "historical_baseline_traces": historical_traces or None,
        "artifact_source": artifact_source if has_measured_eval else "not_yet_measured",
        "status": "measured" if has_measured_eval else "not_yet_measured",
    }
