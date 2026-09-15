"""Fusion helpers in `graph.search` — pure functions, no live services.

These cover the two changes that fixed a real, reproduced retrieval failure:
a 95-byte empty `__init__.py` outranking the 19,564-byte `client.py` that
actually implements the thing the question named.
"""

from __future__ import annotations

from graph.search import fuse_fulltext_labels, interleave


def _hit(uid: str, score: float) -> tuple[str, str, str, float]:
    return (uid, f"name-{uid}", f"summary-{uid}", score)


def test_global_score_ignores_which_label_a_hit_came_from():
    """The bug this exists for: with per-label ranking, the best of 5
    Documents and the best of 264 SourceFiles both land at rank 0 and score
    identically, even though outranking 263 competitors is the far stronger
    signal."""
    per_label = {
        "Document": [_hit("doc-weak", 1.0)],
        "SourceFile": [_hit("file-strong", 9.0), _hit("file-mid", 4.0)],
    }
    ordered = [uid for uid, *_rest in fuse_fulltext_labels(per_label, "global_score")]
    assert ordered == ["file-strong", "file-mid", "doc-weak"]


def test_normalized_merge_scales_each_label_by_its_own_best():
    per_label = {
        "Document": [_hit("doc-best", 2.0), _hit("doc-second", 1.0)],
        "SourceFile": [_hit("file-best", 10.0), _hit("file-second", 1.0)],
    }
    ordered = [uid for uid, *_rest in fuse_fulltext_labels(per_label, "normalized_merge")]
    # Both labels' top hit normalizes to 1.0; the runner-up that is closer to
    # its own leader ranks above the one that is far behind its leader.
    assert ordered[:2] == ["doc-best", "file-best"] or ordered[:2] == ["file-best", "doc-best"]
    assert ordered.index("doc-second") < ordered.index("file-second")


def test_per_label_rank_preserves_the_old_round_robin():
    per_label = {
        "Document": [_hit("d1", 1.0), _hit("d2", 0.5)],
        "SourceFile": [_hit("f1", 9.0)],
    }
    ordered = [uid for uid, *_rest in fuse_fulltext_labels(per_label, "per_label_rank")]
    # f1 outscores d1 nine to one, yet round-robin still places d1 first --
    # this is the behaviour being replaced, pinned so the difference is
    # visible rather than argued about.
    assert ordered == ["d1", "f1", "d2"]


def test_fuse_rejects_an_unknown_strategy():
    try:
        fuse_fulltext_labels({"Document": [_hit("d1", 1.0)]}, "made-up")
    except ValueError as exc:
        assert "made-up" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an unknown strategy must not silently fall through")


def test_fuse_handles_labels_with_no_hits():
    per_label = {"Document": [], "SourceFile": [_hit("f1", 3.0)], "Commit": []}
    assert [uid for uid, *_rest in fuse_fulltext_labels(per_label, "global_score")] == ["f1"]
    assert [uid for uid, *_rest in fuse_fulltext_labels(per_label, "per_label_rank")] == ["f1"]


def test_interleave_alternates_channels_and_dedupes():
    content = [("a", 0.9), ("b", 0.8), ("c", 0.7)]
    name = [("x", 0.4), ("a", 0.3)]
    # `a` appears in both; it keeps its first (content-channel) position and
    # is not emitted twice.
    assert [uid for uid, _ in interleave(content, name)] == ["a", "x", "b", "c"]


def test_interleave_never_merges_by_score():
    """The name channel's scores are systematically higher (short query vs
    short text), so a score merge would let it take every top slot. Round
    robin is what stops that."""
    content = [("content-hit", 0.55)]
    name = [("name-hit", 0.95)]
    assert [uid for uid, _ in interleave(content, name)] == ["content-hit", "name-hit"]


def test_interleave_tolerates_an_empty_channel():
    assert interleave([], [("only", 0.5)]) == [("only", 0.5)]
    assert interleave() == []
