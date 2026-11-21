import {useEffect,useState} from 'react';
import {api} from '../api';
export default function Operations(){
  const [summary,setSummary]=useState(null),[incidents,setIncidents]=useState([]),[alerts,setAlerts]=useState([]);
  const [reason,setReason]=useState(''),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  async function refresh(){const [s,i,a]=await Promise.all([api.opsSummary(),api.opsIncidents(),api.opsAlerts()]);setSummary(s);setIncidents(i);setAlerts(a);}
  useEffect(()=>{let active=true;Promise.all([api.opsSummary(),api.opsIncidents(),api.opsAlerts()]).then(([s,i,a])=>{if(active){setSummary(s);setIncidents(i);setAlerts(a);}}).catch(e=>{if(active)setError(e.message);});return()=>{active=false;};},[]);
  async function change(action){setBusy(true);setError('');try{await action();await refresh();}catch(e){setError(e.message);}finally{setBusy(false);}}
  return <section className="flex flex-col gap-4"><h2 className="font-semibold text-text">Operations</h2>
    <p className="text-sm text-textmuted">Cases remain open until an administrator resolves them. Alerts use signed HTTPS delivery with retries; delivery to a webhook does not prove a human has read it.</p>
    {error&&<p role="alert" className="text-block">{error}</p>}
    <p role="status" className="text-sm text-textmuted">Alert destination: {summary?.delivery||'loading'}. {summary?.delivery==='not_configured'&&'Events are retained locally; no external notification is being sent.'}</p>
    <button className="text-accent self-start" disabled={busy} onClick={()=>change(refresh)}>Refresh operations</button>
    <label className="text-sm text-textmuted">Reason for next case action<input className="input w-full" value={reason} maxLength="2000" onChange={e=>setReason(e.target.value)}/></label>
    <ul className="flex flex-col gap-3">{incidents.map(r=><li className="p-4 rounded border border-border text-sm text-textmuted" key={r.incident_id}><p className="text-text">{r.severity} · {r.kind} · {r.state} · v{r.version}</p><p>{r.summary}</p><p>Condition {r.condition_active?'active':'cleared'} · {r.assigned_to||'unassigned'}</p>{r.last_reason&&<p>Last action: {r.last_reason}</p>}{r.state!=='resolved'&&<div className="flex gap-4 mt-2"><button className="text-accent" disabled={busy||!reason.trim()} onClick={()=>change(()=>api.opsUpdate(r.incident_id,{expected_version:r.version,state:'acknowledged',reason}))}>Acknowledge {r.incident_id}</button><button className="text-accent" disabled={busy||!reason.trim()||(r.condition_active&&!['failed_job','manual_review'].includes(r.kind))} onClick={()=>change(()=>api.opsUpdate(r.incident_id,{expected_version:r.version,state:'resolved',reason}))}>Resolve {r.incident_id}</button></div>}</li>)}</ul>
    <h3 className="text-text">Notification deliveries</h3><ul className="flex flex-col gap-2">{alerts.map(r=><li className="text-sm text-textmuted" key={r.event_id}><code>{r.event_id}</code> · {r.state} · attempts {r.attempts}{r.last_error&&<p>{r.last_error}</p>}{r.state==='failed'&&<button className="text-accent ml-3" disabled={busy||!reason.trim()} onClick={()=>change(()=>api.opsRetry(r.event_id,{reason}))}>Retry {r.event_id}</button>}</li>)}</ul>
  </section>;
}
