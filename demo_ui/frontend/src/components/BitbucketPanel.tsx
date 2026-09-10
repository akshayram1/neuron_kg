import { CheckCircle2, ExternalLink, GitFork, LoaderCircle, LockKeyhole, RefreshCw, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  deleteBitbucketConnection, getBitbucketBranches, getBitbucketRepositories, getBitbucketStatus,
  getBitbucketSyncRun, getBitbucketWorkspace, startBitbucketOAuth, startBitbucketSync,
} from "../api";
import type { BitbucketRepository, BitbucketSyncRun, BitbucketWorkspace, IngestionTokenUsage, OAuthConnectorConnection, OAuthConnectorSource } from "../types";
import { totalIngestionUsage } from "../tokenUsage";
import SyncProgress from "./SyncProgress";

const delay = (milliseconds: number) => new Promise((resolve) => window.setTimeout(resolve, milliseconds));
const DEFAULT_TYPES = [".py", ".md"];

interface Props { open: boolean; onClose: () => void; onSourcesChanged: (sources: OAuthConnectorSource[]) => void; onSyncComplete: () => void; onTokenUsage: (usage: IngestionTokenUsage) => void; }

export default function BitbucketPanel({ open, onClose, onSourcesChanged, onSyncComplete, onTokenUsage }: Props) {
  const [connections, setConnections] = useState<OAuthConnectorConnection[]>([]);
  const [sources, setSources] = useState<OAuthConnectorSource[]>([]);
  const [workspaces, setWorkspaces] = useState<Record<string, BitbucketWorkspace>>({});
  const [repositories, setRepositories] = useState<Record<string, BitbucketRepository[]>>({});
  const [workspaceInput, setWorkspaceInput] = useState<Record<string, string>>({});
  const [selectedWorkspace, setSelectedWorkspace] = useState<Record<string, string>>({});
  const [selectedRepo, setSelectedRepo] = useState<Record<string, string>>({});
  const [branches, setBranches] = useState<Record<string, string[]>>({});
  const [selectedBranch, setSelectedBranch] = useState<Record<string, string>>({});
  const [fileTypes, setFileTypes] = useState<Record<string, string[]>>({});
  const [includeCommits, setIncludeCommits] = useState<Record<string, boolean>>({});
  const [includePullRequests, setIncludePullRequests] = useState<Record<string, boolean>>({});
  const [runs, setRuns] = useState<Record<string, BitbucketSyncRun>>({});
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
    try { const value = await getBitbucketStatus(); if (!mounted.current) return value;
      setConnections(value.connections); setSources(value.sources); onSourcesChangedRef.current(value.sources);
      const latestRuns: Record<string, BitbucketSyncRun> = {};
      for (const run of value.runs ?? []) {
        if (!latestRuns[run.connection_id]) latestRuns[run.connection_id] = run;
      }
      setRuns(latestRuns);
      // Sum every run, not just the latest per connection -- the topbar
      // counter is a lifetime total, not a "most recent sync" snapshot.
      const usage = totalIngestionUsage("Bitbucket", value.runs ?? []);
      if (usage) onTokenUsage(usage);
      return value;
    } catch (reason) { if (mounted.current) setError(reason instanceof Error ? reason.message : "Could not load Bitbucket connections."); return null;
    } finally { if (mounted.current) setLoading(false); }
  }, []);
  useEffect(() => { if (open) void loadStatus(); }, [open, loadStatus]);
  useEffect(() => { if (!open) setConnecting(false); }, [open]);
  useEffect(() => {
    if (!open) return;
    const active = Object.values(runs).filter((run) => run.status === "queued" || run.status === "running");
    if (!active.length) return;
    const timer = window.setTimeout(() => {
      void Promise.all(active.map(async (knownRun) => {
        try {
          const run = await getBitbucketSyncRun(knownRun.run_id);
          if (!mounted.current || !openRef.current) return;
          setRuns((current) => ({ ...current, [run.connection_id]: run }));
          if (run.status === "completed") {
            await loadStatus();
            onSyncComplete();
          } else if (run.status === "failed") {
            setError(run.error || "Bitbucket sync failed.");
          }
        } catch (reason) {
          if (mounted.current) setError(reason instanceof Error ? reason.message : "Could not refresh Bitbucket sync progress.");
        }
      }));
    }, 1500);
    return () => window.clearTimeout(timer);
  }, [open, runs, loadStatus, onSyncComplete]);

  const connect = async () => {
    const popup = window.open("about:blank", "bitbucket-oauth", "popup,width=760,height=820");
    if (!popup) { setError("Popup was blocked. Allow popups and try again."); return; }
    setConnecting(true); setError(null);
    try { const before = new Set(connections.map((item) => item.connection_id));
      const { authorization_url } = await startBitbucketOAuth(); popup.location.assign(authorization_url);
      for (let index = 0; index < 200; index += 1) {
        await delay(1500);
        if (!mounted.current || !openRef.current) { if (!popup.closed) popup.close(); return; }
        const value = await getBitbucketStatus();
        if (!mounted.current || !openRef.current) { if (!popup.closed) popup.close(); return; }
        if (value.connections.some((item) => !before.has(item.connection_id))) {
          setConnections(value.connections); setSources(value.sources); onSourcesChangedRef.current(value.sources); if (!popup.closed) popup.close(); return;
        }
        if (popup.closed) { await loadStatus(); return; }
      } setError("Bitbucket authorization timed out.");
    } catch (reason) { if (!popup.closed) popup.close(); setError(reason instanceof Error ? reason.message : "Could not connect Bitbucket.");
    } finally { if (mounted.current) setConnecting(false); }
  };

  const openWorkspace = async (connectionId: string) => {
    const slug = (workspaceInput[connectionId] ?? "").trim();
    if (!slug) { setError("Type your Bitbucket workspace slug first (e.g. the part after bitbucket.org/ in the URL)."); return; }
    setDiscovering(connectionId); setError(null);
    try {
      const value = await getBitbucketWorkspace(connectionId, slug);
      setWorkspaces((current) => ({ ...current, [connectionId]: value.workspace }));
      setSelectedWorkspace((current) => ({ ...current, [connectionId]: value.workspace.slug }));
      await loadRepositories(connectionId, value.workspace.slug);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not open that Bitbucket workspace.");
    } finally { setDiscovering(null); }
  };
  const loadRepositories = async (connectionId: string, workspace: string) => {
    if (!workspace) return; setDiscovering(connectionId); setError(null);
    try { const value = await getBitbucketRepositories(connectionId, workspace); setRepositories((current) => ({ ...current, [`${connectionId}:${workspace}`]: value.repositories }));
      setFileTypes((current) => ({ ...current, [connectionId]: current[connectionId] ?? DEFAULT_TYPES }));
      setIncludeCommits((current) => ({ ...current, [connectionId]: current[connectionId] ?? true }));
      if (!value.repositories.length) setError("No readable Bitbucket repository found in this workspace.");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not load Bitbucket repositories.");
    } finally { setDiscovering(null); }
  };
  const loadBranches = async (connectionId: string, workspace: string, repositoryUuid: string) => {
    if (!repositoryUuid) return;
    setDiscovering(connectionId); setError(null);
    try {
      const value = await getBitbucketBranches(connectionId, workspace, repositoryUuid);
      setBranches((current) => ({ ...current, [`${connectionId}:${repositoryUuid}`]: value.branches }));
      setSelectedBranch((current) => ({ ...current, [connectionId]: value.default_branch }));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not load branches.");
    } finally { setDiscovering(null); }
  };
  const toggleType = (connectionId: string, value: string) => setFileTypes((current) => {
    const values = current[connectionId] ?? DEFAULT_TYPES;
    return { ...current, [connectionId]: values.includes(value) ? values.filter((item) => item !== value) : [...values, value] };
  });
  const sync = async (connectionId: string) => {
    const workspace = selectedWorkspace[connectionId];
    const repository = (repositories[`${connectionId}:${workspace}`] ?? []).find((item) => item.uuid === selectedRepo[connectionId]);
    const selectedTypes = fileTypes[connectionId] ?? DEFAULT_TYPES;
    const commits = includeCommits[connectionId] ?? true;
    const prs = includePullRequests[connectionId] ?? true;
    const branch = selectedBranch[connectionId] ?? repository?.main_branch ?? "";
    if (!workspace || !repository) { setError("Select one Bitbucket workspace and repository first."); return; }
    if (!selectedTypes.length && !commits && !prs) { setError("Select .py, .md, commit messages, or pull requests."); return; }
    try { const started = await startBitbucketSync(connectionId, workspace, repository.uuid, branch, selectedTypes, commits, prs);
      setRuns((current) => ({ ...current, [connectionId]: {
        run_id: started.run_id, connection_id: connectionId,
        source_id: `${workspace}:${repository.slug}`, status: "queued",
        started_at: new Date().toISOString(), finished_at: null, error: null,
        result: { phase: "queued", current: "Starting Bitbucket sync…", records_done: 0, records_total: 0 },
      } }));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not sync Bitbucket repository."); }
  };
  const disconnect = async (connectionId: string, accountName: string) => {
    if (!window.confirm(`Disconnect ${accountName}? This deletes every Bitbucket graph and bridge link synced under this account.`)) return;
    setDisconnecting(connectionId); setError(null);
    try { await deleteBitbucketConnection(connectionId); await loadStatus(); onSyncComplete(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Could not disconnect Bitbucket account."); }
    finally { if (mounted.current) setDisconnecting(null); }
  };
  if (!open) return null;
  return <div className="connector-overlay" role="presentation" onMouseDown={onClose}><aside className="connector-drawer" role="dialog" aria-modal="true" aria-label="Bitbucket connection" onMouseDown={(event) => event.stopPropagation()}>
    <div className="connector-header"><div className="github-logo"><GitFork size={22} /></div><div><span className="eyebrow">Knowledge source</span><h2>Connect Bitbucket</h2></div><button className="connector-close" onClick={onClose}><X size={18} /></button></div>
    <p className="connector-copy">Authorize Bitbucket Cloud, choose one workspace and repository, then ingest its Python files, Markdown documents and/or commit messages.</p>
    {error && <div className="connector-error">{error}</div>}
    <div className="connector-actions oauth-only"><button className="connect-jira" onClick={() => void connect()} disabled={connecting}>{connecting ? <LoaderCircle className="spin" size={15} /> : <ExternalLink size={15} />}{connecting ? "Waiting for Bitbucket…" : "Continue with Bitbucket"}</button></div>
    <div className="oauth-note github-note"><LockKeyhole size={12} /> Read-only delegated access. Bitbucket still applies the signed-in user's workspace permissions.</div>
    <div className="connection-list"><div className="connection-list-title"><span>Connected accounts</span><span>{connections.length}</span></div>
      {loading && !connections.length && <div className="connection-empty"><LoaderCircle className="spin" size={16} /> Checking Bitbucket…</div>}
      {!loading && !connections.length && <div className="connection-empty">No Bitbucket account connected yet.</div>}
      {connections.map((connection) => { const found = workspaces[connection.connection_id]; const workspace = selectedWorkspace[connection.connection_id] ?? ""; const repoList = repositories[`${connection.connection_id}:${workspace}`] ?? []; const repositoryUuid = selectedRepo[connection.connection_id] ?? ""; const branchList = branches[`${connection.connection_id}:${repositoryUuid}`] ?? []; const run = runs[connection.connection_id]; const syncing = run?.status === "queued" || run?.status === "running";
        return <article className="connection-card" key={connection.connection_id}><div className="connection-card-top"><div className="connection-check"><CheckCircle2 size={15} /></div><div><strong>{connection.account_name}</strong><span>Bitbucket OAuth connection</span></div><div className="connection-card-actions"><button className="connection-disconnect" title="Disconnect and delete all data synced under this account" onClick={() => void disconnect(connection.connection_id, connection.account_name)} disabled={disconnecting === connection.connection_id}>{disconnecting === connection.connection_id ? <LoaderCircle className="spin" size={13} /> : <Trash2 size={13} />}</button></div></div>
          <div className="provider-picker">
            <label><span>Workspace slug</span>
              <input type="text" value={workspaceInput[connection.connection_id] ?? ""} placeholder="e.g. rubik_"
                onChange={(event) => setWorkspaceInput((current) => ({ ...current, [connection.connection_id]: event.target.value }))}
                onKeyDown={(event) => { if (event.key === "Enter") void openWorkspace(connection.connection_id); }} />
            </label>
            <button className="provider-sync" onClick={() => void openWorkspace(connection.connection_id)} disabled={discovering === connection.connection_id}>{discovering === connection.connection_id ? <LoaderCircle className="spin" size={13} /> : <GitFork size={13} />} Load repositories</button>
            {found && <div className="github-source"><code>{found.slug}</code><span>{found.name}</span></div>}
            <label><span>Repository</span><select value={repositoryUuid} disabled={!workspace} onChange={(event) => { const value = event.target.value; setSelectedRepo((current) => ({ ...current, [connection.connection_id]: value })); setSelectedBranch((current) => ({ ...current, [connection.connection_id]: "" })); void loadBranches(connection.connection_id, workspace, value); }}><option value="">Choose a repository…</option>{repoList.map((repo) => <option value={repo.uuid} key={repo.uuid}>{repo.full_name}{repo.private ? " · private" : ""}</option>)}</select></label>
            <label><span>Branch</span><select value={selectedBranch[connection.connection_id] ?? ""} disabled={!repositoryUuid} onChange={(event) => setSelectedBranch((current) => ({ ...current, [connection.connection_id]: event.target.value }))}>{!branchList.length && <option value="">{discovering === connection.connection_id ? "Loading branches…" : "Choose a repository first…"}</option>}{branchList.map((name, index) => <option value={name} key={name}>{name}{index === 0 ? " · default" : ""}</option>)}</select></label>
            <fieldset><legend>Content to ingest</legend>
              {[[".py", "Python source files"], [".md", "Markdown documents"]].map(([value, label]) => <label className="github-check" key={value}><input type="checkbox" checked={(fileTypes[connection.connection_id] ?? DEFAULT_TYPES).includes(value)} onChange={() => toggleType(connection.connection_id, value)} /><span><b>{value}</b> {label}</span></label>)}
              <label className="github-check"><input type="checkbox" checked={includeCommits[connection.connection_id] ?? true} onChange={(event) => setIncludeCommits((current) => ({ ...current, [connection.connection_id]: event.target.checked }))} /><span><b>Commits</b> author, date and message</span></label>
              <label className="github-check"><input type="checkbox" checked={includePullRequests[connection.connection_id] ?? true} onChange={(event) => setIncludePullRequests((current) => ({ ...current, [connection.connection_id]: event.target.checked }))} /><span><b>Pull requests</b> title and description</span></label>
            </fieldset>
            <button className="provider-sync" onClick={() => void sync(connection.connection_id)} disabled={syncing || !repositoryUuid}><RefreshCw className={syncing ? "spin" : ""} size={13} />{syncing ? "Syncing repository…" : "Sync selected repository"}</button></div>
          {sources.filter((item) => item.connection_id === connection.connection_id).map((source) => <div className="github-source" key={source.source_id}><code>{source.source_name}</code><span>{source.last_sync_at ? "Synced" : "Never synced"}</span></div>)}
          <SyncProgress progress={run?.result} provider="Bitbucket" syncing={syncing} />
          {run?.status === "completed" && run.result && <div className="sync-result">{run.result.files_processed ?? 0} files · {run.result.commits_fetched ?? 0} commits · {run.result.pull_requests_fetched ?? 0} PRs · {run.result.records_written ?? 0} written · {run.result.records_kept ?? 0} already synced · {run.result.chunks_ingested} chunks · {run.result.facts_written} facts</div>}
        </article>; })}
    </div></aside></div>;
}
