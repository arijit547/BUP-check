from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import joblib
import numpy as np

FEATURE_NAMES = [
    "normalized_demand_ratio",
    "rolling_residual_z",
    "inventory_ratio",
    "has_inventory_residual",
    "inventory_balance_residual",
    "sin_hour",
    "cos_hour",
]


def hour_factor(profile: str, hour: int) -> float:
    if profile == "industrial":
        return 1.55 if 6 <= hour <= 17 else 0.45
    if profile == "highway":
        return 1.35 if (6 <= hour <= 9 or 16 <= hour <= 20) else 0.75
    if profile == "urban_high":
        return 1.45 if (7 <= hour <= 9 or 16 <= hour <= 20) else 0.70
    if profile == "regional":
        return 1.25 if 7 <= hour <= 20 else 0.65
    return 1.0


def build_feature_vector(
    *,
    demand_liters: float,
    sim_time_iso: str,
    profile: str,
    known_spike_multiplier: float,
    baseline_normalized: float,
    rolling_expected: float,
    rolling_std: float,
    inventory_ratio: float,
    inventory_balance_residual: float | None = None,
) -> list[float]:
    dt = datetime.fromisoformat(sim_time_iso.replace("Z", "+00:00"))
    h_f = hour_factor(profile, dt.hour)
    denom = max(0.01, h_f * max(0.01, known_spike_multiplier))
    norm_val = demand_liters / denom
    norm_ratio = norm_val / max(1.0, baseline_normalized)
    resid_z = (demand_liters - rolling_expected) / max(1.0, rolling_std)
    angle = 2.0 * math.pi * ((dt.hour * 60 + dt.minute) / 1440.0)
    has_inv_res = 1.0 if inventory_balance_residual is not None else 0.0
    inv_res = float(inventory_balance_residual) if inventory_balance_residual is not None else 0.0
    return [
        float(norm_ratio),
        float(resid_z),
        float(inventory_ratio),
        has_inv_res,
        inv_res,
        math.sin(angle),
        math.cos(angle),
    ]


class IsolationForestDetector:
    def __init__(self, artifact_path: str, manifest_path: str | None = None):
        self.artifact_path = Path(artifact_path)
        self.manifest_path = Path(manifest_path) if manifest_path else None
        self.bundle: dict[str, Any] | None = None
        self.status: str = "unloaded"
        self.error: str | None = None
        self.model_version: str = "iforest-unloaded"

    def load(self) -> None:
        if not self.artifact_path.exists():
            self.status = "missing"
            self.error = f"Isolation Forest artifact not found at {self.artifact_path}"
            return
        if self.manifest_path and self.manifest_path.exists():
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            expected = manifest.get("isolation_forest", {}).get("artifact_sha256")
            if expected:
                actual = hashlib.sha256(self.artifact_path.read_bytes()).hexdigest()
                if actual != expected:
                    self.status = "corrupt"
                    self.error = f"Isolation Forest checksum mismatch: expected {expected}, got {actual}"
                    return
        try:
            self.bundle = joblib.load(self.artifact_path)
            self.model_version = str(self.bundle.get("version", "iforest-v1"))
            self.status = "loaded"
            self.error = None
        except Exception as exc:
            self.bundle = None
            self.status = "error"
            self.error = str(exc)

    def score_batch(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.bundle is None or self.status != "loaded":
            raise RuntimeError(self.error or "Isolation Forest artifact not loaded")
        if not items:
            return []
        pipeline = self.bundle["pipeline"]
        threshold = float(self.bundle["decision_threshold"])
        matrix = np.asarray(
            [
                build_feature_vector(
                    demand_liters=float(item["demand_liters"]),
                    sim_time_iso=item["sim_time"],
                    profile=item.get("profile", "urban_high"),
                    known_spike_multiplier=float(item.get("known_spike_multiplier", 1.0)),
                    baseline_normalized=float(item["baseline_normalized"]),
                    rolling_expected=float(item["rolling_expected"]),
                    rolling_std=float(item.get("rolling_std", 5.0)),
                    inventory_ratio=float(item.get("inventory_ratio", 0.5)),
                    inventory_balance_residual=item.get("inventory_balance_residual"),
                )
                for item in items
            ],
            dtype=float,
        )
        scores = pipeline.decision_function(matrix)
        outputs: list[dict[str, Any]] = []
        for item, score, vec in zip(items, scores, matrix):
            val = float(score)
            is_anom = val < threshold
            outputs.append({
                "station_id": item["station_id"],
                "fuel_type": item["fuel_type"],
                "tick": item["tick"],
                "is_anomaly": bool(is_anom),
                "anomaly_score": round(val, 4),
                "threshold": round(threshold, 4),
                "normalized_demand_ratio": round(float(vec[0]), 3),
                "rolling_residual_z": round(float(vec[1]), 3),
            })
        return outputs
