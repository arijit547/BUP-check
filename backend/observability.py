from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
import math
from typing import Any, Literal

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from schemas import SnapshotModel


class ComponentHealth(SnapshotModel):
    status: Literal["ok", "degraded", "error"]
    detail: str | None = None
    latency_ms: float | None = None


class HealthReport(SnapshotModel):
    status: Literal["ok", "degraded"]
    timestamp: datetime
    version: str
    generation_id: str | None = None
    circuit_breaker_state: Literal["CLOSED", "OPEN", "HALF_OPEN"] = "CLOSED"
    components: dict[str, ComponentHealth]
    p95_latency_ms: float | None
    error_rate_percent: float


class MetricsReport(SnapshotModel):
    timestamp: datetime
    generation_id: str | None = None
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
    cache_hits: int = 0
    cache_misses: int = 0
    cache_hit_ratio: float | None = None
    solver_last_status: str | None = None
    solver_last_ms: float | None = None
    forecast_source: str | None = None
    fallback_reason: str | None = None
    last_recovery_seconds: float | None = None
    matured_accuracy: dict[str, Any] | None = None
    simulator: dict[str, float | int] | None = None
    counter_scope: str = "process_lifetime_except_simulator_cumulative"


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


def normalize_metric_path(path: str) -> str:
    if path.startswith("/api/approvals/") and path.endswith("/cancel"):
        return "/api/approvals/{intent_id}/cancel"
    if path.startswith("/api/approvals/"):
        return "/api/approvals/{intent_id}"
    if path == "/api/plans/latest":
        return "/api/plans/latest"
    if path.startswith("/api/plans/") and path.endswith("/approval"):
        return "/api/plans/{plan_id}/approval"
    if path.startswith("/api/plans/"):
        return "/api/plans/{plan_id}"
    return path


class PrometheusMetrics:
    """Bounded low-cardinality Prometheus metrics isolated in a custom registry."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.req_total = Counter(
            "bup_http_requests_total",
            "HTTP API requests by route and status class",
            ["route", "status_class"],
            registry=self.registry,
        )
        self.req_latency = Histogram(
            "bup_http_request_duration_seconds",
            "HTTP API latency in seconds",
            ["route"],
            registry=self.registry,
        )
        self.fallback_total = Counter(
            "bup_fallback_activations_total",
            "Intelligence fallback activations",
            ["reason"],
            registry=self.registry,
        )
        self.active_anomalies = Gauge(
            "bup_active_anomalies",
            "Active open incidents/anomalies",
            registry=self.registry,
        )
        self.service_level = Gauge(
            "bup_simulator_service_level",
            "Cumulative simulator service level",
            registry=self.registry,
        )
        self.unmet_liters = Gauge(
            "bup_simulator_unmet_liters",
            "Cumulative simulator unmet demand in liters",
            registry=self.registry,
        )
        self.solver_ms = Gauge(
            "bup_optimizer_last_duration_ms",
            "Last optimizer solve time in ms",
            registry=self.registry,
        )

    def render(self) -> bytes:
        return generate_latest(self.registry)


@dataclass
class RequestMetrics:
    records: deque[RequestRecord] = field(default_factory=lambda: deque(maxlen=1000))
    total_requests: int = 0
    error_count: int = 0
    fallback_activations: int = 0
    active_anomalies: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    solver_last_status: str | None = None
    solver_last_ms: float | None = None
    forecast_source: str | None = None
    fallback_reason: str | None = None
    degraded_started_at: float | None = None
    last_recovery_seconds: float | None = None
    prom: PrometheusMetrics = field(default_factory=PrometheusMetrics)

    def record_request(self, now: float, path: str, status_code: int, latency_ms: float) -> None:
        self.total_requests += 1
        if status_code >= 500:
            self.error_count += 1
        norm = normalize_metric_path(path)
        self.records.append(RequestRecord(now, norm, status_code, latency_ms))
        status_cls = f"{status_code // 100}xx"
        self.prom.req_total.labels(route=norm, status_class=status_cls).inc()
        self.prom.req_latency.labels(route=norm).observe(latency_ms / 1000.0)

    def mark_degraded(self, now: float) -> None:
        if self.degraded_started_at is None:
            self.degraded_started_at = now

    def mark_recovered(self, now: float) -> None:
        if self.degraded_started_at is not None:
            self.last_recovery_seconds = round(now - self.degraded_started_at, 3)
            self.degraded_started_at = None

    def record_intelligence(
        self,
        fallback_used: bool,
        anomaly_count: int,
        *,
        solver_status: str | None = None,
        solver_ms: float | None = None,
        forecast_source: str | None = None,
        fallback_reason: str | None = None,
        sim_metrics: dict[str, float | int] | None = None,
    ) -> None:
        if fallback_used:
            self.fallback_activations += 1
            self.prom.fallback_total.labels(reason=(fallback_reason or "active")[:40]).inc()
        self.active_anomalies = anomaly_count
        self.solver_last_status = solver_status
        self.solver_last_ms = solver_ms
        self.forecast_source = forecast_source
        self.fallback_reason = fallback_reason
        self.prom.active_anomalies.set(anomaly_count)
        if solver_ms is not None:
            self.prom.solver_ms.set(solver_ms)
        if sim_metrics:
            self.prom.service_level.set(float(sim_metrics.get("service_level", 1.0)))
            self.prom.unmet_liters.set(float(sim_metrics.get("unmet_demand_liters", 0.0)))

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
        total_cache = self.cache_hits + self.cache_misses
        hit_ratio = round(self.cache_hits / total_cache, 4) if total_cache else None
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
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "cache_hit_ratio": hit_ratio,
        }


def utc_now() -> datetime:
    return datetime.now(UTC)
