import asyncio
from contextlib import closing
from datetime import UTC, datetime
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from intelligence.contracts import PlanPreviewSummary
from planning import Allocation, Fuel
from schemas import Identifier, Snapshot, SnapshotModel
from storage import PlatformStore

logger = logging.getLogger(__name__)


class Proposal(SnapshotModel):
    source_depot_id: Identifier
    destination_station_id: Identifier
    route_id: Identifier
    fuel_type: Fuel
    quantity: Annotated[float, Field(gt=0, allow_inf_nan=False)]


class ShipmentPayload(Proposal):
    idempotency_key: str


class Shipment(Allocation):
    id: int
    idempotency_key: str
    route_id: Identifier
    failure_reason: str | None = None


class ApprovalAttempt(SnapshotModel):
    intent_id: str
    generation_id: str = "legacy"
    plan_id: str | None = None
    actor: str = "operator"
    decision_rationale: str | None = None
    proposal: ShipmentPayload
    state: Literal["READY", "UNKNOWN", "CONFIRMED", "REJECTED"]
    message: str | None = None
    shipment: Shipment | None = None
    created_at: str | None = None
    updated_at: str | None = None


class ApprovalLedger(SnapshotModel):
    generation_id: str | None = None
    attempts: list[ApprovalAttempt]
    simulator_error: str | None = None
    rebind_required: bool = False


class PlanApprovalRequest(BaseModel):
    model_config = ConfigDict(strict=False)
    decision_rationale: str | None = Field(default=None, max_length=500)
    expected_config_hash: str | None = Field(default=None, max_length=64)
    reconcile_only: bool = False


class PlanApprovalItemResult(SnapshotModel):
    item_index: int
    intent_id: str
    proposal: ShipmentPayload
    eta_ticks: int | None = None
    state: Literal["UNSENT", "READY", "UNKNOWN", "CONFIRMED", "REJECTED"]
    message: str | None = None
    shipment: Shipment | None = None
    updated_at: str | None = None


class PlanApprovalResult(SnapshotModel):
    plan_id: str
    generation_id: str
    as_of_tick: int
    current_tick: int | None = None
    actor: str = "operator"
    decision_rationale: str | None = None
    state: Literal["CONFIRMED", "PARTIAL", "STOPPED", "UNKNOWN"]
    stopped_reason: str | None = None
    reconcile_required: bool = False
    requires_new_plan: bool = False
    total_shipments: int
    confirmed_count: int
    rejected_count: int
    unknown_count: int
    unsent_count: int
    items: list[PlanApprovalItemResult]
    created_at: str | None = None
    updated_at: str | None = None


class LatestPlanResponse(SnapshotModel):
    generation_id: str
    plan: PlanPreviewSummary | None = None
    approval: PlanApprovalResult | None = None


SHIPMENTS = TypeAdapter(list[Shipment])


def _utc_iso() -> str:
    return datetime.now(UTC).isoformat()


class ApprovalStore:
    def __init__(self, path: str):
        self.path = Path(path)
        PlatformStore(path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def get_or_create(
        self,
        intent_id: UUID,
        proposal: Proposal,
        *,
        generation_id: str = "default",
        plan_id: str | None = None,
        actor: str = "operator",
        decision_rationale: str | None = None,
    ) -> ApprovalAttempt:
        key = str(intent_id)
        now = _utc_iso()
        payload = ShipmentPayload(**proposal.model_dump(), idempotency_key=f"bup-{key}")
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO approvals (
                    intent_id, payload, state, generation_id, plan_id, actor, decision_rationale, created_at, updated_at
                ) VALUES (?, ?, 'READY', ?, ?, ?, ?, ?, ?)
                """,
                (key, payload.model_dump_json(), generation_id, plan_id, actor, decision_rationale, now, now),
            )
            row = conn.execute(
                "SELECT intent_id, payload, state, message, shipment, generation_id, plan_id, actor, decision_rationale, created_at, updated_at "
                "FROM approvals WHERE intent_id = ?",
                (key,),
            ).fetchone()
        attempt = self._attempt(row)
        if attempt.proposal != payload:
            raise HTTPException(409, "This approval intent already has a different shipment payload.")
        if attempt.generation_id not in ("legacy", generation_id):
            raise HTTPException(
                409,
                "This approval intent belongs to an earlier simulator generation and is blocked after reset.",
            )
        return attempt

    def list(self, *, generation_id: str | None = None, limit: int = 100, offset: int = 0) -> list[ApprovalAttempt]:
        with closing(self._connect()) as conn:
            if generation_id:
                rows = conn.execute(
                    "SELECT intent_id, payload, state, message, shipment, generation_id, plan_id, actor, decision_rationale, created_at, updated_at "
                    "FROM approvals WHERE generation_id = ? ORDER BY rowid DESC LIMIT ? OFFSET ?",
                    (generation_id, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT intent_id, payload, state, message, shipment, generation_id, plan_id, actor, decision_rationale, created_at, updated_at "
                    "FROM approvals ORDER BY rowid DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
        return [self._attempt(row) for row in rows]

    def get(self, intent_id: str) -> ApprovalAttempt | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT intent_id, payload, state, message, shipment, generation_id, plan_id, actor, decision_rationale, created_at, updated_at "
                "FROM approvals WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
        return self._attempt(row) if row else None

    @staticmethod
    def _attempt(row: tuple) -> ApprovalAttempt:
        key, payload, state, message, shipment, gen_id, plan_id, actor, rationale, created_at, updated_at = row
        return ApprovalAttempt(
            intent_id=key,
            generation_id=gen_id or "legacy",
            plan_id=plan_id,
            actor=actor or "operator",
            decision_rationale=rationale,
            proposal=ShipmentPayload.model_validate_json(payload),
            state=state,
            message=message,
            shipment=Shipment.model_validate_json(shipment) if shipment else None,
            created_at=created_at,
            updated_at=updated_at,
        )

    def save(
        self,
        attempt: ApprovalAttempt,
        state: Literal["READY", "UNKNOWN", "CONFIRMED", "REJECTED"],
        message: str | None = None,
        shipment: Shipment | None = None,
    ) -> ApprovalAttempt:
        now = _utc_iso()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE approvals SET state = ?, message = ?, shipment = ?, updated_at = ? WHERE intent_id = ?",
                (state, message, shipment.model_dump_json() if shipment else None, now, attempt.intent_id),
            )
        return ApprovalAttempt(
            intent_id=attempt.intent_id,
            generation_id=attempt.generation_id,
            plan_id=attempt.plan_id,
            actor=attempt.actor,
            decision_rationale=attempt.decision_rationale,
            proposal=attempt.proposal,
            state=state,
            message=message,
            shipment=shipment,
            created_at=attempt.created_at,
            updated_at=now,
        )

    def exact_payload(self, intent_id: str) -> str:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT payload FROM approvals WHERE intent_id = ?", (intent_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Stored approval payload not found.")
        return row[0]


def proposal_conflict(proposal: Proposal, snapshot: Snapshot, shipments: list[Shipment]) -> str | None:
    depot = next((item for item in snapshot.depots if item.id == proposal.source_depot_id), None)
    station = next((item for item in snapshot.stations if item.id == proposal.destination_station_id), None)
    route = next((item for item in snapshot.routes if item.id == proposal.route_id), None)
    if not depot or not station or not route:
        return "The depot, station, or route is no longer present. Refresh the recommendation."
    if (route.source_depot_id, route.destination_station_id) != (depot.id, station.id):
        return "The route no longer connects this depot and station. Refresh the recommendation."
    if depot.status not in ("OPEN", "CONSTRAINED"):
        return "The source depot is closed. Refresh the recommendation."
    if route.status != "AVAILABLE":
        return "The selected route is disrupted. Refresh the recommendation."
    if station.status != "OPEN":
        return "The destination station is unavailable. Refresh the recommendation."
    if proposal.quantity > route.max_shipment:
        return "The quantity exceeds the current route limit. Refresh the recommendation."
    if proposal.quantity > getattr(depot.inventory, proposal.fuel_type):
        return "The depot has insufficient stock for this quantity. Refresh the recommendation."
    used = sum(
        shipment.quantity for shipment in shipments
        if shipment.source_depot_id == depot.id
        and shipment.created_tick == snapshot.simulation.tick
        and shipment.status in ("PENDING", "IN_TRANSIT")
    )
    if used + proposal.quantity > depot.dispatch_capacity_per_tick:
        return "The depot has insufficient dispatch capacity this tick. Refresh the recommendation."
    if proposal.quantity + getattr(station.inventory, proposal.fuel_type) > getattr(station.capacity, proposal.fuel_type):
        return "The station has insufficient storage capacity. Refresh the recommendation."
    return None


def upstream_problem(exc: Exception) -> str:
    if isinstance(exc, HTTPException) and isinstance(exc.detail, str):
        return exc.detail
    if isinstance(exc, ValidationError):
        logger.warning("Allocation data failed schema validation: %s", exc.errors(include_input=False))
        return "Simulator returned invalid allocation data."
    logger.warning("Allocation request failed: %s", exc)
    return "Simulator is unavailable. Try again when the connection recovers."


def matches(shipment: Shipment, payload: ShipmentPayload) -> bool:
    return all(getattr(shipment, key) == value for key, value in payload.model_dump().items())


async def submit(
    store: ApprovalStore,
    attempt: ApprovalAttempt,
    client: httpx.AsyncClient,
    read_shipments: Callable[[], Awaitable[list[Shipment]]],
    read_fresh_state: Callable[[], Awaitable[tuple[Snapshot, list[Shipment]]]],
) -> ApprovalAttempt:
    if attempt.state in ("CONFIRMED", "REJECTED"):
        return attempt

    try:
        if attempt.state == "READY":
            snapshot, shipments = await read_fresh_state()
            if conflict := proposal_conflict(attempt.proposal, snapshot, shipments):
                return store.save(attempt, "REJECTED", conflict)
        else:
            shipments = await read_shipments()
    except (httpx.HTTPError, ValidationError, HTTPException) as exc:
        return store.save(attempt, attempt.state, upstream_problem(exc))

    if existing := next((row for row in shipments if matches(row, attempt.proposal)), None):
        return store.save(attempt, "CONFIRMED", shipment=existing)

    # Commit this transition before the first POST. A restart from here uses replay.
    attempt = store.save(attempt, "UNKNOWN", "Awaiting simulator confirmation.")
    payload = store.exact_payload(attempt.intent_id)
    for delay in (0.25, 0.5):
        try:
            response = await client.post(
                "/v1/allocations", content=payload, headers={"Content-Type": "application/json"},
            )
            if response.status_code in (404, 409, 422):
                try:
                    detail = response.json().get("detail")
                except (ValueError, AttributeError):
                    detail = None
                message = detail.get("message") if isinstance(detail, dict) else None
                return store.save(attempt, "REJECTED", message or "The simulator rejected this proposal. Refresh the recommendation.")
            response.raise_for_status()
            shipment = Shipment.model_validate_json(response.content)
            if matches(shipment, attempt.proposal):
                return store.save(attempt, "CONFIRMED", shipment=shipment)
            logger.warning("Simulator returned an unexpected allocation for intent %s", attempt.intent_id)
        except (httpx.HTTPError, ValidationError) as exc:
            upstream_problem(exc)

        await asyncio.sleep(delay)
        try:
            shipments = await read_shipments()
        except (httpx.HTTPError, ValidationError, HTTPException) as exc:
            return store.save(attempt, "UNKNOWN", f"Submission outcome unknown. {upstream_problem(exc)}")
        if existing := next((row for row in shipments if matches(row, attempt.proposal)), None):
            return store.save(attempt, "CONFIRMED", shipment=existing)

    return store.save(attempt, "UNKNOWN", "Submission outcome unknown. Retry this approval to check for the original shipment.")


async def cancel_pending(
    store: ApprovalStore,
    attempt: ApprovalAttempt,
    client: httpx.AsyncClient,
) -> ApprovalAttempt:
    if attempt.shipment is None:
        raise HTTPException(409, "No simulator shipment exists for this approval intent.")
    if attempt.shipment.status != "PENDING":
        raise HTTPException(409, f"Only PENDING shipments can be cancelled (current status: {attempt.shipment.status}).")
    try:
        response = await client.post(f"/v1/allocations/{attempt.shipment.id}/cancel")
        if response.status_code in (404, 409, 422):
            detail = response.json().get("detail") if response.headers.get("content-type", "").startswith("application/json") else None
            msg = detail.get("message") if isinstance(detail, dict) else "Simulator rejected cancellation."
            raise HTTPException(response.status_code, msg)
        response.raise_for_status()
        updated = Shipment.model_validate_json(response.content)
        return store.save(attempt, "CONFIRMED", message="Cancelled by operator.", shipment=updated)
    except HTTPException:
        raise
    except (httpx.HTTPError, ValidationError) as exc:
        raise HTTPException(503, upstream_problem(exc)) from exc


def build_plan_approval_result(
    hdr: dict,
    item_rows: list[dict],
    *,
    current_tick: int | None = None,
) -> PlanApprovalResult:
    items = [
        PlanApprovalItemResult(
            item_index=int(r["item_index"]),
            intent_id=r["intent_id"],
            proposal=ShipmentPayload.model_validate_json(r["payload_json"]),
            eta_ticks=r["eta_ticks"],
            state=r["state"],
            message=r["message"],
            shipment=Shipment.model_validate_json(r["shipment_json"]) if r["shipment_json"] else None,
            updated_at=r["updated_at"],
        )
        for r in item_rows
    ]
    confirmed = sum(1 for i in items if i.state == "CONFIRMED")
    rejected = sum(1 for i in items if i.state == "REJECTED")
    unknown = sum(1 for i in items if i.state == "UNKNOWN")
    unsent = sum(1 for i in items if i.state in ("UNSENT", "READY"))
    return PlanApprovalResult(
        plan_id=hdr["plan_id"],
        generation_id=hdr["generation_id"],
        as_of_tick=int(hdr["as_of_tick"]),
        current_tick=current_tick,
        actor=hdr["actor"] or "operator",
        decision_rationale=hdr["decision_rationale"],
        state=hdr["state"],
        stopped_reason=hdr["stopped_reason"],
        reconcile_required=unknown > 0,
        requires_new_plan=bool(hdr["requires_new_plan"]) or rejected > 0,
        total_shipments=len(items),
        confirmed_count=confirmed,
        rejected_count=rejected,
        unknown_count=unknown,
        unsent_count=unsent,
        items=items,
        created_at=hdr["created_at"],
        updated_at=hdr["updated_at"],
    )


async def reconcile_plan_approval_view(
    *,
    platform_store: PlatformStore,
    plan_id: str,
    shipments: list[Shipment] | None = None,
    current_tick: int | None = None,
) -> PlanApprovalResult | None:
    loaded = platform_store.get_plan_approval(plan_id)
    if loaded is None:
        return None
    hdr, rows = loaded
    if shipments is not None:
        by_key = {s.idempotency_key: s for s in shipments}
        for r in rows:
            payload = ShipmentPayload.model_validate_json(r["payload_json"])
            live = by_key.get(payload.idempotency_key)
            if live is not None and matches(live, payload):
                live_json = live.model_dump_json()
                if r["state"] != "CONFIRMED" or r["shipment_json"] != live_json:
                    platform_store.save_plan_item(
                        plan_id=plan_id,
                        item_index=int(r["item_index"]),
                        state="CONFIRMED",
                        message=r["message"] if live.status == "CANCELLED" else None,
                        shipment_json=live_json,
                        allocation_id=live.id,
                    )
        loaded = platform_store.get_plan_approval(plan_id)
        assert loaded is not None
        hdr, rows = loaded
    return build_plan_approval_result(hdr, rows, current_tick=current_tick)


async def execute_plan_approval(
    *,
    platform_store: PlatformStore,
    approval_store: ApprovalStore,
    plan: PlanPreviewSummary,
    actor: str,
    decision_rationale: str | None,
    reconcile_only: bool,
    client: httpx.AsyncClient,
    read_shipments: Callable[[], Awaitable[list[Shipment]]],
    read_fresh_state: Callable[[], Awaitable[tuple[Snapshot, list[Shipment]]]],
    verify_generation: Callable[[Snapshot, list[Shipment]], None],
) -> PlanApprovalResult:
    """Persist immutable batch payloads then execute/reconcile each shipment with fresh validation and stop-on-error."""
    # Read fresh simulator state and verify generation BEFORE persisting plan_approvals rows
    initial_snap, initial_shipments = await read_fresh_state()
    verify_generation(initial_snap, initial_shipments)
    latest_tick = initial_snap.simulation.tick
    sim_by_key = {s.idempotency_key: s for s in initial_shipments}

    hdr, rows = platform_store.init_or_get_plan_approval(
        plan,
        actor=actor,
        decision_rationale=decision_rationale,
    )

    if len(rows) == 0:
        hdr = platform_store.finalize_plan_approval(
            plan_id=plan.plan_id,
            state="CONFIRMED",
            stopped_reason=None,
            requires_new_plan=False,
            confirmed_count=0,
            rejected_count=0,
            unknown_count=0,
            unsent_count=0,
        )
        return build_plan_approval_result(hdr, rows, current_tick=latest_tick)

    stopped_reason: str | None = hdr.get("stopped_reason")
    requires_new_plan = bool(hdr.get("requires_new_plan"))
    halt_batch = False

    for row in rows:
        idx = int(row["item_index"])
        intent_uuid = UUID(row["intent_id"])
        payload = ShipmentPayload.model_validate_json(row["payload_json"])
        proposal = Proposal(
            source_depot_id=payload.source_depot_id,
            destination_station_id=payload.destination_station_id,
            route_id=payload.route_id,
            fuel_type=payload.fuel_type,
            quantity=payload.quantity,
        )

        # Sync against already-confirmed simulator record without resending
        if row["state"] == "CONFIRMED":
            live = sim_by_key.get(payload.idempotency_key)
            if live is not None and matches(live, payload):
                msg = row["message"] if live.status == "CANCELLED" else None
                platform_store.save_plan_item(
                    plan_id=plan.plan_id,
                    item_index=idx,
                    state="CONFIRMED",
                    message=msg,
                    shipment_json=live.model_dump_json(),
                    allocation_id=live.id,
                )
            continue

        if halt_batch:
            break

        # If a prior shipment was permanently REJECTED on this immutable plan, do not dispatch later rows silently.
        if row["state"] == "REJECTED":
            requires_new_plan = True
            stopped_reason = row["message"] or "A shipment in this plan was rejected; generate a new plan for remaining demand."
            halt_batch = True
            break

        # Reconcile UNKNOWN outcome with its exact persisted intent before moving forward.
        if row["state"] == "UNKNOWN":
            live = sim_by_key.get(payload.idempotency_key)
            if live is not None and matches(live, payload):
                attempt = approval_store.get_or_create(
                    intent_uuid,
                    proposal,
                    generation_id=plan.generation_id,
                    plan_id=plan.plan_id,
                    actor=actor,
                    decision_rationale=decision_rationale,
                )
                approval_store.save(attempt, "CONFIRMED", shipment=live)
                platform_store.save_plan_item(
                    plan_id=plan.plan_id,
                    item_index=idx,
                    state="CONFIRMED",
                    message=None,
                    shipment_json=live.model_dump_json(),
                    allocation_id=live.id,
                )
                continue

            attempt = approval_store.get_or_create(
                intent_uuid,
                proposal,
                generation_id=plan.generation_id,
                plan_id=plan.plan_id,
                actor=actor,
                decision_rationale=decision_rationale,
            )
            outcome = await submit(approval_store, attempt, client, read_shipments, read_fresh_state)
            if outcome.state == "CONFIRMED" and outcome.shipment is not None:
                sim_by_key[outcome.shipment.idempotency_key] = outcome.shipment
                platform_store.save_plan_item(
                    plan_id=plan.plan_id,
                    item_index=idx,
                    state="CONFIRMED",
                    message=None,
                    shipment_json=outcome.shipment.model_dump_json(),
                    allocation_id=outcome.shipment.id,
                )
                continue
            if outcome.state == "REJECTED":
                requires_new_plan = True
                stopped_reason = outcome.message or "Reconciled shipment rejected by simulator constraints."
            else:
                stopped_reason = outcome.message or "Shipment outcome still unknown; reconcile before submitting remaining items."
            platform_store.save_plan_item(
                plan_id=plan.plan_id,
                item_index=idx,
                state=outcome.state,
                message=outcome.message,
                shipment_json=outcome.shipment.model_dump_json() if outcome.shipment else None,
                allocation_id=outcome.shipment.id if outcome.shipment else None,
            )
            halt_batch = True
            break

        # Reconcile-only mode resolves UNKNOWN rows without dispatching UNSENT rows
        if reconcile_only:
            continue

        # Fresh read + generation + constraint revalidation immediately before each write
        try:
            fresh_snap, fresh_shipments = await read_fresh_state()
        except (httpx.HTTPError, ValidationError, HTTPException) as exc:
            stopped_reason = f"Fresh simulator check unavailable before shipment #{idx + 1}: {upstream_problem(exc)}"
            halt_batch = True
            break
        try:
            verify_generation(fresh_snap, fresh_shipments)
        except HTTPException as exc:
            requires_new_plan = True
            stopped_reason = str(exc.detail)
            halt_batch = True
            break
        latest_tick = fresh_snap.simulation.tick
        sim_by_key = {s.idempotency_key: s for s in fresh_shipments}

        attempt = approval_store.get_or_create(
            intent_uuid,
            proposal,
            generation_id=plan.generation_id,
            plan_id=plan.plan_id,
            actor=actor,
            decision_rationale=decision_rationale,
        )
        outcome = await submit(approval_store, attempt, client, read_shipments, read_fresh_state)
        platform_store.save_plan_item(
            plan_id=plan.plan_id,
            item_index=idx,
            state=outcome.state,
            message=None if outcome.state == "CONFIRMED" else outcome.message,
            shipment_json=outcome.shipment.model_dump_json() if outcome.shipment else None,
            allocation_id=outcome.shipment.id if outcome.shipment else None,
        )
        if outcome.state == "CONFIRMED" and outcome.shipment is not None:
            sim_by_key[outcome.shipment.idempotency_key] = outcome.shipment
            continue

        if outcome.state == "REJECTED":
            requires_new_plan = True
            stopped_reason = outcome.message or "Fresh feasibility check rejected this shipment."
        else:
            stopped_reason = outcome.message or "Shipment outcome unknown; batch halted before further submissions."
        halt_batch = True
        break

    loaded = platform_store.get_plan_approval(plan.plan_id)
    assert loaded is not None
    _, updated_rows = loaded
    confirmed = sum(1 for r in updated_rows if r["state"] == "CONFIRMED")
    rejected = sum(1 for r in updated_rows if r["state"] == "REJECTED")
    unknown = sum(1 for r in updated_rows if r["state"] == "UNKNOWN")
    unsent = sum(1 for r in updated_rows if r["state"] in ("UNSENT", "READY"))

    if unknown > 0:
        batch_state = "UNKNOWN"
    elif confirmed == len(updated_rows):
        batch_state = "CONFIRMED"
        stopped_reason = None
        requires_new_plan = False
    elif rejected > 0:
        batch_state = "STOPPED"
    elif confirmed > 0 and unsent > 0:
        batch_state = "PARTIAL"
    else:
        batch_state = "STOPPED" if requires_new_plan else "PARTIAL"

    final_hdr = platform_store.finalize_plan_approval(
        plan_id=plan.plan_id,
        state=batch_state,
        stopped_reason=stopped_reason,
        requires_new_plan=requires_new_plan,
        confirmed_count=confirmed,
        rejected_count=rejected,
        unknown_count=unknown,
        unsent_count=unsent,
    )
    return build_plan_approval_result(final_hdr, updated_rows, current_tick=latest_tick)
