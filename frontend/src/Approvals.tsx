import { useRef, useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import { fetchJson } from "./api";

export type Proposal = {
  source_depot_id: string;
  destination_station_id: string;
  route_id: string;
  fuel_type: string;
  quantity: number;
};

type Intent = { intent_id: string; proposal: Proposal };

type Attempt = Intent & {
  state: "READY" | "UNKNOWN" | "CONFIRMED" | "REJECTED";
  message: string | null;
  shipment: { id: number; status: string; failure_reason: string | null } | null;
};

type Ledger = { attempts: Attempt[]; simulator_error: string | null };

const outboxKey = "bup.approval-intent";
const endpoint = "/api/approvals";

function readOutbox(): Intent | null {
  try {
    return JSON.parse(localStorage.getItem(outboxKey) ?? "null") as Intent | null;
  } catch {
    return null;
  }
}

export function useApprovals() {
  const [outbox, setOutbox] = useState(readOutbox);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const busyRef = useRef(false);
  const { mutate } = useSWRConfig();
  const ledger = useSWR(endpoint, fetchJson<Ledger>, { refreshInterval: 10_000 });
  const attempts = ledger.data?.attempts ?? [];
  const simulatorError = ledger.data?.simulator_error ?? ledger.error?.message;

  async function send(intent: Intent) {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setNotice("");
    try {
      localStorage.setItem(outboxKey, JSON.stringify(intent));
      setOutbox(intent);
      const attempt = await fetchJson<Attempt>(`${endpoint}/${intent.intent_id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(intent.proposal),
      });
      localStorage.removeItem(outboxKey);
      setOutbox(null);
      setNotice(attempt.shipment
        ? `Simulator allocation #${attempt.shipment.id} confirmed.`
        : attempt.message ?? "Approval outcome recorded.");
      await ledger.mutate(current => ({
        attempts: [attempt, ...(current?.attempts ?? []).filter(row => row.intent_id !== attempt.intent_id)],
        simulator_error: current?.simulator_error ?? null,
      }));
      void mutate("/api/snapshot");
      void mutate("/api/planning");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Approval request interrupted.");
      await ledger.mutate();
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }

  function approve(proposal: Proposal) {
    if (outbox || simulatorError || !ledger.data) return;
    return send({ intent_id: crypto.randomUUID(), proposal });
  }

  function pending(proposal: Proposal) {
    return attempts.some(row => (row.state === "READY" || row.state === "UNKNOWN")
      && row.proposal.route_id === proposal.route_id && row.proposal.fuel_type === proposal.fuel_type);
  }

  return { approve, send, pending, busy, notice, outbox, attempts, simulatorError, ready: !!ledger.data };
}

export function ShipmentPanel({ approvals }: { approvals: ReturnType<typeof useApprovals> }) {
  const { attempts, outbox, send, busy, notice, simulatorError } = approvals;
  const labels = { READY: "Waiting for fresh state", UNKNOWN: "Outcome unknown", CONFIRMED: "Confirmed", REJECTED: "Rejected" };
  return (
    <section className="panel shipment-panel" aria-labelledby="shipments-title">
      <div className="panel-heading"><h2 id="shipments-title">Approval and shipment status</h2></div>
      {notice && <p role="status">{notice}</p>}
      {simulatorError && <p role="alert">{simulatorError} Showing last known shipment statuses.</p>}
      {outbox && (
        <div className="banner error-banner">
          <span>Approval response interrupted. Retry the stored intent to confirm its outcome.</span>
          <button disabled={busy} onClick={() => void send(outbox)}>Retry saved approval</button>
        </div>
      )}
      {attempts.length === 0 ? <p>No approvals recorded.</p> : (
        <div className="planning-scroll" tabIndex={0} role="region" aria-label="Approval history">
          <table className="planning-table">
            <thead><tr><th>Route</th><th>Fuel / quantity</th><th>Status</th><th>Details</th></tr></thead>
            <tbody>
              {attempts.map(attempt => (
                <tr key={attempt.intent_id}>
                  <th scope="row">{attempt.proposal.route_id}</th>
                  <td>{attempt.proposal.fuel_type} · {attempt.proposal.quantity.toLocaleString()} L</td>
                  <td>{attempt.shipment ? `#${attempt.shipment.id} · ${attempt.shipment.status.replaceAll("_", " ")}` : labels[attempt.state]}</td>
                  <td className="shipment-details">
                    {attempt.shipment?.failure_reason ?? attempt.message}
                    {(attempt.state === "UNKNOWN" || attempt.state === "READY") && (
                      <button className="refresh-button" disabled={busy || !!outbox} onClick={() => {
                        const { source_depot_id, destination_station_id, route_id, fuel_type, quantity } = attempt.proposal;
                        void send({ intent_id: attempt.intent_id, proposal: {
                          source_depot_id, destination_station_id, route_id, fuel_type, quantity,
                        } });
                      }}>Retry approval</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
