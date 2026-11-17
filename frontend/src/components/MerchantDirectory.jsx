import {useEffect,useState} from 'react';
import {api} from '../api';
const empty={merchant_id:'',owner_id:'',legal_name:'',category:'grocery',domain:'',operator_reference:'',active:true,expected_version:0};
export default function MerchantDirectory({identity}) {
  const [rows,setRows]=useState([]),[form,setForm]=useState({...empty,owner_id:identity.owner_id||''});
  const [error,setError]=useState(''),[busy,setBusy]=useState(false),[notice,setNotice]=useState('');
  const canManage=['admin','owner'].includes(identity.role);
  useEffect(()=>{let active=true;api.listMerchants().then(v=>{if(active)setRows(v);}).catch(e=>{if(active)setError(e.message);});return()=>{active=false;};},[]);
  const input=name=>({value:form[name],onChange:e=>setForm({...form,[name]:e.target.value})});
  async function save(e){e.preventDefault();setBusy(true);setError('');setNotice('');try{
    const body={...form};if(!body.merchant_id)delete body.merchant_id;
    const saved=await api.saveMerchant(body);setRows(await api.listMerchants());setForm({...empty,owner_id:identity.owner_id||''});setNotice(`Saved ${saved.merchant_id}. This is operator attestation; provider settlement identity remains unverified.`);
  }catch(e){setError(e.message);}finally{setBusy(false);}}
  return <section className="flex flex-col gap-5">
    <h2 className="font-semibold text-text">Merchant directory</h2><p className="text-sm text-textmuted">Agents submit a stable merchant ID. Sentinel reads its name, category and domain from this owner-controlled registry. Updates invalidate pending bindings. Operator registration does not prove provider/acquirer identity or settlement routing.</p>
    {error&&<p role="alert" className="text-block">{error}</p>}{notice&&<p role="status" className="text-approve">{notice}</p>}
    {canManage&&<form onSubmit={save} className="grid sm:grid-cols-2 gap-4 bg-surface border border-border rounded-xl p-5">
      <label className="text-sm text-textmuted">Merchant ID (blank creates one)<input className="input w-full" maxLength="128" readOnly={form.expected_version>0} {...input('merchant_id')}/></label>
      <label className="text-sm text-textmuted">Merchant owner ID<input className="input w-full" required readOnly={identity.role!=='admin'||form.expected_version>0} {...input('owner_id')}/></label>
      <label className="text-sm text-textmuted">Registered merchant name<input className="input w-full" required maxLength="200" {...input('legal_name')}/></label>
      <label className="text-sm text-textmuted">Registered category<input className="input w-full" required maxLength="100" {...input('category')}/></label>
      <label className="text-sm text-textmuted">Registered domain<input className="input w-full" required maxLength="253" {...input('domain')}/></label>
      <label className="text-sm text-textmuted">Registration evidence/reference<input className="input w-full" required maxLength="2000" {...input('operator_reference')}/></label>
      <label className="text-sm text-textmuted"><input type="checkbox" checked={form.active} onChange={e=>setForm({...form,active:e.target.checked})}/> Active identity</label>
      <button disabled={busy} className="rounded border border-accent/40 p-2 text-accent disabled:opacity-50">Save merchant identity</button>
    </form>}
    <ul className="flex flex-col gap-3">{rows.map(row=><li key={row.merchant_id} className="border border-border rounded p-4 text-sm text-textmuted"><p className="text-text">{row.legal_name} · {row.category} · v{row.version} · {row.active?'active':'inactive'}</p><code className="break-all">{row.merchant_id}</code><p>{row.domain} · operator attested · settlement unverified</p>{canManage&&<button className="mt-2 text-accent" onClick={()=>setForm({merchant_id:row.merchant_id,owner_id:row.owner_id,legal_name:row.legal_name,category:row.category,domain:row.domain,operator_reference:row.operator_reference,active:row.active,expected_version:row.version})}>Edit {row.merchant_id}</button>}</li>)}</ul>
  </section>;
}
