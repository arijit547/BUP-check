import { useState } from "react";
import { Network } from "lucide-react";

interface FuelBag {
  DIESEL: number;
  PETROL: number;
  OCTANE: number;
}

interface TopologyDepot {
  id: string;
  name: string;
  status: string;
  dispatch_capacity_per_tick?: number;
  inventory: FuelBag;
}

interface TopologyStation {
  id: string;
  name: string;
  status: string;
  inventory: FuelBag;
  capacity: FuelBag;
}

interface TopologyRoute {
  id: string;
  source_depot_id: string;
  destination_station_id: string;
  status: string;
  transit_ticks: number;
  max_shipment: number;
}

interface NetworkTopologyProps {
  depots: TopologyDepot[];
  stations: TopologyStation[];
  routes: TopologyRoute[];
  tickMinutes: number;
  incomingByStation?: Record<string, number>;
}

const totalLiters = (bag: FuelBag) => Math.round(bag.DIESEL + bag.PETROL + bag.OCTANE);

export function NetworkTopology({
  depots,
  stations,
  routes,
  tickMinutes,
  incomingByStation = {},
}: NetworkTopologyProps) {
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const depotCoords = new Map<string, { x: number; y: number }>();
  depots.forEach((d, idx) => {
    depotCoords.set(d.id, { x: 115, y: 78 + idx * 120 });
  });

  const stationCoords = new Map<string, { x: number; y: number }>();
  stations.forEach((s, idx) => {
    stationCoords.set(s.id, { x: 645, y: 45 + idx * 66 });
  });

  const selectedRouteCount = routes.filter(
    (r) => !selectedId || r.source_depot_id === selectedId || r.destination_station_id === selectedId,
  );

  return (
    <section className="panel section-block" aria-label="Network topology and routes">
      <header className="panel-heading">
        <div>
          <div className="eyebrow">SUPPLY NETWORK SCHEMATIC</div>
          <h2>Depots, corridors & live status</h2>
        </div>
        <span className="count-badge">
          <Network size={12} /> {routes.filter((r) => r.status === "AVAILABLE").length}/{routes.length} routes open
        </span>
      </header>

      <div className="topology-wrap">
        <svg viewBox="0 0 780 280" className="topology-svg" role="img" aria-label="Fuel depot to station topology diagram">
          <defs>
            <marker id="arrow-ok" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
              <path d="M0,0 L6,3 L0,6 Z" fill="#156b52" />
            </marker>
            <marker id="arrow-bad" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
              <path d="M0,0 L6,3 L0,6 Z" fill="#b94f44" />
            </marker>
          </defs>

          {routes.map((route, idx) => {
            const src = depotCoords.get(route.source_depot_id);
            const dst = stationCoords.get(route.destination_station_id);
            if (!src || !dst) return null;
            const ok = route.status === "AVAILABLE";
            const highlighted =
              !selectedId || route.source_depot_id === selectedId || route.destination_station_id === selectedId;
            const midX = (src.x + 88 + (dst.x - 88)) / 2;
            const midY = (src.y + dst.y) / 2 + (idx % 2 === 0 ? -9 : 11);
            return (
              <g key={route.id} opacity={highlighted ? 1 : 0.25}>
                <line
                  x1={src.x + 88}
                  y1={src.y}
                  x2={dst.x - 88}
                  y2={dst.y}
                  stroke={ok ? "#2a8468" : "#c46151"}
                  strokeWidth={ok ? 2.2 : 2}
                  strokeDasharray={ok ? undefined : "5 4"}
                  markerEnd={ok ? "url(#arrow-ok)" : "url(#arrow-bad)"}
                />
                <rect x={midX - 44} y={midY - 9} width="88" height="17" rx="4" fill="#ffffff" stroke="#e1e7e2" />
                <text x={midX} y={midY + 3} textAnchor="middle" fontSize="9" fill={ok ? "#324940" : "#b94f44"}>
                  {route.transit_ticks * tickMinutes}m · {Math.round(route.max_shipment / 1000)}kL
                </text>
              </g>
            );
          })}

          {depots.map((d) => {
            const c = depotCoords.get(d.id)!;
            const constrained = d.status !== "OPEN";
            const active = selectedId === d.id;
            return (
              <g
                key={d.id}
                onClick={() => setSelectedId(active ? null : d.id)}
                className="topology-node"
                role="button"
                tabIndex={0}
                onKeyDown={(e) => e.key === "Enter" && setSelectedId(active ? null : d.id)}
              >
                <rect
                  x={c.x - 95}
                  y={c.y - 32}
                  width="182"
                  height="64"
                  rx="9"
                  fill={active ? "#eef7f1" : "#fbfcfa"}
                  stroke={constrained ? "#d3a342" : active ? "#156b52" : "#cfd9d2"}
                  strokeWidth={active ? 2 : 1.3}
                />
                <text x={c.x - 84} y={c.y - 10} fontSize="11" fontWeight="600" fill="#202d2a">
                  {d.name}
                </text>
                <text x={c.x - 84} y={c.y + 6} fontSize="9.5" fill={constrained ? "#a67d35" : "#478a6b"}>
                  {d.status} · cap {Math.round(d.dispatch_capacity_per_tick ?? 0).toLocaleString()} L/tick
                </text>
                <text x={c.x - 84} y={c.y + 21} fontSize="9.5" fill="#6f7d75">
                  Stock {totalLiters(d.inventory).toLocaleString()} L
                </text>
              </g>
            );
          })}

          {stations.map((s) => {
            const c = stationCoords.get(s.id)!;
            const outage = s.status !== "OPEN";
            const active = selectedId === s.id;
            const pct = Math.round((totalLiters(s.inventory) / Math.max(1, totalLiters(s.capacity))) * 100);
            const inc = incomingByStation[s.id] || 0;
            return (
              <g
                key={s.id}
                onClick={() => setSelectedId(active ? null : s.id)}
                className="topology-node"
                role="button"
                tabIndex={0}
                onKeyDown={(e) => e.key === "Enter" && setSelectedId(active ? null : s.id)}
              >
                <rect
                  x={c.x - 88}
                  y={c.y - 25}
                  width="206"
                  height="50"
                  rx="8"
                  fill={active ? "#eef7f1" : outage ? "#fff6f4" : "#ffffff"}
                  stroke={outage ? "#c46151" : active ? "#156b52" : "#d9e1db"}
                  strokeWidth={active ? 2 : 1.3}
                />
                <text x={c.x - 78} y={c.y - 5} fontSize="11" fontWeight="600" fill="#202d2a">
                  {s.name}
                </text>
                <text x={c.x - 78} y={c.y + 12} fontSize="9.5" fill={outage ? "#b94f44" : "#5e6e66"}>
                  {s.status} · {pct}% full {inc > 0 ? `· +${Math.round(inc).toLocaleString()}L` : ""}
                </text>
              </g>
            );
          })}
        </svg>

        <div className="topology-side">
          <p className="muted-inline">
            {selectedId ? `Filtered to corridors connected to ${selectedId}. Click node again to clear.` : "Click any depot or station to filter connected routes."}
          </p>
          <ul className="route-chip-list">
            {selectedRouteCount.map((r) => (
              <li key={r.id}>
                <span>{r.id}</span>
                <span className={r.status === "AVAILABLE" ? "planning-benefit" : "critical-text"}>
                  {r.status} · {r.transit_ticks}t · max {r.max_shipment.toLocaleString()}L
                </span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </section>
  );
}
