from statistics import mean, pstdev
from typing import Any

from intelligence.contracts import (
    FUELS,
    Anomaly,
    DemandObservation,
    Forecast,
    Fuel,
    SimulatorEvent,
    StationDetail,
    SupplyArrival,
)
from schemas import Snapshot


def detect_rule_anomalies(
    snapshot: Snapshot,
    observed: dict[tuple[str, Fuel], dict[int, float | DemandObservation]],
    station_details: dict[str, StationDetail],
    forecasts: list[Forecast],
    dispatch_used: dict[str, float],
    supply_arrivals: list[SupplyArrival],
    events: list[SimulatorEvent],
) -> list[Anomaly]:
    anomalies: list[Anomaly] = []
    tick = snapshot.simulation.tick
    station_names = {station.id: station.name for station in snapshot.stations}
    depot_names = {depot.id: depot.name for depot in snapshot.depots}

    for station in snapshot.stations:
        if station.status == "OUTAGE":
            anomalies.append(Anomaly(
                id=f"outage-{station.id}",
                dedup_key=f"outage-{station.id}",
                category="STATION_OUTAGE",
                severity="CRITICAL",
                entity_type="station",
                entity_id=station.id,
                source="RULE",
                summary=f"{station.name} is in OUTAGE; demand cannot be served until reopened.",
                evidence=[f"station.status == OUTAGE at tick {tick}"],
                suggestion="Hold new dispatches to this station until status returns to OPEN.",
            ))

        detail = station_details.get(station.id)
        for fuel in FUELS:
            raw_items = sorted(observed.get((station.id, fuel), {}).items())
            samples = [
                (t, val.demand_liters if isinstance(val, DemandObservation) else float(val))
                for t, val in raw_items
            ]
            if len(samples) >= 4:
                latest_tick, latest = samples[-1]
                prior = [val for _, val in samples[:-1]]
                base = mean(prior)
                std = pstdev(prior) if len(prior) > 1 else 0.0
                z = (latest - base) / std if std > 1e-6 else None
                ratio = latest / base if base > 0 else 1.0
                mult = detail.demand_multiplier if detail else 1.0
                if (latest_tick == tick - 1) and ((z is not None and z >= 2.0) or ratio >= 1.35 or mult >= 1.25):
                    severity = "CRITICAL" if ((z is not None and z >= 2.8) or ratio >= 1.6 or mult >= 1.5) else "WARNING"
                    anomalies.append(Anomaly(
                        id=f"spike-{station.id}-{fuel}",
                        dedup_key=f"spike-{station.id}-{fuel}",
                        category="DEMAND_SPIKE",
                        severity=severity,
                        entity_type="station",
                        entity_id=station.id,
                        fuel_type=fuel,
                        source="RULE",
                        expected_value=round(base, 3),
                        observed_value=round(latest, 3),
                        baseline_value=round(base, 3),
                        z_score=round(z, 2) if z is not None else None,
                        summary=(
                            f"{station.name} {fuel} demand spiked to {latest:.1f} L/tick "
                            f"vs {base:.1f} L baseline (multiplier {mult:.2f}x)."
                        ),
                        evidence=[
                            f"latest_tick={latest_tick}, ratio={ratio:.2f}x, multiplier={mult:.2f}x",
                        ],
                        suggestion="Prioritize shorter-ETA replenishment and raise safety stock buffer.",
                    ))
                elif (latest_tick == tick - 1) and ((z is not None and z <= -2.2) or ratio <= 0.45):
                    anomalies.append(Anomaly(
                        id=f"drop-{station.id}-{fuel}",
                        dedup_key=f"drop-{station.id}-{fuel}",
                        category="DEMAND_DROP",
                        severity="WARNING",
                        entity_type="station",
                        entity_id=station.id,
                        fuel_type=fuel,
                        source="RULE",
                        expected_value=round(base, 3),
                        observed_value=round(latest, 3),
                        baseline_value=round(base, 3),
                        z_score=round(z, 2) if z is not None else None,
                        summary=f"{station.name} {fuel} demand dropped sharply to {latest:.1f} L/tick vs {base:.1f} L.",
                        evidence=[f"ratio={ratio:.2f}x at tick {latest_tick}"],
                        suggestion="Verify whether an unlogged demand pause or sensor gap occurred.",
                    ))

    for forecast in forecasts:
        if forecast.shortage_risk in ("CRITICAL", "HIGH"):
            eta_msg = (
                f"projected stockout at tick {forecast.stockout_tick}"
                if forecast.stockout_tick is not None
                else "low inventory reserve"
            )
            anomalies.append(Anomaly(
                id=f"risk-{forecast.station_id}-{forecast.fuel_type}",
                dedup_key=f"risk-{forecast.station_id}-{forecast.fuel_type}",
                category="STOCKOUT_RISK",
                severity="CRITICAL" if forecast.shortage_risk == "CRITICAL" else "WARNING",
                entity_type="station",
                entity_id=forecast.station_id,
                fuel_type=forecast.fuel_type,
                source="RULE",
                observed_value=forecast.unmet_liters,
                summary=f"{forecast.station_name} {forecast.fuel_type}: {eta_msg} ({forecast.shortage_risk}).",
                evidence=[f"shortage_risk={forecast.shortage_risk}, unmet={forecast.unmet_liters or 0:.1f} L"],
                suggestion="Dispatch preventive allocation on fastest open corridor.",
            ))

    for route in snapshot.routes:
        if route.status == "DISRUPTED":
            dest_name = station_names.get(route.destination_station_id, route.destination_station_id)
            anomalies.append(Anomaly(
                id=f"route-{route.id}",
                dedup_key=f"route-{route.id}",
                category="ROUTE_DISRUPTED",
                severity="CRITICAL",
                entity_type="route",
                entity_id=route.id,
                source="RULE",
                summary=f"{route.id} to {dest_name} is DISRUPTED; rerouting required.",
                evidence=[f"route.status == DISRUPTED at tick {tick}"],
                suggestion="Use alternate open depot corridor; avoid dispatching on disrupted route.",
            ))

    for depot in snapshot.depots:
        if depot.status == "CONSTRAINED":
            anomalies.append(Anomaly(
                id=f"depot-{depot.id}",
                dedup_key=f"depot-{depot.id}",
                category="DEPOT_CONSTRAINED",
                severity="WARNING",
                entity_type="depot",
                entity_id=depot.id,
                source="RULE",
                summary=f"{depot.name} is CONSTRAINED; monitor dispatch limits and resupplies.",
                evidence=[f"depot.status == CONSTRAINED at tick {tick}"],
                suggestion="Rebalance dispatches toward unconstrained depot where corridors allow.",
            ))
        if depot.dispatch_capacity_per_tick > 0:
            used = dispatch_used.get(depot.id, 0.0)
            utilization = used / depot.dispatch_capacity_per_tick
            if utilization >= 0.75:
                anomalies.append(Anomaly(
                    id=f"dispatch-{depot.id}",
                    dedup_key=f"dispatch-{depot.id}",
                    category="DISPATCH_BOTTLENECK",
                    severity="CRITICAL" if utilization >= 0.95 else "WARNING",
                    entity_type="depot",
                    entity_id=depot.id,
                    source="RULE",
                    expected_value=depot.dispatch_capacity_per_tick,
                    observed_value=round(used, 3),
                    baseline_value=depot.dispatch_capacity_per_tick,
                    summary=(
                        f"{depot.name} dispatch capacity {utilization * 100:.0f}% utilized "
                        f"({used:.0f}/{depot.dispatch_capacity_per_tick:.0f} L)."
                    ),
                    evidence=[f"utilization={utilization * 100:.1f}% at tick {tick}"],
                    suggestion="Prioritize highest-urgency fuels within remaining tick dispatch capacity.",
                ))

    for arrival in supply_arrivals:
        if arrival.status == "DELAYED":
            depot_name = depot_names.get(arrival.depot_id, arrival.depot_id)
            anomalies.append(Anomaly(
                id=f"supply-{arrival.id}",
                dedup_key=f"supply-{arrival.id}",
                category="SUPPLY_DELAYED",
                severity="WARNING",
                entity_type="supply",
                entity_id=arrival.id,
                fuel_type=arrival.fuel_type,
                source="RULE",
                observed_value=float(arrival.planned_tick),
                summary=(
                    f"Incoming {arrival.quantity:.0f} L {arrival.fuel_type} to {depot_name} "
                    f"delayed (planned tick {arrival.planned_tick})."
                ),
                evidence=[f"arrival.status == DELAYED (planned_tick={arrival.planned_tick})"],
                suggestion="Preserve depot reserves until replenishment arrives.",
            ))

    for ev in events:
        if ev.type == "supply_shortfall" and ev.status == "ACTIVE" and ev.start_tick <= tick:
            factor = float((ev.parameters or {}).get("factor", 0.5))
            anomalies.append(Anomaly(
                id=f"shortfall-{ev.id}",
                dedup_key=f"shortfall-{ev.id}",
                category="SUPPLY_SHORTFALL",
                severity="CRITICAL" if factor <= 0.6 else "WARNING",
                entity_type="supply",
                entity_id=f"event-{ev.id}",
                source="RULE",
                observed_value=factor,
                expected_value=1.0,
                summary=f"Depot supply shortfall event #{ev.id} active (factor {factor:.2f}x).",
                evidence=[f"event type=supply_shortfall ticks {ev.start_tick}..{ev.end_tick}"],
                suggestion="Conserve depot stock for highest-risk stations.",
            ))
        elif ev.type == "shipment_delay" and ev.status == "ACTIVE" and ev.start_tick <= tick:
            delay_ticks = int((ev.parameters or {}).get("delay_ticks", 2))
            depots_str = ", ".join((ev.parameters or {}).get("depot_ids", [])) or "network"
            anomalies.append(Anomaly(
                id=f"delay-event-{ev.id}",
                dedup_key=f"delay-event-{ev.id}",
                category="SUPPLY_DELAYED",
                severity="CRITICAL" if delay_ticks >= 4 else "WARNING",
                entity_type="supply",
                entity_id=f"event-{ev.id}",
                source="RULE",
                observed_value=float(delay_ticks),
                expected_value=0.0,
                summary=f"Depot replenishment/shipment delay event #{ev.id} (+{delay_ticks} ticks on {depots_str}).",
                evidence=[f"event type=shipment_delay ticks {ev.start_tick}..{ev.end_tick}, delay_ticks={delay_ticks}"],
                suggestion="Account for delayed inbound depot arrivals when scheduling high-volume transfers.",
            ))

    anomalies.sort(key=lambda item: (0 if item.severity == "CRITICAL" else 1, item.id))
    return anomalies


def merge_ml_anomalies(
    rule_anomalies: list[Anomaly],
    ml_scores: list[dict[str, Any]],
    *,
    snapshot: Snapshot,
    station_details: dict[str, StationDetail],
    events: list[SimulatorEvent],
) -> list[Anomaly]:
    """Combine authoritative rules with Isolation Forest scores while distinguishing expected spikes."""
    if not ml_scores:
        return rule_anomalies
    tick = snapshot.simulation.tick
    station_names = {s.id: s.name for s in snapshot.stations}
    out = list(rule_anomalies)
    rule_spike_keys = {(a.entity_id, a.fuel_type) for a in rule_anomalies if a.category == "DEMAND_SPIKE"}

    for score_row in ml_scores:
        if not score_row.get("is_anomaly"):
            continue
        sid = score_row["station_id"]
        fuel = score_row["fuel_type"]
        detail = station_details.get(sid)
        known_spike = (detail and detail.demand_multiplier > 1.05) or any(
            e.type == "demand_spike" and e.start_tick <= score_row["tick"] < e.end_tick for e in events
        )
        if (sid, fuel) in rule_spike_keys:
            # Enrich existing spike alert to HYBRID
            for idx, item in enumerate(out):
                if item.category == "DEMAND_SPIKE" and item.entity_id == sid and item.fuel_type == fuel:
                    out[idx] = item.model_copy(
                        update={
                            "source": "HYBRID",
                            "score": score_row["anomaly_score"],
                            "evidence": [*item.evidence, f"IsolationForest score={score_row['anomaly_score']} (< {score_row['threshold']})"],
                        }
                    )
            continue

        if known_spike:
            continue

        st_name = station_names.get(sid, sid)
        out.append(Anomaly(
            id=f"iforest-{sid}-{fuel}",
            dedup_key=f"iforest-{sid}-{fuel}",
            category="UNEXPECTED_DEMAND_ANOMALY",
            severity="WARNING",
            entity_type="station",
            entity_id=sid,
            fuel_type=fuel,
            source="ML",
            score=score_row["anomaly_score"],
            summary=(
                f"Isolation Forest flagged unexpected {st_name} {fuel} telemetry "
                f"(score {score_row['anomaly_score']:.3f} < {score_row['threshold']:.3f})."
            ),
            evidence=[
                f"normalized_demand_ratio={score_row['normalized_demand_ratio']}",
                f"rolling_residual_z={score_row['rolling_residual_z']}",
                f"tick={score_row['tick']}",
            ],
            suggestion="Inspect station demand trend and recent shipments for unannounced demand shift.",
        ))

    out.sort(key=lambda item: (0 if item.severity == "CRITICAL" else 1, item.id))
    return out
