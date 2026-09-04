import { ChevronDown, Users } from "lucide-react";
import { useState, useRef, useEffect } from "react";

export default function AgentSelector({ agents, selectedAgentId, onSelect }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    function onClickOutside(e) {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    }
    document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, []);

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="listbox"
        aria-expanded={open}
        className="flex items-center gap-2 px-3 py-1.5 rounded-lg border border-border bg-surface2/60 hover:border-borderlight text-[12px] font-mono text-textmuted hover:text-text transition-colors"
      >
        <Users size={13} strokeWidth={2} className="text-textfaint" />
        {selectedAgentId}
        <ChevronDown size={13} strokeWidth={2} className={`text-textfaint transition-transform ${open ? "rotate-180" : ""}`} />
      </button>

      {open && (
        <div
          role="listbox"
          className="absolute z-40 mt-1.5 min-w-[220px] max-h-72 overflow-y-auto rounded-lg border border-border bg-surface shadow-card py-1"
        >
          {agents.length === 0 && (
            <div className="px-3 py-2 text-[12px] text-textfaint font-mono">No agents yet</div>
          )}
          {agents.map((a) => (
            <button
              key={a.agent_id}
              role="option"
              aria-selected={a.agent_id === selectedAgentId}
              onClick={() => {
                onSelect(a.agent_id);
                setOpen(false);
              }}
              className={`w-full text-left px-3 py-2 text-[12px] font-mono transition-colors ${
                a.agent_id === selectedAgentId
                  ? "bg-accent/10 text-accent"
                  : "text-textmuted hover:bg-surface2/70 hover:text-text"
              }`}
            >
              {a.agent_id}
            </button>
          ))}
          <div className="border-t border-border/60 mt-1 pt-1 px-3 py-1.5 text-[10px] text-textfaint">
            Create a new agent from the Policy tab
          </div>
        </div>
      )}
    </div>
  );
}
