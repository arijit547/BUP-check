import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import closing
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

import httpx
from fastapi import HTTPException
from pydantic import Field, TypeAdapter, ValidationError

from planning import Allocation, Fuel
from schemas import Identifier, Snapshot, SnapshotModel

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
    failure_reason: str | None


class ApprovalAttempt(SnapshotModel):
    intent_id: str
    proposal: ShipmentPayload
    state: Literal["READY", "UNKNOWN", "CONFIRMED", "REJECTED"]
    message: str | None
    shipment: Shipment | None


class ApprovalLedger(SnapshotModel):
    attempts: list[ApprovalAttempt]
    simulator_error: str | None


SHIPMENTS = TypeAdapter(list[Shipment])


class ApprovalStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS approvals (
                    intent_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    state TEXT NOT NULL,
                    message TEXT,
                    shipment TEXT
                )
            """)

    def get_or_create(self, intent_id: UUID, proposal: Proposal) -> ApprovalAttempt:
        key = str(intent_id)
        payload = ShipmentPayload(**proposal.model_dump(), idempotency_key=f"bup-{key}")
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute(
                "INSERT OR IGNORE INTO approvals (intent_id, payload, state) VALUES (?, ?, 'READY')",
                (key, payload.model_dump_json()),
            )
            row = conn.execute("SELECT * FROM approvals WHERE intent_id = ?", (key,)).fetchone()
        attempt = self._attempt(row)
        if attempt.proposal != payload:
            raise HTTPException(409, "This approval intent already has a different shipment payload.")
        return attempt

    def list(self) -> list[ApprovalAttempt]:
        with closing(sqlite3.connect(self.path)) as conn:
            return [self._attempt(row) for row in conn.execute("SELECT * FROM approvals ORDER BY rowid DESC")]

    @staticmethod
    def _attempt(row: tuple) -> ApprovalAttempt:
        key, payload, state, message, shipment = row
        return ApprovalAttempt(
            intent_id=key, proposal=ShipmentPayload.model_validate_json(payload),
            state=state, message=message,
            shipment=Shipment.model_validate_json(shipment) if shipment else None,
        )

    def save(
        self, attempt: ApprovalAttempt, state: Literal["READY", "UNKNOWN", "CONFIRMED", "REJECTED"],
        message: str | None = None, shipment: Shipment | None = None,
    ) -> ApprovalAttempt:
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute(
                "UPDATE approvals SET state = ?, message = ?, shipment = ? WHERE intent_id = ?",
                (state, message, shipment.model_dump_json() if shipment else None, attempt.intent_id),
            )
        return ApprovalAttempt(
            intent_id=attempt.intent_id, proposal=attempt.proposal,
            state=state, message=message, shipment=shipment,
        )

    def exact_payload(self, intent_id: str) -> str:
        with closing(sqlite3.connect(self.path)) as conn:
            return conn.execute("SELECT payload FROM approvals WHERE intent_id = ?", (intent_id,)).fetchone()[0]


def proposal_conflict(proposal: Proposal, snapshot: Snapshot, shipments: list[Shipment]) -> str | None:
    depot = next((item for item in snapshot.depots if item.id == proposal.source_depot_id), None)
    station = next((item for item in snapshot.stations if item.id == proposal.destination_station_id), None)
    route = next((item for item in snapshot.routes if item.id == proposal.route_id), None)
    if not depot or not station or not route:
        return "The depot, station, or route is no longer present. Refresh the recommendation."
    if (route.source_depot_id, route.destination_station_id) != (depot.id, station.id):
        return "The route no longer connects this depot and station. Refresh the recommendation."
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
