import cytoscape, { type Core, type ElementDefinition } from "cytoscape";
import { Boxes, LocateFixed, Maximize2, Minimize2, Minus, Plus, RefreshCw, Search, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type {
  GraphEdge,
  GraphNode,
  GraphPayload,
  GraphSelection,
  Highlight,
} from "../types";

// One entry per label in graph.ontology (plan.md §2.2/§2.3) plus structural
// kinds. Add a color here whenever a new node label is introduced.
const TYPE_COLORS: Record<string, string> = {
  Term: "#64a8ff",
  Decision: "#ff8873",
  System: "#ad8cff",
  Person: "#f6c85f",
  WorkItem: "#59dcb2",
  Project: "#c48fff",
  Repository: "#59dcb2",
  Workspace: "#f2f4f7",
  SourceFile: "#72b7ff",
  Commit: "#84d6ba",
  PullRequest: "#9ad0ff",
  Document: "#f2f4f7",
  Entity: "#91a0b5",
  SourceRecord: "#7db6ff",
  Finding: "#7f1d1d",
  Wisdom: "#8b5cf6",
};

const TYPE_ORDER = [
  "Project", "Workspace", "Repository", "WorkItem", "SourceFile", "Commit",
  "Finding", "Wisdom", "PullRequest", "Document", "Person", "Term", "Decision", "System",
];

type LayoutMode = "network" | "clusters";

const CLUSTER_META: Record<string, { label: string; color: string; order: number }> = {
  jira: { label: "Jira", color: "#6ea8ff", order: 1 },
  notion: { label: "Notion", color: "#e5e7eb", order: 2 },
  bitbucket: { label: "Bitbucket", color: "#4f8cff", order: 3 },
  github: { label: "GitHub", color: "#a8b3c4", order: 4 },
  findings: { label: "Findings", color: "#ef4444", order: 5 },
  wisdom: { label: "Wisdom", color: "#a78bfa", order: 6 },
  shared: { label: "Shared knowledge", color: "#59dcb2", order: 7 },
};

function clusterForNode(node: GraphNode): string {
  if (node.type === "Finding") return "findings";
  if (node.type === "Wisdom") return "wisdom";
  const provider = node.group?.toLowerCase();
  return provider && provider in CLUSTER_META ? provider : "shared";
}

interface GraphCanvasProps {
  graph: GraphPayload | null;
  highlight: Highlight;
  loading: boolean;
  onRefresh: () => void;
  onSelect: (selection: GraphSelection) => void;
}

interface CanvasSearchResult {
  id: string;
  kind: "node" | "edge";
  title: string;
  meta: string;
  selection: Exclude<GraphSelection, null>;
  score: number;
}

function runLayout(cy: Core, animate = true) {
  cy.nodes(".cluster-label").addClass("is-hidden");
  cy.elements().not(".cluster-label").layout({
    name: "concentric",
    animate,
    animationDuration: 520,
    concentric: (node) => {
      const type = node.data("type");
      if (["Project", "Repository", "Workspace"].includes(type)) return 100;
      if (["WorkItem", "SourceFile", "Commit", "Document"].includes(type)) return 75;
      if (type === "Person") return 55;
      if (type === "Decision") return 40;
      return 20;
    },
    levelWidth: () => 18,
    minNodeSpacing: 72,
    fit: true,
    padding: 72,
  }).run();
}

function runClusterLayout(cy: Core, animate = true) {
  const buckets = new Map<string, ReturnType<Core["collection"]>>();
  cy.nodes().not(".cluster-label").forEach((node) => {
    const key = String(node.data("cluster") || "shared");
    const bucket = buckets.get(key) ?? cy.collection();
    bucket.merge(node);
    buckets.set(key, bucket);
  });
  const groups = [...buckets.entries()].sort(
    ([left], [right]) => (CLUSTER_META[left]?.order ?? 99) - (CLUSTER_META[right]?.order ?? 99),
  );
  if (!groups.length) return;

  const columns = groups.length <= 2 ? groups.length : 3;
  const rows = Math.ceil(groups.length / columns);
  const spacingX = 680;
  const spacingY = 560;
  const positions: Record<string, { x: number; y: number }> = {};

  groups.forEach(([key, nodes], groupIndex) => {
    const column = groupIndex % columns;
    const row = Math.floor(groupIndex / columns);
    const centerX = (column - (columns - 1) / 2) * spacingX;
    const centerY = (row - (rows - 1) / 2) * spacingY;
    const count = nodes.length;
    const clusterRadius = Math.max(95, Math.sqrt(count) * 58);
    nodes.forEach((node, index) => {
      if (count === 1) {
        positions[node.id()] = { x: centerX, y: centerY };
        return;
      }
      // Golden-angle spiral keeps both small and large source groups legible
      // without requiring an extra Cytoscape layout plugin.
      const radius = 34 + Math.sqrt(index) * 48;
      const angle = index * 2.399963229728653;
      positions[node.id()] = {
        x: centerX + Math.cos(angle) * radius,
        y: centerY + Math.sin(angle) * radius,
      };
    });
    const label = cy.getElementById(`__cluster_${key}`);
    if (label.length) {
      label.removeClass("is-hidden");
      positions[label.id()] = { x: centerX, y: centerY - clusterRadius - 62 };
    }
  });

  cy.layout({
    name: "preset",
    positions,
    animate,
    animationDuration: 620,
    fit: true,
    padding: 82,
  }).run();
}

function relationLabel(value: string): string {
  return value.replaceAll("_", " ");
}

export default function GraphCanvas({
  graph,
  highlight,
  loading,
  onRefresh,
  onSelect,
}: GraphCanvasProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  const layoutModeRef = useRef<LayoutMode>("network");
  const [layoutBusy, setLayoutBusy] = useState(false);
  const [layoutMode, setLayoutMode] = useState<LayoutMode>("network");
  const [fullscreen, setFullscreen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");

  const elements = useMemo<ElementDefinition[]>(() => {
    if (!graph) return [];
    const nodeElements = graph.nodes.map((node) => ({
      group: "nodes" as const,
      data: {
        ...node,
        cluster: clusterForNode(node),
        color: TYPE_COLORS[node.type] ?? TYPE_COLORS.Entity,
      },
      classes: [
        node.type === "Finding" && node.status === "stale" ? "is-finding-node-stale" : "",
        node.type === "Wisdom" ? `is-wisdom-${node.status ?? "proposed"}` : "",
      ].filter(Boolean).join(" "),
    }));
    const clusterKeys = [...new Set(graph.nodes.map(clusterForNode))];
    const clusterLabels: ElementDefinition[] = clusterKeys.map((key) => ({
      group: "nodes",
      data: {
        id: `__cluster_${key}`,
        label: CLUSTER_META[key]?.label ?? key,
        cluster: key,
        clusterColor: CLUSTER_META[key]?.color ?? CLUSTER_META.shared.color,
        isClusterLabel: true,
      },
      classes: "cluster-label is-hidden",
    }));
    return [
      ...nodeElements,
      ...clusterLabels,
      ...graph.edges.map((edge) => ({
        group: "edges" as const,
        data: { ...edge, displayLabel: relationLabel(edge.label) },
        classes: [
          edge.superseded ? "is-superseded" : "",
          edge.derived ? "is-derived" : "",
          edge.findingStatus === "open" ? "is-finding" : "",
          edge.findingStatus === "stale" ? "is-finding-stale" : "",
        ].filter(Boolean).join(" "),
      })),
    ];
  }, [graph]);

  const searchResults = useMemo<CanvasSearchResult[]>(() => {
    const query = searchQuery.trim().toLowerCase();
    if (!graph || !query) return [];
    const terms = query.split(/\s+/).filter(Boolean);
    const matches = (text: string) => terms.every((term) => text.includes(term));
    const score = (title: string, text: string) => {
      const normalizedTitle = title.toLowerCase();
      if (normalizedTitle === query) return 3;
      if (normalizedTitle.startsWith(query)) return 2;
      return text.includes(query) ? 1 : 0;
    };

    const nodeResults: CanvasSearchResult[] = graph.nodes.flatMap((node) => {
      const text = [node.label, node.type, node.wisdomType, node.summary, ...node.documents]
        .filter(Boolean).join(" ").toLowerCase();
      return matches(text) ? [{
        id: node.id,
        kind: "node",
        title: node.label,
        meta: node.wisdomType ? `${node.type} · ${node.wisdomType}` : node.type,
        selection: { kind: "node", value: node },
        score: score(node.label, text),
      }] : [];
    });
    const edgeResults: CanvasSearchResult[] = graph.edges.flatMap((edge) => {
      const title = relationLabel(edge.label);
      const text = [title, edge.fact, ...edge.documents].join(" ").toLowerCase();
      return matches(text) ? [{
        id: edge.id,
        kind: "edge",
        title,
        meta: edge.fact,
        selection: { kind: "edge", value: edge },
        score: score(title, text),
      }] : [];
    });
    return [...nodeResults, ...edgeResults]
      .sort((left, right) => right.score - left.score || left.title.localeCompare(right.title))
      .slice(0, 10);
  }, [graph, searchQuery]);

  const legendTypes = useMemo(() => {
    const present = new Set((graph?.nodes ?? []).map((node) => node.type));
    const known = TYPE_ORDER.filter((type) => present.has(type));
    const extra = [...present].filter((type) => !TYPE_ORDER.includes(type)).sort();
    return [...known, ...extra];
  }, [graph]);

  useEffect(() => {
    if (!containerRef.current || !graph) return;

    const cy = cytoscape({
      container: containerRef.current,
      elements,
      // 0.18 was tuned for a small demo graph -- with hundreds/thousands of
      // real nodes the concentric layout's natural size is far bigger than
      // that floor, so "zoom out" hit the cap almost immediately and looked
      // broken. A much smaller floor lets the "-" button (and "Fit graph")
      // actually shrink a large graph into view.
      minZoom: 0.02,
      maxZoom: 2.4,
      wheelSensitivity: 0.18,
      selectionType: "single",
      style: [
        {
          selector: "node",
          style: {
            width: 33,
            height: 33,
            shape: "round-rectangle",
            "background-color": "data(color)",
            "border-color": "#141a22",
            "border-width": 3,
            label: "data(label)",
            color: "#dfe8f5",
            "font-size": 9,
            "font-family": "Inter, ui-sans-serif, system-ui",
            "font-weight": 550,
            "text-valign": "bottom",
            "text-margin-y": 8,
            "text-wrap": "ellipsis",
            "text-max-width": "108px",
            "overlay-opacity": 0,
          },
        },
        {
          selector: "node.cluster-label",
          style: {
            width: 126,
            height: 32,
            shape: "round-rectangle",
            "background-color": "#111925",
            "border-color": "data(clusterColor)",
            "border-width": 2,
            label: "data(label)",
            color: "#e7eef8",
            "font-size": 12,
            "font-weight": 750,
            "text-valign": "center",
            "text-margin-y": 0,
            "text-max-width": "116px",
            "z-index": 2,
          },
        },
        {
          selector: "node.cluster-label.is-hidden",
          style: { display: "none" },
        },
        {
          selector: 'node[type = "Project"], node[type = "Repository"], node[type = "Workspace"]',
          style: {
            width: 48,
            height: 48,
            "border-color": "#d7c6ff",
            "border-width": 5,
            "font-size": 11,
            "font-weight": 700,
            "text-max-width": "170px",
          },
        },
        {
          selector: 'node[type = "Finding"]',
          style: {
            shape: "diamond",
            width: 42,
            height: 42,
            "background-color": "#7f1d1d",
            "border-color": "#ef4444",
            "border-width": 4,
            "font-weight": 750,
          },
        },
        {
          selector: 'node[type = "Wisdom"]',
          style: {
            shape: "hexagon",
            width: 48,
            height: 48,
            "background-color": "#6d3fc0",
            "border-color": "#c4a7ff",
            "border-width": 5,
            "font-weight": 780,
            "text-max-width": "160px",
          },
        },
        {
          selector: "node.is-wisdom-active",
          style: {
            "background-color": "#176b52",
            "border-color": "#75ddba",
          },
        },
        {
          selector: "node.is-wisdom-rejected, node.is-wisdom-superseded",
          style: { opacity: 0.42, "border-style": "dashed" },
        },
        {
          selector: "node.is-finding-node-stale",
          style: {
            "background-color": "#543535",
            "border-color": "#795757",
            opacity: 0.5,
          },
        },
        {
          selector: "edge",
          style: {
            width: 1.4,
            "line-color": "#344153",
            "target-arrow-color": "#344153",
            "target-arrow-shape": "triangle",
            "arrow-scale": 0.7,
            "curve-style": "bezier",
            opacity: 0.82,
            label: "data(displayLabel)",
            color: "#9aaac0",
            "font-size": 8,
            "font-family": "Inter, ui-sans-serif, system-ui",
            "font-weight": 700,
            "text-rotation": "autorotate",
            "text-background-color": "#0b1017",
            "text-background-opacity": 0.9,
            "text-background-padding": "3px",
            "text-border-color": "#202a38",
            "text-border-width": 1,
            "text-border-opacity": 0.75,
            "overlay-opacity": 0,
          },
        },
        {
          selector: "edge.is-derived",
          style: {
            "line-color": "#d4a017",
            "target-arrow-color": "#d4a017",
            "line-style": "solid",
            opacity: 0.85,
          },
        },
        {
          selector: "edge.is-superseded",
          style: {
            "line-style": "dashed",
            "line-color": "#4a5361",
            "target-arrow-color": "#4a5361",
            opacity: 0.42,
          },
        },
        {
          selector: "edge.is-finding",
          style: {
            width: 5,
            "line-color": "#7f1d1d",
            "target-arrow-color": "#991b1b",
            color: "#fca5a5",
            opacity: 1,
            "z-index": 35,
          },
        },
        {
          selector: "edge.is-finding-stale",
          style: {
            width: 2,
            "line-style": "dashed",
            "line-color": "#6b3030",
            "target-arrow-color": "#6b3030",
            color: "#9f7777",
            opacity: 0.42,
          },
        },
        {
          selector: ".is-dimmed",
          style: { opacity: 0.1, "text-opacity": 0.08 },
        },
        {
          selector: "node.is-highlighted",
          style: {
            opacity: 1,
            "text-opacity": 1,
            "border-color": "#ffd166",
            "border-width": 6,
            "underlay-color": "#ffd166",
            "underlay-opacity": 0.18,
            "underlay-padding": 10,
            "z-index": 50,
          },
        },
        {
          selector: "edge.is-highlighted",
          style: {
            opacity: 1,
            width: 4,
            "line-color": "#ffd166",
            "target-arrow-color": "#ffd166",
            label: "data(displayLabel)",
            color: "#ffe4a1",
            "font-size": 8,
            "text-background-color": "#10141b",
            "text-background-opacity": 0.92,
            "text-background-padding": "4px",
            "text-rotation": "autorotate",
            "z-index": 40,
          },
        },
        {
          selector: ":selected",
          style: {
            "border-color": "#ffffff",
            "border-width": 5,
            "line-color": "#ffffff",
            "target-arrow-color": "#ffffff",
          },
        },
      ],
    });

    cyRef.current = cy;
    cy.on("tap", "node", (event) => {
      const node = event.target;
      if (node.data("isClusterLabel")) return;
      const selected = graph.nodes.find((item) => item.id === node.id());
      if (selected) onSelect({ kind: "node", value: selected });
    });
    cy.on("tap", "edge", (event) => {
      const edge = event.target;
      onSelect({ kind: "edge", value: graph.edges.find((item) => item.id === edge.id())! });
    });
    cy.on("tap", (event) => {
      if (event.target === cy) onSelect(null);
    });
    cy.on("layoutstart", () => setLayoutBusy(true));
    cy.on("layoutstop", () => setLayoutBusy(false));
    if (layoutModeRef.current === "clusters") runClusterLayout(cy, false);
    else runLayout(cy, false);

    return () => {
      cy.destroy();
      cyRef.current = null;
    };
  }, [elements, graph, onSelect]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;

    cy.elements().removeClass("is-dimmed is-highlighted");
    const nodeIds = highlight.nodes.filter((id) => cy.getElementById(id).length > 0);
    const edgeIds = highlight.edges.filter((id) => cy.getElementById(id).length > 0);
    if (!nodeIds.length && !edgeIds.length) return;

    cy.elements().addClass("is-dimmed");
    const focused = cy.collection();
    nodeIds.forEach((id) => focused.merge(cy.getElementById(id)));
    edgeIds.forEach((id) => {
      const edge = cy.getElementById(id);
      focused.merge(edge);
      focused.merge(edge.connectedNodes());
    });
    focused.removeClass("is-dimmed").addClass("is-highlighted");
    cy.animate({ fit: { eles: focused, padding: 110 }, duration: 650 });
  // A chat turn refreshes the graph after writing new facts. That refresh
  // recreates the Cytoscape instance, so re-apply the active answer path even
  // when the highlight IDs themselves have not changed.
  }, [graph, highlight]);

  useEffect(() => {
    const handleFullscreenChange = () => {
      const panel = containerRef.current?.closest(".map-panel");
      const active = Boolean(panel && document.fullscreenElement === panel);
      setFullscreen(active);
      // Fullscreen dimensions settle after the event. Resize Cytoscape on the
      // next frame, then fit the existing graph without rebuilding it.
      window.requestAnimationFrame(() => {
        const cy = cyRef.current;
        if (!cy) return;
        cy.resize();
        cy.animate({ fit: { eles: cy.elements().not(".cluster-label"), padding: 72 }, duration: 320 });
      });
    };
    document.addEventListener("fullscreenchange", handleFullscreenChange);
    return () => document.removeEventListener("fullscreenchange", handleFullscreenChange);
  }, []);

  const toggleFullscreen = async () => {
    const panel = containerRef.current?.closest<HTMLElement>(".map-panel");
    if (!panel) return;
    try {
      if (document.fullscreenElement === panel) await document.exitFullscreen();
      else await panel.requestFullscreen();
    } catch {
      // Browsers can deny fullscreen outside a direct user gesture; the
      // button remains usable for the next click without breaking the graph.
    }
  };
  const resetLayout = () => {
    if (!cyRef.current) return;
    if (layoutModeRef.current === "clusters") runClusterLayout(cyRef.current);
    else runLayout(cyRef.current);
  };
  const toggleLayoutMode = () => {
    const next: LayoutMode = layoutModeRef.current === "network" ? "clusters" : "network";
    layoutModeRef.current = next;
    setLayoutMode(next);
    if (!cyRef.current) return;
    if (next === "clusters") runClusterLayout(cyRef.current);
    else runLayout(cyRef.current);
  };
  const zoomBy = (factor: number) => {
    const cy = cyRef.current;
    if (!cy) return;
    const level = Math.min(cy.maxZoom(), Math.max(cy.minZoom(), cy.zoom() * factor));
    cy.animate({
      zoom: { level, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } },
    }, { duration: 150 });
  };
  const zoomIn = () => zoomBy(1.25);
  const zoomOut = () => zoomBy(1 / 1.25);
  const focusSearchResult = (result: CanvasSearchResult) => {
    const cy = cyRef.current;
    if (!cy) return;
    const element = cy.getElementById(result.id);
    if (!element.length) return;
    cy.elements().unselect();
    element.select();
    const focus = result.kind === "node"
      ? element.closedNeighborhood()
      : element.union(element.connectedNodes());
    cy.animate({ fit: { eles: focus, padding: 120 }, duration: 400 });
    onSelect(result.selection);
    setSearchQuery("");
  };

  return (
    <div className="graph-stage">
      <div className="graph-search">
        <Search size={14} aria-hidden="true" />
        <input
          type="search"
          value={searchQuery}
          onChange={(event) => setSearchQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && searchResults[0]) focusSearchResult(searchResults[0]);
            if (event.key === "Escape") setSearchQuery("");
          }}
          placeholder="Search nodes and relations…"
          aria-label="Search the graph"
          autoComplete="off"
        />
        {searchQuery && (
          <button onClick={() => setSearchQuery("")} title="Clear search" aria-label="Clear graph search">
            <X size={13} />
          </button>
        )}
        {searchQuery.trim() && (
          <div className="graph-search-results" role="listbox" aria-label="Graph search results">
            {searchResults.map((result) => (
              <button
                key={`${result.kind}:${result.id}`}
                onClick={() => focusSearchResult(result)}
                role="option"
                aria-selected="false"
              >
                <span>{result.title}</span>
                <small>{result.meta}</small>
                <em>{result.kind}</em>
              </button>
            ))}
            {!searchResults.length && <div className="graph-search-empty">No matching nodes or relations</div>}
          </div>
        )}
      </div>
      <div className="graph-toolbar" aria-label="Graph controls">
        <button
          className={layoutMode === "clusters" ? "active" : ""}
          onClick={toggleLayoutMode}
          title={layoutMode === "clusters" ? "Switch to network view" : "Cluster by source and knowledge layer"}
          aria-label={layoutMode === "clusters" ? "Switch to network view" : "Switch to cluster view"}
          aria-pressed={layoutMode === "clusters"}
          disabled={layoutBusy}
        >
          <Boxes size={15} />
        </button>
        <button onClick={zoomIn} title="Zoom in" aria-label="Zoom in">
          <Plus size={15} />
        </button>
        <button onClick={zoomOut} title="Zoom out" aria-label="Zoom out">
          <Minus size={15} />
        </button>
        <button
          className={fullscreen ? "active" : ""}
          onClick={() => void toggleFullscreen()}
          title={fullscreen ? "Exit full screen" : "Open graph full screen"}
          aria-label={fullscreen ? "Exit graph full screen" : "Open graph full screen"}
          aria-pressed={fullscreen}
        >
          {fullscreen ? <Minimize2 size={15} /> : <Maximize2 size={15} />}
        </button>
        <button onClick={resetLayout} title="Re-run layout" aria-label="Re-run layout" disabled={layoutBusy}>
          <LocateFixed size={15} />
        </button>
        <button onClick={onRefresh} title="Refresh graph" aria-label="Refresh graph" disabled={loading}>
          <RefreshCw size={15} className={loading ? "spin" : ""} />
        </button>
      </div>
      <div ref={containerRef} className="graph-canvas" aria-label="Interactive knowledge graph" />
      {!graph && <div className="graph-empty">Connecting to the graph…</div>}
      {graph && legendTypes.length > 0 && (
        <aside className="map-legend" aria-label="Graph legend">
          <div className="legend-title">Legend</div>
          <div className="legend-section">
            <span className="legend-rule">Nodes</span>
            <div className="legend-items">
              {legendTypes.map((type) => (
                <span key={type}>
                  <i
                    className="shape-marker"
                    style={{ background: TYPE_COLORS[type] ?? TYPE_COLORS.Entity }}
                    aria-hidden="true"
                  />
                  {type}
                </span>
              ))}
            </div>
          </div>
          <div className="legend-section">
            <span className="legend-rule">Edges</span>
            <div className="legend-items">
              <span><i className="edge-swatch" aria-hidden="true" /> Live</span>
              <span className="gold-key"><i className="edge-swatch is-derived" aria-hidden="true" /> Derived</span>
              <span><i className="edge-swatch is-superseded" aria-hidden="true" /> Superseded</span>
              <span><i className="edge-swatch is-finding" aria-hidden="true" /> Finding</span>
            </div>
          </div>
        </aside>
      )}
    </div>
  );
}
