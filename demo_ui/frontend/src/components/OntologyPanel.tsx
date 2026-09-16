import { AlertTriangle, Check, LoaderCircle, Undo2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  adoptOntology, getOntologyAdoptions, getOntologyPending, setAutoExtend, unadoptOntology,
} from "../api";
import type { OntologyAdoption, OntologyPending } from "../types";

interface Props {
  graphName: string;
  onChanged: () => void;
}

/**
 * What a sync left out of the graph, and the switch that lets it in.
 *
 * This is the one thing a user cannot see anywhere else: the graph looks
 * complete because the refused facts were never written. On this data 446
 * facts across 12 shapes sat outside the graph, and acting on them meant
 * hand-writing SQL against the ledger.
 *
 * The samples are not decoration. A threshold can only say a shape is common,
 * and the widest candidate here (`Document -DEFINES-> System`, 33 documents)
 * turned out to be 19 tautologies -- "DynamoDB defines DynamoDB". That was
 * caught by reading examples, which is the whole reason a human sees this
 * before adopting and why auto-extend is off by default.
 */
export default function OntologyPanel({ graphName, onChanged }: Props) {
  const [pending, setPending] = useState<OntologyPending | null>(null);
  const [adoptions, setAdoptions] = useState<OntologyAdoption[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [report, history] = await Promise.all([
        getOntologyPending(graphName),
        getOntologyAdoptions(graphName),
      ]);
      setPending(report);
      // Biggest first: the panel scrolls, and a 446-fact batch hidden under a
      // 10-fact one is the version of this list that misleads.
      setAdoptions([...history.adoptions].sort((a, b) => b.facts_expected - a.facts_expected));
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not read ontology status.");
    }
  }, [graphName]);

  useEffect(() => { void load(); }, [load]);

  const run = async (key: string, action: () => Promise<string | null>) => {
    setBusy(key); setError(null); setNote(null);
    try {
      setNote(await action());
      await load();
      onChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "That did not work.");
    } finally {
      setBusy(null);
    }
  };

  if (!pending) {
    return (
      <aside className="ontology-panel">
        <span className="eyebrow">Ontology</span>
        {error ? <p className="error-note">{error}</p> : <p className="ontology-muted">Loading…</p>}
      </aside>
    );
  }

  const nothingPending = pending.eligible_shapes === 0;
  // Below the bar but still refused -- worth showing, because "nothing is
  // eligible" and "nothing is being dropped" are very different states.
  const belowBar = pending.total_facts - pending.eligible_facts;

  return (
    <aside className="ontology-panel">
      <div className="ontology-head">
        <span className="eyebrow">Ontology</span>
        <label className="ontology-switch" title="Adopt eligible shapes automatically after every sync">
          <input
            type="checkbox"
            checked={pending.auto_extend}
            disabled={busy !== null}
            onChange={(event) =>
              void run("switch", async () => {
                const next = await setAutoExtend(graphName, event.target.checked);
                return next.auto_extend ? "Auto-extend on." : "Auto-extend off.";
              })
            }
          />
          Auto-extend
        </label>
      </div>

      {nothingPending ? (
        <p className="ontology-muted">
          Nothing is eligible.
          {belowBar > 0 && ` ${belowBar} facts are still refused, all below the ${pending.min_docs}-document bar.`}
        </p>
      ) : (
        <>
          <p className="ontology-lede">
            <strong>{pending.eligible_facts}</strong> facts across{" "}
            <strong>{pending.eligible_shapes}</strong> shapes are outside the graph.
            The extraction found them; the rulebook has no entry for them.
          </p>

          <ul className="ontology-shapes">
            {pending.eligible.map((shape) => (
              <li key={`${shape.subject_kind}-${shape.relation}-${shape.object_kind}`}>
                <code>
                  {shape.subject_kind} —{shape.relation}→ {shape.object_kind}
                </code>
                <span className="ontology-counts">
                  {shape.facts} facts · {shape.docs} docs
                </span>
                {shape.example && <span className="ontology-example">e.g. {shape.example}</span>}
              </li>
            ))}
          </ul>

          <button
            className="ontology-adopt"
            disabled={busy !== null}
            onClick={() =>
              void run("adopt", async () => {
                const result = await adoptOntology(graphName);
                if (!result.adopted) return result.skipped_reason;
                return `Adopted ${result.shapes.length} shapes. ${result.chunks_requeued} chunks queued — re-run the sync to extract them.`;
              })
            }
          >
            {busy === "adopt" ? <LoaderCircle className="spin" size={13} /> : <Check size={13} />}
            Adopt {pending.eligible_shapes} shapes
          </button>

          <p className="ontology-fineprint">
            <AlertTriangle size={12} /> Only the allow-list widens. Cardinality,
            transitivity and symmetry are never inferred from counts.
          </p>
        </>
      )}

      {adoptions.length > 0 && (
        <div className="ontology-history">
          <span className="eyebrow">Adopted</span>
          {adoptions.map((adoption) => (
            <div className="ontology-batch" key={adoption.batch_id}>
              <div>
                <code>{adoption.batch_id.slice(0, 8)}</code>
                <span className="ontology-counts">
                  {adoption.shapes.length} shapes ·{" "}
                  {adoption.edges > 0
                    ? `${adoption.edges} edges`
                    : `${adoption.facts_expected} facts queued — re-run the sync`}
                </span>
              </div>
              <button
                className="ontology-undo"
                disabled={busy !== null}
                title="Remove these axioms and every edge they let in"
                onClick={() =>
                  void run(adoption.batch_id, async () => {
                    const result = await unadoptOntology(graphName, adoption.batch_id);
                    return `Undone — ${result.edges_removed} edges removed.`;
                  })
                }
              >
                {busy === adoption.batch_id
                  ? <LoaderCircle className="spin" size={12} />
                  : <Undo2 size={12} />}
                Undo
              </button>
            </div>
          ))}
        </div>
      )}

      {note && <p className="ontology-note">{note}</p>}
      {error && <p className="error-note">{error}</p>}
    </aside>
  );
}
