import { BookOpenText, Database, Download, FileText, Github, GitFork, ListTodo } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { getConfig, getGraph, getSkosExportUrl, sendChat } from "./api";
import ChatPanel from "./components/ChatPanel";
import GraphCanvas from "./components/GraphCanvas";
import GitHubPanel from "./components/GitHubPanel";
import JiraPanel from "./components/JiraPanel";
import NotionPanel from "./components/NotionPanel";
import type { AppConfig, ConversationMessage, GitHubSource, GraphPayload, GraphSelection, Highlight, NotionConnection, OAuthConnectorSource } from "./types";

const EMPTY_HIGHLIGHT: Highlight = { nodes: [], edges: [] };
const WELCOME_MESSAGE: ConversationMessage = {
  id: "welcome",
  role: "assistant",
  content: "I’m connected to your company knowledge graph. Ask me about Jira work, GitHub code and commits, Notion docs, owners, or decisions.",
};

const PROVIDER_LABELS: Record<string, { label: string; icon: typeof Database }> = {
  jira: { label: "Jira", icon: ListTodo },
  github: { label: "GitHub", icon: Github },
  notion: { label: "Notion", icon: FileText },
};

function prettyDate(value: string | null): string {
  if (!value) return "Now";
  return new Intl.DateTimeFormat("en", { dateStyle: "medium" }).format(new Date(value));
}

export default function App() {
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [providers, setProviders] = useState<string[]>(["jira", "github", "notion"]);
  const [graph, setGraph] = useState<GraphPayload | null>(null);
  const [graphLoading, setGraphLoading] = useState(true);
  const [graphError, setGraphError] = useState<string | null>(null);
  const [chatError, setChatError] = useState<string | null>(null);
  const [chatBusy, setChatBusy] = useState(false);
  const [messages, setMessages] = useState<ConversationMessage[]>([WELCOME_MESSAGE]);
  const [highlight, setHighlight] = useState<Highlight>(EMPTY_HIGHLIGHT);
  const [selection, setSelection] = useState<GraphSelection>(null);
  const [jiraOpen, setJiraOpen] = useState(false);
  const [githubOpen, setGitHubOpen] = useState(false);
  const [notionOpen, setNotionOpen] = useState(false);
  const [jiraSources, setJiraSources] = useState<OAuthConnectorSource[]>([]);
  const [githubSources, setGitHubSources] = useState<GitHubSource[]>([]);
  const [notionConnections, setNotionConnections] = useState<NotionConnection[]>([]);

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
    () => config?.providers ?? ["jira", "github", "notion"],
    [config],
  );

  const graphSummary = useMemo(() => {
    if (!graph) return "Loading the knowledge graph";
    return `${graph.nodes.length} things · ${graph.edges.length} connections`;
  }, [graph]);

  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark"><GitFork size={17} /></div>
          <div><strong>Neuron</strong><span>Company knowledge graph</span></div>
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

            {selection && (
              <aside className="selection-card">
                <button className="selection-close" onClick={() => setSelection(null)} aria-label="Close details">×</button>
                <span className="selection-kind">
                  {selection.kind === "node" ? selection.value.type : selection.value.label}
                </span>
                {selection.kind === "node" ? (
                  <>
                    <h3>{selection.value.label}</h3>
                    <p>{selection.value.summary || "This item is connected to the surrounding facts."}</p>
                    {selection.value.documents.length > 0 && (
                      <div className="selection-source"><BookOpenText size={13} /> Mentioned in {selection.value.documents.join(", ")}</div>
                    )}
                  </>
                ) : (
                  <>
                    <h3>{selection.value.fact}</h3>
                    <p className={selection.value.superseded ? "superseded-note" : "current-note"}>
                      {selection.value.superseded
                        ? `This was true until ${prettyDate(selection.value.invalidAt)}.`
                        : `Current since ${prettyDate(selection.value.validAt)}.`}
                    </p>
                    {selection.value.documents.length > 0 && (
                      <div className="selection-source"><BookOpenText size={13} /> {selection.value.documents.join(", ")}</div>
                    )}
                  </>
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
      />
      <GitHubPanel
        open={githubOpen}
        onClose={() => setGitHubOpen(false)}
        onSourcesChanged={setGitHubSources}
        onSyncComplete={() => void loadGraph()}
      />
      <NotionPanel
        open={notionOpen}
        onClose={() => setNotionOpen(false)}
        onConnectionsChanged={setNotionConnections}
        onSyncComplete={() => void loadGraph()}
      />
    </main>
  );
}
