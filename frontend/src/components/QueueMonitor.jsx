const STATUS_STYLE = {
  QUEUED: "text-textmuted border-border bg-surface2",
  PROCESSING: "text-accent border-accent/40 bg-accent/10 animate-pulse",
  DONE: "text-approve border-approve/40 bg-approve/10",
  FAILED: "text-block border-block/40 bg-block/10",
};

export default function QueueMonitor({ jobs }) {
  if (!jobs.length) return null;

  const doneCount = jobs.filter((j) => j.status === "DONE" || j.status === "FAILED").length;

  return (
    <div className="bg-surface border border-border rounded-lg p-4">
      <div className="flex items-center justify-between mb-3">
        <h4 className="text-xs font-semibold text-textmuted uppercase tracking-wide">
          Async queue
        </h4>
        <span className="text-xs font-mono text-textmuted">
          {doneCount}/{jobs.length} complete
        </span>
      </div>
      <div
        className="h-1 w-full bg-surface2 rounded-full overflow-hidden mb-3"
        role="progressbar"
        aria-valuenow={doneCount}
        aria-valuemin={0}
        aria-valuemax={jobs.length}
        aria-label="Scenario batch progress"
      >
        <div
          className="h-full bg-accent rounded-full transition-all duration-300 ease-out"
          style={{ width: `${jobs.length ? (doneCount / jobs.length) * 100 : 0}%` }}
        />
      </div>
      <div className="flex flex-wrap gap-2">
        {jobs.map((job) => (
          <div
            key={job.job_id}
            className={`px-2.5 py-1 rounded border text-xs font-mono flex items-center gap-1.5 ${STATUS_STYLE[job.status] || ""}`}
          >
            <span>{job.job_id.replace("job_", "#")}</span>
            <span className="opacity-70">{job.status}</span>
            {job.result?.verdict && <span className="font-semibold">{job.result.verdict}</span>}
          </div>
        ))}
      </div>
    </div>
  );
}
