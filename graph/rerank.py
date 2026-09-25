"""Laya relevance scoring and query-relative retrieval roles.

RRF supplies a broad, stable candidate order. Laya then answers its trained
``retrieval_relevance`` question for every token-bounded candidate window.
The policy in this module deliberately does more than a binary hard cut:
direct evidence can reach the answer, deterministic temporal candidates are
kept as temporal context, and a capped set of rejected candidates remains
available as graph-expansion bridges. A rejection is therefore local to one
query and one retrieval pass; it is never written back as a property of the
fact or node.

Laya currently has one trained retrieval question, not a trained role-choice
question. Roles are consequently assigned by deterministic policy around the
calibrated relevance probability instead of inventing an untrained prompt.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

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

# Laya caps the combined state/question sequence at 512 tokens. Keeping the
# node side at 300 leaves room for the user question and trained instruction.
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


class RetrievalRole(StrEnum):
    """A candidate's query-local job in the two-pass search."""

    DIRECT_EVIDENCE = "direct_evidence"
    BRIDGE_CANDIDATE = "bridge_candidate"
    TEMPORAL_CONTEXT = "temporal_context"
    IRRELEVANT = "irrelevant"


@dataclass(frozen=True)
class RerankDecision:
    uid: str
    score: float
    role: RetrievalRole
    model: str
    model_version: str


@runtime_checkable
class Reranker(Protocol):
    """Batched relevance scorer used by the retrieval policy.

    `typing.Protocol` rather than an ABC: this codebase has no existing
    precedent for swappable-backend classes at all (checked `graph/
    vector_store.py` and every `connectors/*/api.py` client — all are
    concrete classes, none define or subclass an ABC/abstract interface for
    a pluggable backend). Absent a precedent to match, Protocol is the
    simpler of the two options the task calls for: it lets both scorer
    Implementations satisfy this interface structurally, so tests and offline
    evaluation can inject a deterministic scorer without loading Laya.

    Batched, not one-at-a-time: a single `score()` call takes the *entire*
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
    for and is reusable by the chat retrieval path.
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
    score for evaluation and calibration." This includes direct, bridge and
    irrelevant candidates because threshold tuning needs the full score
    distribution, not a pre-filtered one.

    Matches graph/expand.py's logging convention: `logger =
    logging.getLogger("neuron.X")`, structured `logger.info("%s: ...", ...)`
    messages naming the operation first.
    """
    for s in scores:
        logger.info(
            "rerank: uid=%s score=%.4f model=%s model_version=%s",
            s.uid, s.score, s.model, s.model_version,
        )


class LayaReranker:
    """Lazy, process-local adapter for Laya's trained relevance question.

    The ``laya`` package is imported only when scoring starts, so Neuron can
    still run with ``NEURON_RERANK=off`` in a lightweight environment. A
    deployment enabling Laya must install that package and set
    ``LAYA_MODEL_DIR`` to a checkpoint directory.

    ``agent_factory`` is intentionally injectable: it keeps unit tests
    offline and also permits a deployment to supply an RPC-backed worker
    with the same Agent call shape.
    """

    # Verbatim from personal_exp/laya/ingest/schema.py
    # QUESTIONS["retrieval_relevance"] (read-only reference, not imported --
    # that module lives outside this repo and pulls in the `laya` package).
    RETRIEVAL_RELEVANCE_QUESTION = {
        "type": "noul",
        "instructions": "Is this graph node needed to answer the user's question?",
    }

    def __init__(
        self,
        model_dir: str | None = None,
        device: str | None = None,
        batch_size: int = 8,
        *,
        agent_factory: Callable[[str, str], object] | None = None,
    ) -> None:
        self.model_dir = model_dir or os.getenv("LAYA_MODEL_DIR")
        self.device = device or os.getenv("LAYA_DEVICE", "cpu")
        self.batch_size = batch_size
        self._agent_factory = agent_factory
        self._agent = None
        self._question = None
        self._model_version = None
        self._lock = threading.Lock()

    def _load(self):
        if self._agent is not None:
            return self._agent
        if not self.model_dir:
            raise RuntimeError("LAYA_MODEL_DIR is required when NEURON_RERANK=laya")
        model_dir = Path(self.model_dir).expanduser()
        question_path = model_dir / "questions.json"
        if not question_path.is_file():
            raise RuntimeError(f"Laya checkpoint is missing questions.json: {model_dir}")
        questions = json.loads(question_path.read_text())
        trained_question = questions.get("retrieval_relevance")
        if trained_question != self.RETRIEVAL_RELEVANCE_QUESTION:
            raise RuntimeError(
                "Laya retrieval_relevance schema does not match the trained Neuron contract"
            )

        if self._agent_factory is None:
            try:
                import laya
            except ImportError as exc:
                raise RuntimeError(
                    "NEURON_RERANK=laya requires the `laya` package in the Neuron runtime"
                ) from exc
            self._agent = laya.Agent(str(model_dir), device=self.device)
        else:
            self._agent = self._agent_factory(str(model_dir), self.device)

        self._question = trained_question
        version_material = question_path.read_bytes()
        config_path = model_dir / "rl_agent_config.json"
        if config_path.is_file():
            version_material += config_path.read_bytes()
        self._model_version = hashlib.sha256(version_material).hexdigest()[:12]
        return self._agent

    def score(self, question: str, candidates: list[RerankCandidate]) -> list[RerankScore]:
        if not candidates:
            return []
        states = [{"question": question, "node": candidate.window} for candidate in candidates]
        with self._lock:
            agent = self._load()
            predictions = agent.predict_batch(
                states,
                {"retrieval_relevance": self._question},
                batch_size=self.batch_size,
                sort_by_length=len(states) > self.batch_size,
            )
        if len(predictions) != len(candidates):
            raise RuntimeError(
                f"Laya returned {len(predictions)} predictions for {len(candidates)} candidates"
            )
        scores = [
            RerankScore(
                uid=candidate.uid,
                score=float(prediction["answers"]["retrieval_relevance"]["noul"]),
                model="laya/retrieval_relevance",
                model_version=self._model_version or "unknown",
            )
            for candidate, prediction in zip(candidates, predictions)
        ]
        _log_scores(scores)
        return scores


def assign_roles(
    scores: list[RerankScore],
    candidates: list[RerankCandidate],
    *,
    direct_threshold: float,
    bridge_threshold: float,
    bridge_limit: int,
) -> list[RerankDecision]:
    """Turn probabilities into query-local direct/bridge/temporal roles.

    Temporal and exact deterministic lanes survive even with a low semantic
    score. Rejected candidates above the bridge floor remain expansion seeds;
    if none clears that floor, the best ``bridge_limit`` rejected candidates
    are retained so a weak first hop cannot make a valid second hop
    unreachable.
    """
    by_uid = {candidate.uid: candidate for candidate in candidates}
    ranked = sorted(scores, key=lambda item: item.score, reverse=True)
    provisional: list[RerankDecision] = []
    rejected: list[RerankScore] = []
    for score in ranked:
        candidate = by_uid.get(score.uid)
        if candidate is None:
            continue
        methods = set(candidate.methods)
        if "time_window" in methods:
            role = RetrievalRole.TEMPORAL_CONTEXT
        elif methods & {"named_entity", "pair"} or score.score >= direct_threshold:
            role = RetrievalRole.DIRECT_EVIDENCE
        elif score.score >= bridge_threshold:
            role = RetrievalRole.BRIDGE_CANDIDATE
        else:
            role = RetrievalRole.IRRELEVANT
            rejected.append(score)
        provisional.append(RerankDecision(
            uid=score.uid, score=score.score, role=role,
            model=score.model, model_version=score.model_version,
        ))

    ranked_bridge_uids = [
        item.uid for item in provisional
        if item.role == RetrievalRole.BRIDGE_CANDIDATE
    ][:bridge_limit]
    bridge_count = len(ranked_bridge_uids)
    fallback_bridge_uids = {
        item.uid for item in rejected[:max(0, bridge_limit - bridge_count)]
    }
    allowed_bridge_uids = set(ranked_bridge_uids) | fallback_bridge_uids
    return [
        RerankDecision(
            uid=item.uid, score=item.score,
            role=(RetrievalRole.BRIDGE_CANDIDATE
                  if item.uid in allowed_bridge_uids
                  else RetrievalRole.IRRELEVANT
                  if item.role == RetrievalRole.BRIDGE_CANDIDATE
                  else item.role),
            model=item.model, model_version=item.model_version,
        )
        for item in provisional
    ]


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
