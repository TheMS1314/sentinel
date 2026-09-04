import { CheckCircle2, AlertTriangle, XCircle, Layers } from "lucide-react";

export default function StatsBar({ summary }) {
  if (!summary) return null;
  const { total, by_verdict } = summary;

  const items = [
    { label: "Approved", value: by_verdict.APPROVE, color: "text-approve", iconBg: "bg-approve/10", iconColor: "text-approve", Icon: CheckCircle2 },
    { label: "Held for review", value: by_verdict.HOLD, color: "text-hold", iconBg: "bg-hold/10", iconColor: "text-hold", Icon: AlertTriangle },
    { label: "Blocked", value: by_verdict.BLOCK, color: "text-block", iconBg: "bg-block/10", iconColor: "text-block", Icon: XCircle },
    { label: "Total evaluated", value: total, color: "text-text", iconBg: "bg-accent/10", iconColor: "text-accent", Icon: Layers },
  ];

  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
      {items.map((item) => (
        <div
          key={item.label}
          className="bg-surface border border-border rounded-xl px-5 py-4 shadow-card hover:border-borderlight transition-colors"
        >
          <div className="flex items-center justify-between mb-3">
            <span className="text-[12px] text-textmuted font-medium">{item.label}</span>
            <div className={`w-7 h-7 rounded-md flex items-center justify-center ${item.iconBg}`}>
              <item.Icon size={14} strokeWidth={2.25} className={item.iconColor} />
            </div>
          </div>
          <div className={`text-3xl font-mono font-semibold tabular-nums tracking-tight ${item.color}`}>
            {item.value}
          </div>
        </div>
      ))}
    </div>
  );
}
