"""Chronological rolling-origin evaluation comparing Rolling Mean, Seasonal Baseline, and pretrained Chronos-2."""
import argparse
from collections import defaultdict
import csv
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, pstdev
import sys
import time

sys.path.insert(0, "/app")
from chronos_adapter import ChronosAdapter  # noqa: E402


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


def pinball(y: float, q: float, level: float) -> float:
    diff = y - q
    return level * diff if diff >= 0 else (level - 1.0) * diff


def aggregate_metrics(rows: list[dict]) -> dict:
    if not rows:
        return {"points": 0}
    errs = [abs(r["p50"] - r["actual"]) for r in rows]
    sq_errs = [(r["p50"] - r["actual"]) ** 2 for r in rows]
    sum_y = sum(r["actual"] for r in rows)
    pb10 = [pinball(r["actual"], r["p10"], 0.1) for r in rows if r["p10"] is not None]
    pb50 = [pinball(r["actual"], r["p50"], 0.5) for r in rows]
    pb90 = [pinball(r["actual"], r["p90"], 0.9) for r in rows if r["p90"] is not None]
    cov = [1.0 if r["p10"] <= r["actual"] <= r["p90"] else 0.0 for r in rows if r["p10"] is not None and r["p90"] is not None]
    return {
        "points": len(rows),
        "mae": round(mean(errs), 3),
        "rmse": round(math.sqrt(mean(sq_errs)), 3),
        "wape": round(sum(errs) / max(1e-6, sum_y), 4),
        "pinball_p10": round(mean(pb10), 3) if pb10 else None,
        "pinball_p50": round(mean(pb50), 3),
        "pinball_p90": round(mean(pb90), 3) if pb90 else None,
        "p10_p90_coverage": round(mean(cov), 4) if cov else None,
    }


def evaluate_dataset(csv_path: Path, adapter: ChronosAdapter, horizons: list[int], origins: list[int], raw_out: list[dict]) -> dict:
    series: dict[tuple[str, str], list[dict]] = defaultdict(list)
    with csv_path.open("r", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            series[(row["station_id"], row["fuel_type"])].append(row)
    for k in series:
        series[k].sort(key=lambda r: int(r["tick"]))

    per_model_rows: dict[tuple[str, int], list[dict]] = defaultdict(list)
    timings: dict[str, list[float]] = defaultdict(list)

    max_h = max(horizons)
    for origin in origins:
        series_inputs = []
        truth_by_key: dict[tuple[str, str], list[float]] = {}
        for (sid, fuel), rows in sorted(series.items()):
            if origin < 16 or origin + max_h > len(rows):
                continue
            ctx = rows[:origin]
            last_row = ctx[-1]
            profile = last_row["demand_profile"]
            r_factor = float(last_row["region_factor"])
            issue_mult = float(last_row["demand_multiplier"] or 1.0)
            last_ts = datetime.fromisoformat(last_row["sim_time"].replace("Z", "+00:00"))
            # Use ONLY information known at origin (causal covariates; future hours deterministic, current multiplier decayed or constant only over known window if active event not future)
            hist_items = []
            for r in ctx:
                r_ts = datetime.fromisoformat(r["sim_time"].replace("Z", "+00:00"))
                hist_items.append({
                    "sim_time": r["sim_time"],
                    "demand_liters": float(r["demand_liters"]),
                    "hour_factor": hour_factor(profile, r_ts.hour),
                    "known_spike_multiplier": float(r["demand_multiplier"] or 1.0),
                    "region_factor": r_factor,
                })
            fut_covs = []
            for step_i in range(max_h):
                f_ts = last_ts + timedelta(minutes=15 * (step_i + 1))
                # At forecast issue time without peeking future events, use known multiplier at issue time for short horizon or 1.0
                fut_covs.append({
                    "sim_time": f_ts.isoformat(),
                    "hour_factor": hour_factor(profile, f_ts.hour),
                    "known_spike_multiplier": issue_mult if step_i < 8 else 1.0,
                    "region_factor": r_factor,
                })
            series_inputs.append({
                "station_id": sid, "fuel_type": fuel,
                "history": hist_items, "future_covariates": fut_covs,
            })
            truth_by_key[(sid, fuel)] = [float(rows[origin + i]["demand_liters"]) for i in range(max_h)]

            # 1) Naive 8-tick rolling mean baseline
            t0 = time.perf_counter()
            recent_8 = [float(r["demand_liters"]) for r in ctx[-8:]]
            rm = mean(recent_8)
            r_std = pstdev(recent_8) if len(recent_8) > 1 else max(1.0, 0.1 * rm)
            timings["rolling_mean"].append((time.perf_counter() - t0) * 1000.0)

            # 2) Seasonal / hour-factor normalized baseline
            t1 = time.perf_counter()
            norm_vals = [
                h["demand_liters"] / max(0.01, h["hour_factor"] * h["known_spike_multiplier"])
                for h in hist_items[-16:]
            ]
            base_rate = mean(norm_vals)
            norm_std = pstdev(norm_vals) if len(norm_vals) > 1 else max(1.0, 0.1 * base_rate)
            seas_preds = []
            for step_i in range(max_h):
                cov = fut_covs[step_i]
                scale = cov["hour_factor"] * cov["known_spike_multiplier"]
                p50 = base_rate * scale
                band = 1.2816 * norm_std * scale
                seas_preds.append((max(0.0, p50 - band), p50, p50 + band))
            timings["seasonal"].append((time.perf_counter() - t1) * 1000.0)

            for h_len in horizons:
                for step_i in range(h_len):
                    y = truth_by_key[(sid, fuel)][step_i]
                    rm_p10 = max(0.0, rm - 1.2816 * r_std)
                    rm_p90 = rm + 1.2816 * r_std
                    per_model_rows[("rolling_mean", h_len)].append({"p10": rm_p10, "p50": rm, "p90": rm_p90, "actual": y})
                    sp10, sp50, sp90 = seas_preds[step_i]
                    per_model_rows[("seasonal", h_len)].append({"p10": sp10, "p50": sp50, "p90": sp90, "actual": y, "station_id": sid, "fuel_type": fuel})
                    raw_out.append({"dataset": csv_path.stem, "origin": origin, "horizon": h_len, "step": step_i, "station_id": sid, "fuel_type": fuel, "model": "seasonal", "p10": round(sp10, 3), "p50": round(sp50, 3), "p90": round(sp90, 3), "actual": y})

        # 3) Real Chronos-2 batched inference across all 12 series
        t2 = time.perf_counter()
        c_res = adapter.predict(
            generation_id=csv_path.stem,
            as_of_tick=origin,
            tick_minutes=15,
            horizon_ticks=max_h,
            series_inputs=series_inputs,
        )
        timings["chronos2_12series"].append((time.perf_counter() - t2) * 1000.0)
        for s_item in c_res["series"]:
            k = (s_item["station_id"], s_item["fuel_type"])
            for h_len in horizons:
                for step_i in range(h_len):
                    pt = s_item["points"][step_i]
                    y = truth_by_key[k][step_i]
                    per_model_rows[("chronos2", h_len)].append({"p10": pt["p10"], "p50": pt["p50"], "p90": pt["p90"], "actual": y, "station_id": k[0], "fuel_type": k[1]})
                    raw_out.append({"dataset": csv_path.stem, "origin": origin, "horizon": h_len, "step": step_i, "station_id": k[0], "fuel_type": k[1], "model": "chronos2", "p10": pt["p10"], "p50": pt["p50"], "p90": pt["p90"], "actual": y})

    summary = {"dataset": csv_path.stem, "origins": origins, "horizons": {}}
    for h_len in horizons:
        summary["horizons"][str(h_len)] = {
            m: aggregate_metrics(per_model_rows[(m, h_len)])
            for m in ("rolling_mean", "seasonal", "chronos2")
        }
    # Fuel breakdown at max horizon (96 or 32)
    h_ref = horizons[-1]
    by_fuel = {}
    for m in ("seasonal", "chronos2"):
        by_fuel[m] = {}
        for fuel in ("DIESEL", "PETROL", "OCTANE"):
            sub = [r for r in per_model_rows[(m, h_ref)] if r.get("fuel_type") == fuel]
            by_fuel[m][fuel] = aggregate_metrics(sub)
    summary["by_fuel_max_horizon"] = by_fuel
    c_times = sorted(timings["chronos2_12series"])
    summary["chronos2_latency_ms"] = {
        "runs": len(c_times),
        "p50": round(c_times[len(c_times) // 2], 2) if c_times else None,
        "p95": round(c_times[max(0, int(0.95 * len(c_times)) - 1)], 2) if c_times else None,
    }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--datasets-dir", default="/datasets")
    parser.add_argument("--out-dir", default="/eval-out")
    args = parser.parse_args()

    adapter = ChronosAdapter("/models/chronos-2", "/app/model-manifest.json")
    adapter.load()
    assert adapter.status == "loaded", f"Chronos load failed: {adapter.error}"

    horizons = [4, 16, 32, 96]
    ds_dir = Path(args.datasets_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_rows: list[dict] = []

    results = {
        "model_id": adapter.model_id,
        "model_revision": adapter.model_revision,
        "load_time_ms": adapter.load_time_ms,
        "datasets": [
            evaluate_dataset(ds_dir / "normal_baseline.csv", adapter, horizons, [48, 96, 144, 190], raw_rows),
            evaluate_dataset(ds_dir / "crisis_dev_mix.csv", adapter, horizons, [36, 48, 72, 96], raw_rows),
            evaluate_dataset(ds_dir / "crisis_heldout_mix.csv", adapter, horizons, [36, 44, 72, 96], raw_rows),
        ],
    }

    (out_dir / "forecast_eval.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    if raw_rows:
        with (out_dir / "forecast_predictions.csv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(raw_rows[0].keys()))
            writer.writeheader()
            writer.writerows(raw_rows)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
