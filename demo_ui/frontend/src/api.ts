import type {
  AppConfig,
  BitbucketRepository,
  BitbucketStatus,
  BitbucketSyncRun,
  BitbucketWorkspace,
  ChatResponse,
  DeleteConnectionResult,
  EntityDetail,
  GraphInfo,
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
  OntologyAdoptResult,
  OntologyAdoption,
  OntologyPending,
  OntologyUnadoptResult,
  SourceRecord,
  StoryRun,
  StoryState,
  StoryWisdomProposal,
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

export function getGraphs(): Promise<{ graphs: GraphInfo[] }> {
  return request("/api/graphs");
}

export function createGraph(name: string, displayName?: string): Promise<{ name: string }> {
  return request("/api/graphs", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, display_name: displayName }),
  });
}

export function getGraph(providers: string[], graphName: string): Promise<GraphPayload> {
  const query = new URLSearchParams({ graph_name: graphName });
  providers.forEach((provider) => query.append("providers", provider));
  return request<GraphPayload>(`/api/graph?${query.toString()}`);
}

export function sendChat(message: string, providers: string[], graphName: string): Promise<ChatResponse> {
  return request<ChatResponse>("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, providers, graph_name: graphName }),
  });
}

export function getEntity(uid: string, providers: string[], graphName: string): Promise<EntityDetail> {
  const query = new URLSearchParams({ graph_name: graphName });
  providers.forEach((provider) => query.append("providers", provider));
  return request<EntityDetail>(`/api/entities/${encodeURIComponent(uid)}?${query.toString()}`);
}

export function getSources(graphName: string): Promise<{ sources: SourceRecord[] }> {
  return request(`/api/sources?${new URLSearchParams({ graph_name: graphName })}`);
}

export function clearGraph(graphName: string): Promise<{ cleared: boolean; nodes_removed: number }> {
  return request(`/api/admin/clear-graph?${new URLSearchParams({ graph_name: graphName })}`, { method: "POST" });
}

export function getSkosExportUrl(providers: string[], graphName: string): string {
  const query = new URLSearchParams({ graph_name: graphName });
  providers.forEach((provider) => query.append("providers", provider));
  return `/api/export/skos?${query.toString()}`;
}

export function getJiraStatus(graphName: string): Promise<JiraStatus> {
  return request<JiraStatus>(`/api/connectors/jira/status?${new URLSearchParams({ graph_name: graphName })}`);
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
export function startJiraSync(
  connectionId: string, site: JiraSite, project: JiraProject, scopeIssueKey = "", graphName: string,
): Promise<{ run_id: string; status: string }> {
  return request("/api/connectors/jira/sync", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ connection_id: connectionId, cloud_id: site.cloud_id,
      site_name: site.name, site_url: site.url, project_id: project.project_id,
      project_key: project.key, project_name: project.name, scope_issue_key: scopeIssueKey,
      graph_name: graphName }),
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

export function getGitHubStatus(graphName: string): Promise<GitHubStatus> {
  return request<GitHubStatus>(`/api/connectors/github/status?${new URLSearchParams({ graph_name: graphName })}`);
}
export function startGitHubOAuth(): Promise<{ installation_url: string }> {
  return request("/api/connectors/github/oauth/start", { method: "POST" });
}
export function getGitHubRepositories(installationId: number): Promise<{ repositories: GitHubRepository[] }> {
  return request(`/api/connectors/github/repositories?${new URLSearchParams({ installation_id: String(installationId) })}`);
}
export function startGitHubSync(
  installationId: number, repositoryId: number, fileTypes: string[], includeCommitMessages: boolean,
  graphName: string,
): Promise<{ run_id: string; status: string }> {
  return request("/api/connectors/github/sync", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ installation_id: installationId, repository_id: repositoryId,
      file_types: fileTypes, include_commit_messages: includeCommitMessages, graph_name: graphName }),
  });
}
export function getGitHubSyncRun(runId: string): Promise<GitHubSyncRun> {
  return request(`/api/connectors/github/sync/${encodeURIComponent(runId)}`);
}
export function deleteGitHubInstallation(installationId: number): Promise<DeleteConnectionResult> {
  return request(`/api/connectors/github/installations/${installationId}`, { method: "DELETE" });
}

export function getBitbucketStatus(graphName: string): Promise<BitbucketStatus> {
  return request<BitbucketStatus>(`/api/connectors/bitbucket/status?${new URLSearchParams({ graph_name: graphName })}`);
}
export function startBitbucketOAuth(): Promise<{ authorization_url: string }> {
  return request<{ authorization_url: string }>("/api/connectors/bitbucket/oauth/start", { method: "POST" });
}
// Bitbucket removed workspace enumeration from its API, so the slug is typed
// by the user and resolved to a real name here (see bitbucket_routes.py).
export function getBitbucketWorkspace(connectionId: string, workspace: string): Promise<{ workspace: BitbucketWorkspace }> {
  return request(`/api/connectors/bitbucket/workspace?${new URLSearchParams({ connection_id: connectionId, workspace })}`);
}
export function getBitbucketRepositories(connectionId: string, workspace: string): Promise<{ repositories: BitbucketRepository[] }> {
  return request(`/api/connectors/bitbucket/repositories?${new URLSearchParams({ connection_id: connectionId, workspace })}`);
}
export function getBitbucketBranches(connectionId: string, workspace: string, repositoryUuid: string): Promise<{ branches: string[]; default_branch: string }> {
  return request(`/api/connectors/bitbucket/branches?${new URLSearchParams({
    connection_id: connectionId, workspace, repository_uuid: repositoryUuid })}`);
}
export function startBitbucketSync(
  connectionId: string, workspace: string, repositoryUuid: string, branch: string,
  fileTypes: string[], includeCommitMessages: boolean, includePullRequests: boolean,
  graphName: string,
): Promise<{ run_id: string; status: string }> {
  return request("/api/connectors/bitbucket/sync", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ connection_id: connectionId, workspace, repository_uuid: repositoryUuid,
      branch, file_types: fileTypes, include_commit_messages: includeCommitMessages,
      include_pull_requests: includePullRequests, graph_name: graphName }),
  });
}
export function getBitbucketSyncRun(runId: string): Promise<BitbucketSyncRun> {
  return request(`/api/connectors/bitbucket/sync/${encodeURIComponent(runId)}`);
}
export function deleteBitbucketConnection(connectionId: string): Promise<DeleteConnectionResult> {
  return request<DeleteConnectionResult>(
    `/api/connectors/bitbucket/connections/${encodeURIComponent(connectionId)}`,
    { method: "DELETE" },
  );
}

export function getNotionStatus(graphName: string): Promise<NotionStatus> {
  return request<NotionStatus>(`/api/connectors/notion/status?${new URLSearchParams({ graph_name: graphName })}`);
}
export function startNotionOAuth(): Promise<{ authorization_url: string }> {
  return request("/api/connectors/notion/oauth/start", { method: "POST" });
}
export function startNotionSync(
  workspaceId: string,
  graphName: string,
  includeChildPages = true,
): Promise<{ run_id: string; status: string }> {
  return request("/api/connectors/notion/sync", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      workspace_id: workspaceId,
      graph_name: graphName,
      include_child_pages: includeChildPages,
    }),
  });
}
export function getNotionSyncRun(runId: string): Promise<NotionSyncRun> {
  return request(`/api/connectors/notion/sync/${encodeURIComponent(runId)}`);
}
export function deleteNotionConnection(workspaceId: string): Promise<DeleteConnectionResult> {
  return request(`/api/connectors/notion/connections/${encodeURIComponent(workspaceId)}`, { method: "DELETE" });
}

export function getOntologyPending(graphName: string): Promise<OntologyPending> {
  return request(`/api/ontology/pending?graph_name=${encodeURIComponent(graphName)}`);
}

export function getOntologyAdoptions(graphName: string): Promise<{ adoptions: OntologyAdoption[] }> {
  return request(`/api/ontology/adoptions?graph_name=${encodeURIComponent(graphName)}`);
}

export function adoptOntology(graphName: string): Promise<OntologyAdoptResult> {
  return request("/api/ontology/adopt", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ graph_name: graphName }),
  });
}

export function unadoptOntology(graphName: string, batchId: string): Promise<OntologyUnadoptResult> {
  return request(
    `/api/ontology/unadopt/${encodeURIComponent(batchId)}?graph_name=${encodeURIComponent(graphName)}`,
    { method: "POST" },
  );
}

export function setAutoExtend(graphName: string, enabled: boolean): Promise<{ auto_extend: boolean }> {
  return request("/api/ontology/auto-extend", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ graph_name: graphName, enabled }),
  });
}

export function startStoryDemo(): Promise<{ run_id: string; graph_name: string; phase: string; status: string }> {
  return request("/api/story-demo/start", { method: "POST" });
}

export function applyStoryPhase(
  graphName: string, phase: string,
): Promise<{ run_id: string; graph_name: string; phase: string; status: string }> {
  const query = new URLSearchParams({ graph_name: graphName });
  return request(`/api/story-demo/apply/${encodeURIComponent(phase)}?${query}`, { method: "POST" });
}

export function getStoryRun(runId: string): Promise<StoryRun> {
  return request(`/api/story-demo/runs/${encodeURIComponent(runId)}`);
}

export function getStoryState(graphName: string): Promise<StoryState> {
  return request(`/api/story-demo/state?${new URLSearchParams({ graph_name: graphName })}`);
}

export function resetStoryDemo(graphName: string): Promise<{ reset: boolean; nodes_removed: number }> {
  return request(`/api/story-demo/reset?${new URLSearchParams({ graph_name: graphName })}`, {
    method: "DELETE",
  });
}

export function reviewStoryWisdom(
  graphName: string, proposalId: string, decision: "approve" | "reject",
): Promise<{ wisdom: StoryWisdomProposal }> {
  const query = new URLSearchParams({ graph_name: graphName });
  return request(`/api/story-demo/wisdom/${encodeURIComponent(proposalId)}/review?${query}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision }),
  });
}
