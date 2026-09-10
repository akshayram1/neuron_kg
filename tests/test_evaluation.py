from scripts.evaluate_retrieval import score_case, summarize


def test_retrieval_and_citation_metrics():
    result = score_case(
        {
            "query": "Which commit implements EAPD-1?",
            "expected_uids": ["commit-2"],
            "expected_citation_record_keys": ["github:1:commit:2"],
        },
        ["commit-1", "commit-2"],
        ["github:1:commit:2", "jira:1:work_item:1"],
    )
    summary = summarize([result])
    assert summary["recall_at_k"] == 1.0
    assert summary["precision_at_k"] == 0.5
    assert summary["mrr"] == 0.5
    assert summary["citation_precision"] == 0.5
    assert summary["citation_recall"] == 1.0


def test_empty_expectations_do_not_inflate_metrics():
    summary = summarize([score_case({"query": "placeholder"}, ["x"], [])])
    assert summary["retrieval_cases_scored"] == 0
    assert summary["recall_at_k"] is None
    assert summary["citation_cases_scored"] == 0
