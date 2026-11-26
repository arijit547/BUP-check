"""Train and evaluate Isolation Forest solely on clean normal baseline simulator traces."""
import argparse
from collections import defaultdict
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
from statistics import mean, pstdev
import sys

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, str(Path(__file__).resolve().parent))
from anomaly_model import FEATURE_NAMES, build_feature_vector, hour_factor  # noqa: E402


def build_rows_from_csv(
    csv_path: Path,
    *,
    exclude_crises: bool = True,
    assume_unannounced_spikes: bool = True,
) -> tuple[np.ndarray, list[dict]]:
    series_hist: dict[tuple[str, str], list[dict]] = defaultdict(list)
    with csv_path.open("r", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key = (row["station_id"], row["fuel_type"])
            series_hist[key].append(row)

    features: list[list[float]] = []
    meta: list[dict] = []
    for (station_id, fuel_type), rows in sorted(series_hist.items()):
        rows.sort(key=lambda r: int(r["tick"]))
        norm_window: list[float] = []
        for idx, row in enumerate(rows):
            demand = float(row["demand_liters"])
            sim_time = row["sim_time"]
            profile = row["demand_profile"]
            mult = float(row["demand_multiplier"] or 1.0)
            events = row["active_event_types"]
            inv = float(row["inventory_before_liters"]) if row["inventory_before_liters"] != "" else None
            cap = float(row["capacity_liters"]) if row["capacity_liters"] != "" else None
            inv_ratio = (inv / cap) if (inv is not None and cap and cap > 0) else 0.5
            dt = datetime.fromisoformat(sim_time.replace("Z", "+00:00"))
            hf = hour_factor(profile, dt.hour)

            if len(norm_window) >= 8:
                base_norm = mean(norm_window[-16:])
                used_mult = 1.0 if assume_unannounced_spikes else mult
                expected = base_norm * hf * used_mult
                std_hist = [v * hf * used_mult for v in norm_window[-16:]]
                r_std = pstdev(std_hist) if len(std_hist) > 1 else max(1.0, expected * 0.1)

                is_crisis = bool(events)
                if not (exclude_crises and is_crisis):
                    inv_res = None
                    if idx > 0 and rows[idx - 1]["inventory_after_liters"] != "" and inv is not None:
                        prev_after = float(rows[idx - 1]["inventory_after_liters"])
                        inv_res = (inv - prev_after) / max(1.0, cap or 1.0)

                    vec = build_feature_vector(
                        demand_liters=demand,
                        sim_time_iso=sim_time,
                        profile=profile,
                        known_spike_multiplier=used_mult,
                        baseline_normalized=base_norm,
                        rolling_expected=expected,
                        rolling_std=max(1.0, r_std),
                        inventory_ratio=inv_ratio,
                        inventory_balance_residual=inv_res,
                    )
                    # Only label rows where demand telemetry for this series actually shifted (multiplier > 1.2)
                    demand_anomaly = ("demand_spike" in events) and (mult > 1.2)
                    features.append(vec)
                    meta.append({
                        "tick": int(row["tick"]),
                        "station_id": station_id,
                        "fuel_type": fuel_type,
                        "events": events,
                        "demand": demand,
                        "expected": expected,
                        "demand_anomaly": demand_anomaly,
                    })

            # Only admit un-spiked observations into clean normal rolling reference when unannounced mode
            if not (assume_unannounced_spikes and mult > 1.2):
                norm_window.append(demand / max(0.01, hf * mult))

    return np.asarray(features, dtype=float), meta


def eval_split(pipeline: Pipeline, threshold: float, X: np.ndarray, meta: list[dict]) -> dict:
    scores = pipeline.decision_function(X)
    preds = [float(s) < threshold for s in scores]
    truth = [bool(m["demand_anomaly"]) for m in meta]
    tp = sum(1 for p, y in zip(preds, truth) if p and y)
    fp = sum(1 for p, y in zip(preds, truth) if p and not y)
    fn = sum(1 for p, y in zip(preds, truth) if not p and y)
    tn = sum(1 for p, y in zip(preds, truth) if not p and not y)
    precision = round(tp / (tp + fp), 4) if (tp + fp) else 0.0
    recall = round(tp / (tp + fn), 4) if (tp + fn) else 0.0
    f1 = round((2 * precision * recall) / (precision + recall), 4) if (precision + recall) else 0.0
    fpr = round(fp / (fp + tn), 4) if (fp + tn) else 0.0

    # Mean detection delay from first anomalous tick in validation/heldout scenario
    anom_ticks = [m["tick"] for m, y in zip(meta, truth) if y]
    det_ticks = [m["tick"] for m, p, y in zip(meta, preds, truth) if p and y]
    delay = (min(det_ticks) - min(anom_ticks)) if (anom_ticks and det_ticks) else None
    return {
        "samples": len(meta), "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": precision, "recall": recall, "f1": f1, "fpr": fpr,
        "detection_delay_ticks": delay,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--datasets-dir", default="artifacts/datasets")
    parser.add_argument("--out-dir", default="ml-service/artifacts")
    parser.add_argument("--manifest", default="ml-service/model-manifest.json")
    args = parser.parse_args()

    ds_dir = Path(args.datasets_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    X_train, train_meta = build_rows_from_csv(ds_dir / "normal_baseline.csv", exclude_crises=True, assume_unannounced_spikes=True)
    X_val, val_meta = build_rows_from_csv(ds_dir / "crisis_dev_mix.csv", exclude_crises=False, assume_unannounced_spikes=True)
    X_test, test_meta = build_rows_from_csv(ds_dir / "crisis_heldout_mix.csv", exclude_crises=False, assume_unannounced_spikes=True)

    best_val = None
    for contam in (0.005, 0.01, 0.015, 0.02):
        pipe = Pipeline([
            ("scaler", StandardScaler()),
            ("iforest", IsolationForest(n_estimators=200, contamination=contam, random_state=42)),
        ])
        pipe.fit(X_train)
        train_scores = pipe.decision_function(X_train)
        for q in (0.0, 0.005, 0.01):
            thresh = float(np.quantile(train_scores, q)) if q > 0 else 0.0
            metrics = eval_split(pipe, thresh, X_val, val_meta)
            key = (metrics["f1"], -metrics["fpr"])
            if best_val is None or key > best_val[0]:
                best_val = (key, contam, thresh, pipe, metrics)

    _, contam, thresh, pipe, val_metrics = best_val
    train_false_positives = eval_split(pipe, thresh, X_train, train_meta)
    heldout_metrics = eval_split(pipe, thresh, X_test, test_meta)
    # Also evaluate false positive rate on normal portion after accounting for known events
    X_val_known, val_known_meta = build_rows_from_csv(ds_dir / "crisis_dev_mix.csv", exclude_crises=False, assume_unannounced_spikes=False)
    val_known_fpr = eval_split(pipe, thresh, X_val_known, val_known_meta)["fpr"]

    version = "iforest-bup-v1.0.0"
    artifact_path = out_dir / "isolation_forest.joblib"
    joblib.dump({
        "version": version,
        "features": FEATURE_NAMES,
        "contamination": contam,
        "decision_threshold": thresh,
        "pipeline": pipe,
        "trained_rows": int(len(X_train)),
    }, artifact_path)

    artifact_sha = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    summary = {
        "version": version,
        "artifact_path": str(artifact_path),
        "artifact_sha256": artifact_sha,
        "features": FEATURE_NAMES,
        "contamination": contam,
        "decision_threshold": round(thresh, 6),
        "train_normal_rows": int(len(X_train)),
        "train_split_fpr": train_false_positives["fpr"],
        "validation_unannounced_metrics": val_metrics,
        "heldout_unannounced_metrics": heldout_metrics,
        "validation_residual_fpr_after_known_mult": val_known_fpr,
        "note": (
            "Fitted exclusively on crisis-free normal baseline traces (normal_baseline.csv). Evaluated on unannounced "
            "demand_spike ticks across station/fuel series; event-level labels (outage/route/delay) that do not alter "
            "station demand_liters are handled by deterministic domain rules."
        ),
    }
    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    manifest["isolation_forest"] = summary
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
