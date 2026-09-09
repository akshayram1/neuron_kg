import { CheckCircle2, ExternalLink, Github, LoaderCircle, LockKeyhole, RefreshCw, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  deleteGitHubInstallation, getGitHubRepositories, getGitHubStatus,
  getGitHubSyncRun, startGitHubOAuth, startGitHubSync,
} from "../api";
import type { GitHubInstallation, GitHubRepository, GitHubSource, GitHubSyncRun } from "../types";
import SyncProgress from "./SyncProgress";

const POLL_MS = 1500;

interface Props {
  open: boolean;
  onClose: () => void;
  onSourcesChanged: (sources: GitHubSource[]) => void;
  onSyncComplete: () => void;
}

function when(value: string | null) {
  return value ? `Synced ${new Intl.DateTimeFormat("en", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value))}` : "Never synced";
}

export default function GitHubPanel({ open, onClose, onSourcesChanged, onSyncComplete }: Props) {
  const [installations, setInstallations] = useState<GitHubInstallation[]>([]);
  const [sources, setSources] = useState<GitHubSource[]>([]);
  const [repositories, setRepositories] = useState<Record<number, GitHubRepository[]>>({});
  const [selectedRepo, setSelectedRepo] = useState<Record<number, number>>({});
  const [fileTypes, setFileTypes] = useState<Record<number, string[]>>({});
  const [includeCommits, setIncludeCommits] = useState<Record<number, boolean>>({});
  const [runs, setRuns] = useState<Record<number, GitHubSyncRun>>({});
  const [loading, setLoading] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [discovering, setDiscovering] = useState<number | null>(null);
  const [disconnecting, setDisconnecting] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadStatus = useCallback(async () => {
    setLoading(true);
    try {
      const status = await getGitHubStatus();
      setInstallations(status.installations);
      setSources(status.sources);
      onSourcesChanged(status.sources);
      setRuns((current) => {
        const next = { ...current };
        for (const run of status.runs) if (!next[run.installation_id] || run.started_at > next[run.installation_id].started_at) next[run.installation_id] = run;
        return next;
      });
      setFileTypes((current) => {
        const next = { ...current };
        for (const source of status.sources) next[source.installation_id] ??= source.file_types;
        return next;
      });
      setIncludeCommits((current) => {
        const next = { ...current };
        for (const source of status.sources) next[source.installation_id] ??= source.include_commit_messages;
        return next;
      });
      setError(null);
      return status;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load GitHub connections.");
      return null;
    } finally { setLoading(false); }
  }, [onSourcesChanged]);

  useEffect(() => { if (open) void loadStatus(); }, [open, loadStatus]);

  useEffect(() => {
    if (!open) return;
    const active = Object.values(runs).filter((run) => run.status === "queued" || run.status === "running");
    if (!active.length) return;
    const timer = window.setInterval(() => {
      void Promise.all(active.map((run) => getGitHubSyncRun(run.run_id))).then((updates) => {
        setRuns((current) => {
          const next = { ...current };
          for (const run of updates) next[run.installation_id] = run;
          return next;
        });
        if (updates.some((run) => run.status === "completed")) {
          void loadStatus(); onSyncComplete();
        }
        const failed = updates.find((run) => run.status === "failed");
        if (failed) setError(failed.error || "GitHub sync failed.");
      }).catch((reason: Error) => setError(reason.message));
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [open, runs, loadStatus, onSyncComplete]);

  const connect = async () => {
    const popup = window.open("about:blank", "github-oauth", "popup,width=760,height=820");
    if (!popup) return setError("Popup was blocked. Allow popups and try again.");
    setConnecting(true); setError(null);
    try {
      const { installation_url } = await startGitHubOAuth();
      popup.location.assign(installation_url);
      const listener = (event: MessageEvent) => {
        if (event.data?.type === "github-oauth-complete") { window.removeEventListener("message", listener); void loadStatus(); }
      };
      window.addEventListener("message", listener);
    } catch (reason) {
      popup.close(); setError(reason instanceof Error ? reason.message : "Could not start GitHub OAuth.");
    } finally { setConnecting(false); }
  };

  const loadRepos = async (installationId: number) => {
    setDiscovering(installationId); setError(null);
    try {
      const result = await getGitHubRepositories(installationId);
      setRepositories((current) => ({ ...current, [installationId]: result.repositories }));
      if (result.repositories.length === 1) setSelectedRepo((current) => ({ ...current, [installationId]: result.repositories[0].repository_id }));
      setFileTypes((current) => ({ ...current, [installationId]: current[installationId] ?? [".py", ".md"] }));
      setIncludeCommits((current) => ({ ...current, [installationId]: current[installationId] ?? true }));
      if (!result.repositories.length) setError("No repository is available to this installation.");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not load repositories."); }
    finally { setDiscovering(null); }
  };

  const toggleType = (installationId: number, value: string) => setFileTypes((current) => {
    const values = current[installationId] ?? [".py", ".md"];
    return { ...current, [installationId]: values.includes(value) ? values.filter((item) => item !== value) : [...values, value] };
  });

  const sync = async (installationId: number) => {
    const repositoryId = selectedRepo[installationId];
    const selectedTypes = fileTypes[installationId] ?? [".py", ".md"];
    const commits = includeCommits[installationId] ?? true;
    if (!repositoryId) return setError("Select one repository first.");
    if (!selectedTypes.length && !commits) return setError("Select .py, .md, or commit messages.");
    try {
      const started = await startGitHubSync(installationId, repositoryId, selectedTypes, commits);
      setRuns((current) => ({ ...current, [installationId]: {
        run_id: started.run_id, installation_id: installationId, repository_id: repositoryId,
        status: "queued", started_at: new Date().toISOString(), finished_at: null,
        result: { phase: "queued", current: "GitHub sync queued…" }, error: null,
      }}));
      setError(null);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not start GitHub sync."); }
  };

  const disconnect = async (installation: GitHubInstallation) => {
    if (!window.confirm(`Disconnect ${installation.account_login} and remove its synced graph data?`)) return;
    setDisconnecting(installation.installation_id);
    try { await deleteGitHubInstallation(installation.installation_id); await loadStatus(); onSyncComplete(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Could not disconnect GitHub."); }
    finally { setDisconnecting(null); }
  };

  if (!open) return null;
  return <div className="connector-overlay" role="presentation" onMouseDown={onClose}>
    <aside className="connector-drawer" role="dialog" aria-modal="true" aria-label="GitHub connection" onMouseDown={(event) => event.stopPropagation()}>
      <div className="connector-header">
        <div className="github-logo"><Github size={22} /></div>
        <div><span className="eyebrow">Knowledge source</span><h2>Connect GitHub</h2></div>
        <button className="connector-close" onClick={onClose}><X size={18} /></button>
      </div>
      <p className="connector-copy">Choose a repository and ingest Python files, Markdown documents, and/or commit messages.</p>
      {error && <div className="connector-error">{error}</div>}
      <div className="connector-actions oauth-only"><button className="connect-github" onClick={() => void connect()} disabled={connecting}>
        {connecting ? <LoaderCircle className="spin" size={15} /> : <ExternalLink size={15} />} Install or connect GitHub
      </button></div>
      <div className="oauth-note github-note"><LockKeyhole size={12} /> Read-only access to repositories selected in the GitHub App installation.</div>
      <div className="connection-list-title"><span>Connected installations</span><span>{installations.length}</span></div>
      {loading && !installations.length && <div className="connection-empty"><LoaderCircle className="spin" size={16} /> Checking installations…</div>}
      {!loading && !installations.length && <div className="connection-empty">No GitHub installation connected yet.</div>}
      {installations.map((installation) => {
        const id = installation.installation_id;
        const repos = repositories[id] ?? [];
        const run = runs[id];
        const syncing = run?.status === "queued" || run?.status === "running";
        const linked = sources.filter((source) => source.installation_id === id);
        return <article className="connection-card" key={id}>
          <div className="connection-card-top">
            <div className="connection-check"><CheckCircle2 size={15} /></div>
            <div><strong>{installation.account_login}</strong><span>Installation #{id}</span></div>
            <div className="connection-card-actions">
              <button onClick={() => void loadRepos(id)} disabled={discovering === id}>{discovering === id ? <LoaderCircle className="spin" size={13} /> : <Github size={13} />} Repos</button>
              <button className="connection-disconnect" onClick={() => void disconnect(installation)} disabled={disconnecting === id}>{disconnecting === id ? <LoaderCircle className="spin" size={13} /> : <Trash2 size={13} />}</button>
            </div>
          </div>
          {!!repos.length && <div className="github-picker">
            <label><span>Repository</span><select value={selectedRepo[id] ?? ""} onChange={(event) => setSelectedRepo((current) => ({ ...current, [id]: Number(event.target.value) }))}>
              <option value="">Choose one repository…</option>{repos.map((repo) => <option value={repo.repository_id} key={repo.repository_id}>{repo.full_name}{repo.private ? " · private" : ""}</option>)}
            </select></label>
            <fieldset><legend>Content to ingest</legend>
              {[[".py", "Python source files"], [".md", "Markdown documents"]].map(([value, label]) => <label className="github-check" key={value}><input type="checkbox" checked={(fileTypes[id] ?? [".py", ".md"]).includes(value)} onChange={() => toggleType(id, value)} /><span><b>{value}</b> {label}</span></label>)}
              <label className="github-check"><input type="checkbox" checked={includeCommits[id] ?? true} onChange={(event) => setIncludeCommits((current) => ({ ...current, [id]: event.target.checked }))} /><span><b>Commits</b> author, date and message</span></label>
            </fieldset>
            <button className="github-sync" onClick={() => void sync(id)} disabled={syncing || !selectedRepo[id]}><RefreshCw className={syncing ? "spin" : ""} size={13} /> {syncing ? "Syncing repository…" : "Sync selected repository"}</button>
          </div>}
          {linked.map((source) => <div className="github-source" key={source.repository_id}><code>{source.repository_full_name}</code><span>{when(source.last_sync_at)}</span></div>)}
          <SyncProgress progress={run?.result} provider="GitHub" syncing={syncing} />
          {run?.status === "completed" && run.result && <div className="sync-result">{run.result.files_processed ?? 0} files · {run.result.commits_fetched ?? 0} commits · {run.result.records_written ?? 0} written · {run.result.facts_written ?? 0} facts</div>}
        </article>;
      })}
    </aside>
  </div>;
}

