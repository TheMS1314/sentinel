import { useState } from "react";
import { PlayCircle, Layers } from "lucide-react";

const DRIFT_TYPES = [
  { value: "amount_creep", label: "Amount creep" },
  { value: "category_drift", label: "Category drift" },
  { value: "velocity_spike", label: "Velocity spike" },
  { value: "off_hours", label: "Off-hours attempt" },
  { value: "prompt_injection", label: "Prompt injection" },
  { value: "merchant_swap", label: "Blocked merchant swap" },
];

export default function ScenarioRunner({ agentId, onRun, onRunAsync, running, disabled }) {
  const [mode, setMode] = useState("well_behaved");
  const [driftType, setDriftType] = useState("prompt_injection");
  const [numTxns, setNumTxns] = useState(4);
  const [useAsync, setUseAsync] = useState(true);

  const run = () => {
    // The backend enforces 1-10 as the real boundary (see SimulateIn in
    // main.py) -- this clamp is just so a typed "9999" or "0" doesn't
    // sail past the number input's cosmetic min/max and hit a 422 the
    // user has no context for. It mirrors the backend's bound, it
    // doesn't replace it.
    const clamped = Math.min(10, Math.max(1, parseInt(numTxns) || 1));
    const body = {
      agent_id: agentId,
      goal: "buy weekly groceries under budget",
      mode,
      drift_type: mode === "adversarial" ? driftType : "none",
      num_transactions: clamped,
    };
    if (useAsync) {
      onRunAsync(body);
    } else {
      onRun(body);
    }
  };

  return (
    <div className="bg-surface border border-border rounded-xl p-5 shadow-card">
      <div className="flex items-center gap-2 mb-4">
        <div className="w-6 h-6 rounded-md bg-accent/10 flex items-center justify-center">
          <PlayCircle size={13} strokeWidth={2.25} className="text-accent" />
        </div>
        <h3 className="font-semibold text-[13px] text-text">Run scenario</h3>
      </div>

      <div className="grid grid-cols-2 gap-2 mb-4">
        <button
          onClick={() => setMode("well_behaved")}
          className={`py-2 rounded-lg text-[13px] font-medium border transition-all ${
            mode === "well_behaved"
              ? "bg-approve/[0.10] text-approve border-approve/35 shadow-[0_0_0_1px_rgb(var(--color-approve)/0.15)]"
              : "bg-surface2/60 text-textmuted border-border hover:text-text hover:border-borderlight"
          }`}
        >
          Well-behaved
        </button>
        <button
          onClick={() => setMode("adversarial")}
          className={`py-2 rounded-lg text-[13px] font-medium border transition-all ${
            mode === "adversarial"
              ? "bg-block/[0.10] text-block border-block/35 shadow-[0_0_0_1px_rgb(var(--color-block)/0.15)]"
              : "bg-surface2/60 text-textmuted border-border hover:text-text hover:border-borderlight"
          }`}
        >
          Adversarial
        </button>
      </div>

      {mode === "adversarial" && (
        <label className="flex flex-col gap-1.5 mb-4">
          <span className="text-[11px] text-textfaint font-medium uppercase tracking-wide">Drift type</span>
          <select className="input" value={driftType} onChange={(e) => setDriftType(e.target.value)}>
            {DRIFT_TYPES.map((d) => (
              <option key={d.value} value={d.value}>
                {d.label}
              </option>
            ))}
          </select>
        </label>
      )}

      <label className="flex flex-col gap-1.5 mb-4">
        <span className="text-[11px] text-textfaint font-medium uppercase tracking-wide">Transactions</span>
        <input className="input" type="number" min="1" max="10" value={numTxns} onChange={(e) => setNumTxns(e.target.value)} />
      </label>

      <label className="flex items-center gap-2.5 mb-5 cursor-pointer select-none group">
        <div className="relative flex items-center">
          <input
            type="checkbox"
            checked={useAsync}
            onChange={(e) => setUseAsync(e.target.checked)}
            className="peer sr-only"
          />
          <div className="w-8 h-[18px] rounded-full bg-surface3 border border-border peer-checked:bg-accent/25 peer-checked:border-accent/50 transition-colors" />
          <div className="absolute left-0.5 w-3.5 h-3.5 rounded-full bg-textfaint peer-checked:bg-accent peer-checked:translate-x-[14px] transition-all" />
        </div>
        <span className="text-[12px] text-textmuted group-hover:text-text transition-colors flex items-center gap-1">
          <Layers size={12} strokeWidth={2} className="text-textfaint" />
          Async queue (concurrent workers)
        </span>
      </label>

      <button
        onClick={run}
        disabled={running || disabled}
        title={disabled ? "This agent has no active policy yet -- create one in the Policy tab first" : undefined}
        className="w-full py-2.5 rounded-lg bg-accent text-white text-[13px] font-semibold hover:bg-accent/90 disabled:opacity-50 disabled:cursor-not-allowed transition-all shadow-[0_2px_12px_-2px_rgb(var(--color-accent)/0.5)] hover:shadow-[0_2px_16px_-2px_rgb(var(--color-accent)/0.65)]"
      >
        {running ? "Running…" : disabled ? "No policy for this agent" : "Run scenario"}
      </button>
      {disabled && (
        <p className="mt-2 text-[11px] text-textfaint text-center">
          Create a policy for <span className="font-mono text-textmuted">{agentId}</span> in the Policy tab first.
        </p>
      )}
    </div>
  );
}
