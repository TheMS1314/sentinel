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
    <p className="text-sm text-textmuted">Pauses new payment dispatches. Already claimed orders may finish. Paused payments keep their budget reservation and resume through the outbox when controls allow.</p>
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
      <p className="text-sm text-textmuted">Local evidence and Sentinel controls. Mastercard Decision Intelligence, Recorded Future and RiskRecon are not connected. Missing evidence is unknown, not a safe rating. Merchant/domain bindings are operator assertions.</p></div>
    {error && <p role="alert" className="text-block">{error} Use Refresh after a configuration conflict.</p>}
    {notice && <p role="status" className="text-approve">{notice}</p>}
    <button className="rounded-lg border border-accent/40 bg-accent/15 px-4 py-2 text-sm text-accent disabled:opacity-50 self-start" disabled={busy} onClick={refresh}>Refresh risk controls</button>
    {!data && !error && <p className="text-textmuted">Loading risk controls…</p>}
    {data && <>
      <div className="bg-surface border border-border rounded-xl p-5">
        <p className="text-sm text-textmuted mb-3">{data.current_indicators} current indicators · {data.merchant_profiles} merchant profiles · {data.pending_payments} pending payments</p>
        <GuardForm key={data.guard.version} guard={data.guard} onSave={body => save(api.saveRiskGuard, body)} busy={busy} />
      </div>
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
