import { AlertTriangle, BookOpenText, BrainCircuit, Database, Download, FileText, FlaskConical, GitBranch, Github, GitFork, ListTodo, LoaderCircle, Trash2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { clearGraph, createGraph, getConfig, getGraph, getGraphs, getSkosExportUrl, sendChat, setReranker } from "./api";
import ChatPanel from "./components/ChatPanel";
import EntityPanel from "./components/EntityPanel";
import OntologyPanel from "./components/OntologyPanel";
import GraphCanvas from "./components/GraphCanvas";
import GraphSelector from "./components/GraphSelector";
import BitbucketPanel from "./components/BitbucketPanel";
import GitHubPanel from "./components/GitHubPanel";
import JiraPanel from "./components/JiraPanel";
import NotionPanel from "./components/NotionPanel";
import StoryDemoPanel from "./components/StoryDemoPanel";
import type { AppConfig, ConversationMessage, GitHubSource, GraphInfo, GraphPayload, GraphSelection, Highlight, IngestionTokenUsage, NotionConnection, OAuthConnectorSource, TokenUsage } from "./types";

const EMPTY_HIGHLIGHT: Highlight = { nodes: [], edges: [] };
const EMPTY_TOKEN_USAGE: TokenUsage = { input: 0, output: 0, total: 0 };
const WELCOME_MESSAGE: ConversationMessage = {
  id: "welcome",
  role: "assistant",
  content: "I’m connected to your company knowledge graph. Ask me about Jira work, GitHub and Bitbucket code and commits, Notion docs, owners, or decisions.",
};
// Versioned intentionally: older saved answers predate graph-knowledge
// citations and should not appear in the lineage-aware demo.
const CHAT_STORAGE_KEY = "neuron.chat.messages.v2";
const GRAPH_STORAGE_KEY = "neuron.graph.selected";
const DEFAULT_GRAPHS: GraphInfo[] = [{ name: "default", displayName: "Default", createdAt: "" }];
type GraphLayer = "all" | "findings" | "wisdom";

function graphForLayer(graph: GraphPayload | null, layer: GraphLayer): GraphPayload | null {
  if (!graph || layer === "all") return graph;
  const findings = new Set(graph.nodes.filter((node) => node.type === "Finding").map((node) => node.id));
  const wisdom = new Set(graph.nodes.filter((node) => node.type === "Wisdom").map((node) => node.id));
  const included = new Set(layer === "findings" ? findings : wisdom);
  const includedEdges = new Set<string>();

  const includeDirectEdges = (seeds: Set<string>, labels: Set<string>) => {
    for (const edge of graph.edges) {
      if (!labels.has(edge.label)) continue;
      if (!seeds.has(edge.source) && !seeds.has(edge.target)) continue;
      included.add(edge.source);
      included.add(edge.target);
      includedEdges.add(edge.id);
    }
  };

  if (layer === "findings") {
    includeDirectEdges(findings, new Set(["FLAGS", "CONTEXT_FROM"]));
  } else {
    // Keep this a lineage view, not a generic two-hop graph expansion. Shared
    // Project/API hubs otherwise pull nearly the entire knowledge graph in.
    includeDirectEdges(wisdom, new Set(["DERIVED_FROM", "APPLIES_TO"]));
    const supportingFindings = new Set([...included].filter((id) => findings.has(id)));
    includeDirectEdges(supportingFindings, new Set(["FLAGS", "CONTEXT_FROM"]));
  }
  return {
    groups: graph.groups,
    nodes: graph.nodes.filter((node) => included.has(node.id)),
    edges: graph.edges.filter((edge) => includedEdges.has(edge.id)),
  };
}

function loadStoredGraphName(): string {
  try { return localStorage.getItem(GRAPH_STORAGE_KEY) || "default"; } catch { return "default"; }
}

function chatStorageKey(graphName: string): string {
  return `${CHAT_STORAGE_KEY}.${graphName}`;
}

// Chat history is never sent to the model or stored server-side (each turn
// takes only the current question) -- this is purely a per-browser
// convenience so a refresh doesn't wipe the visible conversation. Keyed by
// graph so switching graphs shows that graph's own conversation, and a
// brand-new graph starts with an empty one.
function loadStoredMessages(graphName: string): ConversationMessage[] {
  try {
    const raw = localStorage.getItem(chatStorageKey(graphName));
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
  const [graphName, setGraphNameState] = useState<string>(loadStoredGraphName);
  const [graphs, setGraphs] = useState<GraphInfo[]>(DEFAULT_GRAPHS);
  const [graphsLoading, setGraphsLoading] = useState(false);
  const [graph, setGraph] = useState<GraphPayload | null>(null);
  const [graphLoading, setGraphLoading] = useState(true);
  const [graphError, setGraphError] = useState<string | null>(null);
  const [chatError, setChatError] = useState<string | null>(null);
  const [chatBusy, setChatBusy] = useState(false);
  const [messages, setMessages] = useState<ConversationMessage[]>(() => loadStoredMessages(loadStoredGraphName()));
  const [highlight, setHighlight] = useState<Highlight>(EMPTY_HIGHLIGHT);
  const [selection, setSelection] = useState<GraphSelection>(null);
  const [jiraOpen, setJiraOpen] = useState(false);
  const [githubOpen, setGitHubOpen] = useState(false);
  const [bitbucketOpen, setBitbucketOpen] = useState(false);
  const [notionOpen, setNotionOpen] = useState(false);
  const [storyOpen, setStoryOpen] = useState(false);
  const [jiraSources, setJiraSources] = useState<OAuthConnectorSource[]>([]);
  const [githubSources, setGitHubSources] = useState<GitHubSource[]>([]);
  const [bitbucketSources, setBitbucketSources] = useState<OAuthConnectorSource[]>([]);
  const [notionConnections, setNotionConnections] = useState<NotionConnection[]>([]);
  const [ingestionUsage, setIngestionUsage] = useState<Record<string, IngestionTokenUsage>>({});
  const [retrievalUsage, setRetrievalUsage] = useState<TokenUsage>(EMPTY_TOKEN_USAGE);
  const [clearing, setClearing] = useState(false);
  const [graphLayer, setGraphLayer] = useState<GraphLayer>("all");
  const [rerankerBusy, setRerankerBusy] = useState(false);

  const reportIngestionUsage = useCallback((usage: IngestionTokenUsage) => {
    setIngestionUsage((current) => ({ ...current, [usage.provider]: usage }));
  }, []);

  useEffect(() => {
    try { localStorage.setItem(chatStorageKey(graphName), JSON.stringify(messages)); } catch { /* private mode, quota, etc. -- just skip persisting */ }
  }, [messages, graphName]);

  useEffect(() => {
    getConfig()
      .then((value) => {
        setConfig(value);
        setProviders(value.defaultProviders);
      })
      .catch((reason: Error) => setGraphError(reason.message));
  }, []);

  const refreshGraphs = useCallback(async () => {
    setGraphsLoading(true);
    try {
      const value = await getGraphs();
      if (value.graphs.length) setGraphs(value.graphs);
    } catch {
      // Keep showing DEFAULT_GRAPHS -- the picker degrading to just
      // "Default" is fine, it's the graph everything already points at.
    } finally {
      setGraphsLoading(false);
    }
  }, []);

  useEffect(() => { void refreshGraphs(); }, [refreshGraphs]);

  // Switching graphs swaps the visible chat log and resets per-session UI
  // state, but never touches OAuth connections -- those are shared across
  // every graph (see plan: multigraph.py's design note).
  const selectGraph = useCallback((name: string) => {
    setGraphNameState(name);
    try { localStorage.setItem(GRAPH_STORAGE_KEY, name); } catch { /* ignore */ }
    setMessages(loadStoredMessages(name));
    setHighlight(EMPTY_HIGHLIGHT);
    setSelection(null);
    setChatError(null);
    setRetrievalUsage(EMPTY_TOKEN_USAGE);
    setIngestionUsage({});
    setGraphLayer("all");
  }, []);

  const handleCreateGraph = useCallback(async (name: string) => {
    await createGraph(name);
    await refreshGraphs();
    selectGraph(name);
  }, [refreshGraphs, selectGraph]);

  const loadGraph = useCallback(async () => {
    setGraphLoading(true);
    try {
      const payload = await getGraph(providers, graphName);
      setGraph(payload);
      setGraphError(null);
    } catch (reason) {
      setGraphError(reason instanceof Error ? reason.message : "The knowledge graph could not be loaded.");
    } finally {
      setGraphLoading(false);
    }
  }, [providers, graphName]);

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
      const result = await sendChat(content, providers, graphName);
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
  }, [chatBusy, providers, graphName]);

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

  const toggleReranker = useCallback(async (enabled: boolean) => {
    if (!config || rerankerBusy || chatBusy) return;
    setRerankerBusy(true);
    setChatError(null);
    try {
      const result = await setReranker(enabled);
      setConfig((current) => current ? { ...current, reranker: result.reranker } : current);
    } catch (reason) {
      setChatError(reason instanceof Error ? reason.message : "Could not change the Laya setting.");
    } finally {
      setRerankerBusy(false);
    }
  }, [config, rerankerBusy, chatBusy]);

  const graphSummary = useMemo(() => {
    const visible = graphForLayer(graph, graphLayer);
    if (!visible) return "Loading the knowledge graph";
    const suffix = graphLayer === "all" ? "" : ` · ${graphLayer} layer`;
    return `${visible.nodes.length} things · ${visible.edges.length} connections${suffix}`;
  }, [graph, graphLayer]);
  const visibleGraph = useMemo(() => graphForLayer(graph, graphLayer), [graph, graphLayer]);

  const clearTheGraph = useCallback(async () => {
    if (!window.confirm(
      "Clear the entire knowledge graph? This deletes every node, edge and vector, " +
      "and resets the sync ledger so the next sync writes everything back from scratch. " +
      "Connected accounts (Jira/GitHub/Bitbucket/Notion) stay connected — nothing needs reauthorizing.",
    )) return;
    setClearing(true);
    setGraphError(null);
    try {
      await clearGraph(graphName);
      await loadGraph();
    } catch (reason) {
      setGraphError(reason instanceof Error ? reason.message : "Could not clear the graph.");
    } finally {
      setClearing(false);
    }
  }, [loadGraph, graphName]);

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
        <GraphSelector
          graphs={graphs}
          value={graphName}
          loading={graphsLoading}
          onSelect={selectGraph}
          onCreate={handleCreateGraph}
        />
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
          <button className={`notion-trigger ${graphName.startsWith("story-") ? "connected" : ""}`} onClick={() => setStoryOpen(true)}>
            <FlaskConical size={14} /> Story demo
          </button>
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
          reranker={config?.reranker ?? null}
          rerankerBusy={rerankerBusy}
          onToggleReranker={(enabled) => void toggleReranker(enabled)}
          onShowPath={(value) => {
            setHighlight(value);
            setSelection(null);
          }}
          onOpenKnowledge={(uid, type) => {
            const node = graph?.nodes.find((item) => item.id === uid);
            if (!node) return;
            setGraphLayer(type === "Wisdom" ? "wisdom" : type === "Finding" ? "findings" : "all");
            setHighlight({ nodes: [uid], edges: [] });
            setSelection({ kind: "node", value: node });
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
                href={getSkosExportUrl(providers, graphName)}
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
                <span className="source-picker-divider" aria-hidden="true" />
                <button
                  className={`layer-filter findings ${graphLayer === "findings" ? "active" : ""}`}
                  onClick={() => { setGraphLayer((current) => current === "findings" ? "all" : "findings"); setSelection(null); setHighlight(EMPTY_HIGHLIGHT); }}
                  aria-pressed={graphLayer === "findings"}
                  title="Show findings with their affected nodes and evidence lineage"
                >
                  <AlertTriangle size={14} /> Findings
                </button>
                <button
                  className={`layer-filter wisdom ${graphLayer === "wisdom" ? "active" : ""}`}
                  onClick={() => { setGraphLayer((current) => current === "wisdom" ? "all" : "wisdom"); setSelection(null); setHighlight(EMPTY_HIGHLIGHT); }}
                  aria-pressed={graphLayer === "wisdom"}
                  title="Show wisdom proposals with supporting findings and applicable systems"
                >
                  <BrainCircuit size={14} /> Wisdom
                </button>
              </div>
            </div>
          </div>

          <div className="map-body">
            <GraphCanvas
              graph={visibleGraph}
              highlight={highlight}
              loading={graphLoading}
              onRefresh={() => void loadGraph()}
              onSelect={setSelection}
            />

            {selection?.kind === "node" && (
              <EntityPanel
                node={selection.value}
                providers={providers}
                graphName={graphName}
                onClose={() => setSelection(null)}
                onNavigate={(uid) => {
                  const next = visibleGraph?.nodes.find((item) => item.id === uid);
                  if (next) setSelection({ kind: "node", value: next });
                }}
              />
            )}
            <OntologyPanel graphName={graphName} onChanged={() => void loadGraph()} />

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
        graphName={graphName}
        onClose={() => setJiraOpen(false)}
        onSourcesChanged={setJiraSources}
        onSyncComplete={() => void loadGraph()}
        onTokenUsage={reportIngestionUsage}
      />
      <GitHubPanel
        open={githubOpen}
        graphName={graphName}
        onClose={() => setGitHubOpen(false)}
        onSourcesChanged={setGitHubSources}
        onSyncComplete={() => void loadGraph()}
        onTokenUsage={reportIngestionUsage}
      />
      <BitbucketPanel
        open={bitbucketOpen}
        graphName={graphName}
        onClose={() => setBitbucketOpen(false)}
        onSourcesChanged={setBitbucketSources}
        onSyncComplete={() => void loadGraph()}
        onTokenUsage={reportIngestionUsage}
      />
      <NotionPanel
        open={notionOpen}
        graphName={graphName}
        onClose={() => setNotionOpen(false)}
        onConnectionsChanged={setNotionConnections}
        onSyncComplete={() => void loadGraph()}
        onTokenUsage={reportIngestionUsage}
      />
      <StoryDemoPanel
        open={storyOpen}
        graphName={graphName}
        onClose={() => setStoryOpen(false)}
        onGraphCreated={async (name) => {
          await refreshGraphs();
          selectGraph(name);
        }}
        onGraphChanged={() => void loadGraph()}
        onReset={async () => {
          await refreshGraphs();
          selectGraph("default");
        }}
      />
    </main>
  );
}
