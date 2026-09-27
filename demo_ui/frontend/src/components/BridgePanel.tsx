import { Activity, Check, GitMerge, HeartPulse, LoaderCircle, Network, X, XCircle } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  decideLinkCandidate, decideReview, getDashboard, getLinkCandidates, getReviews,
} from "../api";
import type { DashboardData, LinkCandidateItem, ReviewItem } from "../types";

interface Props {
  open: boolean;
  graphName: string;
  onClose: () => void;
  onChanged: () => void;
}

type QueueItem =
  | { kind: "review"; item: ReviewItem }
  | { kind: "link"; item: LinkCandidateItem };

const TYPE_LABELS: Record<string, string> = {
  possibly_same_as: "Possible duplicate",
  fact_update: "Fact update",
  duplicate_pair: "Duplicate pair",
  link_candidate: "Candidate link",
};

function compactPayload(payload: Record<string, unknown>): string {
  const preferred = ["statement", "claim", "reason", "subject_uid", "object_uid", "fact_uid"];
  const entries = preferred.filter((key) => payload[key] != null).map((key) => `${key.replaceAll("_", " ")}: ${String(payload[key])}`);
  if (entries.length) return entries.join(" · ");
  return Object.entries(payload).slice(0, 4).map(([key, value]) => `${key.replaceAll("_", " ")}: ${String(value)}`).join(" · ");
}

export default function BridgePanel({ open, graphName, onClose, onChanged }: Props) {
  const [reviews, setReviews] = useState<ReviewItem[]>([]);
  const [links, setLinks] = useState<LinkCandidateItem[]>([]);
  const [dashboard, setDashboard] = useState<DashboardData | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!open) return;
    try {
      const [reviewRows, linkRows, health] = await Promise.all([
        getReviews(graphName), getLinkCandidates(graphName), getDashboard(graphName),
      ]);
      setReviews(reviewRows.reviews);
      setLinks(linkRows.candidates);
      setDashboard(health);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load review and health data.");
    }
  }, [open, graphName]);

  useEffect(() => { void load(); }, [load]);

  const queue = useMemo<QueueItem[]>(() => [
    ...reviews.map((item): QueueItem => ({ kind: "review", item })),
    ...links.map((item): QueueItem => ({ kind: "link", item })),
  ].sort((a, b) => b.item.created_at.localeCompare(a.item.created_at)), [reviews, links]);

  const decide = async (entry: QueueItem, decision: "approve" | "reject") => {
    const key = `${entry.kind}-${entry.item.id}`;
    setBusy(key); setError(null);
    try {
      if (entry.kind === "review") await decideReview(graphName, entry.item.id, decision);
      else await decideLinkCandidate(graphName, entry.item.id, decision);
      await load();
      onChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The review action failed.");
    } finally {
      setBusy(null);
    }
  };

  if (!open) return null;
  const violations = dashboard?.hygiene.find((item) => item.label === "cardinality_violation")?.isolated ?? 0;
  const disputes = dashboard?.hygiene.find((item) => item.label === "open_dispute")?.isolated ?? 0;
  const isolated = dashboard?.hygiene.filter((item) => !["cardinality_violation", "open_dispute"].includes(item.label)) ?? [];

  return <div className="connector-overlay" role="presentation" onMouseDown={onClose}>
    <aside className="connector-drawer bridge-drawer" role="dialog" aria-modal="true" aria-label="Review queue and graph health" onMouseDown={(event) => event.stopPropagation()}>
      <div className="connector-header">
        <div className="bridge-logo"><GitMerge size={19} /></div>
        <div><span className="eyebrow">Graph operations</span><h2>Review &amp; health</h2></div>
        <button className="connector-close" onClick={onClose} aria-label="Close"><X size={17} /></button>
      </div>
      <p className="connector-copy">Approve one proposal to apply one audited graph action. Rejections remain cached.</p>
      {error && <div className="connector-error">{error}</div>}

      <section className="bridge-health">
        <div className="connection-list-title"><span><HeartPulse size={12} /> Health</span><span>{graphName}</span></div>
        <div className="bridge-health-grid">
          <div className={violations ? "bad" : "good"}><strong>{violations}</strong><span>Cardinality violations</span></div>
          <div><strong>{disputes}</strong><span>Open disputes</span></div>
          <div><strong>{queue.length}</strong><span>Pending reviews</span></div>
          <div><strong>{dashboard?.merges.length ?? 0}</strong><span>Recent merges</span></div>
        </div>
        {isolated.length > 0 && <div className="bridge-isolation">
          {isolated.map((item) => <div key={item.label}>
            <span>{item.label}</span><div><i style={{ width: `${Math.min(100, item.ratio * 100)}%` }} /></div><strong>{Math.round(item.ratio * 100)}%</strong>
          </div>)}
        </div>}
        {dashboard?.resolution.length ? <div className="bridge-resolution">
          <span>Latest resolution</span>
          <p>{dashboard.resolution.map((item) => `${item.label}/${item.resolved_by}: ${item.count}`).join(" · ")}</p>
        </div> : null}
        {dashboard?.latest_eval && <div className="bridge-eval"><Activity size={12} /><div><strong>{dashboard.latest_eval.title}</strong><span>{Object.entries(dashboard.latest_eval.metrics).slice(0, 3).map(([key, value]) => `${key}: ${value}`).join(" · ")}</span></div></div>}
      </section>

      <section className="bridge-queue">
        <div className="connection-list-title"><span>Pending review</span><span>{queue.length}</span></div>
        {!queue.length && <div className="connection-empty">The queue is clear.</div>}
        {queue.map((entry) => {
          const key = `${entry.kind}-${entry.item.id}`;
          const isBusy = busy === key;
          const type = entry.kind === "link" ? "link_candidate" : entry.item.type;
          const copy = entry.kind === "link"
            ? `${entry.item.from_uid} —${entry.item.relation}→ ${entry.item.to_uid}`
            : compactPayload(entry.item.payload);
          return <article className="bridge-review-card" key={key}>
            <div className="bridge-review-head"><span>{TYPE_LABELS[type] ?? type.replaceAll("_", " ")}</span><em>#{entry.item.id}</em></div>
            <p>{copy || "Review the attached proposal."}</p>
            {entry.kind === "link" && <small>{entry.item.derived_rule.replaceAll("_", " ")} · confidence {entry.item.confidence.toFixed(2)}</small>}
            <div className="bridge-review-actions">
              <button className="reject" disabled={busy !== null} onClick={() => void decide(entry, "reject")}><XCircle size={13} /> Reject</button>
              <button className="approve" disabled={busy !== null} onClick={() => void decide(entry, "approve")}>{isBusy ? <LoaderCircle className="spin" size={13} /> : <Check size={13} />} Approve</button>
            </div>
          </article>;
        })}
      </section>
    </aside>
  </div>;
}
