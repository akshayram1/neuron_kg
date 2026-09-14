import { CheckCircle2, ExternalLink, LoaderCircle, RefreshCw, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { deleteNotionConnection, getNotionStatus, getNotionSyncRun, startNotionOAuth, startNotionSync } from "../api";
import type { IngestionTokenUsage, NotionConnection, NotionSyncRun } from "../types";
import { totalIngestionUsage } from "../tokenUsage";
import SyncProgress from "./SyncProgress";

interface Props { open: boolean; graphName: string; onClose: () => void; onConnectionsChanged: (items: NotionConnection[]) => void; onSyncComplete: () => void; onTokenUsage: (usage: IngestionTokenUsage) => void; }
function when(value: string | null) { return value ? `Synced ${new Intl.DateTimeFormat("en", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value))}` : "Never synced"; }

export default function NotionPanel({ open, graphName, onClose, onConnectionsChanged, onSyncComplete, onTokenUsage }: Props) {
  const [connections, setConnections] = useState<NotionConnection[]>([]);
  const [runs, setRuns] = useState<Record<string, NotionSyncRun>>({});
  const [loading, setLoading] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [disconnecting, setDisconnecting] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadStatus = useCallback(async () => {
    setLoading(true);
    try {
      const status = await getNotionStatus(graphName);
      setConnections(status.connections); onConnectionsChanged(status.connections);
      setRuns((current) => {
        const next = { ...current };
        for (const run of status.runs) if (!next[run.workspace_id] || run.started_at > next[run.workspace_id].started_at) next[run.workspace_id] = run;
        return next;
      });
      // Sum every run, not just the latest per workspace -- the topbar
      // counter is a lifetime total, not a "most recent sync" snapshot.
      const usage = totalIngestionUsage("Notion", status.runs ?? []);
      if (usage) onTokenUsage(usage);
      setError(null); return status;
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not load Notion connections."); return null; }
    finally { setLoading(false); }
  }, [graphName, onConnectionsChanged, onTokenUsage]);

  useEffect(() => { if (open) void loadStatus(); }, [open, loadStatus]);
  useEffect(() => {
    if (!open) return;
    const active = Object.values(runs).filter((run) => run.status === "queued" || run.status === "running");
    if (!active.length) return;
    const timer = window.setInterval(() => {
      void Promise.all(active.map((run) => getNotionSyncRun(run.run_id))).then((updates) => {
        setRuns((current) => { const next = { ...current }; for (const run of updates) next[run.workspace_id] = run; return next; });
        if (updates.some((run) => run.status === "completed")) { void loadStatus(); onSyncComplete(); }
        const failed = updates.find((run) => run.status === "failed");
        if (failed) setError(failed.error || "Notion sync failed.");
      }).catch((reason: Error) => setError(reason.message));
    }, 1500);
    return () => window.clearInterval(timer);
  }, [open, runs, loadStatus, onSyncComplete]);

  const connect = async () => {
    const popup = window.open("about:blank", "notion-oauth", "popup,width=620,height=760");
    if (!popup) return setError("Popup was blocked. Allow popups and try again.");
    setConnecting(true); setError(null);
    try {
      const { authorization_url } = await startNotionOAuth(); popup.location.assign(authorization_url);
      const listener = (event: MessageEvent) => { if (event.data?.type === "notion-oauth-complete") { window.removeEventListener("message", listener); void loadStatus(); } };
      window.addEventListener("message", listener);
    } catch (reason) { popup.close(); setError(reason instanceof Error ? reason.message : "Could not start Notion OAuth."); }
    finally { setConnecting(false); }
  };

  const sync = async (workspaceId: string) => {
    try {
      const started = await startNotionSync(workspaceId, graphName);
      setRuns((current) => ({ ...current, [workspaceId]: {
        run_id: started.run_id, workspace_id: workspaceId, status: "queued",
        started_at: new Date().toISOString(), finished_at: null,
        result: { phase: "queued", current: "Notion sync queued…" }, error: null,
      }})); setError(null);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not start Notion sync."); }
  };

  const disconnect = async (connection: NotionConnection) => {
    if (!window.confirm(`Disconnect ${connection.workspace_name} and remove its synced graph data?`)) return;
    setDisconnecting(connection.workspace_id);
    try { await deleteNotionConnection(connection.workspace_id); await loadStatus(); onSyncComplete(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Could not disconnect Notion."); }
    finally { setDisconnecting(null); }
  };

  if (!open) return null;
  return <div className="connector-overlay" role="presentation" onMouseDown={onClose}>
    <aside className="connector-drawer" role="dialog" aria-modal="true" aria-label="Notion connection" onMouseDown={(event) => event.stopPropagation()}>
      <div className="connector-header"><div className="notion-logo">N</div><div><span className="eyebrow">Knowledge source</span><h2>Connect Notion</h2></div><button className="connector-close" onClick={onClose}><X size={18} /></button></div>
      <p className="connector-copy">Select pages in Notion’s secure OAuth window. Their titles, hierarchy and content become one connected workspace graph.</p>
      {error && <div className="connector-error">{error}</div>}
      <div className="connector-actions oauth-only"><button className="connect-notion" onClick={() => void connect()} disabled={connecting}>{connecting ? <LoaderCircle className="spin" size={15} /> : <ExternalLink size={15} />} Continue with Notion</button></div>
      <div className="oauth-note">Only pages shared with this integration can be read.</div>
      <div className="connection-list-title"><span>Connected workspaces</span><span>{connections.length}</span></div>
      {loading && !connections.length && <div className="connection-empty"><LoaderCircle className="spin" size={16} /> Checking workspaces…</div>}
      {!loading && !connections.length && <div className="connection-empty">No Notion workspace connected yet.</div>}
      {connections.map((connection) => {
        const run = runs[connection.workspace_id]; const syncing = run?.status === "queued" || run?.status === "running";
        return <article className="connection-card" key={connection.workspace_id}>
          <div className="connection-card-top"><div className="connection-check"><CheckCircle2 size={15} /></div><div><strong>{connection.workspace_name}</strong><span>{when(connection.last_sync_at)}</span></div><div className="connection-card-actions">
            <button onClick={() => void sync(connection.workspace_id)} disabled={syncing}><RefreshCw className={syncing ? "spin" : ""} size={13} /> {syncing ? "Syncing" : "Sync"}</button>
            <button className="connection-disconnect" onClick={() => void disconnect(connection)} disabled={disconnecting === connection.workspace_id}>{disconnecting === connection.workspace_id ? <LoaderCircle className="spin" size={13} /> : <Trash2 size={13} />}</button>
          </div></div>
          <code>{connection.workspace_id}</code>
          <SyncProgress progress={run?.result} provider="Notion" syncing={syncing} />
          {run?.status === "completed" && run.result && <div className="sync-result">{run.result.pages_fetched ?? 0} pages · {run.result.records_written ?? 0} written · {run.result.facts_written ?? 0} facts</div>}
          {connection.last_sync_error && !syncing && <div className="sync-failed">Last sync: {connection.last_sync_error}</div>}
        </article>;
      })}
    </aside>
  </div>;
}
