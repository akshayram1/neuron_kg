"""Pure-logic tests for `scripts/build_chain_golden.py` -- leakage filter,
anchor/answer selection, subpath trimming, and dev/test group assignment.
None of these touch FalkorDB/Qdrant/OpenAI, so they run in CI."""

from __future__ import annotations

import random

from scripts.build_chain_golden import (
    ChainCandidate,
    assign_splits,
    leakage_free,
    select_anchor_answer,
    trim_subpath,
)


# --------------------------------------------------------------- leakage_free


def test_leakage_free_rejects_literal_name():
    nodes = [{"name": "Migrate rate limiting to Redis", "issue_key": "", "path": "", "sha": "", "pr_ref": ""}]
    question = "What did the Migrate rate limiting to Redis ticket lead to?"
    assert leakage_free(question, nodes) is False


def test_leakage_free_accepts_paraphrase():
    nodes = [{"name": "Migrate rate limiting to Redis", "issue_key": "", "path": "", "sha": "", "pr_ref": ""}]
    question = "Which file changed for the Redis rate-limiting migration?"
    assert leakage_free(question, nodes) is True


def test_leakage_free_rejects_issue_key():
    nodes = [{"name": "", "issue_key": "DS-1026", "path": "", "sha": "", "pr_ref": ""}]
    assert leakage_free("What is DS-1026 about?", nodes) is False
    assert leakage_free("What is that ticket about?", nodes) is True


def test_leakage_free_rejects_path():
    nodes = [{"name": "", "issue_key": "", "path": "nilus/src/projection_reader.py", "sha": "", "pr_ref": ""}]
    assert leakage_free("what does nilus/src/projection_reader.py do", nodes) is False
    assert leakage_free("what does the projection reader module do", nodes) is True


def test_leakage_free_rejects_sha_prefix():
    nodes = [{"name": "", "issue_key": "", "path": "", "sha": "cd10aa0f47d72c9a", "pr_ref": ""}]
    assert leakage_free("what did commit cd10aa0f change", nodes) is False
    assert leakage_free("what did that commit change", nodes) is True


def test_leakage_free_rejects_pr_ref():
    nodes = [{"name": "", "issue_key": "", "path": "", "sha": "", "pr_ref": "PR #42"}]
    assert leakage_free("what did PR #42 change", nodes) is False


def test_leakage_free_case_insensitive():
    nodes = [{"name": "Auth Service", "issue_key": "", "path": "", "sha": "", "pr_ref": ""}]
    assert leakage_free("what does the AUTH SERVICE depend on", nodes) is False


def test_leakage_free_ignores_short_values():
    # A one/two-char field (e.g. a truncated or missing value) must not
    # reject on common-word false positives.
    nodes = [{"name": "a", "issue_key": "", "path": "", "sha": "", "pr_ref": ""}]
    assert leakage_free("what happened after that", nodes) is True


def test_leakage_free_checks_every_node_not_just_the_answer():
    nodes = [
        {"name": "Onboarding Guide", "issue_key": "", "path": "", "sha": "", "pr_ref": ""},
        {"name": "Fix login bug", "issue_key": "", "path": "", "sha": "", "pr_ref": ""},
    ]
    question = "Which file did the commit implementing the Fix login bug ticket touch?"
    assert leakage_free(question, nodes) is False


# --------------------------------------------------------------- select_anchor_answer


def test_select_anchor_answer_always_at_least_two_hops():
    rng = random.Random(0)
    for node_count in (3, 4):
        for _ in range(200):
            anchor, answer = select_anchor_answer(node_count, rng)
            assert abs(anchor - answer) >= 2
            assert 0 <= anchor < node_count
            assert 0 <= answer < node_count


def test_select_anchor_answer_not_always_the_last_node():
    # A 4-node chain has (0,2) and (1,3) as non-last-answer options alongside
    # (0,3): over many draws we must see at least one non-last answer.
    rng = random.Random(1)
    answers = {select_anchor_answer(4, rng)[1] for _ in range(200)}
    assert answers - {3}, "answer should not always land on the chain's last node"


def test_select_anchor_answer_three_node_chain_has_two_hop_pair():
    rng = random.Random(2)
    anchor, answer = select_anchor_answer(3, rng)
    assert {anchor, answer} == {0, 2}


# --------------------------------------------------------------- trim_subpath


def test_trim_subpath_forward():
    candidate = ChainCandidate("p", ["a", "b", "c", "d"], ["R1", "R2", "R3"])
    uids, relations = trim_subpath(candidate, 0, 3)
    assert uids == ["a", "b", "c", "d"]
    assert relations == ["R1", "R2", "R3"]


def test_trim_subpath_middle_slice():
    candidate = ChainCandidate("p", ["a", "b", "c", "d"], ["R1", "R2", "R3"])
    uids, relations = trim_subpath(candidate, 0, 2)
    assert uids == ["a", "b", "c"]
    assert relations == ["R1", "R2"]


def test_trim_subpath_reversed_when_anchor_after_answer():
    candidate = ChainCandidate("p", ["a", "b", "c", "d"], ["R1", "R2", "R3"])
    uids, relations = trim_subpath(candidate, 3, 1)
    # walked answer(1)->anchor(3) in the stored path is b,c,d / R2,R3; walking
    # anchor->answer reverses both the node order and the relation order.
    assert uids == ["d", "c", "b"]
    assert relations == ["R3", "R2"]


def test_trim_subpath_length_invariant():
    candidate = ChainCandidate("p", ["a", "b", "c"], ["R1", "R2"])
    uids, relations = trim_subpath(candidate, 0, 2)
    assert len(uids) == len(relations) + 1


# --------------------------------------------------------------- assign_splits


def test_assign_splits_never_splits_a_group():
    group_keys = ["g1"] * 5 + ["g2"] * 3 + ["g3"] * 2
    rng = random.Random(3)
    assignment = assign_splits(group_keys, rng)
    # every occurrence of a group key must resolve to the same split
    assert len({assignment[k] for k in ["g1"]}) == 1
    assert set(assignment) == {"g1", "g2", "g3"}
    assert set(assignment.values()) <= {"dev", "test"}


def test_assign_splits_uses_both_splits_when_multiple_groups():
    group_keys = ["g1"] * 5 + ["g2"] * 5
    rng = random.Random(4)
    assignment = assign_splits(group_keys, rng)
    assert set(assignment.values()) == {"dev", "test"}


def test_assign_splits_single_group_is_not_an_error():
    group_keys = ["only"] * 10
    rng = random.Random(5)
    assignment = assign_splits(group_keys, rng)
    assert assignment == {"only": "dev"}


def test_assign_splits_roughly_seventy_thirty():
    # 10 same-size groups -> dev should land close to 70% of rows.
    group_keys = []
    for i in range(10):
        group_keys += [f"g{i}"] * 10
    rng = random.Random(6)
    assignment = assign_splits(group_keys, rng)
    dev_rows = sum(10 for k, v in assignment.items() if v == "dev")
    assert 50 <= dev_rows <= 90
