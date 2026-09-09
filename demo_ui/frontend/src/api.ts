import type {
  AppConfig,
  ChatResponse,
  DeleteConnectionResult,
  GraphPayload,
  GitHubRepository,
  GitHubStatus,
  GitHubSyncRun,
  JiraProject,
  JiraSite,
  JiraStatus,
  JiraSyncRun,
  NotionStatus,
  NotionSyncRun,
  SourceRecord,
} from "./types";

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options);
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(payload?.detail ?? `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export function getConfig(): Promise<AppConfig> {
  return request<AppConfig>("/api/config");
}

export function getGraph(providers: string[]): Promise<GraphPayload> {
  const query = new URLSearchParams();
  providers.forEach((provider) => query.append("providers", provider));
  return request<GraphPayload>(`/api/graph?${query.toString()}`);
}

export function sendChat(message: string, providers: string[]): Promise<ChatResponse> {
  return request<ChatResponse>("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, providers }),
  });
}

export function getSources(): Promise<{ sources: SourceRecord[] }> {
  return request(`/api/sources`);
}

export function getSkosExportUrl(providers: string[]): string {
  const query = new URLSearchParams();
  providers.forEach((provider) => query.append("providers", provider));
  return `/api/export/skos?${query.toString()}`;
}

export function getJiraStatus(): Promise<JiraStatus> {
  return request<JiraStatus>("/api/connectors/jira/status");
}
export function startJiraOAuth(): Promise<{ authorization_url: string }> {
  return request<{ authorization_url: string }>("/api/connectors/jira/oauth/start", { method: "POST" });
}
export function getJiraSites(connectionId: string): Promise<{ sites: JiraSite[] }> {
  return request(`/api/connectors/jira/sites?${new URLSearchParams({ connection_id: connectionId })}`);
}
export function getJiraProjects(connectionId: string, cloudId: string): Promise<{ projects: JiraProject[] }> {
  return request(`/api/connectors/jira/projects?${new URLSearchParams({ connection_id: connectionId, cloud_id: cloudId })}`);
}
export function startJiraSync(connectionId: string, site: JiraSite, project: JiraProject): Promise<{ run_id: string; status: string }> {
  return request("/api/connectors/jira/sync", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ connection_id: connectionId, cloud_id: site.cloud_id,
      site_name: site.name, site_url: site.url, project_id: project.project_id,
      project_key: project.key, project_name: project.name }),
  });
}
export function getJiraSyncRun(runId: string): Promise<JiraSyncRun> {
  return request(`/api/connectors/jira/sync/${encodeURIComponent(runId)}`);
}
export function deleteJiraConnection(connectionId: string): Promise<DeleteConnectionResult> {
  return request<DeleteConnectionResult>(
    `/api/connectors/jira/connections/${encodeURIComponent(connectionId)}`,
    { method: "DELETE" },
  );
}

export function getGitHubStatus(): Promise<GitHubStatus> {
  return request<GitHubStatus>("/api/connectors/github/status");
}
export function startGitHubOAuth(): Promise<{ installation_url: string }> {
  return request("/api/connectors/github/oauth/start", { method: "POST" });
}
export function getGitHubRepositories(installationId: number): Promise<{ repositories: GitHubRepository[] }> {
  return request(`/api/connectors/github/repositories?${new URLSearchParams({ installation_id: String(installationId) })}`);
}
export function startGitHubSync(
  installationId: number, repositoryId: number, fileTypes: string[], includeCommitMessages: boolean,
): Promise<{ run_id: string; status: string }> {
  return request("/api/connectors/github/sync", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ installation_id: installationId, repository_id: repositoryId,
      file_types: fileTypes, include_commit_messages: includeCommitMessages }),
  });
}
export function getGitHubSyncRun(runId: string): Promise<GitHubSyncRun> {
  return request(`/api/connectors/github/sync/${encodeURIComponent(runId)}`);
}
export function deleteGitHubInstallation(installationId: number): Promise<DeleteConnectionResult> {
  return request(`/api/connectors/github/installations/${installationId}`, { method: "DELETE" });
}

export function getNotionStatus(): Promise<NotionStatus> {
  return request<NotionStatus>("/api/connectors/notion/status");
}
export function startNotionOAuth(): Promise<{ authorization_url: string }> {
  return request("/api/connectors/notion/oauth/start", { method: "POST" });
}
export function startNotionSync(workspaceId: string): Promise<{ run_id: string; status: string }> {
  return request("/api/connectors/notion/sync", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ workspace_id: workspaceId }),
  });
}
export function getNotionSyncRun(runId: string): Promise<NotionSyncRun> {
  return request(`/api/connectors/notion/sync/${encodeURIComponent(runId)}`);
}
export function deleteNotionConnection(workspaceId: string): Promise<DeleteConnectionResult> {
  return request(`/api/connectors/notion/connections/${encodeURIComponent(workspaceId)}`, { method: "DELETE" });
}
