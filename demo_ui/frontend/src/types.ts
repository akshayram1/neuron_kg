export interface AppConfig {
  providers: string[];
  defaultProviders: string[];
}

export interface GraphNode {
  id: string;
  label: string;
  type: string;
  group: string;
  summary: string;
  documents: string[];
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  label: string;
  fact: string;
  group: string;
  validAt: string | null;
  invalidAt: string | null;
  superseded: boolean;
  documents: string[];
  confidence: number | null;
  factUid?: string;
  derived?: boolean;
  derivedRule?: string | null;
}

export interface GraphPayload {
  groups: string[];
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface Highlight {
  nodes: string[];
  edges: string[];
}

export interface ChatCitation {
  recordKey: string;
  name: string;
  url: string | null;
}

export interface ChatResponse {
  answer: string;
  citations: ChatCitation[];
  highlight: Highlight;
  readOnly: boolean;
  tokenUsage: TokenUsage;
}

export interface TokenUsage {
  input: number;
  output: number;
  total: number;
}

export interface IngestionTokenUsage extends TokenUsage {
  provider: string;
  active: boolean;
  startedAt: string;
}

export interface ConversationMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  result?: ChatResponse;
}

export interface EntityFact {
  source: string;
  target: string;
  relation: string;
  evidence: string | null;
  validFrom: string | null;
  validTo: string | null;
  observedFrom: string | null;
  observedTo: string | null;
  documents: string[];
  state: "live" | "historical";
  derived: boolean;
  derivedRule: string | null;
  premises: string[];
  endedUnknown: boolean;
  interval: string | null;
  direction: "in" | "out";
  otherUid?: string;
  otherName?: string;
}

export interface EntityHistoryEvent {
  kind: string;
  relation: string;
  source: string;
  target: string;
  at: string | null;
  interval: string | null;
  documents: string[];
  derived: boolean;
  derivedRule: string | null;
  evidence: string | null;
  direction: "in" | "out";
}

export interface EntityDetail {
  entity: GraphNode & { status?: string | null; issueKey?: string | null; url?: string | null };
  facts: EntityFact[];
  past: EntityFact[];
  derived: EntityFact[];
  history: EntityHistoryEvent[];
  at: string | null;
  asOf: string | null;
}

export type GraphSelection =
  | { kind: "node"; value: GraphNode }
  | { kind: "edge"; value: GraphEdge }
  | null;

export interface SourceRecord {
  recordKey: string;
  provider: string;
  entityType: string;
  name: string;
  url: string | null;
  ingestedAt: string;
  deletedAt: string | null;
}

export interface ConnectorSyncProgress {
  phase?: "queued" | "fetching" | "preparing" | "ingesting" | "done" | string;
  current?: string;
  records_done?: number;
  records_total?: number;
  records_written?: number;
  records_kept?: number;
  records_skipped?: number;
  issues_fetched?: number;
  chunks_ingested?: number;
  chunks_total?: number;
  entities_written?: number;
  facts_written?: number;
  // Other connectors' progress vocabulary (GitHub/Notion/SharePoint) —
  // not produced by Jira, kept here so SyncProgress.tsx stays one shared
  // component when those connectors are built (plan.md §1 build order).
  pages_fetched?: number;
  files_matched?: number;
  files_processed?: number;
  files_fetched?: number;
  commits_fetched?: number;
  pull_requests_fetched?: number;
  episodes_ingested?: number;
  files_too_large?: number;
  files_without_text?: number;
  records_removed?: number;
  missing_pages_retained?: number;
  ingestion_input_tokens?: number;
  ingestion_output_tokens?: number;
  ingestion_total_tokens?: number;
}

export interface GitHubInstallation {
  installation_id: number; account_id: number; account_login: string;
  account_type: string; target_type: string; created_at: string; updated_at: string;
}
export interface GitHubSource {
  installation_id: number; repository_id: number; repository_full_name: string;
  default_branch: string; file_types: string[]; include_commit_messages: boolean;
  last_sync_at: string | null; last_sync_error: string | null; group_id: string;
}
export interface GitHubRepository {
  repository_id: number; full_name: string; name: string; owner: string;
  default_branch: string; private: boolean; html_url: string;
}
export interface GitHubSyncRun {
  run_id: string; installation_id: number; repository_id: number;
  status: "queued" | "running" | "completed" | "failed";
  started_at: string; finished_at: string | null;
  result: ConnectorSyncProgress | null; error: string | null;
}
export interface GitHubStatus {
  configured: boolean; installations: GitHubInstallation[];
  sources: GitHubSource[]; runs: GitHubSyncRun[];
}

export interface NotionConnection {
  workspace_id: string; bot_id: string; workspace_name: string;
  created_at: string; updated_at: string; last_sync_at: string | null;
  last_sync_error: string | null; group_id: string;
}
export interface NotionSyncRun {
  run_id: string; workspace_id: string;
  status: "queued" | "running" | "completed" | "failed";
  started_at: string; finished_at: string | null;
  result: ConnectorSyncProgress | null; error: string | null;
}
export interface NotionStatus {
  configured: boolean; connections: NotionConnection[]; runs: NotionSyncRun[];
}

export interface OAuthConnectorConnection {
  connection_id: string;
  account_id: string;
  account_name: string;
  created_at: string;
  updated_at: string;
}

export interface OAuthConnectorSource {
  connection_id: string;
  source_id: string;
  source_name: string;
  group_id: string;
  last_sync_at: string | null;
  last_sync_error: string | null;
  config: Record<string, unknown>;
}

export interface BitbucketWorkspace { uuid: string; slug: string; name: string; }
export interface BitbucketRepository {
  uuid: string; workspace: string; slug: string; name: string; full_name: string;
  main_branch: string; html_url: string; private: boolean; description: string;
}
export interface BitbucketStatus {
  configured: boolean;
  connections: OAuthConnectorConnection[];
  sources: OAuthConnectorSource[];
  runs: BitbucketSyncRun[];
}
export interface BitbucketSyncRun {
  run_id: string; connection_id: string; source_id: string;
  status: "queued" | "running" | "completed" | "failed";
  started_at: string; finished_at: string | null; result: ConnectorSyncProgress | null; error: string | null;
}

export interface JiraSite { cloud_id: string; name: string; url: string; }
export interface JiraProject { project_id: string; key: string; name: string; description: string; }
export interface JiraStatus {
  configured: boolean;
  connections: OAuthConnectorConnection[];
  sources: OAuthConnectorSource[];
  runs: JiraSyncRun[];
}
export interface JiraSyncResult extends ConnectorSyncProgress {
  project_key?: string;
}
export interface JiraSyncRun {
  run_id: string; connection_id: string; source_id: string;
  status: "queued" | "running" | "completed" | "failed";
  started_at: string; finished_at: string | null; result: JiraSyncResult | null; error: string | null;
}

export interface DeleteConnectionResult {
  deleted: boolean;
  records_removed: number;
}
