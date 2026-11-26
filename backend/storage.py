from contextlib import closing
from datetime import UTC, datetime
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from intelligence.contracts import (
    Anomaly,
    DataQualityReport,
    DemandObservation,
    ForecastsResponse,
    GenerationInfo,
    PlanPreviewSummary,
)
from schemas import Snapshot

SCHEMA_VERSION = 4


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class PlatformStore:
    """Transactional SQLite store with versioned migrations for approvals, generations, history, forecasts, and incidents."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _migrate(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
            """)
            applied = {
                row["version"] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
            }

            if 1 not in applied:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS approvals (
                        intent_id TEXT PRIMARY KEY,
                        payload TEXT NOT NULL,
                        state TEXT NOT NULL,
                        message TEXT,
                        shipment TEXT
                    )
                """)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (1, ?)", (utc_now_iso(),)
                )

            if 2 not in applied:
                cols = {row["name"] for row in conn.execute("PRAGMA table_info(approvals)").fetchall()}
                for col, ddl in (
                    ("generation_id", "TEXT NOT NULL DEFAULT 'legacy'"),
                    ("plan_id", "TEXT"),
                    ("actor", "TEXT DEFAULT 'operator'"),
                    ("decision_rationale", "TEXT"),
                    ("created_at", "TEXT"),
                    ("updated_at", "TEXT"),
                ):
                    if col not in cols:
                        conn.execute(f"ALTER TABLE approvals ADD COLUMN {col} {ddl}")

                conn.execute("""
                    CREATE TABLE IF NOT EXISTS generations (
                        generation_id TEXT PRIMARY KEY,
                        sim_instance_id INTEGER,
                        scenario_id TEXT NOT NULL,
                        scenario_version TEXT NOT NULL,
                        seed INTEGER NOT NULL,
                        last_seen_tick INTEGER NOT NULL,
                        last_seen_sim_time TEXT NOT NULL,
                        status TEXT NOT NULL,
                        reason TEXT,
                        started_at TEXT NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS demand_observations (
                        generation_id TEXT NOT NULL,
                        station_id TEXT NOT NULL,
                        fuel_type TEXT NOT NULL,
                        tick INTEGER NOT NULL,
                        sim_time TEXT,
                        region_id TEXT,
                        demand_liters REAL NOT NULL,
                        served_liters REAL,
                        unmet_liters REAL,
                        observed_at TEXT NOT NULL,
                        quality_flag TEXT NOT NULL DEFAULT 'OK',
                        PRIMARY KEY (generation_id, station_id, fuel_type, tick)
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_obs_gen_tick ON demand_observations (generation_id, tick)"
                )
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS snapshot_history (
                        generation_id TEXT NOT NULL,
                        tick INTEGER NOT NULL,
                        sim_time TEXT NOT NULL,
                        snapshot_json TEXT NOT NULL,
                        events_json TEXT NOT NULL,
                        arrivals_json TEXT NOT NULL,
                        metrics_json TEXT,
                        captured_at TEXT NOT NULL,
                        PRIMARY KEY (generation_id, tick)
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS forecast_runs (
                        run_id TEXT PRIMARY KEY,
                        generation_id TEXT NOT NULL,
                        as_of_tick INTEGER NOT NULL,
                        horizon_ticks INTEGER NOT NULL,
                        source TEXT NOT NULL,
                        model_version TEXT NOT NULL,
                        fallback_reason TEXT,
                        feature_version TEXT NOT NULL,
                        config_hash TEXT NOT NULL,
                        issued_at TEXT NOT NULL,
                        latency_ms REAL,
                        payload_json TEXT NOT NULL
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_forecast_gen_tick ON forecast_runs (generation_id, as_of_tick)"
                )
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS forecast_points (
                        run_id TEXT NOT NULL,
                        generation_id TEXT NOT NULL,
                        station_id TEXT NOT NULL,
                        fuel_type TEXT NOT NULL,
                        issue_tick INTEGER NOT NULL,
                        target_tick INTEGER NOT NULL,
                        p10 REAL,
                        p50 REAL NOT NULL,
                        p90 REAL,
                        actual_demand REAL,
                        matched_at TEXT,
                        PRIMARY KEY (run_id, station_id, fuel_type, target_tick)
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_fp_match ON forecast_points (generation_id, station_id, fuel_type, target_tick)"
                )
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS incidents (
                        incident_id TEXT PRIMARY KEY,
                        generation_id TEXT NOT NULL,
                        dedup_key TEXT NOT NULL,
                        category TEXT NOT NULL,
                        entity_type TEXT NOT NULL,
                        entity_id TEXT NOT NULL,
                        fuel_type TEXT,
                        severity TEXT NOT NULL,
                        status TEXT NOT NULL,
                        source TEXT NOT NULL,
                        score REAL,
                        expected_value REAL,
                        observed_value REAL,
                        first_tick INTEGER NOT NULL,
                        last_tick INTEGER NOT NULL,
                        clear_streak INTEGER NOT NULL DEFAULT 0,
                        resolved_tick INTEGER,
                        summary TEXT NOT NULL,
                        evidence_json TEXT NOT NULL,
                        suggestion TEXT,
                        UNIQUE (generation_id, dedup_key)
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS plans (
                        plan_id TEXT PRIMARY KEY,
                        generation_id TEXT NOT NULL,
                        as_of_tick INTEGER NOT NULL,
                        policy_mode TEXT NOT NULL,
                        model_version TEXT NOT NULL,
                        solver_status TEXT NOT NULL,
                        config_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS evaluation_runs (
                        eval_id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        scope TEXT NOT NULL,
                        scenario_config_json TEXT NOT NULL,
                        summary_json TEXT NOT NULL
                    )
                """)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (2, ?)", (utc_now_iso(),)
                )

            if 3 not in applied:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_approvals_gen ON approvals (generation_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_incidents_gen_status ON incidents (generation_id, status)"
                )
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (3, ?)", (utc_now_iso(),)
                )

            if 4 not in applied:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS plan_approvals (
                        plan_id TEXT PRIMARY KEY,
                        generation_id TEXT NOT NULL,
                        as_of_tick INTEGER NOT NULL,
                        actor TEXT NOT NULL DEFAULT 'operator',
                        decision_rationale TEXT,
                        state TEXT NOT NULL,
                        stopped_reason TEXT,
                        requires_new_plan INTEGER NOT NULL DEFAULT 0,
                        total_shipments INTEGER NOT NULL,
                        confirmed_count INTEGER NOT NULL DEFAULT 0,
                        rejected_count INTEGER NOT NULL DEFAULT 0,
                        unknown_count INTEGER NOT NULL DEFAULT 0,
                        unsent_count INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS plan_approval_items (
                        plan_id TEXT NOT NULL,
                        item_index INTEGER NOT NULL,
                        generation_id TEXT NOT NULL,
                        intent_id TEXT NOT NULL,
                        eta_ticks INTEGER,
                        payload_json TEXT NOT NULL,
                        state TEXT NOT NULL,
                        message TEXT,
                        shipment_json TEXT,
                        allocation_id INTEGER,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (plan_id, item_index),
                        UNIQUE (intent_id)
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_plan_approvals_gen ON plan_approvals (generation_id, updated_at)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_plan_items_gen ON plan_approval_items (generation_id, plan_id)"
                )
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (4, ?)", (utc_now_iso(),)
                )

    def ping(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute("SELECT 1").fetchone()

    def get_active_generation(self) -> GenerationInfo | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM generations WHERE status IN ('ACTIVE', 'REBIND_REQUIRED') ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return self._to_generation(row)

    @staticmethod
    def _to_generation(row: sqlite3.Row) -> GenerationInfo:
        data = dict(row)
        return GenerationInfo(
            generation_id=data["generation_id"],
            sim_instance_id=data["sim_instance_id"],
            scenario_id=data["scenario_id"],
            scenario_version=data["scenario_version"],
            seed=data["seed"],
            last_seen_tick=data["last_seen_tick"],
            last_seen_sim_time=datetime.fromisoformat(data["last_seen_sim_time"]),
            status=data["status"],
            reason=data["reason"],
            started_at=datetime.fromisoformat(data["started_at"]),
        )

    def create_generation(
        self,
        *,
        sim_instance_id: int | None,
        scenario_id: str,
        scenario_version: str,
        seed: int,
        tick: int,
        sim_time: datetime,
        reason: str = "initial",
        status: str = "ACTIVE",
        supersede_previous: bool = True,
    ) -> GenerationInfo:
        gen_id = str(uuid4())
        now = utc_now_iso()
        sim_time_iso = sim_time.isoformat()
        with closing(self._connect()) as conn, conn:
            if supersede_previous:
                conn.execute(
                    "UPDATE generations SET status = 'SUPERSEDED', reason = COALESCE(reason, ?) WHERE status IN ('ACTIVE', 'REBIND_REQUIRED')",
                    (reason,),
                )
            conn.execute(
                """
                INSERT INTO generations (
                    generation_id, sim_instance_id, scenario_id, scenario_version, seed,
                    last_seen_tick, last_seen_sim_time, status, reason, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (gen_id, sim_instance_id, scenario_id, scenario_version, seed, tick, sim_time_iso, status, reason, now),
            )
            row = conn.execute("SELECT * FROM generations WHERE generation_id = ?", (gen_id,)).fetchone()
        return self._to_generation(row)

    def touch_generation(self, generation_id: str, tick: int, sim_time: datetime) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                UPDATE generations
                SET last_seen_tick = MAX(last_seen_tick, ?), last_seen_sim_time = ?
                WHERE generation_id = ? AND status = 'ACTIVE'
                """,
                (tick, sim_time.isoformat(), generation_id),
            )

    def rebind_generation(self, generation_id: str) -> GenerationInfo | None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE generations SET status = 'ACTIVE', reason = 'operator_rebind' WHERE generation_id = ?",
                (generation_id,),
            )
            row = conn.execute("SELECT * FROM generations WHERE generation_id = ?", (generation_id,)).fetchone()
        return self._to_generation(row) if row else None

    def upsert_observations(
        self,
        generation_id: str,
        observations: list[DemandObservation],
        station_regions: dict[str, str],
        tick_minutes: int,
        current_tick: int,
        current_sim_time: datetime,
    ) -> DataQualityReport:
        now = utc_now_iso()
        dup_ignored = 0
        future_rejected = 0
        inserted = 0
        with closing(self._connect()) as conn, conn:
            for item in observations:
                if item.tick >= current_tick and current_tick > 0:
                    future_rejected += 1
                    continue
                delta_min = (current_tick - item.tick) * tick_minutes
                obs_time = datetime.fromtimestamp(current_sim_time.timestamp() - delta_min * 60, tz=UTC).isoformat()
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO demand_observations (
                        generation_id, station_id, fuel_type, tick, sim_time, region_id,
                        demand_liters, served_liters, unmet_liters, observed_at, quality_flag
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OK')
                    """,
                    (
                        generation_id,
                        item.station_id,
                        item.fuel_type,
                        item.tick,
                        obs_time,
                        station_regions.get(item.station_id),
                        float(item.demand_liters),
                        float(item.served_liters) if item.served_liters is not None else None,
                        float(item.unmet_liters) if item.unmet_liters is not None else None,
                        now,
                    ),
                )
                if cur.rowcount == 0:
                    dup_ignored += 1
                else:
                    inserted += 1
                    # Mature any earlier forecasts targeting this (station, fuel, tick)
                    conn.execute(
                        """
                        UPDATE forecast_points
                        SET actual_demand = ?, matched_at = ?
                        WHERE generation_id = ? AND station_id = ? AND fuel_type = ? AND target_tick = ?
                          AND actual_demand IS NULL
                        """,
                        (float(item.demand_liters), now, generation_id, item.station_id, item.fuel_type, item.tick),
                    )
            stats = conn.execute(
                """
                SELECT COUNT(*) AS cnt, MIN(tick) AS min_t, MAX(tick) AS max_t
                FROM demand_observations WHERE generation_id = ?
                """,
                (generation_id,),
            ).fetchone()
            # Bound snapshot/observation retention for very long runs (>15,000 ticks -> keep recent 2500 ticks)
            conn.execute(
                """
                DELETE FROM demand_observations
                WHERE generation_id = ? AND tick < ? - 2500
                """,
                (generation_id, current_tick),
            )
        min_tick = stats["min_t"]
        max_tick = stats["max_t"]
        total_rows = int(stats["cnt"] or 0)
        series_count = max(1, len(station_regions) * 3)
        expected = ((max_tick - min_tick + 1) * series_count) if (min_tick is not None and max_tick is not None) else 0
        missing_ticks = max(0, (expected - total_rows) // series_count) if expected else 0
        return DataQualityReport(
            generation_id=generation_id,
            collected_rows=total_rows,
            expected_rows=expected,
            missing_ticks=missing_ticks,
            duplicate_rows_ignored=dup_ignored,
            future_ticks_rejected=future_rejected,
            min_tick=min_tick,
            max_tick=max_tick,
        )

    def load_observations(self, generation_id: str, limit_ticks: int = 400) -> list[DemandObservation]:
        with closing(self._connect()) as conn:
            max_row = conn.execute(
                "SELECT MAX(tick) AS m FROM demand_observations WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
            max_tick = max_row["m"] if max_row and max_row["m"] is not None else 0
            min_tick = max(0, int(max_tick) - limit_ticks + 1)
            rows = conn.execute(
                """
                SELECT station_id, fuel_type, tick, demand_liters, served_liters, unmet_liters
                FROM demand_observations
                WHERE generation_id = ? AND tick >= ?
                ORDER BY tick ASC, station_id ASC, fuel_type ASC
                """,
                (generation_id, min_tick),
            ).fetchall()
        return [
            DemandObservation(
                station_id=row["station_id"],
                fuel_type=row["fuel_type"],
                tick=row["tick"],
                demand_liters=row["demand_liters"],
                served_liters=row["served_liters"],
                unmet_liters=row["unmet_liters"],
            )
            for row in rows
        ]

    def record_snapshot(
        self,
        generation_id: str,
        snapshot: Snapshot,
        events: list[dict[str, Any]],
        arrivals: list[dict[str, Any]],
        metrics: dict[str, Any] | None,
    ) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO snapshot_history (
                    generation_id, tick, sim_time, snapshot_json, events_json, arrivals_json, metrics_json, captured_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generation_id,
                    snapshot.simulation.tick,
                    snapshot.simulation.sim_time.isoformat(),
                    snapshot.model_dump_json(),
                    json.dumps(events),
                    json.dumps(arrivals),
                    json.dumps(metrics) if metrics else None,
                    utc_now_iso(),
                ),
            )
            conn.execute(
                "DELETE FROM snapshot_history WHERE generation_id = ? AND tick < ? - 500",
                (generation_id, snapshot.simulation.tick),
            )

    def record_forecast_run(self, response: ForecastsResponse, latency_ms: float | None = None) -> str:
        raw = response.model_dump_json()
        cfg_hash = hashlib.sha256(
            f"{response.history_ticks}:{response.horizon_ticks}:{response.source}:{response.model_version}".encode()
        ).hexdigest()[:16]
        run_id = f"fc-{response.generation_id[:8]}-{response.as_of_tick}-{cfg_hash}"
        with closing(self._connect()) as conn, conn:
            exists = conn.execute("SELECT 1 FROM forecast_runs WHERE run_id = ?", (run_id,)).fetchone()
            if exists:
                return run_id
            conn.execute(
                """
                INSERT INTO forecast_runs (
                    run_id, generation_id, as_of_tick, horizon_ticks, source, model_version,
                    fallback_reason, feature_version, config_hash, issued_at, latency_ms, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    response.generation_id,
                    response.as_of_tick,
                    response.horizon_ticks,
                    response.source,
                    response.model_version,
                    response.fallback_reason,
                    response.feature_version,
                    cfg_hash,
                    response.forecast_generated_at.isoformat(),
                    latency_ms,
                    raw,
                ),
            )
            for series in response.series:
                for pt in series.forecast:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO forecast_points (
                            run_id, generation_id, station_id, fuel_type, issue_tick, target_tick, p10, p50, p90
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            response.generation_id,
                            series.station_id,
                            series.fuel_type,
                            response.as_of_tick,
                            pt.tick,
                            pt.p10,
                            pt.p50,
                            pt.p90,
                        ),
                    )
        return run_id

    def matured_forecast_accuracy(self, generation_id: str) -> dict[str, float | int | None]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT p10, p50, p90, actual_demand
                FROM forecast_points
                WHERE generation_id = ? AND actual_demand IS NOT NULL
                ORDER BY target_tick DESC LIMIT 1000
                """,
                (generation_id,),
            ).fetchall()
        if not rows:
            return {"matured_points": 0, "mae": None, "wape": None, "pinball_p50": None, "p10_p90_coverage": None}
        abs_errs = [abs(float(r["p50"]) - float(r["actual_demand"])) for r in rows]
        actual_sum = sum(float(r["actual_demand"]) for r in rows)
        cov_rows = [r for r in rows if r["p10"] is not None and r["p90"] is not None]
        coverage = None
        if cov_rows:
            hits = sum(1 for r in cov_rows if float(r["p10"]) <= float(r["actual_demand"]) <= float(r["p90"]))
            coverage = round(hits / len(cov_rows), 4)
        mae = round(sum(abs_errs) / len(abs_errs), 3)
        wape = round(sum(abs_errs) / actual_sum, 4) if actual_sum > 1e-6 else None
        pinball_50 = round(0.5 * sum(abs_errs) / len(abs_errs), 3)
        return {
            "matured_points": len(rows),
            "mae": mae,
            "wape": wape,
            "pinball_p50": pinball_50,
            "p10_p90_coverage": coverage,
        }

    def sync_incidents(self, generation_id: str, tick: int, active: list[Anomaly]) -> list[Anomaly]:
        """Persist incident lifecycle with hysteresis (requires 2 clear ticks to auto-resolve)."""
        active_by_key = {(item.dedup_key or item.id): item for item in active}
        result: list[Anomaly] = []
        with closing(self._connect()) as conn, conn:
            existing_rows = conn.execute(
                "SELECT * FROM incidents WHERE generation_id = ?", (generation_id,)
            ).fetchall()
            existing = {row["dedup_key"]: row for row in existing_rows}

            for dkey, item in active_by_key.items():
                prev = existing.get(dkey)
                first_tick = min(prev["first_tick"], tick) if prev else tick
                inc_id = prev["incident_id"] if prev else f"inc-{generation_id[:8]}-{dkey}"
                conn.execute(
                    """
                    INSERT INTO incidents (
                        incident_id, generation_id, dedup_key, category, entity_type, entity_id,
                        fuel_type, severity, status, source, score, expected_value, observed_value,
                        first_tick, last_tick, clear_streak, resolved_tick, summary, evidence_json, suggestion
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, ?)
                    ON CONFLICT(generation_id, dedup_key) DO UPDATE SET
                        severity = excluded.severity,
                        status = 'OPEN',
                        source = excluded.source,
                        score = excluded.score,
                        expected_value = excluded.expected_value,
                        observed_value = excluded.observed_value,
                        last_tick = excluded.last_tick,
                        clear_streak = 0,
                        resolved_tick = NULL,
                        summary = excluded.summary,
                        evidence_json = excluded.evidence_json,
                        suggestion = excluded.suggestion
                    """,
                    (
                        inc_id,
                        generation_id,
                        dkey,
                        item.category,
                        item.entity_type,
                        item.entity_id,
                        item.fuel_type,
                        item.severity,
                        item.source,
                        item.score,
                        item.expected_value,
                        item.observed_value,
                        first_tick,
                        tick,
                        item.summary,
                        json.dumps(item.evidence),
                        item.suggestion,
                    ),
                )
                result.append(
                    item.model_copy(update={"id": inc_id, "dedup_key": dkey, "status": "OPEN", "first_tick": first_tick, "last_tick": tick})
                )

            for dkey, prev in existing.items():
                if dkey in active_by_key:
                    continue
                if prev["status"] == "OPEN":
                    streak = int(prev["clear_streak"] or 0) + 1
                    # Debounce: require 2 clean observations or tick jump >= 2 to mark RESOLVED
                    if streak >= 2 or tick - int(prev["last_tick"]) >= 2:
                        conn.execute(
                            "UPDATE incidents SET status = 'RESOLVED', clear_streak = ?, resolved_tick = ? WHERE incident_id = ?",
                            (streak, tick, prev["incident_id"]),
                        )
                    else:
                        conn.execute(
                            "UPDATE incidents SET clear_streak = ? WHERE incident_id = ?",
                            (streak, prev["incident_id"]),
                        )
        result.sort(key=lambda item: (0 if item.severity == "CRITICAL" else 1, item.id))
        return result

    def list_incidents(
        self,
        generation_id: str,
        *,
        status: Literal["OPEN", "RESOLVED", "ALL"] = "ALL",
        category: str | None = None,
        limit: int = 100,
    ) -> tuple[list[Anomaly], int, int]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM incidents WHERE generation_id = ? ORDER BY CASE status WHEN 'OPEN' THEN 0 ELSE 1 END, last_tick DESC",
                (generation_id,),
            ).fetchall()
        open_count = sum(1 for r in rows if r["status"] == "OPEN")
        resolved_count = sum(1 for r in rows if r["status"] == "RESOLVED")
        out: list[Anomaly] = []
        for r in rows:
            if status != "ALL" and r["status"] != status:
                continue
            if category and r["category"] != category:
                continue
            out.append(
                Anomaly(
                    id=r["incident_id"],
                    dedup_key=r["dedup_key"],
                    category=r["category"],
                    severity=r["severity"],
                    entity_type=r["entity_type"],
                    entity_id=r["entity_id"],
                    fuel_type=r["fuel_type"],
                    summary=r["summary"],
                    source=r["source"],
                    status=r["status"],
                    first_tick=r["first_tick"],
                    last_tick=r["last_tick"],
                    score=r["score"],
                    expected_value=r["expected_value"],
                    observed_value=r["observed_value"],
                    evidence=json.loads(r["evidence_json"]),
                    suggestion=r["suggestion"],
                )
            )
            if len(out) >= limit:
                break
        return out, open_count, resolved_count

    @staticmethod
    def plan_item_intent_id(generation_id: str, plan_id: str, item_index: int, route_id: str, fuel_type: str) -> str:
        return str(uuid5(NAMESPACE_URL, f"https://bup-check.local/plan-intent/{generation_id}/{plan_id}/{item_index}/{route_id}/{fuel_type}"))

    def record_plan(self, plan: PlanPreviewSummary) -> None:
        with closing(self._connect()) as conn, conn:
            # Plans are immutable once persisted so an in-flight/approved plan_id cannot silently change quantities.
            conn.execute(
                """
                INSERT OR IGNORE INTO plans (
                    plan_id, generation_id, as_of_tick, policy_mode, model_version,
                    solver_status, config_hash, created_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.plan_id,
                    plan.generation_id,
                    plan.as_of_tick,
                    plan.policy_mode,
                    plan.model_version,
                    plan.solver_status,
                    plan.config_hash,
                    utc_now_iso(),
                    plan.model_dump_json(),
                ),
            )
            # Retain at most recent 100 unapproved plans per generation, while keeping any plan referenced by approvals intact.
            conn.execute(
                """
                DELETE FROM plans
                WHERE generation_id = ?
                  AND plan_id NOT IN (
                      SELECT plan_id FROM plans WHERE generation_id = ? ORDER BY rowid DESC LIMIT 100
                  )
                  AND plan_id NOT IN (SELECT plan_id FROM plan_approvals)
                  AND plan_id NOT IN (SELECT plan_id FROM approvals WHERE plan_id IS NOT NULL)
                """,
                (plan.generation_id, plan.generation_id),
            )

    def get_plan(self, plan_id: str) -> PlanPreviewSummary | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT payload_json FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
        return PlanPreviewSummary.model_validate_json(row["payload_json"]) if row else None

    def get_latest_plan(self, generation_id: str) -> PlanPreviewSummary | None:
        with closing(self._connect()) as conn:
            # Prefer the most recently interacted batch approval plan in this generation; fallback to latest generated plan.
            row = conn.execute(
                """
                SELECT p.payload_json
                FROM plan_approvals pa
                JOIN plans p ON p.plan_id = pa.plan_id
                WHERE pa.generation_id = ?
                ORDER BY pa.updated_at DESC, pa.rowid DESC
                LIMIT 1
                """,
                (generation_id,),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT payload_json FROM plans WHERE generation_id = ? ORDER BY rowid DESC LIMIT 1",
                    (generation_id,),
                ).fetchone()
        return PlanPreviewSummary.model_validate_json(row["payload_json"]) if row else None

    def init_or_get_plan_approval(
        self,
        plan: PlanPreviewSummary,
        *,
        actor: str,
        decision_rationale: str | None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        now = utc_now_iso()
        total = len(plan.shipments)
        initial_state = "CONFIRMED" if total == 0 else "PARTIAL"
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO plan_approvals (
                    plan_id, generation_id, as_of_tick, actor, decision_rationale,
                    state, stopped_reason, requires_new_plan, total_shipments,
                    confirmed_count, rejected_count, unknown_count, unsent_count,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, 0, ?, 0, 0, 0, ?, ?, ?)
                """,
                (
                    plan.plan_id,
                    plan.generation_id,
                    plan.as_of_tick,
                    actor,
                    decision_rationale,
                    initial_state,
                    total,
                    total,
                    now,
                    now,
                ),
            )
            for idx, rec in enumerate(plan.shipments):
                intent_id = self.plan_item_intent_id(
                    plan.generation_id, plan.plan_id, idx, rec.route_id, rec.fuel_type
                )
                payload_obj = {
                    "source_depot_id": rec.source_depot_id,
                    "destination_station_id": rec.destination_station_id,
                    "route_id": rec.route_id,
                    "fuel_type": rec.fuel_type,
                    "quantity": rec.quantity_liters,
                    "idempotency_key": f"bup-{intent_id}",
                }
                conn.execute(
                    """
                    INSERT OR IGNORE INTO plan_approval_items (
                        plan_id, item_index, generation_id, intent_id, eta_ticks,
                        payload_json, state, message, shipment_json, allocation_id,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'UNSENT', NULL, NULL, NULL, ?, ?)
                    """,
                    (
                        plan.plan_id,
                        idx,
                        plan.generation_id,
                        intent_id,
                        rec.eta_ticks,
                        json.dumps(payload_obj),
                        now,
                        now,
                    ),
                )
            hdr = conn.execute("SELECT * FROM plan_approvals WHERE plan_id = ?", (plan.plan_id,)).fetchone()
            items = conn.execute(
                "SELECT * FROM plan_approval_items WHERE plan_id = ? ORDER BY item_index ASC",
                (plan.plan_id,),
            ).fetchall()
        return dict(hdr), [dict(r) for r in items]

    def get_plan_approval(self, plan_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        with closing(self._connect()) as conn:
            hdr = conn.execute("SELECT * FROM plan_approvals WHERE plan_id = ?", (plan_id,)).fetchone()
            if hdr is None:
                return None
            items = conn.execute(
                "SELECT * FROM plan_approval_items WHERE plan_id = ? ORDER BY item_index ASC",
                (plan_id,),
            ).fetchall()
        return dict(hdr), [dict(r) for r in items]

    def get_plan_item_by_intent(self, intent_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM plan_approval_items WHERE intent_id = ?", (intent_id,)).fetchone()
        return dict(row) if row else None

    def find_plan_item_for_shipment(
        self,
        plan_id: str,
        *,
        source_depot_id: str,
        destination_station_id: str,
        route_id: str,
        fuel_type: str,
        quantity: float,
    ) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM plan_approval_items WHERE plan_id = ? ORDER BY item_index ASC",
                (plan_id,),
            ).fetchall()
        for row in rows:
            p = json.loads(row["payload_json"])
            if (
                p["source_depot_id"] == source_depot_id
                and p["destination_station_id"] == destination_station_id
                and p["route_id"] == route_id
                and p["fuel_type"] == fuel_type
                and abs(float(p["quantity"]) - float(quantity)) <= 1e-6
            ):
                return dict(row)
        return None

    def save_plan_item(
        self,
        *,
        plan_id: str,
        item_index: int,
        state: str,
        message: str | None,
        shipment_json: str | None,
        allocation_id: int | None,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                UPDATE plan_approval_items
                SET state = ?, message = ?, shipment_json = ?, allocation_id = ?, updated_at = ?
                WHERE plan_id = ? AND item_index = ?
                """,
                (state, message, shipment_json, allocation_id, now, plan_id, item_index),
            )
            row = conn.execute(
                "SELECT * FROM plan_approval_items WHERE plan_id = ? AND item_index = ?",
                (plan_id, item_index),
            ).fetchone()
        return dict(row)

    def sync_plan_item_by_intent(
        self,
        *,
        intent_id: str,
        state: str,
        message: str | None,
        shipment_json: str | None,
        allocation_id: int | None,
    ) -> None:
        now = utc_now_iso()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                UPDATE plan_approval_items
                SET state = ?, message = ?, shipment_json = ?, allocation_id = ?, updated_at = ?
                WHERE intent_id = ?
                """,
                (state, message, shipment_json, allocation_id, now, intent_id),
            )

    def finalize_plan_approval(
        self,
        *,
        plan_id: str,
        state: str,
        stopped_reason: str | None,
        requires_new_plan: bool,
        confirmed_count: int,
        rejected_count: int,
        unknown_count: int,
        unsent_count: int,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                UPDATE plan_approvals
                SET state = ?, stopped_reason = ?, requires_new_plan = ?,
                    confirmed_count = ?, rejected_count = ?, unknown_count = ?, unsent_count = ?,
                    updated_at = ?
                WHERE plan_id = ?
                """,
                (
                    state,
                    stopped_reason,
                    1 if requires_new_plan else 0,
                    confirmed_count,
                    rejected_count,
                    unknown_count,
                    unsent_count,
                    now,
                    plan_id,
                ),
            )
            hdr = conn.execute("SELECT * FROM plan_approvals WHERE plan_id = ?", (plan_id,)).fetchone()
        return dict(hdr)
