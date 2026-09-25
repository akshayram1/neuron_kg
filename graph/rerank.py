"""Reranker bake-off (25-plan.md §2 "Phase 2 — Reranker bake-off: generic
cross-encoder vs Laya") — the common scorer interface (§2.1) plus a
shape-compatible Laya adapter stub.

Only §2.1's interface is built here: the `Reranker` protocol, a best-effort
`LayaReranker` stub (see its docstring), and `select_final`'s selection
*mechanism*. There is deliberately no cross-encoder implementation in this
module — the repo owner does not want the `sentence-transformers`/`torch`
dependency footprint that a generic pretrained cross-encoder would bring in
(removed 25 Sep 2026; see QUERIES.md for the earlier pinned-model note this
superseded). A scorer satisfying `Reranker` can be added back here, or
provided by a caller, whenever a concrete choice is made. Wiring a scorer
into `graph/chat.py` (§2.2), CPU/GPU serving benchmarks (§2.3), threshold
tuning (§2.4) and the query-type router (§2.5) are separate, later tasks and
are deliberately NOT done here.

Both scorer implementations accept the same `(question, candidate_window)`
shape (`RerankCandidate`) and return the same score/metadata shape
(`RerankScore`) — 25-plan.md §2.1: "Both implementations accept the same
(question, candidate_window) records and return a score plus model/version
metadata." Candidate windows are expected to already be token-bounded and
frozen by the caller before either model runs (via `graph.text_window
.best_window`, see `candidates_from_hits` below) — this module does not
mutate or re-window candidate text.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import tiktoken

from graph.search import SearchHit
from graph.text_window import best_window

logger = logging.getLogger("neuron.rerank")

# Same tiktoken convention as graph/vector_store.py (`tiktoken.get_encoding
# ("cl100k_base")`) and graph/text_window.py's own doctring, which names this
# exact split of responsibility: "this module is shared by both the
# context-packing path (graph/chat.py) and the reranker candidate path
# (graph/rerank.py)".
_encoding = tiktoken.get_encoding("cl100k_base")

# Shared default candidate-window token budget. Not from the current
# 25-plan.md §2.1 text (which leaves the number to the caller), but kept
# consistent with the one concrete number this plan has stated for a model
# in this family: Laya's own sequence cap is 512 tokens total, of which the
# question/instructions already consume some budget (personal_exp/laya/
# writeup.md: "the sequence is capped at 512 tokens... the question and its
# options must fit in 192 tokens"). 300 tokens for the node/document side
# leaves comfortable headroom for the question within that 512-token cap,
# and is a reasonable shared default for a generic cross-encoder too, most
# of which (including the one pinned below) also cap combined query+document
# length around 512 tokens.
DEFAULT_CANDIDATE_WINDOW_TOKENS = 300


@dataclass
class RerankCandidate:
    """One (question, candidate_window) record to be scored.

    `window` is the already token-bounded, already frozen text for this
    candidate — produced upstream by `graph.text_window.best_window` (see
    `candidates_from_hits`), never re-windowed here. `label`/`name`/`methods`
    are carried through only as minimal metadata a scorer or caller might
    want for logging/diagnostics; no scorer is required to use them.

    Mirrors `graph.search.SearchHit`'s plain-dataclass convention rather than
    a pydantic model — this repo's read-time value types (`SearchHit`,
    `TokenUsage`) are plain dataclasses, pydantic is reserved for chat.py's
    LLM structured-output schemas.
    """

    uid: str
    window: str
    label: str = ""
    name: str = ""
    methods: list[str] = field(default_factory=list)


@dataclass
class RerankScore:
    """One candidate's score plus the model/version that produced it.

    `model`/`model_version` are mandatory (not optional) on every score by
    design — 25-plan.md §2.1 requires "a score plus model/version metadata"
    on every candidate so the eval/calibration log this module writes (see
    `_log_scores`) is never ambiguous about which scorer produced which
    number, which matters once §2.3's fallback-on-failure path and §2.4's
    per-model tuning both read this same log.
    """

    uid: str
    score: float
    model: str
    model_version: str


@runtime_checkable
class Reranker(Protocol):
    """Common interface any scorer implementation (e.g. `LayaReranker`, or a
    future generic cross-encoder) satisfies — 25-plan.md §2.1.

    `typing.Protocol` rather than an ABC: this codebase has no existing
    precedent for swappable-backend classes at all (checked `graph/
    vector_store.py` and every `connectors/*/api.py` client — all are
    concrete classes, none define or subclass an ABC/abstract interface for
    a pluggable backend). Absent a precedent to match, Protocol is the
    simpler of the two options the task calls for: it lets both scorer
    classes satisfy this interface structurally (matching method signature
    is enough), with no shared base class, no `__init__` coupling, and no
    import-time dependency from one scorer implementation on another --
    matters concretely here since a future cross-encoder implementation
    would need `sentence-transformers`/`torch` importable and `LayaReranker`
    would need the separate `laya` package, and neither should have to
    import a base class module that drags in the other's dependencies.

    Batched, not one-at-a-time, per 25-plan.md §2.1's batching requirement
    for both implementations: a single `score()` call takes the *entire*
    candidate list for one question and returns one `RerankScore` per
    candidate, in the same order as the input list (deterministic,
    order-preserving -- see tests/test_rerank.py).
    """

    def score(self, question: str, candidates: list[RerankCandidate]) -> list[RerankScore]:
        """Score every candidate against `question` in one batched call.

        Must raise (not swallow) on scorer failure or timeout -- 25-plan.md
        §2.3: "On scorer failure or timeout, log the model/version and fall
        back to the Phase 1 RRF order." Building that fallback is a §2.2
        wiring concern (out of scope here); this method's job is only to
        fail loudly enough that a future caller can catch it.
        """
        ...


def candidates_from_hits(
    question: str,
    hits: list[SearchHit],
    *,
    tokens: int = DEFAULT_CANDIDATE_WINDOW_TOKENS,
    encoder=_encoding,
) -> list[RerankCandidate]:
    """Build frozen, token-bounded `RerankCandidate`s from `SearchHit`s.

    Thin convenience wrapper around `graph.text_window.best_window` — per
    that module's own docstring, it is explicitly shared by the
    context-packing path (`graph/chat.py`) and this reranker candidate path.
    Does not reimplement windowing; every candidate's `window` is exactly
    what `best_window` returns for `(question, hit.summary, tokens)`.

    Not itself part of the `Reranker` protocol -- it is the "freeze the
    candidate windows before either model runs" step 25-plan.md §2.1 calls
    for, shared by both implementations and reusable by whatever wires this
    into `graph/chat.py`'s `retrieve` (§2.2, out of scope here).
    """
    return [
        RerankCandidate(
            uid=hit.uid,
            window=best_window(question, hit.summary, tokens, encoder),
            label=hit.label,
            name=hit.name,
            methods=list(hit.methods),
        )
        for hit in hits
    ]


def _log_scores(scores: list[RerankScore]) -> None:
    """Log every candidate's score — 25-plan.md §2.1: "Log every candidate
    score for evaluation and calibration." This is deliberately every score,
    not just the ones that will survive `select_final`'s threshold: §2.4's
    future threshold tuning and calibration work needs the full distribution,
    not a pre-filtered one.

    Matches graph/expand.py's logging convention: `logger =
    logging.getLogger("neuron.X")`, structured `logger.info("%s: ...", ...)`
    messages naming the operation first.
    """
    for s in scores:
        logger.info(
            "rerank: uid=%s score=%.4f model=%s model_version=%s",
            s.uid, s.score, s.model, s.model_version,
        )


# ---------------------------------------------------------------------------
# Laya adapter -- best-effort stub, NOT full integration (see class
# docstring for exactly what was and wasn't discoverable).
#
# No generic cross-encoder implementation lives in this module (removed 25
# Sep 2026, per the repo owner: no `sentence-transformers`/`torch` dependency
# footprint). Variant C of Phase 2's bake-off ("B + a generic pretrained
# cross-encoder") therefore has no implementation to run yet -- either a
# lighter-weight scorer (e.g. an ONNX-exported model, ONNX Runtime only, no
# torch) or this exact model brought back deliberately would need to be
# chosen before that variant can be benchmarked. See QUERIES.md.
# ---------------------------------------------------------------------------


class LayaReranker:
    """Shape-compatible stub for a Laya `retrieval_relevance` reranker.

    This is intentionally NOT a working implementation. Per this task's
    scope, full Laya integration (loading the real `laya` package and the
    trained `laya-ingest` checkpoint, wiring it into retrieval) is
    25-plan.md §5.3/§10, a separate, larger task. This class exists only so
    `graph.rerank.Reranker` is verifiably shape-compatible with a Laya
    adapter and 25-plan.md §2.2's future wiring is not blocked by an
    interface that only fits one implementation.

    What was found in `/Users/akshaychame/personal_exp/laya` (read-only,
    nothing there was modified) and used to write this stub as precisely as
    possible without guessing:

    - `ingest/schema.py`'s `QUESTIONS["retrieval_relevance"]` is the exact,
      already-trained question this reranker must ask:
      `{"type": "noul", "instructions": "Is this graph node needed to
      answer the user's question?"}` -- reproduced verbatim below as
      `RETRIEVAL_RELEVANCE_QUESTION`, matching 25-plan.md §2.1's "the
      instruction text ... must match its trained schema exactly".
    - The same file's comment above that entry gives the trained state
      shape verbatim: `# state: {"question": "<user question>", "node":
      "<serialized graph node or fact>"}` -- i.e. one state dict per
      candidate of the form `{"question": question, "node":
      candidate.window}`.
    - `graph_view/reranker.py` (Laya's own, separate reranker prototype --
      not part of Neuron, not imported by this file) shows a real,
      *working* call shape against that same schema:
      `agent.predict_batch(states, {"retrieval_relevance": questions[
      "retrieval_relevance"], ...}, batch_size=self.batch_size,
      sort_by_length=len(states) > self.batch_size)`, and reads each
      prediction's score back as `prediction["answers"][
      "retrieval_relevance"]["noul"]`. That confirms Laya's public batching
      entry point is `agent.predict_batch`, not a loop over `agent.predict`.
    - `writeup.md` documents the agent construction as `laya.Agent(str(
      MODEL_DIR), device=...)`, where `MODEL_DIR` is a local directory
      containing `model.safetensors`/`questions.json`/`rl_agent_config
      .json` -- in the sibling repo, `personal_exp/laya/model/laya-ingest`.

    What was intentionally NOT guessed or reproduced here:

    - The `laya` package itself and the trained `model/laya-ingest`
      checkpoint are not part of this repo, not a Neuron dependency, and
      were not copied or vendored in -- doing so would be exactly the kind
      of full integration this task is explicitly not scoped to do (and
      `/Users/akshaychame/personal_exp/laya` was read-only for this task).
    - `laya.Agent`'s exact constructor signature, `predict_batch`'s full
      parameter set/return type, and any version-specific behavior were
      only ever observed via `graph_view/reranker.py`'s usage, never via
      the `laya` package's own source (it lives in an installed `.venv`,
      not `personal_exp/laya`'s own tracked files) -- reproducing that
      exact call as "working" code without being able to run or verify it
      against the real package would be the kind of wrong guess baked into
      a fake-working stub this task explicitly warns against.

    See QUERIES (final report) for what a real implementer still needs.
    """

    # Verbatim from personal_exp/laya/ingest/schema.py
    # QUESTIONS["retrieval_relevance"] (read-only reference, not imported --
    # that module lives outside this repo and pulls in the `laya` package).
    RETRIEVAL_RELEVANCE_QUESTION = {
        "type": "noul",
        "instructions": "Is this graph node needed to answer the user's question?",
    }

    def __init__(self, model_dir: str | None = None, device: str = "cpu",
                 batch_size: int = 8) -> None:
        self.model_dir = model_dir
        self.device = device
        self.batch_size = batch_size
        self._agent = None

    def score(self, question: str, candidates: list[RerankCandidate]) -> list[RerankScore]:
        raise NotImplementedError(
            "LayaReranker.score is a shape-only stub (25-plan.md §2.1's Laya "
            "adapter; full integration is §5.3/§10, out of this task's scope). "
            "A real implementation would: (1) lazily load "
            "`laya.Agent(self.model_dir, device=self.device)` once per process, "
            "matching personal_exp/laya/graph_view/reranker.py's "
            "`LayaRetriever._load`; (2) build one state dict per candidate as "
            "`{'question': question, 'node': candidate.window}`, matching "
            "personal_exp/laya/ingest/schema.py's own state-shape comment for "
            "`retrieval_relevance` verbatim; (3) call `self._agent.predict_batch("
            "states, {'retrieval_relevance': self.RETRIEVAL_RELEVANCE_QUESTION}, "
            "batch_size=self.batch_size, sort_by_length=len(states) > "
            "self.batch_size)`, matching personal_exp/laya/graph_view/"
            "reranker.py's real (working, in that sibling repo) call shape; "
            "(4) read each prediction's `answers['retrieval_relevance']['noul']` "
            "as the RerankScore.score. Not implemented here because the `laya` "
            "package and the trained model/laya-ingest checkpoint are not a "
            "Neuron dependency and live outside this repo (personal_exp/laya "
            "was read-only for this task) -- wiring them in for real is "
            "25-plan.md §5.3/§10, a separate task."
        )


# ---------------------------------------------------------------------------
# Final selector -- structure only. Threshold *value* is a placeholder;
# real tuning is 25-plan.md §2.4 and needs dev-split golden data.
# ---------------------------------------------------------------------------

# PLACEHOLDER, not a tuned value (25-plan.md §2.4 is out of scope here and
# needs real dev-split labels to choose this for real). `-inf` is a
# deliberately safe default: it disables the threshold filter entirely
# (every candidate "passes"), so before real tuning, `select_final`'s
# behavior is governed only by its structural caps (`min_keep`/`max_keep`),
# never by an unvalidated guessed cutoff silently discarding candidates a
# real threshold might have kept.
DEFAULT_THRESHOLD = float("-inf")
DEFAULT_MIN_KEEP = 3
DEFAULT_MAX_KEEP = 12


def select_final(
    scores: list[RerankScore],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    min_keep: int = DEFAULT_MIN_KEEP,
    max_keep: int = DEFAULT_MAX_KEEP,
) -> list[RerankScore]:
    """Apply 25-plan.md §2.1's final-selector *mechanism*: "a threshold
    tuned on dev, followed by diversity and hard resource caps (min_keep,
    max_keep, token budget). The count caps protect latency and prompt
    size; they do not define relevance."

    Order, faithfully as specified (not invented): (1) threshold filter,
    (2) diversity, (3) hard caps.

    1. Threshold filter: keep candidates with `score >= threshold`.
    2. Diversity: NO real diversity algorithm is implemented -- the plan
       names diversity as a step without specifying any concrete criterion
       to implement against (no similarity metric, no per-source/per-label
       cap, nothing). Implementing a real algorithm would mean inventing
       plan content that isn't there. This step is therefore a documented
       no-op placeholder: dedupe by uid only, order preserved. Flagged in
       QUERIES (final report).
    3. Hard caps: if fewer than `min_keep` candidates survive steps 1-2 but
       more distinct candidates exist overall, backfill from the next
       highest-scoring candidates (by uid, deduped) regardless of
       threshold, up to `min_keep` -- this is the same "never return
       nothing just because nothing cleared threshold" safety net
       25-plan.md's earlier Laya-reranker draft encoded as `kept or [h for
       _, h in scored[:min_keep]]`, generalized here to "top up to
       min_keep" rather than only "fall back when kept is completely
       empty". Then clamp to `max_keep`. `min_keep` can never manufacture
       candidates that don't exist -- if fewer than `min_keep` distinct
       uids were scored at all, every one of them is returned.

    Does NOT enforce a token budget: that cap needs each candidate's token
    count / the packed-context budget, neither of which `RerankScore`
    carries (it is score + model metadata only, per 25-plan.md §2.1's
    return shape). Token-budget trimming belongs to whichever caller
    ultimately packs the returned candidates' text into the chat context
    (25-plan.md §2.2/§1.2, out of scope here) -- flagged in QUERIES.
    """
    ranked_all = sorted(scores, key=lambda s: s.score, reverse=True)

    deduped_all: list[RerankScore] = []
    seen_all: set[str] = set()
    for s in ranked_all:
        if s.uid in seen_all:
            continue
        seen_all.add(s.uid)
        deduped_all.append(s)

    # Step 1 (threshold) + step 2 (diversity placeholder: dedupe by uid).
    # `deduped_all` is globally sorted descending, so filtering it by
    # `score >= threshold` yields exactly the leading prefix that passes --
    # i.e. `passed` below is always a prefix of `deduped_all`.
    passed = [s for s in deduped_all if s.score >= threshold]

    # Step 3 (hard caps): back-fill to min_keep from the same deduped,
    # globally-ranked pool if threshold left too few candidates.
    kept = passed if len(passed) >= min_keep else deduped_all[:min_keep]

    return kept[:max_keep]
