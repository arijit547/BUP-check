from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Identifier = Annotated[str, Field(min_length=1)]
Liters = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class SnapshotModel(BaseModel):
    model_config = ConfigDict(strict=True)


class Simulation(SnapshotModel):
    id: int | None = None
    scenario_id: str | None = None
    scenario_version: str | None = None
    seed: int | None = None
    tick: int = Field(ge=0)
    tick_minutes: int = Field(gt=0)
    sim_time: datetime = Field(description="Simulator time normalized to UTC.")
    status: Literal["PAUSED", "RUNNING"]

    @field_validator("sim_time")
    @classmethod
    def normalize_sim_time(cls, value: datetime) -> datetime:
        # The published simulator can omit the documented UTC offset.
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class FuelVolumes(SnapshotModel):
    DIESEL: Liters
    PETROL: Liters
    OCTANE: Liters


class InventoryLocation(SnapshotModel):
    id: Identifier
    name: str
    region_id: Identifier
    inventory: FuelVolumes
    capacity: FuelVolumes


class Depot(InventoryLocation):
    status: Literal["OPEN", "CONSTRAINED"]
    dispatch_capacity_per_tick: Liters


class Station(InventoryLocation):
    status: Literal["OPEN", "OUTAGE"]


class Route(SnapshotModel):
    id: Identifier
    source_depot_id: Identifier
    destination_station_id: Identifier
    status: Literal["AVAILABLE", "DISRUPTED"]
    transit_ticks: int = Field(gt=0)
    max_shipment: Liters


class Snapshot(SnapshotModel):
    simulation: Simulation
    depots: list[Depot]
    stations: list[Station]
    routes: list[Route]


class SnapshotError(SnapshotModel):
    detail: Literal[
        "Simulator request failed",
        "Simulator returned invalid data",
        "Simulator marked data stale",
        "Simulation changed while collecting snapshot",
    ]
