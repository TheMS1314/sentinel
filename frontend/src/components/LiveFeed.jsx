import { VerdictBadge, Pill } from "./Badges";
import { humanizeScenarioTag } from "../scenarioLabels";
import { useState } from "react";
import { Zap, Bot, ScrollText, Clock, Inbox, Search, X, ChevronLeft, ChevronRight, ChevronDown } from "lucide-react";

const VERDICT_EDGE = {
  APPROVE: "before:bg-approve",
  HOLD: "before:bg-hold",
  BLOCK: "before:bg-block",
};

const VERDICTS = ["APPROVE", "HOLD", "BLOCK"];

function formatTime(iso) {
  try {
    const d = new Date(iso);
    return d.toLocaleString("en-IN", { hour: "2-digit", minute: "2-digit", day: "2-digit", month: "short" });
  } catch {
    return iso;
  }
}

// Section 26 fix (see backend main.py comment on created_at_iso):
// requested_at_iso is often a deterministic, scenario-generated
// timestamp -- great for reproducible eval data, meaningless as "when
// did this actually happen" when you're watching a live demo. This
// formats the REAL processing time (created_at_iso) relative to now,
// which is what actually matters when you're staring at the feed live.
function formatRelative(iso) {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return null;
  const diffSec = Math.round((Date.now() - then) / 1000);
  if (diffSec < 5) return "Just now";
  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.round(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.round(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  return formatTime(iso);
}

function DecisionSource({ decidedBy }) {
  if (decidedBy === "llm_reasoner") {
    return <Pill muted icon={Bot}>LLM reasoner</Pill>;
  }
  if (decidedBy === "behavioral_baseline") {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[11px] font-mono text-accent bg-accent/10 border border-accent/20">
        <Zap size={11} strokeWidth={2.25} />
        risk score
      </span>
    );
  }
  return <Pill muted icon={ScrollText}>rule engine</Pill>;
}

function TransactionDetails({ tx }) {
  const rows = [
    ["Request ID", tx.request_id],
    ["Agent", tx.agent_id],
    ["Category", tx.merchant_category],
    ["Decided by", tx.decided_by || "—"],
    ["Confidence", tx.confidence != null ? `${(tx.confidence * 100).toFixed(0)}%` : "—"],
    ["Model", tx.model_version || "—"],
    ["Razorpay status", tx.razorpay_status || "—"],
    ["Stated intent", tx.stated_intent || "—"],
  ];
  return (
    <div className="mt-3 pt-3 border-t border-border/60">
      {/* REQUESTED vs PROCESSED, shown distinctly per design spec: the
          agent's claimed event time (often a fixed scenario timestamp,
          reproducible for eval) is NOT the same moment Sentinel
          actually evaluated it. Collapsing these into one timestamp is
          what made the feed look "stuck on an old date" when the demo
          was run on a different day than the scenario data assumes. */}
      <div className="grid grid-cols-2 gap-x-6 mb-3 pb-3 border-b border-border/40">
        <div>
          <div className="text-[10px] text-textfaint uppercase tracking-wide font-semibold mb-0.5">Requested</div>
          <div className="text-[12px] font-mono text-textmuted">{formatTime(tx.requested_at_iso)}</div>
        </div>
        <div>
          <div className="text-[10px] text-textfaint uppercase tracking-wide font-semibold mb-0.5">Processed</div>
          <div className="text-[12px] font-mono text-textmuted">
            {tx.created_at_iso ? formatTime(tx.created_at_iso) : "—"}
          </div>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-x-6 gap-y-1.5">
      {rows.map(([label, value]) => (
        <div key={label} className="flex items-baseline gap-2 text-[12px] font-mono min-w-0">
          <span className="text-textfaint shrink-0">{label}:</span>
          <span className="text-textmuted truncate">{value}</span>
        </div>
      ))}
      {tx.similar_past_cases && tx.similar_past_cases.length > 0 && (
        <div className="col-span-2 text-[12px] font-mono">
          <span className="text-textfaint">Similar past cases: </span>
          <span className="text-textmuted">{tx.similar_past_cases.length} retrieved</span>
        </div>
      )}
      </div>
    </div>
  );
}

export default function LiveFeed({
  transactions,
  onResolveHold,
  scenarioTags = [],
  scenarioFilter,
  onScenarioFilterChange,
  verdictFilter,
  onVerdictFilterChange,
  search,
  onSearchChange,
  page,
  totalPages,
  total,
  onPageChange,
  loading,
  error,
}) {
  const [expanded, setExpanded] = useState(null);

  const hasActiveFilters = scenarioFilter !== "all" || verdictFilter !== "all" || search;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-2.5">
        <div className="relative">
          <Search size={14} strokeWidth={2} className="absolute left-3 top-1/2 -translate-y-1/2 text-textfaint" aria-hidden="true" />
          <label htmlFor="tx-search" className="sr-only">Search transactions by merchant, agent, or intent</label>
          <input
            id="tx-search"
            value={search}
            onChange={(e) => onSearchChange(e.target.value)}
            placeholder="Search merchant, agent, or intent…"
            className="w-full pl-9 pr-8 py-2 rounded-lg bg-surface2/60 border border-border text-[13px] font-mono text-text placeholder:text-textfaint focus:outline-none focus:border-accent/50 transition-colors"
          />
          {search && (
            <button
              onClick={() => onSearchChange("")}
              aria-label="Clear search"
              className="absolute right-2.5 top-1/2 -translate-y-1/2 text-textfaint hover:text-text"
            >
              <X size={14} strokeWidth={2} />
            </button>
          )}
        </div>

        <div className="flex items-center gap-1.5 flex-wrap">
          <span className="text-[10px] text-textfaint uppercase tracking-wide font-semibold mr-0.5">Verdict</span>
          {["all", ...VERDICTS].map((v) => (
            <button
              key={v}
              onClick={() => onVerdictFilterChange(v)}
              className={`px-2.5 py-1 rounded-full text-[11px] font-mono border transition-colors ${
                verdictFilter === v
                  ? "bg-accent/15 text-accent border-accent/40"
                  : "bg-surface2/60 text-textfaint border-border hover:text-textmuted hover:border-borderlight"
              }`}
            >
              {v === "all" ? "All" : v}
            </button>
          ))}
        </div>

        {scenarioTags.length > 0 && (
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="text-[10px] text-textfaint uppercase tracking-wide font-semibold mr-0.5">Scenario</span>
            <button
              onClick={() => onScenarioFilterChange("all")}
              className={`px-2.5 py-1 rounded-full text-[11px] font-mono border transition-colors ${
                scenarioFilter === "all"
                  ? "bg-accent/15 text-accent border-accent/40"
                  : "bg-surface2/60 text-textfaint border-border hover:text-textmuted hover:border-borderlight"
              }`}
            >
              All
            </button>
            {scenarioTags.map((s) => (
              <button
                key={s}
                onClick={() => onScenarioFilterChange(s)}
                className={`px-2.5 py-1 rounded-full text-[11px] font-mono border transition-colors ${
                  scenarioFilter === s
                    ? "bg-accent/15 text-accent border-accent/40"
                    : "bg-surface2/60 text-textfaint border-border hover:text-textmuted hover:border-borderlight"
                }`}
              >
                {humanizeScenarioTag(s)}
              </button>
            ))}
          </div>
        )}
      </div>

      {error && (
        <div className="bg-block/[0.07] border border-block/25 text-block text-[13px] font-mono rounded-lg px-4 py-2.5">
          {error}
        </div>
      )}

      {loading && (
        <div className="text-textmuted font-mono text-sm py-16 text-center border border-dashed border-border rounded-xl">
          Loading…
        </div>
      )}

      {!loading && !error && transactions.length === 0 && (
        <div className="flex flex-col items-center justify-center gap-3 py-20 text-center border border-dashed border-border rounded-xl">
          <div className="w-10 h-10 rounded-full bg-surface2 flex items-center justify-center">
            <Inbox size={18} strokeWidth={2} className="text-textfaint" />
          </div>
          <div className="text-textmuted text-sm font-medium">
            {hasActiveFilters ? "No transactions match these filters" : "No transactions yet"}
          </div>
          <div className="text-textfaint text-xs">
            {hasActiveFilters ? "Try clearing search or filters" : "Run a scenario to populate the feed"}
          </div>
        </div>
      )}

      {!loading && !error && transactions.length > 0 && (
        <div className="flex flex-col gap-2">
          {transactions.map((tx) => {
            const isOpen = expanded === tx.request_id;
            return (
              <div
                key={tx.request_id}
                className={`animate-slidein relative pl-4 before:absolute before:left-0 before:top-0 before:bottom-0 before:w-[3px] before:rounded-full ${VERDICT_EDGE[tx.verdict] || "before:bg-border"} bg-surface border border-border hover:border-borderlight rounded-lg px-4 py-3.5 shadow-card transition-colors`}
              >
                <button
                  onClick={() => setExpanded(isOpen ? null : tx.request_id)}
                  aria-expanded={isOpen}
                  aria-label={`${isOpen ? "Collapse" : "Expand"} details for ${tx.merchant_name} transaction, ${tx.verdict}`}
                  className="w-full text-left"
                >
                  <div className="flex items-center justify-between gap-3 flex-wrap">
                    <div className="flex items-center gap-2.5 min-w-0">
                      <VerdictBadge verdict={tx.verdict} />
                      <span className="font-mono text-[13px] text-text font-medium truncate">{tx.merchant_name}</span>
                      <Pill muted>{tx.merchant_category}</Pill>
                      <span className="font-mono text-[13px] text-text tabular-nums font-medium">
                        ₹{tx.amount.toFixed(2)}
                      </span>
                    </div>
                    <div className="flex items-center gap-2 text-[11px] text-textfaint font-mono shrink-0">
                      <span className="flex items-center gap-1" title={`Processed ${formatTime(tx.created_at_iso)}`}>
                        <Clock size={11} strokeWidth={2} />
                        {formatRelative(tx.created_at_iso) || formatTime(tx.requested_at_iso)}
                      </span>
                      {tx.decided_by && <DecisionSource decidedBy={tx.decided_by} />}
                      {tx.scenario_tag && <Pill muted>{humanizeScenarioTag(tx.scenario_tag)}</Pill>}
                      <ChevronDown
                        size={14}
                        strokeWidth={2}
                        className={`text-textfaint transition-transform ${isOpen ? "rotate-180" : ""}`}
                      />
                    </div>
                  </div>
                </button>

                {tx.reasons && tx.reasons.length > 0 && (
                  <div className="mt-2 flex flex-col gap-1 pl-0.5 border-l border-border/60 ml-0.5">
                    {tx.reasons.map((r, i) => (
                      <div
                        key={i}
                        className={`text-[12px] font-mono pl-2.5 leading-relaxed ${
                          tx.decided_by === "behavioral_baseline" && i === tx.reasons.length - 1
                            ? "text-accent"
                            : "text-textmuted"
                        }`}
                      >
                        {r}
                      </div>
                    ))}
                  </div>
                )}

                {isOpen && <TransactionDetails tx={tx} />}

                {tx.verdict === "HOLD" && !tx.human_override && (
                  <div className="mt-3 flex gap-2">
                    <button
                      onClick={() => onResolveHold(tx.request_id, "APPROVE")}
                      className="px-3 py-1.5 rounded-md text-[12px] font-medium bg-approve/[0.08] text-approve border border-approve/25 hover:bg-approve/15 hover:border-approve/40 transition-colors"
                    >
                      Approve
                    </button>
                    <button
                      onClick={() => onResolveHold(tx.request_id, "BLOCK")}
                      className="px-3 py-1.5 rounded-md text-[12px] font-medium bg-block/[0.08] text-block border border-block/25 hover:bg-block/15 hover:border-block/40 transition-colors"
                    >
                      Block
                    </button>
                  </div>
                )}

                {tx.human_override && (
                  <div className="mt-2.5 text-[11px] font-mono text-accent flex items-center gap-1.5">
                    <span className="w-1 h-1 rounded-full bg-accent" />
                    Human resolved: {tx.human_override}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {!loading && !error && total > 0 && totalPages > 1 && (
        <div className="flex items-center justify-between pt-1">
          <span className="text-[11px] text-textfaint font-mono">
            Page {page} of {totalPages} · {total} total
          </span>
          <div className="flex items-center gap-1.5">
            <button
              onClick={() => onPageChange(page - 1)}
              disabled={page <= 1}
              aria-label="Previous page"
              className="p-1.5 rounded-md border border-border text-textmuted hover:text-text hover:border-borderlight disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              <ChevronLeft size={14} strokeWidth={2} />
            </button>
            <button
              onClick={() => onPageChange(page + 1)}
              disabled={page >= totalPages}
              aria-label="Next page"
              className="p-1.5 rounded-md border border-border text-textmuted hover:text-text hover:border-borderlight disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              <ChevronRight size={14} strokeWidth={2} />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
