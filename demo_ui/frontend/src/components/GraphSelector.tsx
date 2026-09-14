import { Check, LoaderCircle, Plus, X } from "lucide-react";
import { useState } from "react";
import type { GraphInfo } from "../types";

interface Props {
  graphs: GraphInfo[];
  value: string;
  loading: boolean;
  onSelect: (name: string) => void;
  onCreate: (name: string) => Promise<void>;
}

export default function GraphSelector({ graphs, value, loading, onSelect, onCreate }: Props) {
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    const name = draft.trim().toLowerCase();
    if (!name) return;
    setBusy(true); setError(null);
    try {
      await onCreate(name);
      setCreating(false); setDraft("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not create graph.");
    } finally {
      setBusy(false);
    }
  };

  if (creating) {
    return (
      <div className="graph-selector graph-selector-creating">
        <input
          autoFocus type="text" placeholder="new-graph-name" value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") void submit();
            if (event.key === "Escape") { setCreating(false); setDraft(""); setError(null); }
          }}
        />
        <button className="graph-selector-icon" onClick={() => void submit()} disabled={busy || !draft.trim()} title="Create graph">
          {busy ? <LoaderCircle className="spin" size={13} /> : <Check size={13} />}
        </button>
        <button className="graph-selector-icon" onClick={() => { setCreating(false); setDraft(""); setError(null); }} title="Cancel">
          <X size={13} />
        </button>
        {error && <span className="graph-selector-error">{error}</span>}
      </div>
    );
  }

  return (
    <div className="graph-selector" title="Switch between isolated graphs. Each has its own data, vectors, sync history and cost — connected accounts stay connected across all of them.">
      <select value={value} disabled={loading} onChange={(event) => onSelect(event.target.value)}>
        {graphs.map((item) => (
          <option value={item.name} key={item.name}>{item.displayName}</option>
        ))}
      </select>
      <button className="graph-selector-icon" onClick={() => setCreating(true)} title="Create a new, empty graph">
        <Plus size={13} />
      </button>
    </div>
  );
}
