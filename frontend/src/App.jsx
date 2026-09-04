import { useState, useEffect, useCallback, useRef } from "react";
import { Activity, ShieldCheck, BarChart3 } from "lucide-react";
import { api } from "./api";
import LiveFeed from "./components/LiveFeed";
import PolicyEditor from "./components/PolicyEditor";
import ScenarioRunner from "./components/ScenarioRunner";
import EvalResults from "./components/EvalResults";
import StatsBar from "./components/StatsBar";
import QueueMonitor from "./components/QueueMonitor";
import ThemeToggle from "./components/ThemeToggle";
import AgentSelector from "./components/AgentSelector";
import { useToast } from "./components/Toast";

const DEFAULT_AGENT_ID = "agent_demo_1";
const PAGE_SIZE = 15;

const TABS = [
  { id: "feed", label: "Live feed", Icon: Activity },
  { id: "policy", label: "Policy", Icon: ShieldCheck },
  { id: "eval", label: "Evaluation", Icon: BarChart3 },
];

export default function App() {
  const { push: pushToast } = useToast();
  const [theme, setTheme] = useState(() => document.documentElement.getAttribute("data-theme") || "dark");
  const [tab, setTab] = useState("feed");
  const [selectedAgentId, setSelectedAgentId] = useState(DEFAULT_AGENT_ID);
  const [agents, setAgents] = useState([]);
  const [transactions, setTransactions] = useState([]);
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [total, setTotal] = useState(0);
  const [scenarioTags, setScenarioTags] = useState([]);
  const [scenarioFilter, setScenarioFilter] = useState("all");
  const [verdictFilter, setVerdictFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [summary, setSummary] = useState(null);
  const [policy, setPolicy] = useState(null);
  const [evalReport, setEvalReport] = useState(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);
  const [feedError, setFeedError] = useState(null);
  const [loadingFeed, setLoadingFeed] = useState(true);
  const [activeJobs, setActiveJobs] = useState([]);
  const pollRef = useRef(null);

  // Debounce search input so every keystroke doesn't fire a request --
  // 300ms is short enough to feel responsive, long enough that normal
  // typing speed doesn't hit the backend once per character.
  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(search), 300);
    return () => clearTimeout(t);
  }, [search]);

  // Switching agents is a full context switch -- back to page 1 so we
  // don't end up on "page 4" of a different agent's much shorter feed.
  useEffect(() => {
    setPage(1);
  }, [selectedAgentId]);

  // Any filter change resets to page 1 -- staying on page 4 of a
  // now-5-row filtered result would just show an empty page.
  useEffect(() => {
    setPage(1);
  }, [debouncedSearch, verdictFilter, scenarioFilter]);

  const refreshFeed = useCallback(async (showLoading = false) => {
    if (showLoading) setLoadingFeed(true);
    try {
      const [txPage, summ] = await Promise.all([
        api.listTransactionsPage({
          page,
          pageSize: PAGE_SIZE,
          search: debouncedSearch || undefined,
          verdict: verdictFilter === "all" ? undefined : verdictFilter,
          scenarioTag: scenarioFilter === "all" ? undefined : scenarioFilter,
          agentId: selectedAgentId,
        }),
        api.summary(selectedAgentId),
      ]);
      setTransactions(txPage.items);
      setTotalPages(txPage.total_pages);
      setTotal(txPage.total);
      setSummary(summ);
      setFeedError(null);
    } catch (e) {
      setFeedError(e.message);
    } finally {
      setLoadingFeed(false);
    }
  }, [page, debouncedSearch, verdictFilter, scenarioFilter, selectedAgentId]);

  const refreshScenarioTags = useCallback(async () => {
    try {
      const tags = await api.listScenarioTags();
      setScenarioTags(tags);
    } catch {
      // non-critical -- filter chips just won't show if this fails
    }
  }, []);

  const refreshAgents = useCallback(async () => {
    try {
      const list = await api.listPolicies();
      setAgents(list);
      // If the currently selected agent has no policy (e.g. first load
      // with only the seeded demo agent, or the selection got stale),
      // fall back to whatever agent actually exists rather than
      // silently showing an empty dashboard.
      if (list.length > 0 && !list.some((a) => a.agent_id === selectedAgentId)) {
        setSelectedAgentId(list[0].agent_id);
      }
    } catch {
      // non-critical -- selector just won't populate if this fails
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const refreshPolicy = useCallback(async () => {
    try {
      const p = await api.getPolicy(selectedAgentId);
      setPolicy(p);
    } catch {
      setPolicy(null);
    }
  }, [selectedAgentId]);

  const refreshEval = useCallback(async () => {
    try {
      const r = await api.evalReport();
      setEvalReport(r);
    } catch {
      setEvalReport(null);
    }
  }, []);

  useEffect(() => {
    refreshFeed(true);
  }, [refreshFeed]);

  useEffect(() => {
    refreshPolicy();
  }, [refreshPolicy]);

  useEffect(() => {
    refreshScenarioTags();
    refreshAgents();
    refreshEval();
    const interval = setInterval(() => {
      refreshFeed(false);
      refreshScenarioTags();
    }, 5000);
    return () => clearInterval(interval);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const toggleTheme = () => {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("sentinel-theme", next);
  };

  const handleCreatePolicy = async (policyBody) => {
    try {
      await api.createPolicy(policyBody);
      // Creating a policy for a *different* agent_id than the one
      // currently selected used to silently do nothing visible -- the
      // rest of the dashboard stayed pinned to whatever agent it
      // started on. Now the app actually follows wherever the saved
      // policy points, since the backend has always supported multiple
      // agents; the UI just wasn't using that.
      setSelectedAgentId(policyBody.agent_id);
      await refreshAgents();
      setError(null);
      pushToast(`Policy saved for ${policyBody.agent_id}`, "success");
    } catch (e) {
      setError(e.message);
      pushToast(`Couldn't save policy: ${e.message}`, "error");
    }
  };

  const handleRunScenario = async (body) => {
    setRunning(true);
    try {
      await api.simulate(body);
      await refreshFeed(false);
      await refreshScenarioTags();
      setError(null);
      pushToast("Scenario complete", "success");
    } catch (e) {
      setError(e.message);
      pushToast(`Scenario failed: ${e.message}`, "error");
    } finally {
      setRunning(false);
    }
  };

  const handleRunScenarioAsync = async (body) => {
    setRunning(true);
    setActiveJobs([]);
    try {
      const { job_ids, batch_id } = await api.simulateAsync(body);
      setActiveJobs(job_ids.map((id) => ({ job_id: id, status: "QUEUED", result: null })));

      if (pollRef.current) clearInterval(pollRef.current);
      // One request per poll against the batch endpoint, not one per
      // job -- N parallel per-job polls at short intervals is exactly
      // the request-storm pattern that can trip the API's rate limit
      // for larger scenario runs.
      pollRef.current = setInterval(async () => {
        try {
          const batch = await api.getBatch(batch_id);
          setActiveJobs(batch.jobs);
          if (batch.complete) {
            clearInterval(pollRef.current);
            pollRef.current = null;
            setRunning(false);
            await refreshFeed(false);
            await refreshScenarioTags();
            const failed = batch.jobs.filter((j) => j.status === "FAILED").length;
            if (failed > 0) {
              pushToast(`Scenario finished — ${failed} of ${batch.total} transaction(s) failed`, "error");
            } else {
              pushToast(`Scenario complete — ${batch.total} transactions evaluated`, "success");
            }
            setTimeout(() => setActiveJobs([]), 4000);
          }
        } catch (e) {
          clearInterval(pollRef.current);
          pollRef.current = null;
          setRunning(false);
          setError(e.message);
          pushToast(`Lost track of scenario progress: ${e.message}`, "error");
        }
      }, 600);
      setError(null);
    } catch (e) {
      setError(e.message);
      pushToast(`Couldn't start scenario: ${e.message}`, "error");
      setRunning(false);
    }
  };

  const handleResolveHold = async (requestId, verdict) => {
    try {
      await api.resolveHold(requestId, verdict);
      await refreshFeed(false);
      pushToast(`Hold ${verdict === "APPROVE" ? "approved" : "blocked"}`, "success");
    } catch (e) {
      setError(e.message);
      pushToast(`Couldn't resolve hold: ${e.message}`, "error");
    }
  };

  return (
    <div className="min-h-screen bg-bg bg-[radial-gradient(ellipse_80%_50%_at_50%_-20%,var(--bg-glow),transparent)]">
      <header className="sticky top-0 z-10 backdrop-blur-sm bg-bg/90 border-b border-border">
        <div className="max-w-6xl mx-auto px-6 h-16 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="relative flex items-center justify-center w-8 h-8 rounded-md bg-approve/10 border border-approve/20">
              <div className="w-2 h-2 rounded-full bg-approve animate-pulsedot" />
            </div>
            <div className="flex items-baseline gap-2.5">
              <h1 className="font-mono font-bold text-[15px] tracking-[0.08em] text-text">SENTINEL</h1>
              <span className="text-textfaint text-[13px] hidden sm:inline">Agent Payment Trust Layer</span>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <AgentSelector agents={agents} selectedAgentId={selectedAgentId} onSelect={setSelectedAgentId} />
            <nav className="flex gap-1 bg-surface2/60 p-1 rounded-lg border border-border">
              {TABS.map((t) => {
                const active = tab === t.id;
                return (
                  <button
                    key={t.id}
                    onClick={() => setTab(t.id)}
                    className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[13px] font-medium transition-all ${
                      active
                        ? "bg-surface3 text-text shadow-card"
                        : "text-textmuted hover:text-text hover:bg-surface2"
                    }`}
                  >
                    <t.Icon size={14} strokeWidth={2.25} />
                    {t.label}
                  </button>
                );
              })}
            </nav>
            <ThemeToggle theme={theme} onToggle={toggleTheme} />
          </div>
        </div>
      </header>

      <main className="max-w-6xl mx-auto px-6 py-8 flex flex-col gap-7">
        {error && (
          <div className="bg-block/[0.07] border border-block/25 text-block text-[13px] font-mono rounded-lg px-4 py-2.5">
            {error} — is the backend running on :8000?
          </div>
        )}

        {tab === "feed" && (
          <>
            <StatsBar summary={summary} />

            {activeJobs.length > 0 && <QueueMonitor jobs={activeJobs} />}

            <div className="grid grid-cols-1 lg:grid-cols-3 gap-7">
              <div className="lg:col-span-2">
                <h2 className="text-[11px] font-semibold text-textfaint mb-3.5 uppercase tracking-[0.12em]">
                  Transaction feed
                </h2>
                <LiveFeed
                  transactions={transactions}
                  onResolveHold={handleResolveHold}
                  scenarioTags={scenarioTags}
                  scenarioFilter={scenarioFilter}
                  onScenarioFilterChange={setScenarioFilter}
                  verdictFilter={verdictFilter}
                  onVerdictFilterChange={setVerdictFilter}
                  search={search}
                  onSearchChange={setSearch}
                  page={page}
                  totalPages={totalPages}
                  total={total}
                  onPageChange={setPage}
                  loading={loadingFeed}
                  error={feedError}
                />
              </div>
              <div>
                <h2 className="text-[11px] font-semibold text-textfaint mb-3.5 uppercase tracking-[0.12em]">
                  Simulate
                </h2>
                <ScenarioRunner
                  agentId={selectedAgentId}
                  onRun={handleRunScenario}
                  onRunAsync={handleRunScenarioAsync}
                  running={running}
                  disabled={!policy}
                />
              </div>
            </div>
          </>
        )}

        {tab === "policy" && (
          <div className="max-w-2xl">
            <PolicyEditor onCreatePolicy={handleCreatePolicy} currentPolicy={policy} agentId={selectedAgentId} />
          </div>
        )}

        {tab === "eval" && <EvalResults report={evalReport} />}
      </main>
    </div>
  );
}
