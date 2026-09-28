"""Measure human-review precision (and, where genuinely possible, Expected
Calibration Error) for Laya-backed review types recorded in a
`ConnectorLedger`'s `reviews` table.

Why this exists: 25-plan.md §10.4 ("Promotion rule (suggest -> auto)") says a
Laya decision type moves from `suggest` to `auto` only when, on >= 200
reviewed items, precision of the accepted class >= 0.95 and the ECE on the
hand-labelled set <= 0.05. QUERIES.md's "Laya promotion remains
measurement-gated" entry restates the same bar for entity/fact-update. No
script computing either number existed anywhere in this repo before this one.

Example:
    uv run python -m scripts.measure_review_precision --graph default
    uv run python -m scripts.measure_review_precision --graph default --type possibly_same_as
    uv run python -m scripts.measure_review_precision --graph default --results-md eval/results.md

--- Which review types this script can score, and why (read the real
payload-writing code before trusting this table -- it changes if that code
changes) ---

`ledger.list_reviews()` only ever returns rows from the generic `reviews`
table (`type`/`payload`/`identity`/`state`/...). Three producers write into
it with a payload this script knows how to read:

- `fact_update` (`graph/resolve_text_fact.py::_resolve_conflict`): payload
  keys are `action`, `old_fact_uid`, `old_from_uid`, `old_to_uid`,
  `old_rel_type`, `old_valid_at`, `new_from_uid`, `new_to_uid`,
  `new_rel_type`, `new_valid_at`, `new_source_time`, `new_evidence`,
  `new_source_record_key`. There is NO predicted-confidence field anywhere
  in that dict. This is not an oversight this script can route around --
  `graph/resolve_text_fact.py::classify_fact_update` (around line 169) calls
  `(classifier or LayaFactUpdateClassifier()).classify(...)` and receives
  `(choice, _confidence)` -- the leading underscore is real: the classifier's
  own confidence is computed and then immediately discarded, never returned
  by `classify_fact_update`, never reaching `_resolve_conflict`'s `payload`
  dict. ECE for `fact_update` is therefore NOT computable today. Making it
  computable needs (a) `classify_fact_update` to return the confidence
  alongside `choice` instead of throwing it away, (b) that value threaded
  through to `_resolve_conflict`, and (c) a new payload key there (e.g.
  `"fact_update_confidence"`) next to the existing keys above. This script
  will pick up that key automatically once it exists (see
  `REVIEW_TYPE_SPECS` below) -- but will not fabricate a number in the
  meantime, per this task's explicit instruction.

- `possibly_same_as` (`graph/semantic_pass.py::_resolve_semantic_entity`,
  rung 6 / `_rung6_laya_same_entity`): payload includes `"score"`, which
  *is* Laya's own predicted `same_entity` probability (`top_p` returned by
  `_rung6_laya_same_entity`, itself `LayaSameEntityClassifier.score_batch`'s
  output) -- a genuine model-predicted confidence. ECE is computable for
  this type.

- `duplicate_pair` (`graph/duplicate_collector.py::propose_duplicate_reviews`):
  payload includes `"score"` too, but per that module's own docstring this
  is `duplicate_score(...)`, a hand-weighted combination of
  `vector_similarity_score`/`lexical_overlap_score`/`shared_targets_score`/
  `shared_source_records_score` -- there is no Laya model anywhere in
  `graph/duplicate_collector.py` (confirmed by reading the whole file).
  It is a real number in a confidence-like range, so this script *can*
  mechanically bin and score it, and does -- but it is not a Laya-trained,
  calibrated probability, so an ECE computed on it answers a different
  question than the one §10.4 asks. This script reports it separately,
  labelled `heuristic_only`, and never lets it produce a `PASS` verdict.

`link_candidate` proposals are excluded entirely, on structural grounds, not
just a missing-field judgment call: they never go through `create_review` /
the `reviews` table at all. `graph/link_candidates.py` writes them via
`ledger.create_link_candidate(...)` into a *separate* `link_candidates`
table with its own `LinkCandidate` dataclass, `list_link_candidates`,
`approve_link_candidate` and `reject_link_candidate` -- there is no
`type`/`payload` JSON blob for `ledger.list_reviews()` to return, so this
script (which only reads via `list_reviews`, per this task's brief) cannot
see them at all. Even if it could, `graph/link_candidates.py::_propose`
stores `confidence=0.5` unconditionally for every row regardless of Laya's
real classifier confidence (Laya's confidence only gates *whether* a row is
written, per that function's own docstring) -- so there would be no genuine
per-item signal to bin even with a structural workaround.

25-plan.md §10.4's last sentence -- "Decision entity merges are permanently
excluded from auto-promotion" -- also matters here: both `possibly_same_as`
and `duplicate_pair` payloads carry a `"label"` key, and rows with
`label == "Decision"` can never legitimately reach `PASS` regardless of
measured numbers. This script reports how many decided reviews of a type
are Decision-label as an FYI count; it does not silently exclude them from
the precision/ECE math (removing real decided outcomes would distort the
count for every other label), but the printed verdict for a type carries a
note when Decision-label rows are present.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from connectors.core.ledger import ConnectorLedger, Review, ReviewState
from graph import multigraph, vector_store
from util.paths import DATA_DIR

# 25-plan.md §10.4's stated bar.
PRECISION_BAR = 0.95
ECE_BAR = 0.05

# A very large `list_reviews(limit=...)` so a real run's whole review
# history comes back in one call -- `list_reviews` has no "all" sentinel,
# just a default of 200 (demo-UI-page sized), which would silently truncate
# a promotion measurement to its newest 200 rows.
_LIST_ALL_LIMIT = 1_000_000

# Number of equal-width bins across [0, 1] for the standard ECE computation:
# |mean predicted confidence - actual approval rate| per bin, weighted by
# bin population, summed across bins.
DEFAULT_ECE_BINS = 10

# §10.4's own text: "on >= 200 reviewed items" is the plan's literal number
# for this exact measurement, not a generic statistics rule of thumb. This
# script defaults `--min-samples` to that number (not the smaller example a
# task brief can suggest) as the more conservative, spec-faithful choice --
# a lower default risks a premature PASS on less evidence than the plan
# actually requires. `--min-samples` remains overridable for a caller who
# wants to see preliminary numbers below that bar (always reported as
# INSUFFICIENT_DATA regardless of the numbers themselves).
DEFAULT_MIN_SAMPLES = 200


@dataclass(frozen=True)
class ReviewTypeSpec:
    """What this script knows about one `reviews.type` value's payload."""

    confidence_key: str | None
    is_model_probability: bool
    note: str
    label_key: str | None = None


REVIEW_TYPE_SPECS: dict[str, ReviewTypeSpec] = {
    "fact_update": ReviewTypeSpec(
        confidence_key=None,
        is_model_probability=False,
        note=(
            "graph/resolve_text_fact.py::_resolve_conflict's payload carries "
            "no confidence/probability field. Laya's own confidence IS "
            "computed one call earlier (classify_fact_update's "
            "'choice, _confidence = classifier.classify(...)') but is "
            "discarded there, never reaching this payload. ECE needs: (1) "
            "classify_fact_update to return the confidence instead of "
            "naming it '_confidence' and dropping it, (2) that value "
            "threaded to _resolve_conflict, (3) a new payload key there "
            "(e.g. 'fact_update_confidence') alongside the existing keys."
        ),
    ),
    "possibly_same_as": ReviewTypeSpec(
        confidence_key="score",
        is_model_probability=True,
        note=(
            "graph/semantic_pass.py::_rung6_laya_same_entity's returned "
            "top_p (LayaSameEntityClassifier.score_batch's own predicted "
            "same_entity probability) is stored verbatim as payload['score']."
        ),
        label_key="label",
    ),
    "duplicate_pair": ReviewTypeSpec(
        confidence_key="score",
        is_model_probability=False,
        note=(
            "graph/duplicate_collector.py::propose_duplicate_reviews stores "
            "payload['score'] = duplicate_score(...), a hand-weighted "
            "combination of vector/lexical/shared-target/shared-source-"
            "record signals. No Laya model is involved anywhere in that "
            "module (confirmed by reading it in full) -- this is a real "
            "number in a confidence-like range, not a Laya-calibrated "
            "probability, so an ECE computed on it is reported separately "
            "and never produces a PASS verdict."
        ),
        label_key="label",
    ),
}

# Documented, not queried: `link_candidate` proposals live in a structurally
# separate `link_candidates` table (`LinkCandidate`, `list_link_candidates`,
# `approve_link_candidate`/`reject_link_candidate`), never in `reviews`, so
# `ledger.list_reviews()` cannot see them regardless of payload shape. See
# this module's docstring for the full reasoning.
EXCLUDED_TYPES: dict[str, str] = {
    "link_candidate": (
        "graph/link_candidates.py writes these into the separate "
        "link_candidates table via ledger.create_link_candidate(...), not "
        "ledger.create_review(...) -- there is no type/payload row in "
        "`reviews` for this script's ledger.list_reviews() call to return. "
        "Even structurally reachable, every row's stored confidence is a "
        "hardcoded 0.5 regardless of Laya's real classifier score (Laya's "
        "confidence only gates whether a row is written at all), so there "
        "would be no genuine per-item signal to calibrate against."
    ),
}


@dataclass
class TypeResult:
    review_type: str
    spec: ReviewTypeSpec | None
    approved: int = 0
    rejected: int = 0
    pending: int = 0
    decision_label_decided: int = 0
    ece_value: float | None = None
    ece_bins: list[dict[str, Any]] = field(default_factory=list)
    ece_scored: int = 0
    ece_skipped_missing: int = 0
    ece_status: str = "no_confidence_field"  # computed | heuristic_only | no_confidence_field | insufficient_confidence_values
    verdict: str = "INSUFFICIENT_DATA"

    @property
    def decided_total(self) -> int:
        return self.approved + self.rejected

    @property
    def precision(self) -> float | None:
        if self.decided_total == 0:
            return None
        return self.approved / self.decided_total


def fetch_decided_reviews(
    ledger: ConnectorLedger, review_type: str, *, limit: int = _LIST_ALL_LIMIT,
) -> list[Review]:
    """All `approved`/`rejected` rows of `review_type`, newest first
    (`list_reviews`'s own order) -- `pending` rows are fetched separately
    only for the FYI pending count, never mixed into precision/ECE math."""
    rows = ledger.list_reviews(type=review_type, limit=limit)
    return [r for r in rows if r.state in (str(ReviewState.APPROVED), str(ReviewState.REJECTED))]


def discover_types(ledger: ConnectorLedger, *, limit: int = _LIST_ALL_LIMIT) -> list[str]:
    """Every distinct `type` present among decided reviews in this ledger,
    sorted. Used when `--type` is not given, so a future review type this
    script has no `REVIEW_TYPE_SPECS` entry for still gets a precision-only
    row (with an honest 'unrecognized type' note) instead of silently being
    left out of the report."""
    rows = ledger.list_reviews(limit=limit)
    return sorted({
        r.type for r in rows if r.state in (str(ReviewState.APPROVED), str(ReviewState.REJECTED))
    })


def compute_ece(
    pairs: list[tuple[float, bool]], *, n_bins: int = DEFAULT_ECE_BINS,
) -> tuple[float, list[dict[str, Any]]]:
    """Standard binned Expected Calibration Error.

    `pairs` is `(predicted_confidence, was_approved)` for every decided
    review that has a numeric confidence value. Bins are equal-width over
    [0, 1]; a bin with zero members contributes nothing (zero weight, not
    a NaN/zero-filled term). Returns `(ece, per_bin_detail)` -- the detail
    lets a caller/test hand-verify the aggregate against the same per-bin
    numbers a human would compute by hand.
    """
    bins: list[list[tuple[float, bool]]] = [[] for _ in range(n_bins)]
    for confidence, approved in pairs:
        clamped = min(max(confidence, 0.0), 1.0)
        index = min(int(clamped * n_bins), n_bins - 1)
        bins[index].append((confidence, approved))

    total = len(pairs)
    ece = 0.0
    detail: list[dict[str, Any]] = []
    for i, members in enumerate(bins):
        lo, hi = i / n_bins, (i + 1) / n_bins
        if not members:
            detail.append({
                "bin": [lo, hi], "count": 0, "mean_confidence": None, "approval_rate": None,
                "gap": None,
            })
            continue
        mean_confidence = sum(c for c, _ in members) / len(members)
        approval_rate = sum(1.0 for _, a in members if a) / len(members)
        gap = abs(mean_confidence - approval_rate)
        weight = len(members) / total
        ece += weight * gap
        detail.append({
            "bin": [lo, hi], "count": len(members), "mean_confidence": mean_confidence,
            "approval_rate": approval_rate, "gap": gap,
        })
    return ece, detail


def score_type(
    review_type: str, decided: list[Review], pending_count: int, *, min_samples: int,
) -> TypeResult:
    spec = REVIEW_TYPE_SPECS.get(review_type)
    result = TypeResult(review_type=review_type, spec=spec, pending=pending_count)
    result.approved = sum(1 for r in decided if r.state == str(ReviewState.APPROVED))
    result.rejected = sum(1 for r in decided if r.state == str(ReviewState.REJECTED))

    label_key = spec.label_key if spec else None
    if label_key:
        result.decision_label_decided = sum(
            1 for r in decided if r.payload.get(label_key) == "Decision"
        )

    confidence_key = spec.confidence_key if spec else None
    if confidence_key is None:
        result.ece_status = "no_confidence_field"
    else:
        pairs: list[tuple[float, bool]] = []
        missing = 0
        for r in decided:
            raw = r.payload.get(confidence_key)
            if raw is None or not isinstance(raw, (int, float)):
                missing += 1
                continue
            pairs.append((float(raw), r.state == str(ReviewState.APPROVED)))
        result.ece_skipped_missing = missing
        if not pairs:
            result.ece_status = "insufficient_confidence_values"
        else:
            ece_value, bins = compute_ece(pairs)
            result.ece_value = ece_value
            result.ece_bins = bins
            result.ece_scored = len(pairs)
            result.ece_status = "computed" if spec.is_model_probability else "heuristic_only"

    result.verdict = _verdict(result, min_samples=min_samples)
    return result


def _verdict(result: TypeResult, *, min_samples: int) -> str:
    if result.decided_total < min_samples:
        return "INSUFFICIENT_DATA"
    precision = result.precision
    precision_ok = precision is not None and precision >= PRECISION_BAR
    if result.ece_status == "computed":
        ece_ok = result.ece_value is not None and result.ece_value <= ECE_BAR
        return "PASS" if precision_ok and ece_ok else "FAIL"
    if result.ece_status == "no_confidence_field":
        return "ECE_NOT_COMPUTABLE"
    if result.ece_status == "heuristic_only":
        # A heuristic combiner score is not §10.4's ECE, no matter how it
        # scores -- never let it produce PASS.
        return "ECE_NOT_COMPUTABLE"
    return "ECE_NOT_COMPUTABLE"  # insufficient_confidence_values


def _fmt(value: float | None, digits: int = 4) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def render_report(graph_name: str, min_samples: int, results: list[TypeResult]) -> str:
    lines = [
        f"Review precision/ECE measurement — graph={graph_name!r}, "
        f"min_samples={min_samples} (25-plan.md §10.4 bar: "
        f"precision >= {PRECISION_BAR}, ECE <= {ECE_BAR})",
        "",
    ]
    if not results:
        lines.append("No decided (approved/rejected) reviews found for this graph.")
        return "\n".join(lines)

    for r in results:
        lines.append(f"## {r.review_type}")
        lines.append(
            f"  decided: {r.decided_total} (approved={r.approved}, rejected={r.rejected}, "
            f"pending={r.pending})"
        )
        precision_str = _fmt(r.precision)
        lines.append(f"  precision: {precision_str} from {r.decided_total} decided reviews")
        if r.decided_total < 30:
            lines.append(
                "  ⚠ tiny sample — a precision computed from this few reviews is not "
                "meaningful evidence of anything, regardless of its value."
            )
        if r.spec is None:
            lines.append(
                "  confidence field: none known — unrecognized review type; add a "
                "REVIEW_TYPE_SPECS entry if this type's payload carries a predicted-"
                "confidence key."
            )
        else:
            lines.append(f"  confidence field: {r.spec.note}")
        if r.ece_status == "computed":
            lines.append(
                f"  ECE: {_fmt(r.ece_value)} (Laya-predicted confidence, "
                f"{r.ece_scored} scored, {r.ece_skipped_missing} skipped for missing value)"
            )
        elif r.ece_status == "heuristic_only":
            lines.append(
                f"  ECE (informational only, heuristic combiner score, NOT a Laya "
                f"probability, does not count toward §10.4): {_fmt(r.ece_value)} "
                f"({r.ece_scored} scored, {r.ece_skipped_missing} skipped for missing value)"
            )
        elif r.ece_status == "insufficient_confidence_values":
            lines.append(
                "  ECE: not computed — a confidence field is expected for this type "
                "but no decided review actually carried a numeric value for it."
            )
        else:
            lines.append("  ECE: not computable — no confidence field in this payload today.")
        if r.decision_label_decided:
            lines.append(
                f"  note: {r.decision_label_decided} of {r.decided_total} decided reviews "
                "are label=Decision — 25-plan.md §10.4 permanently excludes Decision "
                "entity merges from auto-promotion regardless of these numbers."
            )
        lines.append(f"  verdict: {r.verdict}")
        lines.append("")

    if EXCLUDED_TYPES:
        lines.append("## excluded from this report")
        for name, reason in EXCLUDED_TYPES.items():
            lines.append(f"  {name}: {reason}")
        lines.append("")

    return "\n".join(lines)


def to_json_payload(graph_name: str, min_samples: int, results: list[TypeResult]) -> dict[str, Any]:
    return {
        "graph": graph_name,
        "min_samples": min_samples,
        "precision_bar": PRECISION_BAR,
        "ece_bar": ECE_BAR,
        "generated_at": datetime.now(UTC).isoformat(),
        "types": {
            r.review_type: {
                "approved": r.approved,
                "rejected": r.rejected,
                "pending": r.pending,
                "decided_total": r.decided_total,
                "precision": r.precision,
                "decision_label_decided": r.decision_label_decided,
                "ece_status": r.ece_status,
                "ece_value": r.ece_value,
                "ece_scored": r.ece_scored,
                "ece_skipped_missing": r.ece_skipped_missing,
                "ece_bins": r.ece_bins,
                "is_model_probability": r.spec.is_model_probability if r.spec else None,
                "confidence_key": r.spec.confidence_key if r.spec else None,
                "note": r.spec.note if r.spec else "unrecognized review type",
                "verdict": r.verdict,
            }
            for r in results
        },
        "excluded_types": EXCLUDED_TYPES,
    }


def append_results_md(path: Path, graph_name: str, min_samples: int, results: list[TypeResult]) -> None:
    header = (
        "# eval/results.md — review precision/ECE runs\n\n"
        "One section per `scripts/measure_review_precision.py` run, oldest "
        "first. Precision/ECE numbers are never averaged across graphs or "
        "review types into one headline figure.\n\n---\n\n"
    )
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(header)
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"## review precision/ECE — {now}",
        "",
        f"- graph: `{graph_name}`",
        f"- min_samples: {min_samples} (§10.4 bar: precision >= {PRECISION_BAR}, ECE <= {ECE_BAR})",
        "",
        "| type | decided | approved | rejected | precision | ece | ece_status | verdict |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.review_type} | {r.decided_total} | {r.approved} | {r.rejected} | "
            f"{_fmt(r.precision)} | {_fmt(r.ece_value)} | {r.ece_status} | {r.verdict} |"
        )
    lines.append("")
    with path.open("a") as f:
        f.write("\n".join(lines))
        f.write("\n---\n\n")


def resolve_target(graph_name: str) -> multigraph.GraphTarget:
    """Same resolution `scripts/evaluate_retrieval.py` and the API routes
    use, so this measurement reads the exact ledger file a given graph name
    actually writes to."""
    return multigraph.resolve(
        graph_name, data_dir=DATA_DIR,
        base_falkor_name=os.getenv("FALKOR_GRAPH", "neuron"),
        base_collection=vector_store.COLLECTION,
    )


def run(graph_name: str, *, review_type: str | None, min_samples: int) -> list[TypeResult]:
    target = resolve_target(graph_name)
    ledger = ConnectorLedger(target.ledger_path)

    types = [review_type] if review_type else discover_types(ledger)
    results: list[TypeResult] = []
    for t in types:
        decided = fetch_decided_reviews(ledger, t)
        pending_count = len(ledger.list_reviews(type=t, state=ReviewState.PENDING, limit=_LIST_ALL_LIMIT))
        results.append(score_type(t, decided, pending_count, min_samples=min_samples))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--graph", default=multigraph.DEFAULT_GRAPH_NAME)
    parser.add_argument(
        "--type", default=None,
        help="Restrict to one review type (e.g. fact_update, possibly_same_as, "
        "duplicate_pair). Default: every type with decided reviews in this ledger.",
    )
    parser.add_argument(
        "--min-samples", type=int, default=DEFAULT_MIN_SAMPLES,
        help=f"Minimum decided (approved+rejected) reviews for a type's verdict to be "
        f"anything other than INSUFFICIENT_DATA. Default {DEFAULT_MIN_SAMPLES}, "
        "matching 25-plan.md §10.4's own '>= 200 reviewed items' text.",
    )
    parser.add_argument(
        "--results-md", type=Path, default=None,
        help="Append a markdown summary section to this file (created if missing), "
        "matching scripts/evaluate_retrieval.py's --results-md convention.",
    )
    args = parser.parse_args()

    results = run(args.graph, review_type=args.type, min_samples=args.min_samples)

    print(render_report(args.graph, args.min_samples, results))
    print(json.dumps(to_json_payload(args.graph, args.min_samples, results), indent=2, sort_keys=True))

    if args.results_md:
        append_results_md(args.results_md, args.graph, args.min_samples, results)
        print(f"results.md: {args.results_md}")


if __name__ == "__main__":
    main()
