const BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";
// Dev-only: matches the dev-mode fallback the backend uses when
// SENTINEL_API_KEY is unset. In any real deployment this should come
// from a build-time env var, never hardcoded like this.
const API_KEY = import.meta.env.VITE_SENTINEL_API_KEY || "dev-only-insecure-key-change-me";
const REQUEST_TIMEOUT_MS = 15000;

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function request(path, options = {}) {
  let res;
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    res = await fetch(`${BASE_URL}${path}`, {
      headers: { "Content-Type": "application/json", "X-API-Key": API_KEY },
      signal: controller.signal,
      ...options,
    });
  } catch (err) {
    // AbortError (timeout) and a genuine network failure (backend
    // down, CORS, DNS) both throw here, before there's any response
    // to inspect -- both get the same honest "couldn't reach it"
    // message rather than surfacing a cryptic DOMException.
    const message = err.name === "AbortError" ? "Request timed out" : "Could not reach the backend";
    throw new ApiError(0, message);
  } finally {
    clearTimeout(timeoutId);
  }
  if (!res.ok) {
    let message = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      message = body?.error?.message || message;
    } catch {
      // response wasn't JSON (e.g. a proxy error page) -- fall back to status text
      message = res.statusText || message;
    }
    throw new ApiError(res.status, message);
  }
  return res.json();
}

function qs(params) {
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") usp.set(k, v);
  }
  const s = usp.toString();
  return s ? `?${s}` : "";
}

export const api = {
  ApiError,
  createPolicy: (policy) => request("/policies", { method: "POST", body: JSON.stringify(policy) }),
  listPolicies: () => request("/policies"),
  getPolicy: (agentId) => request(`/policies/${agentId}`),
  // Paginated + filterable transaction feed (the real Phase 2/3 contract).
  listTransactionsPage: ({ page = 1, pageSize = 25, search, verdict, scenarioTag, agentId } = {}) =>
    request(`/transactions${qs({ page, page_size: pageSize, search, verdict, scenario_tag: scenarioTag, agent_id: agentId })}`),
  listScenarioTags: () => request("/transactions/scenarios"),
  summary: (agentId) => request(`/transactions/summary${qs({ agent_id: agentId })}`),
  simulate: (body) => request("/simulate", { method: "POST", body: JSON.stringify(body) }),
  simulateAsync: (body) => request("/simulate-async", { method: "POST", body: JSON.stringify(body) }),
  getJob: (jobId) => request(`/jobs/${jobId}`),
  getBatch: (batchId) => request(`/jobs/batch/${batchId}`),
  resolveHold: (requestId, humanVerdict) =>
    request(`/holds/${requestId}/resolve`, {
      method: "POST",
      body: JSON.stringify({ human_verdict: humanVerdict }),
    }),
  evalReport: () => request("/eval/report"),
};
