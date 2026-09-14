import { BookOpenText, ExternalLink, LoaderCircle } from "lucide-react";
import { useEffect, useState } from "react";
import { getEntity } from "../api";
import type { EntityDetail, EntityFact, GraphNode } from "../types";

interface EntityPanelProps {
  node: GraphNode;
  providers: string[];
  graphName: string;
  onClose: () => void;
  onNavigate: (uid: string) => void;
}

type Tab = "view" | "relations" | "history" | "derived";

function viewTabLabel(type: string): string {
  if (type === "SourceFile") return "Code";
  if (type === "WorkItem") return "Ticket";
  if (type === "Document") return "Page";
  if (type === "Commit" || type === "PullRequest") return "Text";
  return "Data";
}

function storedBody(text: string | null | undefined): string {
  const raw = text?.trim() ?? "";
  if (!raw) return "";
  if (raw.startsWith("[SOURCE]")) {
    const split = raw.split("\n\n");
    return split.slice(1).join("\n\n").trim();
  }
  return raw;
}

function pretty(value: string | null | undefined): string {
  if (!value) return "unknown date";
  return value.slice(0, 10);
}

function FactLine({
  fact, onNavigate,
}: {
  fact: EntityFact;
  onNavigate: (uid: string) => void;
}) {
  const other = fact.otherName || (fact.direction === "out" ? fact.target : fact.source);
  return (
    <button
      type="button"
      className={`entity-fact${fact.derived ? " is-derived" : ""}${fact.state === "historical" ? " is-past" : ""}`}
      onClick={() => fact.otherUid && onNavigate(fact.otherUid)}
    >
      <span className="entity-fact-rel">{fact.direction === "in" ? "←" : "→"} {fact.relation.replaceAll("_", " ")}</span>
      <span className="entity-fact-name">{other}</span>
      {fact.interval && <span className="entity-fact-when">{fact.interval}</span>}
      {fact.derived && fact.derivedRule && (
        <span className="entity-fact-why">inferred · {fact.derivedRule.replaceAll("_", " ")}</span>
      )}
    </button>
  );
}

export default function EntityPanel({ node, providers, graphName, onClose, onNavigate }: EntityPanelProps) {
  const [detail, setDetail] = useState<EntityDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("view");

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setError(null);
    setTab("view");
    void getEntity(node.id, providers, graphName)
      .then((payload) => { if (!cancelled) setDetail(payload); })
      .catch((err: Error) => { if (!cancelled) setError(err.message); });
    return () => { cancelled = true; };
  }, [node.id, providers, graphName]);

  const derivedCount = detail?.derived.length ?? 0;
  const showDerived = derivedCount > 0;
  const viewLabel = viewTabLabel(detail?.entity.type || node.type);
  const body = storedBody(detail?.entity.body);
  const fields = detail?.entity.fields ?? {};
  const fieldEntries = Object.entries(fields);
  const sourceUrl = detail?.entity.url;

  return (
    <aside className={`selection-card entity-dock${tab === "view" ? " is-wide" : ""}`}>
      <button className="selection-close" onClick={onClose} aria-label="Close details">×</button>
      <span className="selection-kind">{node.type}</span>
      <h3>{node.label}</h3>
      {tab !== "view" && (
        <p>{detail?.entity.summary || node.summary || "This item is connected to the surrounding facts."}</p>
      )}
      {(detail?.entity.documents ?? node.documents).length > 0 && tab !== "view" && (
        <div className="selection-source">
          <BookOpenText size={13} /> Mentioned in {(detail?.entity.documents ?? node.documents).join(", ")}
        </div>
      )}

      <div className="entity-tabs" role="tablist">
        {(["view", "relations", "history", "derived"] as const)
          .filter((item) => item !== "derived" || showDerived)
          .map((item) => (
            <button
              key={item}
              type="button"
              role="tab"
              className={tab === item ? "active" : ""}
              onClick={() => setTab(item)}
            >
              {item === "view" ? viewLabel
                : item === "relations" ? "Relations"
                : item === "history" ? "History"
                : `Derived${derivedCount ? ` ${derivedCount}` : ""}`}
            </button>
          ))}
      </div>

      {!detail && !error && (
        <p className="entity-loading"><LoaderCircle className="spin" size={12} /> Loading facts…</p>
      )}
      {error && <p className="error-note">{error}</p>}

      {detail && tab === "view" && (
        <div className="entity-list entity-view">
          {(detail.entity.issueKey || detail.entity.status || fieldEntries.length > 0 || sourceUrl) && (
            <dl className="entity-fields">
              {detail.entity.issueKey && <><dt>Key</dt><dd>{detail.entity.issueKey}</dd></>}
              {detail.entity.status && <><dt>Status</dt><dd>{detail.entity.status}</dd></>}
              {fieldEntries.map(([key, value]) => (
                <span className="entity-field-pair" key={key}>
                  <dt>{key}</dt>
                  <dd>{value}</dd>
                </span>
              ))}
              {sourceUrl && (
                <>
                  <dt>URL</dt>
                  <dd>
                    <a href={sourceUrl} target="_blank" rel="noreferrer">
                      Open source <ExternalLink size={11} />
                    </a>
                  </dd>
                </>
              )}
            </dl>
          )}
          {body
            ? <pre className="entity-body">{body}</pre>
            : <p className="entity-empty">No stored text on this node — only the fields above, if any.</p>}
        </div>
      )}

      {detail && tab === "relations" && (
        <div className="entity-list">
          {detail.facts.length === 0 && detail.past.length === 0 && (
            <p className="entity-empty">No recorded facts yet.</p>
          )}
          {detail.facts.map((fact, index) => (
            <FactLine key={`${fact.relation}-${fact.otherUid}-${index}`} fact={fact} onNavigate={onNavigate} />
          ))}
          {detail.past.length > 0 && (
            <details className="entity-past">
              <summary>{detail.past.length} past</summary>
              {detail.past.map((fact, index) => (
                <FactLine key={`past-${fact.relation}-${fact.otherUid}-${index}`} fact={fact} onNavigate={onNavigate} />
              ))}
            </details>
          )}
        </div>
      )}

      {detail && tab === "history" && (
        <div className="entity-list">
          <p className="entity-hint">When we learned or revised this — not when it was true in the world.</p>
          {detail.history.length === 0 && <p className="entity-empty">No record-axis events yet.</p>}
          {detail.history.map((event, index) => (
            <div className="entity-history" key={`${event.at}-${event.relation}-${index}`}>
              <span className="entity-fact-rel">{event.kind} · {event.relation.replaceAll("_", " ")}</span>
              <span className="entity-fact-name">{event.direction === "in" ? event.source : event.target}</span>
              <span className="entity-fact-when">{pretty(event.at)}{event.interval ? ` · ${event.interval}` : ""}</span>
            </div>
          ))}
        </div>
      )}

      {detail && tab === "derived" && (
        <div className="entity-list">
          <p className="entity-hint">Inferred edges. A real Jira or GitHub fact always wins if they disagree.</p>
          {detail.derived.map((fact, index) => (
            <FactLine key={`der-${fact.relation}-${fact.otherUid}-${index}`} fact={fact} onNavigate={onNavigate} />
          ))}
        </div>
      )}
    </aside>
  );
}
