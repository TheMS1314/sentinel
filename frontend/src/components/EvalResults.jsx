import { Target, Grid3x3, Layers, AlertCircle } from "lucide-react";

const VERDICT_COLOR = { APPROVE: "text-approve", HOLD: "text-hold", BLOCK: "text-block" };

function Card({ icon: Icon, title, right, children }) {
  return (
    <div className="bg-surface border border-border rounded-xl p-6 shadow-card">
      <div className="flex items-center justify-between mb-5">
        <div className="flex items-center gap-2">
          <div className="w-6 h-6 rounded-md bg-accent/10 flex items-center justify-center">
            <Icon size={13} strokeWidth={2.25} className="text-accent" />
          </div>
          <h3 className="font-semibold text-[13px] text-text">{title}</h3>
        </div>
        {right}
      </div>
      {children}
    </div>
  );
}

export default function EvalResults({ report }) {
  if (!report) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 py-20 text-center border border-dashed border-border rounded-xl">
        <div className="w-10 h-10 rounded-full bg-surface2 flex items-center justify-center">
          <Target size={18} strokeWidth={2} className="text-textfaint" />
        </div>
        <div className="text-textmuted text-sm font-medium">Evaluation results pending</div>
        <div className="text-textfaint text-xs">Run the eval harness on the backend, then refresh</div>
      </div>
    );
  }

  const hasLiveKey = report.runtime?.llm_mode === "live";

  return (
    <div className="flex flex-col gap-5">
      {report.generated_at && (
        <div className="flex items-center justify-between text-[11px] font-mono text-textfaint px-0.5">
          <span>
            Last evaluated:{" "}
            {new Date(report.generated_at).toLocaleString("en-IN", {
              day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
            })}
          </span>
          <span className={`px-2 py-0.5 rounded-full border ${hasLiveKey ? "text-approve border-approve/30 bg-approve/[0.06]" : "text-textfaint border-border"}`}>
            {hasLiveKey ? `Live model (${report.llm_provider})` : (report.runtime?.llm_mode === "mock" ? "Mock LLM mode" : "Historical run · mode unverified")}
          </span>
        </div>
      )}

      <Card icon={Target} title="Golden dataset accuracy" right={<span className="text-[11px] text-textfaint font-mono">{report.total_cases} cases</span>}>
        <div className="text-5xl font-mono font-bold text-approve tracking-tight tabular-nums">
          {(report.overall_accuracy * 100).toFixed(1)}<span className="text-2xl text-approve/60">%</span>
        </div>
      </Card>

      {report.hard_tier2 && <Card icon={Target} title="Hard dataset result">
        <p className="font-mono">{(report.hard_tier2.overall_accuracy * 100).toFixed(1)}% on {report.hard_tier2.total_cases} cases</p>
        <p className="text-sm text-textmuted">Constructed benchmark; results describe this run.</p>
      </Card>}

      <Card icon={Grid3x3} title="Per-class metrics">
        <div className="overflow-x-auto"><table className="w-full text-[13px] font-mono">
          <thead>
            <tr className="text-textfaint text-[11px] border-b border-border">
              <th className="text-left pb-2.5 font-medium uppercase tracking-wide">Verdict</th>
              <th className="text-right pb-2.5 font-medium uppercase tracking-wide">Precision</th>
              <th className="text-right pb-2.5 font-medium uppercase tracking-wide">Recall</th>
              <th className="text-right pb-2.5 font-medium uppercase tracking-wide">F1</th>
              <th className="text-right pb-2.5 font-medium uppercase tracking-wide">Support</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(report.per_class).map(([verdict, m]) => (
              <tr key={verdict} className="border-b border-border/50 last:border-0">
                <td className={`py-2.5 font-semibold ${VERDICT_COLOR[verdict]}`}>{verdict}</td>
                <td className="text-right py-2.5 text-textmuted tabular-nums">{m.precision.toFixed(2)}</td>
                <td className="text-right py-2.5 text-textmuted tabular-nums">{m.recall.toFixed(2)}</td>
                <td className="text-right py-2.5 text-textmuted tabular-nums">{m.f1.toFixed(2)}</td>
                <td className="text-right py-2.5 text-textfaint tabular-nums">{m.support}</td>
              </tr>
            ))}
          </tbody>
        </table></div>
      </Card>

      <Card icon={Grid3x3} title="Confusion matrix">
        <div className="overflow-x-auto"><table className="w-full text-[13px] font-mono">
          <thead>
            <tr>
              <th className="text-left pb-2 text-[11px] text-textfaint font-medium"></th>
              <th className="text-center pb-2 text-[11px] text-textfaint font-medium uppercase tracking-wide" colSpan={3}>
                Actual
              </th>
            </tr>
            <tr className="border-b border-border">
              <th className="text-left pb-2.5 text-[11px] text-textfaint font-medium uppercase tracking-wide">Expected</th>
              {report.confusion_matrix.labels.map((l) => (
                <th key={l} className={`text-right pb-2.5 text-[11px] font-medium ${VERDICT_COLOR[l]}`}>
                  {l}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {report.confusion_matrix.labels.map((rowLabel, i) => (
              <tr key={rowLabel} className="border-b border-border/50 last:border-0">
                <td className={`py-2.5 font-semibold ${VERDICT_COLOR[rowLabel]}`}>{rowLabel}</td>
                {report.confusion_matrix.matrix[i].map((val, j) => (
                  <td
                    key={j}
                    className={`text-right py-2.5 tabular-nums ${
                      i === j ? "text-text font-semibold" : val > 0 ? "text-block" : "text-textfaint/50"
                    }`}
                  >
                    {val}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table></div>
      </Card>

      <Card icon={Layers} title="Accuracy by decision layer">
        <div className="grid grid-cols-2 gap-3">
          {Object.entries(report.by_decider).map(([decider, stats]) => (
            <div key={decider} className="bg-surface2/70 border border-border/60 rounded-lg p-4">
              <div className="text-[11px] text-textfaint font-mono mb-1.5">{decider}</div>
              <div className="text-[26px] font-mono font-bold text-text tabular-nums tracking-tight">
                {stats.n > 0 ? `${(stats.accuracy * 100).toFixed(1)}%` : "—"}
              </div>
              <div className="text-[11px] text-textfaint font-mono mt-1">n={stats.n}</div>
            </div>
          ))}
        </div>
        {report.by_decider.llm_reasoner?.n > 0 && (
          <p className="text-[12px] text-textfaint mt-4 leading-relaxed flex items-start gap-2">
            <AlertCircle size={13} strokeWidth={2} className="mt-0.5 shrink-0" />
            Decision-layer accuracy shown here reflects the outputs recorded in this evaluation run.
          </p>
        )}
      </Card>

      {report.misses?.length > 0 ? (
        <Card icon={AlertCircle} title={`${report.misses.length} misses`}>
          <div className="flex flex-col gap-2">
            {report.misses.map((m) => (
              <div key={m.case_id} className="text-[12px] font-mono bg-surface2/70 rounded-lg p-2.5">
                <span className="text-textfaint">{m.category}</span> — expected{" "}
                <span className={VERDICT_COLOR[m.expected]}>{m.expected}</span>, got{" "}
                <span className={VERDICT_COLOR[m.actual]}>{m.actual}</span>
              </div>
            ))}
          </div>
        </Card>
      ) : (
        <Card icon={AlertCircle} title="Error analysis">
          <div className="flex items-center gap-2 text-[13px] text-approve">
            <span className="w-1.5 h-1.5 rounded-full bg-approve" />
            No misclassified cases in this run.
          </div>
        </Card>
      )}
    </div>
  );
}
