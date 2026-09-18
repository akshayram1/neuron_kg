import type { ConnectorSyncProgress } from "../types";

interface SyncProgressProps {
  progress: ConnectorSyncProgress | null | undefined;
  provider: string;
  syncing: boolean;
}

function phaseLabel(provider: string, phase?: string): string {
  if (phase === "fetching") return `Fetching from ${provider}`;
  if (phase === "preparing") return `Preparing ${provider} content`;
  if (phase === "ingesting") return "Writing to knowledge graph";
  if (phase === "semantic") return "Understanding content with AI";
  return "Waiting to start";
}

export default function SyncProgress({ progress, provider, syncing }: SyncProgressProps) {
  if (!syncing) return null;

  const done = progress?.records_done ?? 0;
  const total = progress?.records_total ?? 0;
  const details: string[] = [];
  if (total > 0) details.push(`${done} / ${total} records`);
  else details.push(progress?.phase === "fetching" ? "Discovering content…" : "Starting…");

  const pages = progress?.pages_fetched ?? 0;
  const files = progress?.files_matched ?? progress?.files_processed ?? progress?.files_fetched ?? 0;
  const commits = progress?.commits_fetched ?? 0;
  const written = progress?.records_written ?? progress?.episodes_ingested ?? progress?.chunks_ingested ?? 0;
  const unchanged = progress?.records_kept ?? progress?.records_skipped ?? 0;
  const facts = progress?.facts_written ?? 0;
  const chunks = progress?.chunks_ingested ?? 0;
  const chunkTotal = progress?.chunks_total ?? 0;
  const pass1Links = progress?.pass1_links_written ?? 0;
  const skippedChunks = progress?.chunks_llm_skipped ?? progress?.chunks_deterministic ?? 0;
  const hybridChunks = progress?.chunks_hybrid ?? 0;
  const llmCalls = progress?.llm_calls ?? 0;

  if (pages > 0) details.push(`${pages} pages`);
  if (files > 0) details.push(`${files} files`);
  if (commits > 0) details.push(`${commits} commits`);
  if (written > 0) details.push(`${written} written`);
  if (unchanged > 0) details.push(`${unchanged} unchanged`);
  if (facts > 0) details.push(`${facts} facts`);
  if (chunkTotal > 0) details.push(`${chunks} / ${chunkTotal} AI chunks`);
  if (pass1Links > 0) details.push(`${pass1Links} Pass 1 links`);
  if (skippedChunks > 0) details.push(`${skippedChunks} LLM skipped`);
  if (hybridChunks > 0) details.push(`${hybridChunks} hybrid chunks`);
  if (llmCalls > 0) details.push(`${llmCalls} LLM calls`);

  const percentage = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;

  return (
    <div className="sync-progress" aria-live="polite">
      <div className="sync-progress-label">{phaseLabel(provider, progress?.phase)}</div>
      <div className="sync-progress-current">{progress?.current || "Starting connector pipeline…"}</div>
      <div
          className={`sync-progress-bar ${total > 0 ? "" : "indeterminate"}`}
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={total || undefined}
          aria-valuenow={total > 0 ? done : undefined}
        >
          <i style={total > 0 ? { width: `${percentage}%` } : undefined} />
        </div>
      <div className="sync-progress-count">{details.join(" · ")}</div>
    </div>
  );
}
