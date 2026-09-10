import type { ConnectorSyncProgress, IngestionTokenUsage } from "./types";

interface TokenTrackedRun {
  status: string;
  started_at: string;
  result: ConnectorSyncProgress | null;
}

export function latestIngestionUsage(
  provider: string,
  runs: TokenTrackedRun[],
): IngestionTokenUsage | null {
  const latest = [...runs].sort((left, right) =>
    right.started_at.localeCompare(left.started_at),
  )[0];
  if (!latest) return null;
  return {
    provider,
    active: latest.status === "queued" || latest.status === "running",
    startedAt: latest.started_at,
    input: latest.result?.ingestion_input_tokens ?? 0,
    output: latest.result?.ingestion_output_tokens ?? 0,
    total: latest.result?.ingestion_total_tokens ?? 0,
  };
}

// Sums every run's tokens for this provider, not just the latest one -- the
// topbar counter is meant to answer "how many tokens has this provider spent
// in total", and a per-sync-click reset made that number look wrong (it only
// ever showed the most recent click's cost, forgetting everything before it).
export function totalIngestionUsage(
  provider: string,
  runs: TokenTrackedRun[],
): IngestionTokenUsage | null {
  if (!runs.length) return null;
  let input = 0, output = 0, total = 0, startedAt = "";
  let active = false;
  for (const run of runs) {
    input += run.result?.ingestion_input_tokens ?? 0;
    output += run.result?.ingestion_output_tokens ?? 0;
    total += run.result?.ingestion_total_tokens ?? 0;
    if (run.status === "queued" || run.status === "running") active = true;
    if (run.started_at > startedAt) startedAt = run.started_at;
  }
  return { provider, active, startedAt, input, output, total };
}
