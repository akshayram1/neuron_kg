import type { ConnectorSyncProgress } from "../types";

interface SyncProgressProps {
  progress: ConnectorSyncProgress | null | undefined;
  provider: string;
  syncing: boolean;
}

// Sync coverage (25-plan.md Phase 0, "Sync coverage report"): each connector
// route logs provider-reported total / fetched / ledger / skipped-by-rule
// counts to the ledger at the end of a run (`ConnectorLedger.record_sync_
// coverage`) and echoes the same numbers into the run's `result` object --
// the same payload this component already receives as `progress`, so no new
// fetch is needed here. Declared locally rather than in ../types since only
// this component reads them.
interface SyncCoverageFields {
  provider_reported_total?: number | null;
  fetched_count?: number;
  ledger_count?: number;
  skipped_by_rule_count?: number;
}

function coverageSummary(
  progress: (ConnectorSyncProgress & SyncCoverageFields) | null | undefined,
): string | null {
  const fetched = progress?.fetched_count;
  if (fetched == null) return null;
  const total = progress?.provider_reported_total;
  const ledgerCount = progress?.ledger_count;
  const skipped = progress?.skipped_by_rule_count ?? 0;
  const parts = [total != null ? `${fetched}/${total} fetched` : `${fetched} fetched`];
  if (skipped > 0) parts.push(`${skipped} skipped`);
  // Only worth a callout when it differs -- equal counts mean the write
  // that record_sync_coverage re-read from the ledger matches what the sync
  // loop believed it wrote, i.e. nothing silently failed to commit.
  if (ledgerCount != null && ledgerCount !== fetched) parts.push(`${ledgerCount} in ledger`);
  return parts.join(", ");
}

function phaseLabel(provider: string, phase?: string): string {
  if (phase === "fetching") return `Fetching from ${provider}`;
  if (phase === "preparing") return `Preparing ${provider} content`;
  if (phase === "ingesting") return "Writing to knowledge graph";
  if (phase === "semantic") return "Understanding content with AI";
  return "Waiting to start";
}

export default function SyncProgress({ progress, provider, syncing }: SyncProgressProps) {
  const coverage = coverageSummary(progress);

  if (!syncing) {
    // The sync-coverage numbers are only known once the run is done, and by
    // then `syncing` has already flipped false -- still worth a compact,
    // one-line summary rather than nothing.
    if (!coverage) return null;
    return (
      <div className="sync-progress" aria-live="polite">
        <div className="sync-progress-count">{provider} coverage: {coverage}</div>
      </div>
    );
  }

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
  if (coverage) details.push(`coverage: ${coverage}`);

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
