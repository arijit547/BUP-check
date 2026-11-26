import { useMemo, useState } from "react";
import useSWR from "swr";
import { fetchJson, fetchJsonWithMeta } from "./api";
import PlanningPanel from "./PlanningPanel";
import { HealthPanel } from "./ObservabilityPanel";
import { ShipmentPanel, useApprovals } from "./Approvals";
import { AuthBar } from "./AuthBar";
import { NetworkTopology } from "./NetworkTopology";
import { ForecastExplorer } from "./ForecastExplorer";
import { ShortageIncidents } from "./ShortageIncidents";
import { AllocationPlanner } from "./AllocationPlanner";
import { EvaluationHealth } from "./EvaluationHealth";
import type { AuthStatus } from "./types";
import { Activity, AlertTriangle, Boxes, CircleHelp, Clock3, Cpu, Fuel, LineChart, MapPin, Network, RefreshCw, Route as RouteIcon, ShieldAlert, ShieldCheck, SlidersHorizontal, Truck } from "lucide-react";

type FuelVolumes = { DIESEL: number; PETROL: number; OCTANE: number };
type Place = { id: string; name: string; region_id: string; inventory: FuelVolumes; capacity: FuelVolumes; status: string; dispatch_capacity_per_tick?: number };
type Snapshot = {
  simulation: { tick: number; tick_minutes: number; sim_time: string; status: "PAUSED" | "RUNNING" };
  depots: Place[];
  stations: Place[];
  routes: { id: string; source_depot_id: string; destination_station_id: string; status: "AVAILABLE" | "DISRUPTED"; transit_ticks: number; max_shipment: number }[];
};
const fuels: (keyof FuelVolumes)[] = ["DIESEL", "PETROL", "OCTANE"];
const labels: Record<keyof FuelVolumes, string> = { DIESEL: "Diesel", PETROL: "Petrol", OCTANE: "Octane" };
const number = (value: number) => new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(value);
const percent = (inventory: number, capacity: number) => capacity > 0 ? Math.round(inventory / capacity * 100) : 0;

export default function App() {
  const { data, error, isLoading, isValidating, mutate } = useSWR("/api/snapshot", async (url: string) => {
    const { data: snap, headers } = await fetchJsonWithMeta<Snapshot>(url);
    const observedHdr = headers.get("x-bup-observed-at");
    const observedDate = observedHdr ? new Date(observedHdr) : new Date();
    const observedLabel = Number.isNaN(observedDate.getTime())
      ? new Date().toLocaleTimeString()
      : observedDate.toLocaleTimeString();
    const asOfTick = Number(headers.get("x-bup-as-of-tick") ?? snap.simulation.tick);
    const currentTick = Number(headers.get("x-bup-current-tick") ?? snap.simulation.tick);
    const ageTicks = Number(headers.get("x-bup-age-ticks") ?? Math.max(0, currentTick - asOfTick));
    return {
      ...snap,
      observedLabel,
      receivedAt: new Date().toLocaleTimeString(),
      asOfTick,
      currentTick,
      ageTicks,
      cached: headers.get("x-bup-cached") === "true",
      degraded: headers.get("x-bup-degraded") === "true",
    };
  }, { refreshInterval: 10000, revalidateOnFocus: true, shouldRetryOnError: true, errorRetryCount: 5, errorRetryInterval: 3000 });
  const approvals = useApprovals();
  const { data: auth, mutate: mutateAuth } = useSWR("/api/auth/session", fetchJson<AuthStatus>, { refreshInterval: 30000 });
  const authLocked = Boolean(auth?.auth_required && !auth?.authenticated);
  const stale = error || approvals.simulatorError || data?.degraded;
  const [activeTab, setActiveTab] = useState<"overview" | "network" | "forecasts" | "shortages" | "optimizer" | "models">("overview");
  const [routeId, setRouteId] = useState("");
  const [fuel, setFuel] = useState<keyof FuelVolumes>("DIESEL");
  const [quantity, setQuantity] = useState(3000);

  const route = data?.routes.find(item => item.id === routeId) ?? data?.routes.find(item => item.status === "AVAILABLE");
  const source = route && data?.depots.find(item => item.id === route.source_depot_id);
  const destination = route && data?.stations.find(item => item.id === route.destination_station_id);
  const preview = useMemo(() => {
    if (!route || !source || !destination) return { errors: ["No route and inventory data are available yet."], remaining: 0 };
    const errors: string[] = [];
    if (route.status !== "AVAILABLE") errors.push("This route is disrupted.");
    if (!Number.isFinite(quantity) || quantity <= 0) errors.push("Enter a quantity greater than zero.");
    if (quantity > route.max_shipment) errors.push(`Route limit is ${number(route.max_shipment)} L.`);
    if (quantity > source.inventory[fuel]) errors.push(`${source.name} has only ${number(source.inventory[fuel])} L available.`);
    const remaining = destination.capacity[fuel] - destination.inventory[fuel];
    if (quantity > remaining) errors.push(`${destination.name} has room for ${number(Math.max(0, remaining))} L.`);
    if (source.status !== "OPEN" && source.status !== "CONSTRAINED") errors.push("The source depot is closed.");
    if (destination.status !== "OPEN") errors.push("The destination station is closed.");
    if (source.dispatch_capacity_per_tick !== undefined && quantity > source.dispatch_capacity_per_tick) errors.push(`Depot dispatch limit is ${number(source.dispatch_capacity_per_tick)} L per tick.`);
    return { errors, remaining };
  }, [route, source, destination, quantity, fuel]);

  const alerts = data?.stations.flatMap(station => fuels.flatMap(type => {
    const level = percent(station.inventory[type], station.capacity[type]);
    return level < 35 ? [{ station, fuel: type, level, critical: level < 20 }] : [];
  })) ?? [];

  return <div className="app-shell">
    <aside className="rail"><div className="brand-mark"><Fuel size={22} /></div><div className="rail-line"/><div className="rail-current"><Activity size={19}/></div><div className="rail-bottom"><CircleHelp size={18}/></div></aside>
    <main className="workspace">
      <header className="topbar"><div className="brand-name"><span className="brand-mini"><Fuel size={15}/></span> BUP <span className="muted">/</span> FUEL OPERATIONS</div><div className="top-actions"><AuthBar auth={auth ?? null} onAuthChange={(next) => { void mutateAuth(next, false); void mutate(); }} /><span className="live-pill"><i/> SIMULATION ENVIRONMENT</span><button className="refresh-button" onClick={() => void mutate()} disabled={isValidating} aria-label="Refresh snapshot"><RefreshCw size={15} className={isValidating ? "spin" : ""}/><span>Refresh</span></button><div className="avatar">BO</div></div></header>
      <section className="page-intro"><div><div className="eyebrow">OPERATIONS CENTER <span>·</span> BANGLADESH NETWORK</div><h1>Fuel network overview</h1><p>Monitor stock positions and review a dispatch against live simulator constraints.</p></div><div className="updated"><span className={stale ? "updated-dot stale-dot" : "updated-dot"}/><div><b>{data ? `TICK ${String(data.simulation.tick).padStart(3, "0")}` : "CONNECTING"}</b><small>{data ? `${stale ? "Stale · last validated" : "Updated"} at ${data.observedLabel}` : "Waiting for simulator"}</small>{data && <div className="muted-inline" style={{ fontSize: 11, marginTop: 2 }}>as of t{data.asOfTick} / sim t{data.currentTick} · age {data.ageTicks}t · {data.cached ? "CACHED" : "FRESH"}{data.degraded ? " · DEGRADED" : ""} · HTTP {data.receivedAt}</div>}</div></div></section>
      {stale && <div className="banner error-banner" role="alert"><AlertTriangle size={17}/><span>{error?.message ?? approvals.simulatorError ?? (data?.degraded ? "Serving cached snapshot while simulator advances." : "")}{data && ` · Showing validated snapshot from ${data.observedLabel}.`}</span><button onClick={() => void mutate()}>Retry</button></div>}
      {isLoading && !data ? <section className="loading-card"><span className="loader"/><h2>Connecting to the simulator</h2><p>Loading the current network snapshot…</p></section> : data && <>
        <section className="metrics-grid">
          <article className="metric-card"><div className="metric-top"><span>SIMULATION CLOCK</span><Clock3 size={17}/></div><div className="metric-value">{new Date(data.simulation.sim_time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })}<small> UTC</small></div><div className="metric-foot">Tick {data.simulation.tick} <span>·</span> {data.simulation.tick_minutes} min intervals</div></article>
          <article className="metric-card"><div className="metric-top"><span>ENGINE STATUS</span><Activity size={17}/></div><div className="metric-value status-value"><i className={data.simulation.status === "RUNNING" ? "good" : "paused"}/>{data.simulation.status === "RUNNING" ? "Running" : "Paused"}</div><div className="metric-foot">Official BUP simulator instance</div></article>
          <article className="metric-card"><div className="metric-top"><span>NETWORK FOOTPRINT</span><MapPin size={17}/></div><div className="metric-value">{data.depots.length + data.stations.length}<small> locations</small></div><div className="metric-foot">{data.depots.length} depots <span>·</span> {data.stations.length} stations</div></article>
          <article className="metric-card"><div className="metric-top"><span>OPEN CORRIDORS</span><RouteIcon size={17}/></div><div className="metric-value">{data.routes.filter(item => item.status === "AVAILABLE").length}<small> / {data.routes.length}</small></div><div className="metric-foot">Routes available for preview</div></article>
        </section>

        <nav className="nav-tabs" aria-label="Operations views">
          <button type="button" className={`nav-tab ${activeTab === "overview" ? "active" : ""}`} onClick={() => setActiveTab("overview")}><Activity size={14}/> Overview</button>
          <button type="button" className={`nav-tab ${activeTab === "network" ? "active" : ""}`} onClick={() => setActiveTab("network")}><Network size={14}/> Network</button>
          <button type="button" className={`nav-tab ${activeTab === "forecasts" ? "active" : ""}`} onClick={() => setActiveTab("forecasts")}><LineChart size={14}/> Forecasts</button>
          <button type="button" className={`nav-tab ${activeTab === "shortages" ? "active" : ""}`} onClick={() => setActiveTab("shortages")}><ShieldAlert size={14}/> Shortages & Incidents</button>
          <button type="button" className={`nav-tab ${activeTab === "optimizer" ? "active" : ""}`} onClick={() => setActiveTab("optimizer")}><SlidersHorizontal size={14}/> Optimizer & What-If</button>
          <button type="button" className={`nav-tab ${activeTab === "models" ? "active" : ""}`} onClick={() => setActiveTab("models")}><Cpu size={14}/> Models & Eval</button>
        </nav>

        {activeTab === "overview" && <>
          <section className="section-block"><div className="section-heading"><div><div className="eyebrow">STORAGE POSITIONS</div><h2>Inventory by location</h2></div><div className="fuel-legend">{fuels.map(type => <span key={type}><i className={`legend-dot ${type.toLowerCase()}`}/>{labels[type]}</span>)}</div></div>
            <div className="inventory-groups"><InventoryGroup title="DEPOTS" icon={<Boxes size={16}/>} locations={data.depots} fuels={fuels}/><InventoryGroup title="STATIONS" icon={<MapPin size={16}/>} locations={data.stations} fuels={fuels}/></div>
          </section>

          <PlanningPanel approvals={approvals} snapshotUnavailable={!data || !!error || authLocked} />
          <HealthPanel />
          <section className="lower-grid"><div className="panel alerts-panel"><div className="panel-heading"><div><div className="eyebrow">STATION WATCH</div><h2>Stock warnings</h2></div><span className={`count-badge ${alerts.length ? "warning-count" : ""}`}>{alerts.length} alerts</span></div><div className="warning-note"><AlertTriangle size={14}/> Inventory-based warnings only. These are not demand forecasts.</div>{alerts.length ? <div className="alert-list">{alerts.map(alert => <div className="alert-row" key={`${alert.station.id}-${alert.fuel}`}><div className={`alert-icon ${alert.critical ? "critical" : ""}`}><AlertTriangle size={16}/></div><div className="alert-copy"><b>{alert.station.name}</b><span>{labels[alert.fuel]} <i>·</i> {number(alert.station.inventory[alert.fuel])} L remaining</span></div><span className={`level ${alert.critical ? "critical-text" : ""}`}>{alert.level}% <small>capacity</small></span></div>)}</div> : <div className="empty-healthy"><ShieldCheck size={20}/><span>No station inventory is below the 35% warning threshold.</span></div>}</div>
            <div className="panel preview-panel"><div className="panel-heading"><div><div className="eyebrow">READ-ONLY TOOL</div><h2>Allocation preview</h2></div><span className="preview-label">NO DISPATCH</span></div><div className="form-grid"><label>Route<select value={route?.id ?? ""} onChange={event => setRouteId(event.target.value)}>{data.routes.map(item => <option key={item.id} value={item.id} disabled={item.status !== "AVAILABLE"}>{data.depots.find(d => d.id === item.source_depot_id)?.name ?? item.source_depot_id} → {data.stations.find(s => s.id === item.destination_station_id)?.name ?? item.destination_station_id}{item.status === "DISRUPTED" ? " · Disrupted" : ""}</option>)}</select></label><label>Fuel type<select value={fuel} onChange={event => setFuel(event.target.value as keyof FuelVolumes)}>{fuels.map(type => <option key={type} value={type}>{labels[type]}</option>)}</select></label><label className="quantity-field">Quantity <span>(liters)</span><input type="number" min="0" step="100" value={quantity} onChange={event => setQuantity(Number(event.target.value))}/></label></div><div className="route-detail"><span><Truck size={15}/> Transit estimate</span><b>{route ? `${route.transit_ticks} ticks · ${route.transit_ticks * data.simulation.tick_minutes} min` : "No available route"}</b></div><div className={`preview-result ${preview.errors.length ? "preview-invalid" : "preview-valid"}`}>{preview.errors.length ? <><AlertTriangle size={17}/><div><b>Cannot fit this preview</b>{preview.errors.map(message => <p key={message}>{message}</p>)}</div></> : <><ShieldCheck size={19}/><div><b>Fits current snapshot</b><p>{number(quantity)} L {labels[fuel].toLowerCase()} from {source?.name} to {destination?.name}. {number(preview.remaining - quantity)} L capacity remains after this hypothetical shipment.</p></div></>}</div><p className="read-only-foot">Preview uses current inventory and route limits only. No allocation is submitted.</p></div></section>
        </>}
        {activeTab === "network" && <NetworkTopology depots={data.depots} stations={data.stations} routes={data.routes} tickMinutes={data.simulation.tick_minutes} />}
        {activeTab === "forecasts" && <ForecastExplorer asOfTick={data.simulation.tick} />}
        {activeTab === "shortages" && <ShortageIncidents asOfTick={data.simulation.tick} />}
        {activeTab === "optimizer" && <AllocationPlanner routes={data.routes} tick={data.simulation.tick} disabled={!!stale || authLocked} onActionComplete={() => { void mutate(); }} />}
        {activeTab === "models" && <EvaluationHealth onRebound={() => { void mutate(); }} />}

      </>}
      <ShipmentPanel approvals={approvals} />
      <footer className="footer">
        <span>{stale ? "SIMULATOR DATA STALE" : data ? "CONNECTED TO BUP SIMULATOR" : "CONNECTING TO SIMULATOR"}</span>
        <span>AUTO-REFRESH 10S</span>
      </footer>
    </main>
  </div>;
}

function InventoryGroup({ title, icon, locations, fuels }: { title: string; icon: React.ReactNode; locations: Place[]; fuels: (keyof FuelVolumes)[] }) {
  return <article className="inventory-group"><header><span>{icon}{title}</span><small>{locations.length} FACILITIES</small></header><div className="location-list">{locations.map(location => <div className="location-card" key={location.id}><div className="location-title"><div><b>{location.name}</b><span>{location.region_id.replace("region-", "").toUpperCase()}</span></div><span className={`facility-status ${location.status === "OPEN" ? "" : "constrained"}`}><i/>{location.status}</span></div><div className="fuel-columns">{fuels.map(type => { const stock = location.inventory[type]; const capacity = location.capacity[type]; const ratio = percent(stock, capacity); return <div className="fuel-position" key={type}><div className="fuel-position-heading"><span>{labels[type]}</span><b>{number(stock)} <small>L</small></b></div><div className="progress-track"><i className={`progress-fill ${type.toLowerCase()} ${ratio < 35 ? "low" : ""}`} style={{ width: `${Math.min(100, ratio)}%` }}/></div><div className="capacity-line"><span>{ratio}% stocked</span><span>Cap. {number(capacity)} L</span></div></div>; })}</div></div>)}</div></article>;
}
