import { CheckCircle2, AlertTriangle, XCircle, HelpCircle } from "lucide-react";

const VERDICT_CONFIG = {
  APPROVE: { color: "text-approve", border: "border-approve/30", bg: "bg-approve/[0.08]", Icon: CheckCircle2 },
  HOLD: { color: "text-hold", border: "border-hold/30", bg: "bg-hold/[0.08]", Icon: AlertTriangle },
  BLOCK: { color: "text-block", border: "border-block/30", bg: "bg-block/[0.08]", Icon: XCircle },
};

export function VerdictBadge({ verdict }) {
  const cfg = VERDICT_CONFIG[verdict] || { color: "text-textmuted", border: "border-border", bg: "bg-surface2", Icon: HelpCircle };
  const { Icon } = cfg;
  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[11px] font-mono font-semibold tracking-wider border ${cfg.color} ${cfg.border} ${cfg.bg}`}
    >
      <Icon size={12} strokeWidth={2.5} />
      {verdict || "—"}
    </span>
  );
}

export function Pill({ children, muted, icon: Icon }) {
  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-[11px] font-mono ${
        muted ? "text-textfaint bg-surface2/80" : "text-textmuted bg-surface2"
      }`}
    >
      {Icon && <Icon size={11} strokeWidth={2} />}
      {children}
    </span>
  );
}
