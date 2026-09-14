import { CheckCircle2, ExternalLink, ListTodo, LoaderCircle, LockKeyhole, RefreshCw, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { deleteJiraConnection, getJiraProjects, getJiraSites, getJiraStatus, getJiraSyncRun, startJiraOAuth, startJiraSync } from "../api";
import type { JiraProject, JiraSite, JiraSyncRun, OAuthConnectorConnection, OAuthConnectorSource } from "../types";
import type { IngestionTokenUsage } from "../types";
import { totalIngestionUsage } from "../tokenUsage";
import SyncProgress from "./SyncProgress";

const delay = (milliseconds: number) => new Promise((resolve) => window.setTimeout(resolve, milliseconds));

interface Props { open: boolean; graphName: string; onClose: () => void; onSourcesChanged: (sources: OAuthConnectorSource[]) => void; onSyncComplete: () => void; onTokenUsage: (usage: IngestionTokenUsage) => void; }

export default function JiraPanel({ open, graphName, onClose, onSourcesChanged, onSyncComplete, onTokenUsage }: Props) {
  const [connections, setConnections] = useState<OAuthConnectorConnection[]>([]);
  const [sources, setSources] = useState<OAuthConnectorSource[]>([]);
  const [sites, setSites] = useState<Record<string, JiraSite[]>>({});
  const [projects, setProjects] = useState<Record<string, JiraProject[]>>({});
  const [selectedSite, setSelectedSite] = useState<Record<string, string>>({});
  const [selectedProject, setSelectedProject] = useState<Record<string, string>>({});
  const [scopeIssueKey, setScopeIssueKey] = useState<Record<string, string>>({});
  const [runs, setRuns] = useState<Record<string, JiraSyncRun>>({});
  const [loading, setLoading] = useState(false); const [connecting, setConnecting] = useState(false);
  const [discovering, setDiscovering] = useState<string | null>(null); const [error, setError] = useState<string | null>(null);
  const [disconnecting, setDisconnecting] = useState<string | null>(null);
  const mounted = useRef(true);
  const openRef = useRef(open);
  const onSourcesChangedRef = useRef(onSourcesChanged);
  openRef.current = open;
  onSourcesChangedRef.current = onSourcesChanged;
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  const loadStatus = useCallback(async () => {
    setLoading(true); setError(null);
    try { const value = await getJiraStatus(graphName); if (!mounted.current) return value;
      setConnections(value.connections); setSources(value.sources); onSourcesChangedRef.current(value.sources);
      const latestRuns: Record<string, JiraSyncRun> = {};
      for (const run of value.runs ?? []) {
        if (!latestRuns[run.connection_id]) latestRuns[run.connection_id] = run;
      }
      setRuns(latestRuns);
      // Sum every run, not just the latest per connection -- the topbar
      // counter is a lifetime total, not a "most recent sync" snapshot.
      const usage = totalIngestionUsage("Jira", value.runs ?? []);
      if (usage) onTokenUsage(usage);
      return value;
    } catch (reason) { if (mounted.current) setError(reason instanceof Error ? reason.message : "Could not load Jira connections."); return null;
    } finally { if (mounted.current) setLoading(false); }
  }, [graphName]);
  useEffect(() => { if (open) void loadStatus(); }, [open, loadStatus]);
  useEffect(() => { if (!open) setConnecting(false); }, [open]);
  useEffect(() => {
    if (!open) return;
    const active = Object.values(runs).filter((run) => run.status === "queued" || run.status === "running");
    if (!active.length) return;
    const timer = window.setTimeout(() => {
      void Promise.all(active.map(async (knownRun) => {
        try {
          const run = await getJiraSyncRun(knownRun.run_id);
          if (!mounted.current || !openRef.current) return;
          setRuns((current) => ({ ...current, [run.connection_id]: run }));
          if (run.status === "completed") {
            await loadStatus();
            onSyncComplete();
          } else if (run.status === "failed") {
            setError(run.error || "Jira sync failed.");
          }
        } catch (reason) {
          if (mounted.current) setError(reason instanceof Error ? reason.message : "Could not refresh Jira sync progress.");
        }
      }));
    }, 1500);
    return () => window.clearTimeout(timer);
  }, [open, runs, loadStatus, onSyncComplete]);

  const connect = async () => {
    const popup = window.open("about:blank", "jira-oauth", "popup,width=760,height=820");
    if (!popup) { setError("Popup was blocked. Allow popups and try again."); return; }
    setConnecting(true); setError(null);
    try { const before = new Set(connections.map((item) => item.connection_id));
      const { authorization_url } = await startJiraOAuth(); popup.location.assign(authorization_url);
      for (let index = 0; index < 200; index += 1) {
        await delay(1500);
        if (!mounted.current || !openRef.current) { if (!popup.closed) popup.close(); return; }
        const value = await getJiraStatus(graphName);
        if (!mounted.current || !openRef.current) { if (!popup.closed) popup.close(); return; }
        if (value.connections.some((item) => !before.has(item.connection_id))) {
          setConnections(value.connections); setSources(value.sources); onSourcesChangedRef.current(value.sources); if (!popup.closed) popup.close(); return;
        }
        if (popup.closed) { await loadStatus(); return; }
      } setError("Jira authorization timed out.");
    } catch (reason) { if (!popup.closed) popup.close(); setError(reason instanceof Error ? reason.message : "Could not connect Jira.");
    } finally { if (mounted.current) setConnecting(false); }
  };

  const loadSites = async (connectionId: string) => {
    setDiscovering(connectionId); setError(null);
    try { const value = await getJiraSites(connectionId); setSites((current) => ({ ...current, [connectionId]: value.sites }));
      if (value.sites.length === 1) { setSelectedSite((current) => ({ ...current, [connectionId]: value.sites[0].cloud_id })); await loadProjects(connectionId, value.sites[0].cloud_id); }
      if (!value.sites.length) setError("No Jira Cloud site is available for this grant.");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not load Jira sites.");
    } finally { setDiscovering(null); }
  };
  const loadProjects = async (connectionId: string, cloudId: string) => {
    if (!cloudId) return; setDiscovering(connectionId); setError(null);
    try { const value = await getJiraProjects(connectionId, cloudId); setProjects((current) => ({ ...current, [`${connectionId}:${cloudId}`]: value.projects }));
      if (!value.projects.length) setError("No readable Jira project found on this site.");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not load Jira projects.");
    } finally { setDiscovering(null); }
  };
  const sync = async (connectionId: string) => {
    const site = (sites[connectionId] ?? []).find((item) => item.cloud_id === selectedSite[connectionId]);
    const project = (projects[`${connectionId}:${site?.cloud_id}`] ?? []).find((item) => item.project_id === selectedProject[connectionId]);
    if (!site || !project) { setError("Select one Jira site and project first."); return; }
    try { const started = await startJiraSync(connectionId, site, project, (scopeIssueKey[connectionId] ?? "").trim(), graphName);
      setRuns((current) => ({ ...current, [connectionId]: {
        run_id: started.run_id, connection_id: connectionId,
        source_id: `${site.cloud_id}:${project.project_id}`, status: "queued",
        started_at: new Date().toISOString(), finished_at: null, error: null,
        result: { phase: "queued", current: "Starting Jira sync…", records_done: 0, records_total: 0 },
      } }));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not sync Jira project."); }
  };
  const disconnect = async (connectionId: string, accountName: string) => {
    if (!window.confirm(`Disconnect ${accountName}? This deletes every Jira graph and bridge link synced under this account.`)) return;
    setDisconnecting(connectionId); setError(null);
    try { await deleteJiraConnection(connectionId); await loadStatus(); onSyncComplete(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Could not disconnect Jira account."); }
    finally { if (mounted.current) setDisconnecting(null); }
  };
  if (!open) return null;
  return <div className="connector-overlay" role="presentation" onMouseDown={onClose}><aside className="connector-drawer" role="dialog" aria-modal="true" aria-label="Jira connection" onMouseDown={(event) => event.stopPropagation()}>
    <div className="connector-header"><div className="jira-logo"><ListTodo size={22} /></div><div><span className="eyebrow">Knowledge source</span><h2>Connect Jira</h2></div><button className="connector-close" onClick={onClose}><X size={18} /></button></div>
    <p className="connector-copy">Authorize Jira Cloud, choose one site and project, then ingest its issues, descriptions, assignees, reporters and comments.</p>
    {error && <div className="connector-error">{error}</div>}
    <div className="connector-actions oauth-only"><button className="connect-jira" onClick={() => void connect()} disabled={connecting}>{connecting ? <LoaderCircle className="spin" size={15} /> : <ExternalLink size={15} />}{connecting ? "Waiting for Atlassian…" : "Continue with Atlassian"}</button></div>
    <div className="oauth-note github-note"><LockKeyhole size={12} /> Read-only delegated access. Jira still applies the signed-in user’s project permissions.</div>
    <div className="connection-list"><div className="connection-list-title"><span>Connected accounts</span><span>{connections.length}</span></div>
      {loading && !connections.length && <div className="connection-empty"><LoaderCircle className="spin" size={16} /> Checking Jira…</div>}
      {!loading && !connections.length && <div className="connection-empty">No Jira account connected yet.</div>}
      {connections.map((connection) => { const connectionSites = sites[connection.connection_id] ?? []; const cloudId = selectedSite[connection.connection_id] ?? ""; const projectList = projects[`${connection.connection_id}:${cloudId}`] ?? []; const projectId = selectedProject[connection.connection_id] ?? ""; const run = runs[connection.connection_id]; const syncing = run?.status === "queued" || run?.status === "running";
        return <article className="connection-card" key={connection.connection_id}><div className="connection-card-top"><div className="connection-check"><CheckCircle2 size={15} /></div><div><strong>{connection.account_name}</strong><span>Atlassian OAuth connection</span></div><div className="connection-card-actions">{!connectionSites.length && <button onClick={() => void loadSites(connection.connection_id)} disabled={discovering === connection.connection_id}>{discovering === connection.connection_id ? <LoaderCircle className="spin" size={13} /> : <ListTodo size={13} />} Load sites</button>}<button className="connection-disconnect" title="Disconnect and delete all data synced under this account" onClick={() => void disconnect(connection.connection_id, connection.account_name)} disabled={disconnecting === connection.connection_id}>{disconnecting === connection.connection_id ? <LoaderCircle className="spin" size={13} /> : <Trash2 size={13} />}</button></div></div>
          {connectionSites.length > 0 && <div className="provider-picker"><label><span>Jira site</span><select value={cloudId} onChange={(event) => { const value = event.target.value; setSelectedSite((current) => ({ ...current, [connection.connection_id]: value })); setSelectedProject((current) => ({ ...current, [connection.connection_id]: "" })); void loadProjects(connection.connection_id, value); }}><option value="">Choose a site…</option>{connectionSites.map((site) => <option value={site.cloud_id} key={site.cloud_id}>{site.name}</option>)}</select></label>
            <label><span>Project</span><select value={projectId} disabled={!cloudId} onChange={(event) => setSelectedProject((current) => ({ ...current, [connection.connection_id]: event.target.value }))}><option value="">Choose a project…</option>{projectList.map((item) => <option value={item.project_id} key={item.project_id}>{item.key} · {item.name}</option>)}</select></label>
            <label><span>Scope to issue subtree (optional)</span><input type="text" placeholder="e.g. DATAOS-3833 — leave empty for whole project" value={scopeIssueKey[connection.connection_id] ?? ""} onChange={(event) => setScopeIssueKey((current) => ({ ...current, [connection.connection_id]: event.target.value }))} /></label>
            <button className="provider-sync" onClick={() => void sync(connection.connection_id)} disabled={syncing || !projectId}><RefreshCw className={syncing ? "spin" : ""} size={13} />{syncing ? (run.result?.records_total ? `Syncing ${run.result.records_done ?? 0}/${run.result.records_total}` : "Fetching issues…") : "Sync selected project"}</button></div>}
          {sources.filter((item) => item.connection_id === connection.connection_id).map((source) => <div className="github-source" key={source.source_id}><code>{source.group_id}</code><span>{source.source_name}</span></div>)}
          <SyncProgress progress={run?.result} provider="Jira" syncing={syncing} />
          {run?.status === "completed" && run.result && <div className="sync-result">{run.result.records_done ?? run.result.issues_fetched} / {run.result.records_total ?? run.result.issues_fetched} records · {run.result.issues_fetched} issues · {run.result.records_written ?? 0} written · {run.result.records_kept ?? 0} already synced · {run.result.chunks_ingested} chunks · {run.result.facts_written} facts</div>}
        </article>; })}
    </div></aside></div>;
}
