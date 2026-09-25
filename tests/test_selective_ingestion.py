from connectors.core.chunking.models import SourceChunk
from connectors.core.ledger import ConnectorLedger, SemanticStatus
from graph.selective_ingestion import has_pending, selective_chunk_writes


def _chunk(text: str) -> SourceChunk:
    return SourceChunk("entity", "chunk-1", 0, text, 10, "hash", "semantic_structural")


def test_exact_reference_only_skips_llm():
    writes = selective_chunk_writes(
        [_chunk("[SOURCE]\nKind: PullRequest\nName: PR #7\n\nImplements AUTH-42.")],
        resolved_anchors=frozenset({"AUTH-42".casefold()}),
    )

    assert not has_pending(writes)
    assert writes[0].status == SemanticStatus.DONE
    assert writes[0].llm_text is None
    assert writes[0].resolution_status == "deterministic"


def test_partial_chunk_sends_only_unresolved_claim():
    writes = selective_chunk_writes(
        [_chunk(
            "[SOURCE]\nKind: Document\nName: Migration\n\n"
            "This document references AUTH-42.\n\n"
            "The old endpoint must remain available through October."
        )],
        resolved_anchors=frozenset({"auth-42"}),
    )

    assert has_pending(writes)
    assert writes[0].resolution_status == "partial"
    assert "AUTH-42" not in (writes[0].llm_text or "")
    assert "old endpoint" in (writes[0].llm_text or "")
    assert writes[0].text.endswith("through October.")


def test_anchor_sentence_with_architecture_claim_still_goes_to_llm():
    writes = selective_chunk_writes(
        [_chunk("PR #7 implements AUTH-42 by migrating callers to a new gateway architecture.")],
        resolved_anchors=frozenset({"auth-42"}),
    )

    assert has_pending(writes)
    assert "new gateway architecture" in (writes[0].llm_text or "")


def test_ledger_keeps_full_text_but_returns_only_llm_text(tmp_path):
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite")
    record_key = "notion:c:page:p1"
    ledger.commit(record_key, "hash", primary_node_uid="node")
    writes = selective_chunk_writes(
        [_chunk("Documents AUTH-42.\n\nUnresolved architecture claim.")],
        resolved_anchors=frozenset({"auth-42"}),
    )
    ledger.save_chunks(record_key, writes)

    pending = ledger.pending_chunks(5)
    assert pending[0].text == "Unresolved architecture claim."
    assert pending[0].source_text == "Documents AUTH-42.\n\nUnresolved architecture claim."
