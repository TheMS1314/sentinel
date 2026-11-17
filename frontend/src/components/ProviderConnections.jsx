import {useEffect,useState} from 'react';
import {api} from '../api';
export default function ProviderConnections(){
 const [data,setData]=useState(null),[form,setForm]=useState({merchant_id:'',provider:'recorded_future',external_id:'',expected_version:0}),[error,setError]=useState(''),[notice,setNotice]=useState(''),[busy,setBusy]=useState(false);
 useEffect(()=>{let active=true;api.providers().then(v=>{if(active)setData(v);}).catch(e=>{if(active)setError(e.message);});return()=>{active=false;};},[]);
 async function act(fn){setBusy(true);setError('');setNotice('');try{const v=await fn();setNotice(v.status?`Refresh status: ${v.status}${v.error?' · '+v.error:''}`:'Binding saved. Refresh evidence before use.');setData(await api.providers());}catch(e){setError(e.message);}finally{setBusy(false);}}
 return <section className="flex flex-col gap-4"><h2 className="text-text font-semibold">Provider connections</h2><p className="text-sm text-textmuted">Read-only domain enrichment and cyber ratings require licensed access. A configured credential is unverified until a successful contract run. Bound or required feeds hold payment dispatch when evidence is missing, stale or elevated. Mastercard Decision Intelligence and Safety Net require separate network onboarding.</p>
 {error&&<p role="alert" className="text-block">{error}</p>}{notice&&<p role="status" className="text-textmuted">{notice}</p>}
 <ul className="text-sm text-textmuted">{Object.entries(data?.configuration||{}).map(([name,state])=><li key={name}>{name}: {state}</li>)}</ul>
 <form className="grid sm:grid-cols-2 gap-4" onSubmit={e=>{e.preventDefault();act(()=>api.bindProvider({...form,expected_version:Number(form.expected_version)}));}}>
 <label className="text-sm text-textmuted">Binding merchant ID<input className="input w-full" required value={form.merchant_id} onChange={e=>setForm({...form,merchant_id:e.target.value})}/></label>
 <label className="text-sm text-textmuted">Provider<select className="input w-full" value={form.provider} onChange={e=>setForm({...form,provider:e.target.value})}><option value="recorded_future">Recorded Future</option><option value="riskrecon">RiskRecon sandbox</option></select></label>
 <label className="text-sm text-textmuted">External entity ID<input className="input w-full" required maxLength="300" placeholder="idn:domain.example or licensed TOE ID" value={form.external_id} onChange={e=>setForm({...form,external_id:e.target.value})}/></label>
 <label className="text-sm text-textmuted">Expected binding version (0 creates)<input className="input w-full" type="number" min="0" value={form.expected_version} onChange={e=>setForm({...form,expected_version:e.target.value})}/></label>
 <button disabled={busy} className="text-accent border rounded p-2">Save provider binding</button></form>
 <ul className="flex flex-col gap-3 text-sm text-textmuted">{data?.bindings.map(r=><li className="p-3 border border-border rounded" key={r.binding_id}><p>{r.merchant_id} · {r.provider} · {r.external_id} · v{r.version}</p><button className="text-accent mr-4" onClick={()=>setForm({merchant_id:r.merchant_id,provider:r.provider,external_id:r.external_id,expected_version:r.version})}>Edit {r.binding_id}</button><button className="text-accent" disabled={busy} onClick={()=>act(()=>api.refreshProvider(r.binding_id))}>Refresh {r.binding_id}</button></li>)}</ul></section>;
}
