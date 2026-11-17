import { useEffect, useState } from 'react';
import { api } from '../api';

export default function AccessControl({ currentSubject }) {
  const [rows,setRows]=useState([]);
  const [error,setError]=useState('');
  const [secret,setSecret]=useState('');
  const [busy,setBusy]=useState(false);
  const [form,setForm]=useState({subject:'',role:'agent',owner_id:'',agent_ids:''});
  const [editing,setEditing]=useState(null);
  useEffect(()=>{let active=true;api.listCredentials().then(value=>{if(active)setRows(value);}).catch(e=>{if(active)setError(e.message);});return()=>{active=false;};},[]);
  async function mutate(body) {
    setBusy(true);setError('');setSecret('');
    try {const result=await api.changeCredential(body);setSecret(result.credential||'');setRows(await api.listCredentials());setEditing(null);}
    catch(e){setError(e.message);}finally{setBusy(false);}
  }
  const input=(name)=>({value:form[name],onChange:e=>setForm({...form,[name]:e.target.value})});
  return <section className="flex flex-col gap-5">
    <h2 className="font-semibold text-text">Shared access control</h2>
    <p className="text-sm text-textmuted">Keys are hashed in the shared database. Rotation invalidates old keys and browser sessions; scope changes also invalidate old queued commands and pending dispatch authority. Keys are returned only when created or rotated. Change your current administrator credential through another administrator or privileged recovery.</p>
    {error&&<p role="alert" className="text-block">{error}</p>}
    {secret&&<div className="rounded border border-hold/40 p-4"><p className="text-sm text-textmuted">Store this new credential securely now. It will not be shown again.</p><output aria-label="New credential" className="font-mono text-sm text-text break-all">{secret}</output><button onClick={()=>setSecret('')} className="ml-3 text-sm text-accent">Hide credential</button></div>}
    <form className="bg-surface border border-border rounded-xl p-5 grid sm:grid-cols-2 gap-4" onSubmit={e=>{e.preventDefault();mutate({subject:form.subject,action:editing?'scope':'create',expected_version:editing?.version||0,role:form.role,owner_id:form.owner_id,agent_ids:form.agent_ids.split(',').map(v=>v.trim()).filter(Boolean)});}}>
      <label className="text-sm text-textmuted">Subject<input className="input w-full" required maxLength="128" readOnly={Boolean(editing)} {...input('subject')}/></label>
      <label className="text-sm text-textmuted">Role<select className="input w-full" {...input('role')}>{['agent','owner','reviewer','admin'].map(role=><option key={role}>{role}</option>)}</select></label>
      <label className="text-sm text-textmuted">Owner scope<input className="input w-full" maxLength="128" {...input('owner_id')}/></label>
      <label className="text-sm text-textmuted">Agent IDs (comma separated)<input className="input w-full" {...input('agent_ids')}/></label>
      <button disabled={busy} className="rounded border border-accent/40 p-2 text-accent disabled:opacity-50">{editing?'Save scope':'Create credential'}</button>
      {editing&&<button type="button" onClick={()=>{setEditing(null);setForm({subject:'',role:'agent',owner_id:'',agent_ids:''});}} className="text-sm text-textmuted">Cancel edit</button>}
    </form>
    <ul className="flex flex-col gap-3">{rows.map(row=><li key={row.subject} className="bg-surface border border-border rounded p-4 text-sm text-textmuted">
      <p className="font-mono break-all">{row.subject} · {row.role} · v{row.version} · {row.active?'active':'revoked'}</p><p>{row.owner_id||row.agent_ids.join(', ')||'Administrator scope'}</p>
      <div className="mt-2 flex gap-4"><button disabled={busy || row.subject === currentSubject} aria-label={`Rotate ${row.subject}`} onClick={()=>mutate({subject:row.subject,action:'rotate',expected_version:row.version})}>Rotate key</button><button disabled={busy || row.subject === currentSubject} aria-label={`Revoke ${row.subject}`} onClick={()=>mutate({subject:row.subject,action:'revoke',expected_version:row.version})}>Revoke</button><button disabled={busy || row.subject === currentSubject} onClick={()=>{setEditing(row);setForm({...row,agent_ids:row.agent_ids.join(', ')});}}>Edit scope</button></div>
    </li>)}</ul>
  </section>;
}
