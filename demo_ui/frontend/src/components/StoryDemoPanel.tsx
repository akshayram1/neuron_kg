import { AlertTriangle, BrainCircuit, Check, CheckCircle2, FlaskConical, LoaderCircle, Play, RotateCcw, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { applyStoryPhase, getStoryRun, getStoryState, resetStoryDemo, reviewStoryWisdom, startStoryDemo } from "../api";
import type { StoryFinding, StoryRun, StoryState } from "../types";
import SyncProgress from "./SyncProgress";

const PHASES = [
  { id: "baseline", title: "Create graph + baseline", detail: "Load Auth, MCP and Argo from Jira, Notion and Bitbucket." },
  { id: "deprecation", title: "Deprecate Auth API v1", detail: "Surface MCP and Argo in the breaking-change blast radius." },
  { id: "migration-claim", title: "MCP claims v2 migration", detail: "Compare Jira/Notion claims with current Bitbucket code." },
  { id: "code-catches-up", title: "MCP code catches up", detail: "Resolve MCP's mismatch while Argo remains affected." },
  { id: "v1-removal", title: "Auth v1 removal exposes Argo", detail: "Materialize the predicted risk and propose a consumer-readiness playbook." },
];

interface Props {
  open: boolean;
  graphName: string;
  onClose: () => void;
  onGraphCreated: (name: string) => Promise<void>;
  onGraphChanged: () => void;
  onReset: () => Promise<void>;
}

export default function StoryDemoPanel({
  open, graphName, onClose, onGraphCreated, onGraphChanged, onReset,
}: Props) {
  const [state, setState] = useState<StoryState | null>(null);
  const [run, setRun] = useState<StoryRun | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [routingScope, setRoutingScope] = useState<"phase" | "total">("phase");
  const [reviewingWisdom, setReviewingWisdom] = useState<string | null>(null);
  const isStoryGraph = graphName.startsWith("story-");

  const load = useCallback(async () => {
    if (!isStoryGraph) { setState(null); return; }
    try {
      const value = await getStoryState(graphName);
      setState(value);
      setRun(value.running);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load the story demo.");
    }
  }, [graphName, isStoryGraph]);

  useEffect(() => { if (open) void load(); }, [open, load]);
  useEffect(() => {
    if (!open || !run || run.status !== "running") return;
    const timer = window.setTimeout(async () => {
      try {
        const current = await getStoryRun(run.runId);
        setRun(current);
        if (current.status === "completed") {
          await load();
          onGraphChanged();
        } else if (current.status === "failed") {
          setError(current.error || "Story phase failed.");
        }
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : "Could not refresh phase progress.");
      }
    }, 1200);
    return () => window.clearTimeout(timer);
  }, [open, run, load, onGraphChanged]);

  const start = async () => {
    setBusy(true); setError(null);
    try {
      const value = await startStoryDemo();
      await onGraphCreated(value.graph_name);
      const current = await getStoryRun(value.run_id);
      setRun(current);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not start the story demo.");
    } finally { setBusy(false); }
  };

  const apply = async (phase: string) => {
    setBusy(true); setError(null);
    try {
      const value = await applyStoryPhase(graphName, phase);
      setRun(await getStoryRun(value.run_id));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not apply the story phase.");
    } finally { setBusy(false); }
  };

  const reset = async () => {
    if (!window.confirm("Reset this  story graph and delete its Postgres, pgvector and Falkor data?")) return;
    setBusy(true); setError(null);
    try {
      await resetStoryDemo(graphName);
      setState(null); setRun(null);
      await onReset();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not reset the story demo.");
    } finally { setBusy(false); }
  };

  const reviewWisdom = async (proposalId: string, decision: "approve" | "reject") => {
    setReviewingWisdom(proposalId); setError(null);
    try {
      await reviewStoryWisdom(graphName, proposalId, decision);
      await load();
      onGraphChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not review the wisdom proposal.");
    } finally { setReviewingWisdom(null); }
  };

  if (!open) return null;
  const completed = new Set(state?.completedPhases ?? []);
  const activeRun = run?.status === "running" ? run : null;
  const openFindingCount = state?.findings.filter((finding) => finding.status === "open").length ?? 0;
  const formatTime = (value: string | null | undefined) => value
    ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value))
    : "Unknown time";
  const findings = state?.findings ?? [];
  const openFindings = findings.filter((finding) => finding.status === "open");
  const historicalFindings = findings.filter((finding) => finding.status !== "open");
  const routingProgress = routingScope === "total" ? state?.routingTotals : state?.lastRun?.progress;
  const routingMetricsAvailable = routingScope === "total"
    ? Boolean(state?.routingTotals.routing_runs_measured)
    : routingProgress?.pass1_links_written !== undefined;
  const routingValue = (value: number | undefined) => routingMetricsAvailable ? (value ?? 0) : "—";
  const renderFinding = (finding: StoryFinding) => <article className={`story-finding ${finding.status}`} key={finding.id}>
    <div className="story-finding-header">
      <AlertTriangle size={14} />
      <strong>{finding.title}</strong>
      <span>{finding.status}</span>
    </div>
    <p>{finding.summary}</p>
    <small className="story-finding-reasoning">{finding.reasoning}</small>
    {finding.status === "stale" && finding.staleReason && <div className="story-stale-reason">
      Stale: {finding.staleReason}
    </div>}
    <div className="story-finding-time">
      <span>Detected {formatTime(finding.createdAt)}</span>
      <span>Last supported {formatTime(finding.lastSeenAt)}</span>
      {finding.staleAt && <span>Marked stale {formatTime(finding.staleAt)}</span>}
    </div>
    {!!finding.sources.length && <details className="story-finding-evidence">
      <summary><span>Evidence</span><strong>{finding.sources.length} {finding.sources.length === 1 ? "source" : "sources"}</strong></summary>
      <div className="story-finding-sources">
        {finding.sources.map((source) => <div key={`${finding.id}:${source.recordKey}:${source.role}:${source.chunkId ?? "record"}`}>
          <span>{source.provider ?? "source"} · {source.role.replaceAll("_", " ")}</span>
          {source.url
            ? <a href={source.url} target="_blank" rel="noreferrer">{source.name ?? source.recordKey}</a>
            : <strong>{source.name ?? source.recordKey}</strong>}
          <time>{formatTime(source.sourceTime)}</time>
          {source.excerpt && <small title={source.excerpt}>{source.excerpt}</small>}
        </div>)}
      </div>
    </details>}
  </article>;

  return <div className="connector-overlay" role="presentation" onMouseDown={onClose}>
    <aside className="connector-drawer story-drawer" role="dialog" aria-modal="true" aria-label=" story demo" onMouseDown={(event) => event.stopPropagation()}>
      <div className="connector-header">
        <div className="story-logo"><FlaskConical size={22} /></div>
        <div><span className="eyebrow">Guided scenario</span><h2> story demo</h2></div>
        <button className="connector-close" onClick={onClose} aria-label="Close"><X size={18} /></button>
      </div>
      <p className="connector-copy">Run real ingestion, extraction, pgvector retrieval and Falkor reasoning without connecting external accounts.</p>
      {error && <div className="connector-error">{error}</div>}

      {!isStoryGraph && <button className="story-start" onClick={() => void start()} disabled={busy}>
        {busy ? <LoaderCircle className="spin" size={15} /> : <Play size={15} />}
        Create fresh graph and ingest baseline
      </button>}

      {isStoryGraph && <>
        <div className="story-graph-name"><span>Active graph</span><code>{graphName}</code></div>
        <div className="story-phases">
          {PHASES.map((phase, index) => {
            const done = completed.has(phase.id);
            const running = activeRun?.phase === phase.id;
            const previousDone = index === 0 || completed.has(PHASES[index - 1].id);
            const disabled = phase.id === "baseline" || done || !previousDone || Boolean(activeRun) || busy;
            return <article className={`story-phase ${done ? "done" : ""}`} key={phase.id}>
              <div className="story-phase-index">{done ? <CheckCircle2 size={16} /> : index + 1}</div>
              <div className="story-phase-copy"><strong>{phase.title}</strong><span>{phase.detail}</span></div>
              {phase.id !== "baseline" && <button onClick={() => void apply(phase.id)} disabled={disabled}>
                {running ? <LoaderCircle className="spin" size={13} /> : <Play size={13} />}
                {running ? "Running" : done ? "Complete" : "Run"}
              </button>}
            </article>;
          })}
        </div>
        {activeRun && <SyncProgress progress={activeRun.progress} provider="Story" syncing />}

        {state?.lastRun && <div className="story-routing">
          <div className="story-routing-header">
            <div><span>Selective AI routing</span><strong>{routingScope === "phase" ? state.lastRun.phase : "all completed phases"}</strong></div>
            <div className="story-routing-scope" aria-label="Routing metric scope">
              <button className={routingScope === "phase" ? "active" : ""} onClick={() => setRoutingScope("phase")}>Phase</button>
              <button className={routingScope === "total" ? "active" : ""} onClick={() => setRoutingScope("total")}>Total</button>
            </div>
          </div>
          <div className="story-routing-grid">
            <div title="Graph relationships written by deterministic Pass 1 before any LLM call."><strong>{routingValue(routingProgress?.pass1_links_written)}</strong><span>Pass 1 links</span></div>
            <div title="Changed source records that produced at least one deterministic graph relationship."><strong>{routingValue(routingProgress?.pass1_records_linked)}</strong><span>Records linked</span></div>
            <div title="Chunks completely handled by Pass 1; no LLM call was needed."><strong>{routingValue(routingProgress?.chunks_llm_skipped)}</strong><span>LLM skipped</span></div>
            <div title="Pass 1 linked the record, then only its unresolved semantic evidence went to the LLM."><strong>{routingValue(routingProgress?.chunks_hybrid)}</strong><span>Hybrid chunks</span></div>
            <div title="Chunks sent to the LLM without any deterministic record relationship."><strong>{routingValue(routingProgress?.chunks_llm_only)}</strong><span>LLM-only chunks</span></div>
            <div><strong>{routingProgress?.llm_calls ?? 0}</strong><span>LLM calls</span></div>
            <div className="story-routing-tokens"><strong>{routingProgress?.ingestion_total_tokens ?? 0}</strong><span>LLM tokens</span></div>
          </div>
          {!routingMetricsAvailable && <p className="story-routing-legacy">Detailed Pass 1 metrics were not captured for this older run. Create a fresh story graph to populate them.</p>}
        </div>}

        <div className="story-findings">
          <div className="connection-list-title"><span>Findings</span><span>{openFindingCount} open · {state?.findings.length ?? 0} total</span></div>
          {!state?.findings.length && <div className="connection-empty">No findings in the current phase.</div>}
          {openFindings.map(renderFinding)}
          {!!historicalFindings.length && <details className="story-finding-history">
            <summary><span>Finding history</span><strong>{historicalFindings.length} stale / resolved</strong></summary>
            <div>{historicalFindings.map(renderFinding)}</div>
          </details>}
        </div>
        <div className="story-wisdom">
          <div className="connection-list-title">
            <span>Wisdom layer</span>
            <span>{state?.wisdom.filter((item) => item.status === "proposed").length ?? 0} review</span>
          </div>
          {!state?.wisdom.length && <div className="story-wisdom-empty">
            <BrainCircuit size={18} />
            <strong>No wisdom proposal yet</strong>
            <span>A grounded proposal appears after a claim-versus-code finding is detected.</span>
          </div>}
          {state?.wisdom.map((proposal) => <article className={`story-wisdom-card ${proposal.status}`} key={proposal.id}>
            <div className="story-wisdom-card-header">
              <div className="story-wisdom-icon"><BrainCircuit size={15} /></div>
              <div><span>{proposal.type} · v{proposal.version}</span><strong>{proposal.title}</strong></div>
              <em>{proposal.status}</em>
            </div>
            <blockquote>{proposal.statement}</blockquote>
            <div className="story-wisdom-explanation">
              <span>Why this was proposed</span>
              <p>{proposal.rationale}</p>
            </div>
            <div className="story-wisdom-action">
              <span>Recommended action</span>
              <p>{proposal.recommendedAction}</p>
            </div>
            <div className="story-wisdom-meta">
              <span>{Math.round(proposal.confidence * 100)}% confidence</span>
              <span>{proposal.supportingFindings.length} supporting {proposal.supportingFindings.length === 1 ? "finding" : "findings"}</span>
              <span>{proposal.generationMethod.replaceAll("_", " ")}</span>
            </div>
            {!!proposal.supportingFindings.length && <details className="story-wisdom-lineage">
              <summary>View finding lineage</summary>
              <div>{proposal.supportingFindings.map((finding) => <div key={finding.id}>
                <strong>{finding.title}</strong>
                <span>{finding.status} · {finding.sources.length} sources</span>
                <p>{finding.summary}</p>
              </div>)}</div>
            </details>}
            {proposal.status === "proposed" && <>
              <p className="story-wisdom-review-reason">{String(proposal.properties.reviewReason ?? "Human review is required before this becomes active wisdom.")}</p>
              <div className="story-wisdom-review-actions">
                <button className="approve" disabled={reviewingWisdom === proposal.id} onClick={() => void reviewWisdom(proposal.id, "approve")}>
                  {reviewingWisdom === proposal.id ? <LoaderCircle className="spin" size={13} /> : <Check size={13} />} Approve wisdom
                </button>
                <button className="reject" disabled={reviewingWisdom === proposal.id} onClick={() => void reviewWisdom(proposal.id, "reject")}>
                  <X size={13} /> Reject
                </button>
              </div>
            </>}
          </article>)}
        </div>
        <button className="story-reset" onClick={() => void reset()} disabled={busy || Boolean(activeRun)}>
          <RotateCcw size={14} /> Reset demo
        </button>
      </>}
    </aside>
  </div>;
}
