import { useState, useEffect } from "react";
import { ShieldCheck, Save } from "lucide-react";

function Field({ label, children }) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-[11px] text-textfaint font-medium uppercase tracking-wide">{label}</span>
      {children}
    </label>
  );
}

const DEFAULT_FORM = {
  agent_id: "agent_demo_1",
  owner_human_id: "human_demo",
  authorized_goal: "buy weekly groceries under budget",
  max_amount_per_txn: 2000,
  max_daily_total: 5000,
  allowed_categories: "grocery",
  allowed_merchants: "",
  blocked_merchants: "ShadyStore, UnverifiedGoods",
  time_window_start: 6,
  time_window_end: 22,
  velocity_max_count: 3,
  velocity_window_minutes: 1440,
};

export default function PolicyEditor({ onCreatePolicy, currentPolicy, agentId, ownerId }) {
  const [form, setForm] = useState({ ...DEFAULT_FORM, agent_id: agentId || DEFAULT_FORM.agent_id });

  useEffect(() => {
    if (currentPolicy?.policy) {
      const p = currentPolicy.policy;
      setForm({
        agent_id: p.agent_id,
        owner_human_id: p.owner_human_id,
        allowed_merchants: (p.allowed_merchants || []).join(", "),
        authorized_goal: p.authorized_goal || "buy weekly groceries under budget",
        max_amount_per_txn: p.max_amount_per_txn,
        max_daily_total: p.max_daily_total,
        allowed_categories: (p.allowed_categories || []).join(", "),
        blocked_merchants: (p.blocked_merchants || []).join(", "),
        time_window_start: p.time_window?.start_hour ?? 6,
        time_window_end: p.time_window?.end_hour ?? 22,
        velocity_max_count: p.velocity?.max_count ?? 3,
        velocity_window_minutes: p.velocity?.window_minutes ?? 1440,
      });
    } else if (agentId) {
      // Switched to an agent with no policy yet (or a brand-new one
      // about to be created) -- reset to sane defaults but keep
      // whichever agent is actually selected, rather than silently
      // showing stale form data from the previously selected agent.
      setForm({ ...DEFAULT_FORM, agent_id: agentId });
    }
  }, [currentPolicy, agentId]);

  const update = (key) => (e) => setForm({ ...form, [key]: e.target.value });

  const submit = () => {
    onCreatePolicy({
      agent_id: form.agent_id,
      owner_human_id: ownerId || form.owner_human_id,
      authorized_goal: form.authorized_goal,
      max_amount_per_txn: parseFloat(form.max_amount_per_txn),
      max_daily_total: parseFloat(form.max_daily_total),
      allowed_categories: form.allowed_categories.split(",").map((s) => s.trim()).filter(Boolean),
      allowed_merchants: form.allowed_merchants.trim() ? form.allowed_merchants.split(",").map(s => s.trim()).filter(Boolean) : null,
      blocked_merchants: form.blocked_merchants.split(",").map((s) => s.trim()).filter(Boolean),
      time_window_start: parseInt(form.time_window_start),
      time_window_end: parseInt(form.time_window_end),
      velocity_max_count: parseInt(form.velocity_max_count),
      velocity_window_minutes: parseInt(form.velocity_window_minutes),
    });
  };

  return (
    <div className="bg-surface border border-border rounded-xl p-6 shadow-card">
      <div className="flex items-center gap-2 mb-5">
        <div className="w-6 h-6 rounded-md bg-accent/10 flex items-center justify-center">
          <ShieldCheck size={13} strokeWidth={2.25} className="text-accent" />
        </div>
        <h3 className="font-semibold text-[13px] text-text">Agent policy</h3>
      </div>

      <div className="grid grid-cols-2 gap-4">
        <Field label="Authorized goal">
          <input className="input" value={form.authorized_goal} onChange={update("authorized_goal")} />
        </Field>
        <Field label="Agent ID">
          <input
            className="input"
            value={form.agent_id}
            onChange={update("agent_id")}
            title="Editing this and saving creates/updates a policy for that agent -- the whole dashboard switches to follow it."
          />
        </Field>
        <Field label="Owner (human)">
          <input className="input" value={ownerId || form.owner_human_id} readOnly={Boolean(ownerId)} onChange={update("owner_human_id")} />
        </Field>
        <Field label="Max per transaction (₹)">
          <input className="input" type="number" value={form.max_amount_per_txn} onChange={update("max_amount_per_txn")} />
        </Field>
        <Field label="Max daily total (₹)">
          <input className="input" type="number" value={form.max_daily_total} onChange={update("max_daily_total")} />
        </Field>
        <Field label="Allowed categories">
          <input className="input" value={form.allowed_categories} onChange={update("allowed_categories")} />
        </Field>
        <Field label="Allowed merchants (optional)">
          <input className="input" value={form.allowed_merchants} onChange={update("allowed_merchants")} />
        </Field>
        <Field label="Blocked merchants">
          <input className="input" value={form.blocked_merchants} onChange={update("blocked_merchants")} />
        </Field>
        <Field label="Time window start (hr)">
          <input className="input" type="number" value={form.time_window_start} onChange={update("time_window_start")} />
        </Field>
        <Field label="Time window end (hr)">
          <input className="input" type="number" value={form.time_window_end} onChange={update("time_window_end")} />
        </Field>
        <Field label="Max transactions / window">
          <input className="input" type="number" value={form.velocity_max_count} onChange={update("velocity_max_count")} />
        </Field>
        <Field label="Velocity window (min)">
          <input className="input" type="number" value={form.velocity_window_minutes} onChange={update("velocity_window_minutes")} />
        </Field>
      </div>

      <button
        onClick={submit}
        className="mt-6 flex items-center gap-2 px-4 py-2.5 rounded-lg bg-accent text-white text-[13px] font-semibold hover:bg-accent/90 transition-all shadow-[0_2px_12px_-2px_rgb(var(--color-accent)/0.5)]"
      >
        <Save size={14} strokeWidth={2.25} />
        Save policy
      </button>

      {currentPolicy && (
        <div className="mt-5 pt-4 border-t border-border flex items-center gap-2 text-[11px] font-mono text-textfaint">
          <span className="w-1.5 h-1.5 rounded-full bg-approve" />
          Active policy: {currentPolicy.policy_id}
        </div>
      )}
    </div>
  );
}
