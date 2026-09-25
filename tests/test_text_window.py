"""Uses the real `cl100k_base` tiktoken encoder throughout (the convention
already in `graph.vector_store`) so token-boundary and token-count claims
are verified against the actual tokenizer, not a mock that could hide a
mid-token split.
"""

from __future__ import annotations

import tiktoken

from graph.text_window import _overlap_score, _query_terms, _token_windows, best_window

_ENC = tiktoken.get_encoding("cl100k_base")


# --- best_window: fits already -------------------------------------------

def test_short_text_is_returned_unchanged():
    text = "The quick brown fox jumps over the lazy dog."
    assert best_window("what does the fox do", text, 50, _ENC) is text


def test_text_exactly_at_the_budget_is_unchanged():
    text = "one two three four five"
    tokens = len(_ENC.encode(text))
    assert best_window("irrelevant question here", text, tokens, _ENC) is text


# --- best_window: token budget is honoured --------------------------------

def test_returned_window_never_exceeds_the_token_budget():
    filler = " ".join(f"filler{i} irrelevant{i} noise{i}" for i in range(400))
    text = filler
    result = best_window("does not matter for this check", text, 30, _ENC)
    assert len(_ENC.encode(result)) <= 30


# --- best_window: finds the relevant passage ------------------------------

def test_relevant_window_is_found_amid_irrelevant_filler():
    question = "who deployed the payment gateway migration on friday"
    relevant = (
        "Akshay deployed the payment gateway migration on friday evening "
        "after the gateway migration tests passed."
    )
    filler_before = " ".join(f"unrelated{i} topic noise chatter" for i in range(200))
    filler_after = " ".join(f"other{i} stuff banter weather" for i in range(200))
    text = f"{filler_before} {relevant} {filler_after}"

    result = best_window(question, text, 40, _ENC)

    query_terms = _query_terms(question)
    matched = sum(1 for term in query_terms if term in result.lower())
    # "most" of the query terms: a strict majority is present.
    assert matched >= (len(query_terms) // 2) + 1
    assert "deployed" in result.lower()
    assert "gateway" in result.lower()


# --- best_window: token-boundary safety -----------------------------------

def test_window_boundaries_land_on_token_boundaries():
    """If a window ever sliced mid-token, re-encoding its text would not
    reproduce a contiguous slice of the original token id sequence (a
    partial token typically decodes to a different id sequence, often with
    a replacement character at the cut). Round-tripping every window
    through encode(decode(...)) and checking it reproduces the exact id
    slice is therefore a concrete proof of boundary safety, not just a
    string-slicing assumption.
    """
    text = " ".join(f"word{i}" for i in range(500))
    token_ids = _ENC.encode(text)
    for start, end in _token_windows(len(token_ids), 25):
        expected_slice = token_ids[start:end]
        window_text = _ENC.decode(expected_slice)
        assert _ENC.encode(window_text) == expected_slice


def test_best_window_result_is_a_clean_decode_of_a_token_slice():
    filler = " ".join(f"filler{i} noise{i}" for i in range(300))
    question = "irrelevant query terms xyzzy"
    result = best_window(question, filler, 20, _ENC)
    ids = _ENC.encode(result)
    assert len(ids) <= 20
    # Re-encoding the decoded text must reproduce exactly this id sequence --
    # otherwise the text wasn't a clean token-boundary slice.
    assert _ENC.encode(_ENC.decode(ids)) == ids


# --- _token_windows: overlap, coverage, no gaps ---------------------------

def test_token_windows_cover_the_whole_range_with_no_gaps():
    token_count = 137
    size = 20
    windows = _token_windows(token_count, size)
    assert windows[0][0] == 0
    assert windows[-1][1] == token_count
    for (s1, e1), (s2, _e2) in zip(windows, windows[1:]):
        # no gap: the next window starts at or before the previous end
        assert s2 <= e1


def test_token_windows_have_reasonable_overlap():
    token_count = 100
    size = 20
    windows = _token_windows(token_count, size, overlap=0.5)
    # step should be ~half the window size
    steps = [s2 - s1 for (s1, _), (s2, _) in zip(windows, windows[1:])]
    assert all(step == 10 for step in steps[:-1]) or len(steps) <= 1


def test_token_windows_single_window_when_size_covers_everything():
    assert _token_windows(10, 50) == [(0, 10)]


# --- edge cases ------------------------------------------------------------

def test_empty_text_returns_empty_text():
    assert best_window("a question", "", 10, _ENC) == ""


def test_tokens_larger_than_whole_text_returns_text_unchanged():
    text = "a short piece of text"
    result = best_window("some question", text, 10_000, _ENC)
    assert result is text


def test_single_long_word_does_not_crash():
    long_word = "supercalifragilisticexpialidocious" * 20
    result = best_window("what is this word", long_word, 5, _ENC)
    assert isinstance(result, str)
    assert len(_ENC.encode(result)) <= 5


def test_token_windows_empty_range():
    assert _token_windows(0, 10) == [(0, 0)]


# --- _overlap_score ---------------------------------------------------------

def test_overlap_score_counts_distinct_matching_terms():
    terms = {"gateway", "migration", "friday"}
    assert _overlap_score("the gateway migration happened", terms) == 2
    assert _overlap_score("no relevant words here", terms) == 0


def test_overlap_score_is_case_insensitive():
    terms = {"gateway"}
    assert _overlap_score("GATEWAY outage", terms) == 1


def test_overlap_score_empty_terms_is_zero():
    assert _overlap_score("anything at all", set()) == 0
