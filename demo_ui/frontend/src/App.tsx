import { BookOpenText, Database, Download, FileText, GitBranch, Github, GitFork, ListTodo, LoaderCircle, Trash2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { clearGraph, getConfig, getGraph, getSkosExportUrl, sendChat } from "./api";
import ChatPanel from "./components/ChatPanel";
import EntityPanel from "./components/EntityPanel";
import GraphCanvas from "./components/GraphCanvas";
import BitbucketPanel from "./components/BitbucketPanel";
import GitHubPanel from "./components/GitHubPanel";
import JiraPanel from "./components/JiraPanel";
import NotionPanel from "./components/NotionPanel";
import type { AppConfig, ConversationMessage, GitHubSource, GraphPayload, GraphSelection, Highlight, IngestionTokenUsage, NotionConnection, OAuthConnectorSource, TokenUsage } from "./types";

const EMPTY_HIGHLIGHT: Highlight = { nodes: [], edges: [] };
const EMPTY_TOKEN_USAGE: TokenUsage = { input: 0, output: 0, total: 0 };
const WELCOME_MESSAGE: ConversationMessage = {
  id: "welcome",
  role: "assistant",
  content: "I’m connected to your company knowledge graph. Ask me about Jira work, GitHub and Bitbucket code and commits, Notion docs, owners, or decisions.",
};
const CHAT_STORAGE_KEY = "neuron.chat.messages";

// Chat history is never sent to the model or stored server-side (each turn
// takes only the current question) -- this is purely a per-browser
// convenience so a refresh doesn't wipe the visible conversation.
function loadStoredMessages(): ConversationMessage[] {
  try {
    const raw = localStorage.getItem(CHAT_STORAGE_KEY);
    if (!raw) return [WELCOME_MESSAGE];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) && parsed.length ? parsed : [WELCOME_MESSAGE];
  } catch {
    return [WELCOME_MESSAGE];
  }
}

const PROVIDER_LABELS: Record<string, { label: string; icon: typeof Database }> = {
  jira: { label: "Jira", icon: ListTodo },
  github: { label: "GitHub", icon: Github },
  bitbucket: { label: "Bitbucket", icon: GitBranch },
  notion: { label: "Notion", icon: FileText },
};

function prettyDate(value: string | null): string {
  if (!value) return "Now";
  return new Intl.DateTimeFormat("en", { dateStyle: "medium" }).format(new Date(value));
}

function formatTokens(value: number): string {
  return new Intl.NumberFormat("en", { notation: value >= 10_000 ? "compact" : "standard", maximumFractionDigits: 1 }).format(value);
}

export default function App() {
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [providers, setProviders] = useState<string[]>(["jira", "github", "bitbucket", "notion"]);
  const [graph, setGraph] = useState<GraphPayload | null>(null);
  const [graphLoading, setGraphLoading] = useState(true);
  const [graphError, setGraphError] = useState<string | null>(null);
  const [chatError, setChatError] = useState<string | null>(null);
  const [chatBusy, setChatBusy] = useState(false);
  const [messages, setMessages] = useState<ConversationMessage[]>(loadStoredMessages);
  const [highlight, setHighlight] = useState<Highlight>(EMPTY_HIGHLIGHT);
  const [selection, setSelection] = useState<GraphSelection>(null);
  const [jiraOpen, setJiraOpen] = useState(false);
  const [githubOpen, setGitHubOpen] = useState(false);
  const [bitbucketOpen, setBitbucketOpen] = useState(false);
  const [notionOpen, setNotionOpen] = useState(false);
  const [jiraSources, setJiraSources] = useState<OAuthConnectorSource[]>([]);
  const [githubSources, setGitHubSources] = useState<GitHubSource[]>([]);
  const [bitbucketSources, setBitbucketSources] = useState<OAuthConnectorSource[]>([]);
  const [notionConnections, setNotionConnections] = useState<NotionConnection[]>([]);
  const [ingestionUsage, setIngestionUsage] = useState<Record<string, IngestionTokenUsage>>({});
  const [retrievalUsage, setRetrievalUsage] = useState<TokenUsage>(EMPTY_TOKEN_USAGE);
  const [clearing, setClearing] = useState(false);

  const reportIngestionUsage = useCallback((usage: IngestionTokenUsage) => {
    setIngestionUsage((current) => ({ ...current, [usage.provider]: usage }));
  }, []);

  useEffect(() => {
    try { localStorage.setItem(CHAT_STORAGE_KEY, JSON.stringify(messages)); } catch { /* private mode, quota, etc. -- just skip persisting */ }
  }, [messages]);

  useEffect(() => {
    getConfig()
      .then((value) => {
        setConfig(value);
        setProviders(value.defaultProviders);
      })
      .catch((reason: Error) => setGraphError(reason.message));
  }, []);

  const loadGraph = useCallback(async () => {
    setGraphLoading(true);
    try {
      const payload = await getGraph(providers);
      setGraph(payload);
      setGraphError(null);
    } catch (reason) {
      setGraphError(reason instanceof Error ? reason.message : "The knowledge graph could not be loaded.");
    } finally {
      setGraphLoading(false);
    }
  }, [providers]);

  useEffect(() => {
    void loadGraph();
  }, [loadGraph]);

  const toggleProvider = (provider: string) => {
    setProviders((current) =>
      current.includes(provider)
        ? current.length === 1 ? current : current.filter((item) => item !== provider)
        : [...current, provider],
    );
    setSelection(null);
    setHighlight(EMPTY_HIGHLIGHT);
  };

  const askAgent = useCallback(async (content: string) => {
    if (chatBusy) return;
    const userMessage: ConversationMessage = {
      id: `user-${crypto.randomUUID()}`,
      role: "user",
      content,
    };
    setMessages((current) => [...current, userMessage]);
    setChatBusy(true);
    setRetrievalUsage(EMPTY_TOKEN_USAGE);
    setChatError(null);
    try {
      const result = await sendChat(content, providers);
      setMessages((current) => [...current, {
        id: `assistant-${crypto.randomUUID()}`,
        role: "assistant",
        content: result.answer,
        result,
      }]);
      setHighlight(result.highlight);
      setRetrievalUsage(result.tokenUsage);
      setSelection(null);
    } catch (reason) {
      setChatError(reason instanceof Error ? reason.message : "The agent could not answer that question.");
    } finally {
      setChatBusy(false);
    }
  }, [chatBusy, providers]);

  const clearChat = useCallback(() => {
    setMessages([WELCOME_MESSAGE]);
    setChatError(null);
    setHighlight(EMPTY_HIGHLIGHT);
    setSelection(null);
  }, []);

  const availableProviders = useMemo(
    () => config?.providers ?? ["jira", "github", "bitbucket", "notion"],
    [config],
  );

  const graphSummary = useMemo(() => {
    if (!graph) return "Loading the knowledge graph";
    return `${graph.nodes.length} things · ${graph.edges.length} connections`;
  }, [graph]);

  const clearTheGraph = useCallback(async () => {
    if (!window.confirm(
      "Clear the entire knowledge graph? This deletes every node, edge and vector, " +
      "and resets the sync ledger so the next sync writes everything back from scratch. " +
      "Connected accounts (Jira/GitHub/Bitbucket/Notion) stay connected — nothing needs reauthorizing.",
    )) return;
    setClearing(true);
    setGraphError(null);
    try {
      await clearGraph();
      await loadGraph();
    } catch (reason) {
      setGraphError(reason instanceof Error ? reason.message : "Could not clear the graph.");
    } finally {
      setClearing(false);
    }
  }, [loadGraph]);

  // A grand total across every provider's full sync history -- each entry in
  // `ingestionUsage` already sums that one provider's runs (see
  // tokenUsage.ts's `totalIngestionUsage`), so this just adds them together
  // rather than picking one "current" provider, which used to make the
  // counter reset to a single sync click's cost and look wrong.
  const totalIngestion = useMemo(() => {
    const all = Object.values(ingestionUsage);
    return {
      input: all.reduce((sum, item) => sum + item.input, 0),
      output: all.reduce((sum, item) => sum + item.output, 0),
      total: all.reduce((sum, item) => sum + item.total, 0),
      active: all.some((item) => item.active),
      activeProviders: all.filter((item) => item.active).map((item) => item.provider),
    };
  }, [ingestionUsage]);

  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark"><GitFork size={17} /></div>
          <div><strong>Neuron</strong><span>Company knowledge graph</span></div>
        </div>
        <div className="token-metrics" aria-label="Model token usage">
          <div
            className={`token-metric ${totalIngestion.active ? "active" : ""}`}
            title={`Lifetime total across Jira/GitHub/Bitbucket/Notion. Input ${totalIngestion.input} + output ${totalIngestion.output}. Includes extraction and embedding model calls.`}
          >
            <span>{totalIngestion.active ? `${totalIngestion.activeProviders.join(", ")} ingesting` : "Ingestion"}</span>
            <strong>{formatTokens(totalIngestion.total)} tokens</strong>
          </div>
          <div
            className={`token-metric retrieval ${chatBusy ? "active" : ""}`}
            title={`Input ${retrievalUsage.input} + output ${retrievalUsage.output}. Includes query embedding and grounded answer calls.`}
          >
            <span>{chatBusy ? "Retrieving" : "Retrieval"}</span>
            <strong>{chatBusy ? "Counting…" : `${formatTokens(retrievalUsage.total)} tokens`}</strong>
          </div>
        </div>
        <div className="topbar-actions">
          <button className={`notion-trigger ${jiraSources.length > 0 ? "connected" : ""}`} onClick={() => setJiraOpen(true)}>
            <ListTodo size={14} /> Jira
            {jiraSources.length > 0 && <span>{jiraSources.length}</span>}
          </button>
          <button className={`notion-trigger ${githubSources.length > 0 ? "connected" : ""}`} onClick={() => setGitHubOpen(true)}>
            <Github size={14} /> GitHub
            {githubSources.length > 0 && <span>{githubSources.length}</span>}
          </button>
          <button className={`notion-trigger ${bitbucketSources.length > 0 ? "connected" : ""}`} onClick={() => setBitbucketOpen(true)}>
            <GitBranch size={14} /> Bitbucket
            {bitbucketSources.length > 0 && <span>{bitbucketSources.length}</span>}
          </button>
          <button className={`notion-trigger ${notionConnections.length > 0 ? "connected" : ""}`} onClick={() => setNotionOpen(true)}>
            <FileText size={14} /> Notion
            {notionConnections.length > 0 && <span>{notionConnections.length}</span>}
          </button>
          <div className="topbar-status"><span className="status-dot" /> Knowledge is live</div>
        </div>
      </header>

      <div className="workspace">
        <ChatPanel
          messages={messages}
          busy={chatBusy}
          error={chatError}
          activeHighlight={highlight}
          onSend={(message) => void askAgent(message)}
          onClear={clearChat}
          onShowPath={(value) => {
            setHighlight(value);
            setSelection(null);
          }}
        />

        <section className="map-panel" aria-label="Knowledge graph">
          <div className="map-header">
            <div>
              <span className="eyebrow"><Database size={13} /> Knowledge graph</span>
              <h2>What's in the graph</h2>
              <p>{graphSummary}</p>
              {graphError && <p className="error-note">{graphError}</p>}
            </div>
            <div className="map-header-actions">
              <button
                className="skos-export clear-graph"
                onClick={() => void clearTheGraph()}
                disabled={clearing}
                title="Delete every node, edge and vector, and reset the sync ledger. Connected accounts stay connected."
              >
                {clearing ? <LoaderCircle className="spin" size={13} /> : <Trash2 size={13} />} Clear graph
              </button>
              <a
                className="skos-export"
                href={getSkosExportUrl(providers)}
                title="Download the currently selected sources as SKOS RDF (Turtle)"
              >
                <Download size={13} /> Export SKOS
              </a>
              <div className="source-picker" aria-label="Sources">
                {availableProviders.map((provider) => {
                  const meta = PROVIDER_LABELS[provider] ?? { label: provider, icon: Database };
                  const Icon = meta.icon;
                  return (
                    <button
                      key={provider}
                      className={providers.includes(provider) ? "active" : ""}
                      onClick={() => toggleProvider(provider)}
                      aria-pressed={providers.includes(provider)}
                    >
                      <Icon size={14} /> {meta.label}
                    </button>
                  );
                })}
              </div>
            </div>
          </div>

          <div className="map-body">
            <GraphCanvas
              graph={graph}
              highlight={highlight}
              loading={graphLoading}
              onRefresh={() => void loadGraph()}
              onSelect={setSelection}
            />

            {selection?.kind === "node" && (
              <EntityPanel
                node={selection.value}
                providers={providers}
                onClose={() => setSelection(null)}
                onNavigate={(uid) => {
                  const next = graph?.nodes.find((item) => item.id === uid);
                  if (next) setSelection({ kind: "node", value: next });
                }}
              />
            )}
            {selection?.kind === "edge" && (
              <aside className="selection-card">
                <button className="selection-close" onClick={() => setSelection(null)} aria-label="Close details">×</button>
                <span className="selection-kind">{selection.value.label}</span>
                <h3>{selection.value.fact}</h3>
                <p className={selection.value.derived ? "derived-note" : selection.value.superseded ? "superseded-note" : "current-note"}>
                  {selection.value.derived
                    ? `Inferred${selection.value.derivedRule ? ` · ${selection.value.derivedRule.replaceAll("_", " ")}` : ""}.`
                    : selection.value.superseded
                      ? `This was true until ${prettyDate(selection.value.invalidAt)}.`
                      : `Current since ${prettyDate(selection.value.validAt)}.`}
                </p>
                {selection.value.documents.length > 0 && (
                  <div className="selection-source"><BookOpenText size={13} /> {selection.value.documents.join(", ")}</div>
                )}
              </aside>
            )}
          </div>
        </section>
      </div>

      <JiraPanel
        open={jiraOpen}
        onClose={() => setJiraOpen(false)}
        onSourcesChanged={setJiraSources}
        onSyncComplete={() => void loadGraph()}
        onTokenUsage={reportIngestionUsage}
      />
      <GitHubPanel
        open={githubOpen}
        onClose={() => setGitHubOpen(false)}
        onSourcesChanged={setGitHubSources}
        onSyncComplete={() => void loadGraph()}
        onTokenUsage={reportIngestionUsage}
      />
      <BitbucketPanel
        open={bitbucketOpen}
        onClose={() => setBitbucketOpen(false)}
        onSourcesChanged={setBitbucketSources}
        onSyncComplete={() => void loadGraph()}
        onTokenUsage={reportIngestionUsage}
      />
      <NotionPanel
        open={notionOpen}
        onClose={() => setNotionOpen(false)}
        onConnectionsChanged={setNotionConnections}
        onSyncComplete={() => void loadGraph()}
        onTokenUsage={reportIngestionUsage}
      />
    </main>
  );
}
