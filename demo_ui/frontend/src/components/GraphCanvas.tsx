import cytoscape, { type Core, type ElementDefinition } from "cytoscape";
import { LocateFixed, Maximize2, RefreshCw } from "lucide-react";
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
  Document: "#f2f4f7",
  Entity: "#91a0b5",
  SourceRecord: "#7db6ff",
};

interface GraphCanvasProps {
  graph: GraphPayload | null;
  highlight: Highlight;
  loading: boolean;
  onRefresh: () => void;
  onSelect: (selection: GraphSelection) => void;
}

function runLayout(cy: Core, animate = true) {
  cy.layout({
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

export default function GraphCanvas({
  graph,
  highlight,
  loading,
  onRefresh,
  onSelect,
}: GraphCanvasProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  const [layoutBusy, setLayoutBusy] = useState(false);

  const elements = useMemo<ElementDefinition[]>(() => {
    if (!graph) return [];
    return [
      ...graph.nodes.map((node) => ({
        group: "nodes" as const,
        data: { ...node, color: TYPE_COLORS[node.type] ?? TYPE_COLORS.Entity },
      })),
      ...graph.edges.map((edge) => ({
        group: "edges" as const,
        data: { ...edge },
        classes: edge.superseded ? "is-superseded" : "",
      })),
    ];
  }, [graph]);

  useEffect(() => {
    if (!containerRef.current || !graph) return;

    const cy = cytoscape({
      container: containerRef.current,
      elements,
      minZoom: 0.18,
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
          selector: "edge",
          style: {
            width: 1.4,
            "line-color": "#344153",
            "target-arrow-color": "#344153",
            "target-arrow-shape": "triangle",
            "arrow-scale": 0.7,
            "curve-style": "bezier",
            opacity: 0.7,
            label: "",
            "overlay-opacity": 0,
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
            label: "data(label)",
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
      onSelect({ kind: "node", value: graph.nodes.find((item) => item.id === node.id())! });
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
    runLayout(cy, false);

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

  const fitGraph = () => cyRef.current?.animate({ fit: { eles: cyRef.current.elements(), padding: 58 }, duration: 350 });
  const resetLayout = () => {
    if (cyRef.current) runLayout(cyRef.current);
  };

  return (
    <div className="graph-stage">
      <div className="graph-toolbar" aria-label="Graph controls">
        <button onClick={fitGraph} title="Fit graph" aria-label="Fit graph">
          <Maximize2 size={15} />
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
    </div>
  );
}
