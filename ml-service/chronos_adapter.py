from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any

import numpy as np
import pandas as pd


class ChronosAdapter:
    """Offline-capable CPU adapter for pretrained amazon/chronos-2 zero-shot forecasting."""

    def __init__(self, model_dir: str, manifest_path: str | None = None):
        self.model_dir = Path(model_dir)
        self.manifest_path = Path(manifest_path) if manifest_path else None
        self.pipeline: Any = None
        self.status: str = "unloaded"
        self.error: str | None = None
        self.model_id: str = "amazon/chronos-2"
        self.model_revision: str = "unknown"
        self.load_time_ms: float | None = None
        self.last_inference_ms: float | None = None
        self.last_inference_at: str | None = None

    def load(self) -> None:
        started = time.perf_counter()
        if not self.model_dir.exists() or not (self.model_dir / "config.json").exists():
            self.status = "missing"
            self.error = f"Chronos-2 local model files not found at {self.model_dir}"
            return
        if self.manifest_path and self.manifest_path.exists():
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            cmeta = manifest.get("chronos2", {})
            self.model_id = cmeta.get("model_id", self.model_id)
            self.model_revision = cmeta.get("revision", self.model_revision)
            for rel, expected_sha in cmeta.get("files_sha256", {}).items():
                fpath = self.model_dir / rel
                if not fpath.exists():
                    self.status = "corrupt"
                    self.error = f"Missing model artifact file: {rel}"
                    return
                actual = hashlib.sha256(fpath.read_bytes()).hexdigest()
                if actual != expected_sha:
                    self.status = "corrupt"
                    self.error = f"Checksum mismatch for {rel}: expected {expected_sha}, got {actual}"
                    return

        try:
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            from chronos import Chronos2Pipeline

            self.pipeline = Chronos2Pipeline.from_pretrained(
                str(self.model_dir),
                device_map="cpu",
                local_files_only=True,
                trust_remote_code=False,
            )
            self.load_time_ms = round((time.perf_counter() - started) * 1000.0, 2)
            self.status = "loaded"
            self.error = None
        except Exception as exc:
            self.pipeline = None
            self.status = "error"
            self.error = str(exc)

    def predict(
        self,
        *,
        generation_id: str,
        as_of_tick: int,
        tick_minutes: int,
        horizon_ticks: int,
        series_inputs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if self.pipeline is None or self.status != "loaded":
            raise RuntimeError(self.error or f"Chronos-2 model not ready (status={self.status})")

        started = time.perf_counter()
        ctx_rows: list[dict[str, Any]] = []
        fut_rows: list[dict[str, Any]] = []
        id_map: dict[str, tuple[str, str]] = {}
        expected_fut_times: dict[str, list[str]] = {}

        use_covariates = all(
            len(item.get("future_covariates", [])) == horizon_ticks and len(item.get("history", [])) > 0
            for item in series_inputs
        )

        for item in series_inputs:
            sid = item["station_id"]
            fuel = item["fuel_type"]
            series_id = f"{sid}__{fuel}"
            id_map[series_id] = (sid, fuel)
            for row in item["history"]:
                ts = pd.Timestamp(row["sim_time"]).tz_convert("UTC") if pd.Timestamp(row["sim_time"]).tzinfo else pd.Timestamp(row["sim_time"], tz="UTC")
                rec: dict[str, Any] = {
                    "id": series_id,
                    "timestamp": ts,
                    "target": float(row["demand_liters"]) if row["demand_liters"] is not None else np.nan,
                }
                if use_covariates:
                    rec["hour_factor"] = float(row.get("hour_factor", 1.0))
                    rec["known_spike_multiplier"] = float(row.get("known_spike_multiplier", 1.0))
                    rec["region_factor"] = float(row.get("region_factor", 1.0))
                ctx_rows.append(rec)

            fut_times: list[str] = []
            for cov in item.get("future_covariates", []):
                ts = pd.Timestamp(cov["sim_time"]).tz_convert("UTC") if pd.Timestamp(cov["sim_time"]).tzinfo else pd.Timestamp(cov["sim_time"], tz="UTC")
                fut_times.append(ts.isoformat())
                if use_covariates:
                    fut_rows.append({
                        "id": series_id,
                        "timestamp": ts,
                        "hour_factor": float(cov.get("hour_factor", 1.0)),
                        "known_spike_multiplier": float(cov.get("known_spike_multiplier", 1.0)),
                        "region_factor": float(cov.get("region_factor", 1.0)),
                    })
            expected_fut_times[series_id] = fut_times

        ctx_df = pd.DataFrame(ctx_rows)
        ctx_df["timestamp"] = pd.to_datetime(ctx_df["timestamp"], utc=True).dt.tz_localize(None).astype("datetime64[ns]")
        ctx_df = ctx_df.sort_values(["id", "timestamp"]).reset_index(drop=True)
        fut_df = None
        if use_covariates and fut_rows:
            fut_df = pd.DataFrame(fut_rows)
            fut_df["timestamp"] = pd.to_datetime(fut_df["timestamp"], utc=True).dt.tz_localize(None).astype("datetime64[ns]")
            fut_df = fut_df.sort_values(["id", "timestamp"]).reset_index(drop=True)

        freq_str = f"{int(tick_minutes)}min"
        pred_df = self.pipeline.predict_df(
            ctx_df,
            future_df=fut_df,
            id_column="id",
            timestamp_column="timestamp",
            target="target",
            prediction_length=horizon_ticks,
            quantile_levels=[0.1, 0.5, 0.9],
            batch_size=32,
            freq=freq_str,
        )

        results: list[dict[str, Any]] = []
        clipped_count = 0
        reordered_count = 0
        for series_id, (sid, fuel) in id_map.items():
            sub = pred_df[pred_df["id"] == series_id].sort_values("timestamp")
            if len(sub) != horizon_ticks:
                raise ValueError(f"Expected {horizon_ticks} rows for {series_id}, got {len(sub)}")
            points = []
            fut_ts_list = expected_fut_times.get(series_id, [])
            for idx, (_, row) in enumerate(sub.iterrows()):
                q10 = float(row["0.1"]) if "0.1" in row else float(row["predictions"])
                q50 = float(row["0.5"]) if "0.5" in row else float(row["predictions"])
                q90 = float(row["0.9"]) if "0.9" in row else float(row["predictions"])
                if any(not np.isfinite(v) for v in (q10, q50, q90)):
                    raise ValueError(f"Non-finite Chronos output for {series_id} at offset {idx}")
                raw_vals = [q10, q50, q90]
                clipped = [max(0.0, v) for v in raw_vals]
                if clipped != raw_vals:
                    clipped_count += 1
                ordered = sorted(clipped)
                if ordered != clipped:
                    reordered_count += 1
                warnings = []
                if clipped != raw_vals:
                    warnings.append("clipped_nonnegative")
                if ordered != clipped:
                    warnings.append("monotone_reordered")
                points.append({
                    "tick": as_of_tick + idx,
                    "sim_time": fut_ts_list[idx] if idx < len(fut_ts_list) else pd.Timestamp(row["timestamp"]).isoformat(),
                    "p10": round(ordered[0], 3),
                    "p50": round(ordered[1], 3),
                    "p90": round(ordered[2], 3),
                    "quality_warnings": warnings,
                })
            results.append({"station_id": sid, "fuel_type": fuel, "points": points})

        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        self.last_inference_ms = elapsed_ms
        self.last_inference_at = datetime.now(UTC).isoformat()
        quality_notes = []
        if clipped_count:
            quality_notes.append(f"clipped_negative_steps={clipped_count}")
        if reordered_count:
            quality_notes.append(f"reordered_quantile_steps={reordered_count}")

        return {
            "generation_id": generation_id,
            "as_of_tick": as_of_tick,
            "horizon_ticks": horizon_ticks,
            "model_id": self.model_id,
            "model_version": f"{self.model_id}@{self.model_revision[:12]}",
            "source": "CHRONOS2",
            "inference_ms": elapsed_ms,
            "covariates_used": use_covariates,
            "quality_warnings": quality_notes,
            "series": results,
        }
