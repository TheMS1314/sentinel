import { useEffect, useState } from "react";
import { api } from "../api";

function Field({ label, children }) {
  return <label className="flex flex-col gap-1 text-sm text-textmuted"><span>{label}</span>{children}</label>;
}

function GuardForm({ guard, onSave, busy }) {
  const [frozen, setFrozen] = useState(guard.frozen);
  const [windowSeconds, setWindow] = useState(guard.window_seconds);
  const [count, setCount] = useState(guard.max_dispatches ?? "");
  const [amount, setAmount] = useState(guard.max_amount_paise == null ? "" : guard.max_amount_paise / 100);
  const [reason, setReason] = useState("");
  return <form className="flex flex-col gap-3" onSubmit={e => {
    e.preventDefault();
    onSave({expected_version: guard.version, frozen, window_seconds: Number(windowSeconds),
      max_dispatches: count === "" ? null : Number(count),
      max_amount_paise: amount === "" ? null : Math.round(Number(amount) * 100), reason});
  }}>
    <h3 className="text-text font-semibold">Emergency payment guard</h3>
    <p className="text-sm text-textmuted">Pauses new payment dispatches. Already claimed orders may finish. Paused payments keep their budget reservation and can resume through the outbox while the approval is valid. Pending approvals expire after five minutes by default; expired or cancelled reservations are released.</p>
    <label className="flex gap-2 text-text"><input type="checkbox" checked={frozen} onChange={e => setFrozen(e.target.checked)} />Freeze payment dispatch</label>
    <div className="grid sm:grid-cols-3 gap-3">
      <Field label="Rolling window (seconds)"><input className="input" type="number" min="1" max="3600" required value={windowSeconds} onChange={e => setWindow(e.target.value)} /></Field>
      <Field label="Maximum dispatches (blank = unlimited)"><input className="input" type="number" min="1" max="100000" value={count} onChange={e => setCount(e.target.value)} /></Field>
      <Field label="Maximum dispatch amount (₹; blank = unlimited)"><input className="input" type="number" min="0.01" step="0.01" max="1000000000" value={amount} onChange={e => setAmount(e.target.value)} /></Field>
    </div>
    <Field label="Reason for guard change"><input className="input" required maxLength="2000" value={reason} onChange={e => setReason(e.target.value)} /></Field>
    <button className="rounded-lg border border-accent/40 bg-accent/15 px-4 py-2 text-sm text-accent disabled:opacity-50 self-start" disabled={busy}>Save payment guard</button>
  </form>;
}

function EvidenceForm({ kind, onSave, busy }) {
  const [domain, setDomain] = useState("");
  const [source, setSource] = useState("synthetic");
  const [evidence, setEvidence] = useState("");
  const [expires, setExpires] = useState("");
  const [owner, setOwner] = useState("");
  const [merchant, setMerchant] = useState("");
  const [score, setScore] = useState(0);
  const [indicator, setIndicator] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [active, setActive] = useState(true);
  const isMerchant = kind === "merchant";
  return <form className="flex flex-col gap-3" onSubmit={e => {
    e.preventDefault();
    const common = {domain, source, evidence, expires_at: new Date(expires).toISOString()};
    onSave(isMerchant ? {...common, owner_id: owner, merchant_name: merchant, cyber_risk_score: Number(score)}
      : {...common, indicator_id: indicator, confirmed, active});
  }}>
    <h3 className="font-semibold text-text">{isMerchant ? "Merchant/vendor profile" : "Threat indicator"}</h3>
    {isMerchant ? <>
      <Field label="Profile owner ID"><input className="input" required maxLength="128" value={owner} onChange={e => setOwner(e.target.value)} /></Field>
      <Field label="Merchant name"><input className="input" required maxLength="200" value={merchant} onChange={e => setMerchant(e.target.value)} /></Field>
      <Field label="Cyber exposure score (0–100; heuristic)"><input className="input" type="number" required min="0" max="100" value={score} onChange={e => setScore(e.target.value)} /></Field>
    </> : <>
      <Field label="Indicator ID (reuse to update or deactivate)"><input className="input" required maxLength="128" value={indicator} onChange={e => setIndicator(e.target.value)} /></Field>
      <label className="text-sm text-textmuted"><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)} /> Operator confirmed</label>
      <label className="text-sm text-textmuted"><input type="checkbox" checked={active} onChange={e => setActive(e.target.checked)} /> Active indicator</label>
    </>}
    <Field label={isMerchant ? "Profile domain (exact hostname)" : "Indicator domain (exact hostname)"}><input className="input" required maxLength="253" value={domain} onChange={e => setDomain(e.target.value)} /></Field>
    <Field label={isMerchant ? "Profile source" : "Indicator source"}><select className="input" value={source} onChange={e => setSource(e.target.value)}><option value="synthetic">Synthetic/demo</option><option value="operator">Operator evidence</option></select></Field>
    <Field label={isMerchant ? "Profile evidence/reason" : "Indicator evidence/reason"}><textarea className="input" required maxLength="2000" value={evidence} onChange={e => setEvidence(e.target.value)} /></Field>
    <Field label={isMerchant ? "Profile expires (your local time)" : "Indicator expires (your local time)"}><input className="input" type="datetime-local" required value={expires} onChange={e => setExpires(e.target.value)} /></Field>
    <button className="rounded-lg border border-accent/40 bg-accent/15 px-4 py-2 text-sm text-accent disabled:opacity-50 self-start" disabled={busy}>Save {isMerchant ? "merchant profile" : "threat indicator"}</button>
  </form>;
}

function PendingPayments({ payments, busy, onCancel }) {
  const [reason, setReason] = useState("");
  return <div className="bg-surface border border-border rounded-xl p-5 flex flex-col gap-3">
    <h3 className="font-semibold text-text">Undispatched payments</h3>
    <p className="text-sm text-textmuted">Review these before unfreezing. Cancellation is available only before dispatch; processing and unknown outcomes require reconciliation. Showing up to 20 pending reservations.</p>
    <Field label="Reason for cancelling a pending payment"><input className="input" value={reason} onChange={e => setReason(e.target.value)} maxLength="2000" /></Field>
    {payments.length === 0 && <p className="text-sm text-textmuted">No pending reservations.</p>}
    <ul className="flex flex-col gap-3">{payments.map(payment => <li key={payment.request_id} className="border border-border rounded-lg p-3 text-sm text-textmuted">
      <p className="break-all font-mono">{payment.request_id}</p>
      <p>₹{(payment.amount_paise / 100).toFixed(2)} · expires {payment.expires_at ? new Date(payment.expires_at).toLocaleString() : "before dispatch"}</p>
      {payment.reason && <p>{payment.reason}</p>}
      <button className="mt-2 rounded border border-block/40 px-3 py-1 text-block disabled:opacity-50" disabled={busy || !reason.trim()}
        onClick={() => onCancel({requestId: payment.request_id, reason: reason.trim()})}>Cancel {payment.request_id}</button>
    </li>)}</ul>
  </div>;
}

export default function RiskIntelligence() {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  useEffect(() => {
    let active = true;
    api.riskOverview().then(value => {if (active) setData(value);}).catch(e => {if (active) setError(e.message);});
    return () => {active = false;};
  }, []);
  async function save(method, body) {
    setBusy(true); setError(""); setNotice("");
    try {
      await method(body);
      setData(await api.riskOverview());
      setNotice("Saved and recorded in the audit history.");
    } catch (e) {setError(e.message);}
    finally {setBusy(false);}
  }
  async function refresh() {
    setBusy(true);
    try {setData(await api.riskOverview());setError("");}
    catch(e) {setError(e.message);}
    finally {setBusy(false);}
  }
  return <section className="flex flex-col gap-5">
    <div><h2 className="font-semibold text-text">Risk intelligence</h2>
      <p className="text-sm text-textmuted">Local evidence and Sentinel controls. Commercial connection status and refresh controls are in Providers. Mastercard Decision Intelligence remains disconnected. Missing evidence is unknown, not a safe rating. Merchant/domain bindings are operator assertions.</p></div>
    {error && <p role="alert" className="text-block">{error} Use Refresh after a configuration conflict.</p>}
    {notice && <p role="status" className="text-approve">{notice}</p>}
    <button className="rounded-lg border border-accent/40 bg-accent/15 px-4 py-2 text-sm text-accent disabled:opacity-50 self-start" disabled={busy} onClick={refresh}>Refresh risk controls</button>
    {!data && !error && <p className="text-textmuted">Loading risk controls…</p>}
    {data && <>
      <div className="bg-surface border border-border rounded-xl p-5">
        <p className="text-sm text-textmuted mb-3">{data.current_indicators} current indicators · {data.merchant_profiles} merchant profiles · {data.pending_payments} pending payments</p>
        <GuardForm key={data.guard.version} guard={data.guard} onSave={body => save(api.saveRiskGuard, body)} busy={busy} />
      </div>
      <PendingPayments payments={data.pending || []} busy={busy} onCancel={body => save(api.cancelPendingPayment, body)} />
      <div className="grid md:grid-cols-2 gap-5">
        <div className="bg-surface border border-border rounded-xl p-5"><EvidenceForm kind="merchant" onSave={body => save(api.saveMerchantRisk, body)} busy={busy} /></div>
        <div className="bg-surface border border-border rounded-xl p-5"><EvidenceForm kind="indicator" onSave={body => save(api.saveThreatIndicator, body)} busy={busy} /></div>
      </div>
      <div className="bg-surface border border-border rounded-xl p-5"><h3 className="font-semibold text-text">Recent control changes</h3>
        <ul className="text-sm text-textmuted">{data.events.map(event => <li key={event.id}>{event.actor} · {event.kind} · {new Date(event.created_at).toLocaleString()}</li>)}</ul>
      </div>
    </>}
  </section>;
}
