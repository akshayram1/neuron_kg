import {
  AlertTriangle,
  BrainCircuit,
  ChevronDown,
  CircleAlert,
  CornerDownLeft,
  DatabaseZap,
  ExternalLink,
  Sparkles,
  Trash2,
} from "lucide-react";
import { type FormEvent, useEffect, useRef, useState } from "react";
import type { ConversationMessage, Highlight } from "../types";
import MarkdownBody from "./MarkdownBody";

const SAMPLE_QUESTIONS = [
  "What is currently being worked on?",
  "Who was assigned in March 2026?",
  "What did we know about Argus in January 2026?",
];

interface ChatPanelProps {
  messages: ConversationMessage[];
  busy: boolean;
  error: string | null;
  activeHighlight: Highlight;
  onSend: (message: string) => void;
  onClear: () => void;
  onShowPath: (highlight: Highlight) => void;
  onOpenKnowledge: (uid: string, type: string) => void;
}

export default function ChatPanel({
  messages,
  busy,
  error,
  activeHighlight,
  onSend,
  onClear,
  onShowPath,
  onOpenKnowledge,
}: ChatPanelProps) {
  const [draft, setDraft] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, busy]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const value = draft.trim();
    if (!value || busy) return;
    setDraft("");
    onSend(value);
  };

  return (
    <section className="chat-panel" aria-label="Grounded agent">
      <div className="chat-scroll" ref={scrollRef}>
        <div className="chat-intro">
          <div className="chat-intro-top">
            <span className="eyebrow"><Sparkles size={13} /> Grounded agent</span>
            {messages.length > 1 && (
              <button className="clear-chat" type="button" onClick={onClear} disabled={busy}>
                <Trash2 size={12} /> Clear chat
              </button>
            )}
          </div>
          <h1>Ask what the company knows.</h1>
          <p>Answers come from the live knowledge graph, with the supporting Jira records and graph path attached.</p>
        </div>

        {messages.map((message) => {
          const highlighted = message.result?.highlight;
          const knowledgeCitations = message.result?.knowledgeCitations ?? [];
          const findingCitations = knowledgeCitations.filter((citation) => citation.type === "Finding");
          const wisdomCitations = knowledgeCitations.filter((citation) => citation.type === "Wisdom");
          const isActive = Boolean(
            highlighted
            && highlighted.nodes.every((id) => activeHighlight.nodes.includes(id))
            && highlighted.edges.every((id) => activeHighlight.edges.includes(id))
            && (highlighted.nodes.length || highlighted.edges.length),
          );
          return (
            <article className={`message ${message.role}`} key={message.id}>
              <div className="message-role">{message.role === "assistant" ? "Neuron" : "You"}</div>
              <div className="message-body">
                {message.role === "assistant"
                  ? <MarkdownBody text={message.content} />
                  : message.content}
              </div>
              {message.result && (
                <div className="answer-evidence">
                  <details>
                    <summary>
                      <span><DatabaseZap size={15} /> View lineage &amp; sources</span>
                      <span className="fact-count">
                        {knowledgeCitations.length > 0 && `${knowledgeCitations.length} graph ${knowledgeCitations.length === 1 ? "node" : "nodes"} · `}
                        {message.result.citations.length} {message.result.citations.length === 1 ? "source" : "sources"}
                      </span>
                      <ChevronDown className="chevron" size={15} />
                    </summary>
                    <div className="grounding-list citation-list">
                      {wisdomCitations.length > 0 && (
                        <section className="citation-section knowledge-section wisdom-source-section">
                          <h4><BrainCircuit size={12} /> Wisdom</h4>
                          {wisdomCitations.map((citation) => (
                            <button
                              type="button"
                              className={`knowledge-citation ${citation.type.toLowerCase()}`}
                              key={citation.uid}
                              onClick={() => onOpenKnowledge(citation.uid, citation.type)}
                            >
                              <BrainCircuit size={15} />
                              <span>
                                <strong>{citation.name}</strong>
                                <small>
                                  {citation.type}
                                  {citation.status && ` · ${citation.status.toUpperCase()}`}
                                  {citation.severity && ` · ${citation.severity.toUpperCase()}`}
                                  {citation.staleAt && ` · stale ${citation.staleAt.slice(0, 10)}`}
                                </small>
                              </span>
                            </button>
                          ))}
                        </section>
                      )}
                      {findingCitations.length > 0 && (
                        <section className="citation-section knowledge-section finding-source-section">
                          <h4><AlertTriangle size={12} /> Findings</h4>
                          {findingCitations.map((citation) => (
                            <button
                              type="button"
                              className="knowledge-citation finding"
                              key={citation.uid}
                              onClick={() => onOpenKnowledge(citation.uid, citation.type)}
                            >
                              <AlertTriangle size={15} />
                              <span>
                                <strong>{citation.name}</strong>
                                <small>
                                  {citation.status && citation.status.toUpperCase()}
                                  {citation.severity && ` · ${citation.severity.toUpperCase()}`}
                                  {citation.staleAt && ` · stale ${citation.staleAt.slice(0, 10)}`}
                                </small>
                              </span>
                            </button>
                          ))}
                        </section>
                      )}
                      <section className="citation-section source-section">
                        <h4><DatabaseZap size={12} /> Facts &amp; source evidence</h4>
                      {message.result.citations.length > 0 ? message.result.citations.map((citation) => (
                        citation.url ? (
                          <a href={citation.url} target="_blank" rel="noreferrer" key={citation.recordKey}>
                            <span>{citation.name}</span><ExternalLink size={12} />
                          </a>
                        ) : <span className="citation-item" key={citation.recordKey}>{citation.name}</span>
                      )) : <p className="muted-copy">No source record was attached to this answer.</p>}
                      </section>
                      <div className="write-note">Read-only answer — chat does not change the graph.</div>
                    </div>
                  </details>
                  {highlighted && (highlighted.nodes.length > 0 || highlighted.edges.length > 0) && (
                    <button
                      className={`show-path ${isActive ? "active" : ""}`}
                      type="button"
                      onClick={() => onShowPath(highlighted)}
                    >
                      {isActive ? "Showing answer path" : "Show answer path"}
                    </button>
                  )}
                </div>
              )}
            </article>
          );
        })}

        {busy && (
          <div className="thinking" aria-live="polite">
            <span /><span /><span /><p>Searching the knowledge graph…</p>
          </div>
        )}
        {error && <div className="error-banner"><CircleAlert size={16} /><span>{error}</span></div>}
      </div>

      <div className="composer-wrap">
        {messages.length <= 1 && (
          <div className="suggestions">
            {SAMPLE_QUESTIONS.map((question) => (
              <button key={question} onClick={() => onSend(question)} disabled={busy}>{question}</button>
            ))}
          </div>
        )}
        <form className="composer" onSubmit={submit}>
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                event.currentTarget.form?.requestSubmit();
              }
            }}
            rows={1}
            placeholder="Ask about a project, owner, decision, or issue…"
            aria-label="Message"
          />
          <button type="submit" disabled={!draft.trim() || busy} aria-label="Send message">
            <CornerDownLeft size={17} />
          </button>
        </form>
        <div className="composer-meta"><span>Enter to send</span><span>Read-only grounded answers</span></div>
      </div>
    </section>
  );
}
