from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
import math
from typing import Literal

from schemas import SnapshotModel


class ComponentHealth(SnapshotModel):
    status: Literal["ok", "degraded", "error"]
    detail: str | None = None
    latency_ms: float | None = None


class HealthReport(SnapshotModel):
    status: Literal["ok", "degraded"]
    timestamp: datetime
    version: str
    components: dict[str, ComponentHealth]
    p95_latency_ms: float | None
    error_rate_percent: float


class MetricsReport(SnapshotModel):
    timestamp: datetime
    total_requests: int
    error_count: int
    error_rate_percent: float
    requests_per_minute: float
    p50_latency_ms: float | None
    p95_latency_ms: float | None
    p99_latency_ms: float | None
    fallback_activations: int
    active_anomalies: int
    sse_status: Literal["connected", "connecting", "degraded", "disconnected"]
    sse_events_received: int
    sse_reconnects: int
    simulator: dict[str, float | int] | None = None


@dataclass
class SSEState:
    status: Literal["connected", "connecting", "degraded", "disconnected"] = "connecting"
    events_received: int = 0
    reconnects: int = 0
    last_event_type: str | None = None
    last_event_at: datetime | None = None
    last_error: str | None = None


@dataclass
class RequestRecord:
    recorded_at: float
    path: str
    status_code: int
    latency_ms: float


@dataclass
class RequestMetrics:
    records: deque[RequestRecord] = field(default_factory=lambda: deque(maxlen=1000))
    total_requests: int = 0
    error_count: int = 0
    fallback_activations: int = 0
    active_anomalies: int = 0

    def record_request(self, now: float, path: str, status_code: int, latency_ms: float) -> None:
        self.total_requests += 1
        if status_code >= 500:
            self.error_count += 1
        self.records.append(RequestRecord(now, path, status_code, latency_ms))

    def record_intelligence(self, fallback_used: bool, anomaly_count: int) -> None:
        if fallback_used:
            self.fallback_activations += 1
        self.active_anomalies = anomaly_count

    @staticmethod
    def _percentile(sorted_values: list[float], p: float) -> float | None:
        if not sorted_values:
            return None
        index = max(0, min(len(sorted_values) - 1, math.ceil(p * len(sorted_values)) - 1))
        return round(sorted_values[index], 3)

    def snapshot(self, now: float) -> dict[str, float | int | None]:
        latencies = sorted(item.latency_ms for item in self.records)
        recent = [item for item in self.records if now - item.recorded_at <= 60.0]
        error_rate = round(100.0 * self.error_count / self.total_requests, 3) if self.total_requests else 0.0
        return {
            "total_requests": self.total_requests,
            "error_count": self.error_count,
            "error_rate_percent": error_rate,
            "requests_per_minute": float(len(recent)),
            "p50_latency_ms": self._percentile(latencies, 0.50),
            "p95_latency_ms": self._percentile(latencies, 0.95),
            "p99_latency_ms": self._percentile(latencies, 0.99),
            "fallback_activations": self.fallback_activations,
            "active_anomalies": self.active_anomalies,
        }


def utc_now() -> datetime:
    return datetime.now(UTC)
