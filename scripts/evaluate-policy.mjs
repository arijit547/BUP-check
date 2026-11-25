import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { admin, json, seedHistory } from "./simulator.mjs";

const fuels = ["DIESEL", "PETROL", "OCTANE"];
const evaluationEnd = 150;
const cadence = 6;
const disruptions = [
  { type: "demand_spike", start_tick: 42, duration_ticks: 16,
    parameters: { station_ids: ["station-mirpur"], multiplier: 1.8 } },
  { type: "route_disruption", start_tick: 42, duration_ticks: 12,
    parameters: { route_ids: ["route-gazipur-mirpur"] } },
];

function reorder(snapshot) {
  const depots = new Map(snapshot.depots.map(depot => [depot.id, depot]));
  const choices = [];
  for (const station of snapshot.stations.filter(item => item.status === "OPEN")) {
    for (const fuel of fuels) {
      const stock = station.inventory[fuel];
      const capacity = station.capacity[fuel];
      const ratio = capacity > 0 ? stock / capacity : 1;
      if (ratio >= 0.35) continue;
      for (const route of snapshot.routes) {
        if (route.status !== "AVAILABLE" || route.destination_station_id !== station.id) continue;
        const depot = depots.get(route.source_depot_id);
        const limit = Math.min(capacity * 0.6 - stock, depot.inventory[fuel],
          depot.dispatch_capacity_per_tick, route.max_shipment);
        const quantity = Math.floor(limit * 1000) / 1000;
        if (quantity > 0) choices.push({
          source_depot_id: depot.id, destination_station_id: station.id,
          route_id: route.id, fuel_type: fuel, quantity, ratio, eta: route.transit_ticks,
        });
      }
    }
  }
  choices.sort((a, b) => a.ratio - b.ratio || a.eta - b.eta || a.route_id.localeCompare(b.route_id));
  return choices[0];
}

export async function evaluatePolicy(mode, env) {
  const simulator = env.BUP_TEST_SIMULATOR;
  const dashboard = env.BUP_TEST_DASHBOARD;
  await seedHistory(simulator);
  const start = await json(simulator + "/v1/instance");
  for (const event of disruptions) await admin(simulator, "events", event);
  const decisions = [];
  for (let tick = start.tick; tick < evaluationEnd; tick++) {
    const { tick: next } = await admin(simulator, "step");
    if ((next - start.tick - 1) % cadence || next + cadence > evaluationEnd) continue;

    const pick = mode === "forecast-policy"
      ? (await json(dashboard + "/api/planning")).recommendations[0]
      : reorder(await json(dashboard + "/api/snapshot"));
    if (!pick) continue;
    const { source_depot_id, destination_station_id, route_id, fuel_type } = pick;
    const proposal = { source_depot_id, destination_station_id, route_id, fuel_type,
      quantity: pick.quantity_liters ?? pick.quantity };
    const attempt = await json(dashboard + "/api/approvals/" + randomUUID(), {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(proposal),
    });
    assert.equal(attempt.state, "CONFIRMED", attempt.message);
    decisions.push({ tick: next, ...proposal, allocation_id: attempt.shipment.id });
  }
  const metrics = await json(simulator + "/v1/metrics");
  const allocations = await json(simulator + "/v1/allocations");
  assert.equal(allocations.length, decisions.length);
  assert.ok(allocations.every(item => !["PENDING", "IN_TRANSIT"].includes(item.status)));
  const result = {
    policy: mode, seed: start.seed, start_tick: start.tick, end_tick: evaluationEnd,
    decision_every_ticks: cadence, events: disruptions, decisions, metrics,
  };
  await writeFile(resolve(env.BUP_TEST_OUTPUT, "policy.json"), JSON.stringify(result, null, 2) + "\n");
  console.log(JSON.stringify({ policy: mode, allocations: allocations.length, ...metrics }, null, 2));
}
