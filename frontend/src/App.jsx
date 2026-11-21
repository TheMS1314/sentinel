import { useState, useEffect, useCallback, useRef } from "react";
import { Activity, ShieldCheck, BarChart3 } from "lucide-react";
import { api } from "./api";
import ProviderConnections from "./components/ProviderConnections";
import Operations from "./components/Operations";
import MerchantDirectory from "./components/MerchantDirectory";
import AccessControl from "./components/AccessControl";
import RiskIntelligence from "./components/RiskIntelligence";
import LiveFeed from "./components/LiveFeed";
import PolicyEditor from "./components/PolicyEditor";
import ScenarioRunner from "./components/ScenarioRunner";
import EvalResults from "./components/EvalResults";
import StatsBar from "./components/StatsBar";
import QueueMonitor from "./components/QueueMonitor";
import ThemeToggle from "./components/ThemeToggle";
import AgentSelector from "./components/AgentSelector";
import { useToast } from "./components/toastContext";

const DEFAULT_AGENT_ID = "agent_demo_1";
const PAGE_SIZE = 15;

const TABS = [
  { id: "providers", label: "Providers", Icon: ShieldCheck },
  { id: "ops", label: "Operations", Icon: ShieldCheck },
  { id: "feed", label: "Live feed", Icon: Activity },
  { id: "policy", label: "Policy", Icon: ShieldCheck },
  { id: "eval", label: "Evaluation", Icon: BarChart3 },
  { id: "merchants", label: "Merchants", Icon: ShieldCheck },
  { id: "access", label: "Access", Icon: ShieldCheck },
  { id: "risk", label: "Risk intelligence", Icon: ShieldCheck },
];

export function Dashboard({ identity, onLogout }) {
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
  const feedVersion = useRef(0);
  const feedContext = useRef("");
  feedContext.current = JSON.stringify([page, debouncedSearch, verdictFilter, scenarioFilter, selectedAgentId]);
  const policyVersion = useRef(0);
  const mounted = useRef(true);
  const context = useRef(selectedAgentId);
  const resolving = useRef(new Set());
  context.current = selectedAgentId;
  const [pendingHolds, setPendingHolds] = useState([]);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; clearInterval(pollRef.current); };
  }, []);

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
    const expected = JSON.stringify([page, debouncedSearch, verdictFilter, scenarioFilter, selectedAgentId]);
    if (expected !== feedContext.current) return;
    const version = ++feedVersion.current;
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
      if (!mounted.current || version !== feedVersion.current || expected !== feedContext.current) return;
      setTransactions(txPage.items);
      setTotalPages(txPage.total_pages);
      setTotal(txPage.total);
      setSummary(summ);
      setFeedError(null);
    } catch (e) {
      if (mounted.current && version === feedVersion.current) setFeedError(e.message);
    } finally {
      if (mounted.current && version === feedVersion.current) setLoadingFeed(false);
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
      if (list.length > 0 && !list.some((a) => a.agent_id === context.current)) {
        setSelectedAgentId(list[0].agent_id);
      }
    } catch {
      // non-critical -- selector just won't populate if this fails
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const refreshPolicy = useCallback(async () => {
    const version = ++policyVersion.current;
    try {
      const p = await api.getPolicy(selectedAgentId);
      if (mounted.current && version === policyVersion.current && selectedAgentId === context.current) setPolicy(p);
    } catch {
      if (mounted.current && version === policyVersion.current && selectedAgentId === context.current) setPolicy(null);
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
    refreshScenarioTags(); refreshAgents(); refreshEval();
  }, [refreshScenarioTags, refreshAgents, refreshEval]);

  useEffect(() => {
    const interval = setInterval(() => { refreshFeed(false); refreshScenarioTags(); }, 10000);
    return () => clearInterval(interval);
  }, [refreshFeed, refreshScenarioTags]);

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
      context.current = policyBody.agent_id;
      setSelectedAgentId(policyBody.agent_id);
      if (policyBody.agent_id === selectedAgentId) await refreshPolicy();
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
      let polling = false;
      pollRef.current = setInterval(async () => {
        if (polling || !mounted.current) return;
        polling = true;
        try {
          const batch = await api.getBatch(batch_id);
          if (!mounted.current) return;
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

          }
        } catch (e) {
          clearInterval(pollRef.current);
          pollRef.current = null;
          setRunning(false);
          setError(e.message);
          pushToast(`Lost track of scenario progress: ${e.message}`, "error");
        }
        finally { polling = false; }
      }, 2000);
      setError(null);
    } catch (e) {
      setError(e.message);
      pushToast(`Couldn't start scenario: ${e.message}`, "error");
      setRunning(false);
    }
  };

  const handleResolveHold = async (requestId, verdict) => {
    if (resolving.current.has(requestId)) return;
    resolving.current.add(requestId); setPendingHolds([...resolving.current]);
    try {
      await api.resolveHold(requestId, verdict);
      await refreshFeed(false);
      pushToast(`Hold ${verdict === "APPROVE" ? "approved" : "blocked"}`, "success");
    } catch (e) {
      setError(e.message);
      pushToast(`Couldn't resolve hold: ${e.message}`, "error");
    } finally {
      resolving.current.delete(requestId);
      if (mounted.current) setPendingHolds([...resolving.current]);
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
            <button onClick={onLogout} className="text-xs text-textmuted">Sign out ({identity.subject})</button>
            <AgentSelector agents={agents} selectedAgentId={selectedAgentId} onSelect={setSelectedAgentId} />
            <nav className="flex gap-1 bg-surface2/60 p-1 rounded-lg border border-border">
              {TABS.filter(t => (t.id !== "policy" || ["admin", "owner"].includes(identity.role)) && (!["risk", "access", "ops", "providers"].includes(t.id) || identity.role === "admin")).map((t) => {
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
                  pendingHolds={pendingHolds}
                  canReview={["admin", "owner", "reviewer"].includes(identity.role)}
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
                  disabled={!policy || !["admin", "owner"].includes(identity.role)}
                />
              </div>
            </div>
          </>
        )}

        {tab === "policy" && (
          <div className="max-w-2xl">
            <PolicyEditor ownerId={identity.owner_id} onCreatePolicy={handleCreatePolicy} currentPolicy={policy} agentId={selectedAgentId} />
          </div>
        )}

        {tab === "eval" && <EvalResults report={evalReport} />}
        {tab === "providers" && identity.role === "admin" && <ProviderConnections />}
        {tab === "ops" && identity.role === "admin" && <Operations />}
        {tab === "risk" && identity.role === "admin" && <RiskIntelligence />}
        {tab === "merchants" && ["admin","owner","reviewer"].includes(identity.role) && <MerchantDirectory identity={identity} />}
        {tab === "access" && identity.role === "admin" && <AccessControl currentSubject={identity.subject} />}
      </main>
    </div>
  );
}


export default function App() {
  const [identity, setIdentity] = useState(null);
  const [loading, setLoading] = useState(true);
  const [credential, setCredential] = useState('');
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    api.me().then(value => { if (active) setIdentity(value); }).catch(() => {}).finally(() => { if (active) setLoading(false); });
    const reset = () => setIdentity(null);
    window.addEventListener('sentinel:unauthorized', reset);
    return () => { active = false; window.removeEventListener('sentinel:unauthorized', reset); };
  }, []);
  if (loading) return <p className="p-8">Loading session…</p>;
  if (identity) return <Dashboard identity={identity} onLogout={async () => { await api.logout(); setIdentity(null); }} />;
  return <main className="min-h-screen flex items-center justify-center bg-bg text-text">
    <form className="w-96 p-8 flex flex-col gap-4 border border-border rounded-xl" onSubmit={async e => {
      e.preventDefault(); setError('');
      try { const value = await api.login(credential); setCredential(''); setIdentity(value); }
      catch (err) { setError(err.message); }
    }}>
      <h1 className="text-xl font-semibold">Sign in to Sentinel</h1>
      <label>Access credential<input className="input mt-2" type="password" autoComplete="current-password" value={credential} onChange={e => setCredential(e.target.value)} required /></label>
      <button className="bg-accent text-white rounded p-2">Sign in</button>
      {error && <p role="alert">{error}</p>}
    </form>
  </main>;
}
