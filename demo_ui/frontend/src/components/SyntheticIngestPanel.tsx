import { CheckCircle2, FlaskConical, LoaderCircle, RefreshCw, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  getSyntheticStatus, ingestSyntheticBitbucket, ingestSyntheticJira, ingestSyntheticNotion, resetSynthetic,
} from "../api";
import type { IngestionTokenUsage, SyntheticIngestResult, SyntheticProvider, SyntheticStatus } from "../types";

interface Props {
  open: boolean;
  graphName: string;
  onClose: () => void;
  onSyncComplete: () => void;
  onTokenUsage: (usage: IngestionTokenUsage) => void;
}

interface Step {
  provider: SyntheticProvider;
  title: string;
  detail: string;
  ingest: (graphName: string) => Promise<SyntheticIngestResult>;
}

const STEPS: Step[] = [
  {
    provider: "bitbucket", title: "1. Bitbucket",
    detail: "Pull requests and their commits from a captured Bitbucket Cloud API response.",
    ingest: ingestSyntheticBitbucket,
  },
  {
    provider: "jira", title: "2. Jira",
    detail: "Issues, assignees, reporters and comments from a captured Jira issue search.",
    ingest: ingestSyntheticJira,
  },
  {
    provider: "notion", title: "3. Notion",
    detail: "Pages and their rendered content, plus LLM fact extraction.",
    ingest: ingestSyntheticNotion,
  },
];

function summarize(result: SyntheticIngestResult): string {
  switch (result.provider) {
    case "jira":
      return `${result.issues_fetched ?? 0} issues · ${result.records_written} written · ${result.records_kept} already synced`;
    case "bitbucket":
      return `${result.pull_requests_fetched ?? 0} PRs · ${result.commits_written ?? 0} commits · ${result.records_written} written · ${result.records_kept} already synced`;
    case "notion":
      return `${result.pages_fetched ?? 0} pages · ${result.chunks_ingested ?? 0} chunks · ${result.entities_written ?? 0} entities · ${result.facts_written ?? 0} facts`;
  }
}

export default function SyntheticIngestPanel({ open, graphName, onClose, onSyncComplete, onTokenUsage }: Props) {
  const [status, setStatus] = useState<SyntheticStatus | null>(null);
  const [results, setResults] = useState<Partial<Record<SyntheticProvider, SyntheticIngestResult>>>({});
  const [running, setRunning] = useState<SyntheticProvider | null>(null);
  const [resetting, setResetting] = useState<SyntheticProvider | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setStatus(await getSyntheticStatus(graphName));
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load synthetic ingestion status.");
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
          provider: "Notion (synthetic)", active: false, startedAt: new Date().toISOString(),
          input: result.ingestion_input_tokens ?? 0, output: result.ingestion_output_tokens ?? 0,
          total: result.ingestion_total_tokens,
        });
      }
      await load();
      onSyncComplete();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : `Could not ingest synthetic ${step.title}.`);
    } finally {
      setRunning(null);
    }
  };

  const reset = async (step: Step) => {
    setResetting(step.provider); setError(null);
    try {
      await resetSynthetic(step.provider, graphName);
      setResults((current) => { const next = { ...current }; delete next[step.provider]; return next; });
      await load();
      onSyncComplete();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : `Could not reset synthetic ${step.title}.`);
    } finally {
      setResetting(null);
    }
  };

  if (!open) return null;
  return <div className="connector-overlay" role="presentation" onMouseDown={onClose}>
    <aside className="connector-drawer" role="dialog" aria-modal="true" aria-label="Synthetic data ingestion" onMouseDown={(event) => event.stopPropagation()}>
      <div className="connector-header">
        <div className="jira-logo"><FlaskConical size={22} /></div>
        <div><span className="eyebrow">Knowledge source</span><h2>Synthetic data</h2></div>
        <button className="connector-close" onClick={onClose}><X size={18} /></button>
      </div>
      <p className="connector-copy">
        Ingest captured Bitbucket, Jira and Notion API responses — no OAuth connection needed.
        Each step writes into the same unified graph a real sync would.
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
                  <button className="connection-disconnect" title="Remove this synthetic data from the graph"
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
            {stepStatus?.available === false && <div className="connection-empty">No fixture found for this step.</div>}
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
