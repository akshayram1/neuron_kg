import { CheckCircle2, Database, LoaderCircle, RefreshCw, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  getLocalDataStatus, ingestLocalBitbucket, ingestLocalJira, ingestLocalNotion, resetLocalData,
} from "../api";
import type { IngestionTokenUsage, LocalDataIngestResult, LocalDataProvider, LocalDataStatus } from "../types";

interface Props {
  open: boolean;
  graphName: string;
  onClose: () => void;
  onSyncComplete: () => void;
  onTokenUsage: (usage: IngestionTokenUsage) => void;
}

interface Step {
  provider: LocalDataProvider;
  title: string;
  detail: string;
  ingest: (graphName: string) => Promise<LocalDataIngestResult>;
}

const STEPS: Step[] = [
  {
    provider: "bitbucket", title: "1. Bitbucket",
    detail: "Source files, saved commits, changed paths and pull requests from nilus_data.",
    ingest: ingestLocalBitbucket,
  },
  {
    provider: "jira", title: "2. Jira",
    detail: "Issues, assignees, reporters and comments from nilus_data/jira_real.",
    ingest: ingestLocalJira,
  },
  {
    provider: "notion", title: "3. Notion",
    detail: "Pages and their rendered content, plus LLM fact extraction.",
    ingest: ingestLocalNotion,
  },
];

function summarize(result: LocalDataIngestResult): string {
  switch (result.provider) {
    case "jira":
      return `${result.issues_fetched ?? 0} issues · ${result.records_written} written · ${result.records_kept} already synced`;
    case "bitbucket":
      return `${result.files_fetched ?? 0} files · ${result.pull_requests_fetched ?? 0} PRs · ${result.commits_fetched ?? 0} commits · ${result.files_written ?? 0} files written`;
    case "notion":
      return `${result.pages_fetched ?? 0} pages · ${result.chunks_ingested ?? 0} chunks · ${result.entities_written ?? 0} entities · ${result.facts_written ?? 0} facts`;
  }
}

export default function LocalDataIngestPanel({ open, graphName, onClose, onSyncComplete, onTokenUsage }: Props) {
  const [status, setStatus] = useState<LocalDataStatus | null>(null);
  const [results, setResults] = useState<Partial<Record<LocalDataProvider, LocalDataIngestResult>>>({});
  const [running, setRunning] = useState<LocalDataProvider | null>(null);
  const [resetting, setResetting] = useState<LocalDataProvider | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setStatus(await getLocalDataStatus(graphName));
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load local data status.");
    }
  }, [graphName]);

  useEffect(() => { if (open) void load(); }, [open, load]);

  const run = async (step: Step) => {
    setRunning(step.provider); setError(null);
    try {
      const result = await step.ingest(graphName);
      setResults((current) => ({ ...current, [step.provider]: result }));
      if (result.provider === "notion" && result.ingestion_total_tokens) {
        onTokenUsage({
          provider: "Notion (nilus_data)", active: false, startedAt: new Date().toISOString(),
          input: result.ingestion_input_tokens ?? 0, output: result.ingestion_output_tokens ?? 0,
          total: result.ingestion_total_tokens,
        });
      }
      await load();
      onSyncComplete();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : `Could not ingest local ${step.title} data.`);
    } finally {
      setRunning(null);
    }
  };

  const reset = async (step: Step) => {
    setResetting(step.provider); setError(null);
    try {
      await resetLocalData(step.provider, graphName);
      setResults((current) => { const next = { ...current }; delete next[step.provider]; return next; });
      await load();
      onSyncComplete();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : `Could not reset local ${step.title} data.`);
    } finally {
      setResetting(null);
    }
  };

  if (!open) return null;
  return <div className="connector-overlay" role="presentation" onMouseDown={onClose}>
    <aside className="connector-drawer" role="dialog" aria-modal="true" aria-label="Local data ingestion" onMouseDown={(event) => event.stopPropagation()}>
      <div className="connector-header">
        <div className="jira-logo"><Database size={22} /></div>
        <div><span className="eyebrow">Knowledge source</span><h2>Nilus local data</h2></div>
        <button className="connector-close" onClick={onClose}><X size={18} /></button>
      </div>
      <p className="connector-copy">
        Manually ingest the saved Bitbucket, Jira and Notion export from <code>{status?.path ?? "nilus_data"}</code>.
        Each provider writes through the same graph pipeline as a live sync.
      </p>
      {error && <div className="connector-error">{error}</div>}
      <div className="connection-list">
        <div className="connection-list-title"><span>Ingestion steps</span><span>{graphName}</span></div>
        {STEPS.map((step) => {
          const stepStatus = status?.steps[step.provider];
          const result = results[step.provider];
          const busy = running === step.provider;
          const busyReset = resetting === step.provider;
          return <article className="connection-card" key={step.provider}>
            <div className="connection-card-top">
              <div className="connection-check"><CheckCircle2 size={15} /></div>
              <div><strong>{step.title}</strong><span>{step.detail}</span></div>
              <div className="connection-card-actions">
                {!!stepStatus?.ingested_records && (
                  <button className="connection-disconnect" title="Remove this local data from the graph"
                    onClick={() => void reset(step)} disabled={busyReset || busy}>
                    {busyReset ? <LoaderCircle className="spin" size={13} /> : <Trash2 size={13} />}
                  </button>
                )}
              </div>
            </div>
            <button className="provider-sync" onClick={() => void run(step)} disabled={busy || stepStatus?.available === false}>
              <RefreshCw className={busy ? "spin" : ""} size={13} />
              {busy ? "Ingesting…" : stepStatus?.ingested_records ? "Re-ingest" : "Ingest"}
            </button>
            {stepStatus?.available_records && (
              <div className="connection-empty">
                {Object.entries(stepStatus.available_records).map(([name, count]) => `${count} ${name.replaceAll("_", " ")}`).join(" · ")}
              </div>
            )}
            {stepStatus?.available === false && <div className="connection-empty">No saved data found for this step.</div>}
            {!!stepStatus?.ingested_records && !result && (
              <div className="sync-result">{stepStatus.ingested_records} records already in this graph.</div>
            )}
            {result && <div className="sync-result">{summarize(result)}</div>}
          </article>;
        })}
      </div>
    </aside>
  </div>;
}
