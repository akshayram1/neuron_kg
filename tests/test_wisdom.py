from types import SimpleNamespace

from graph.wisdom import (
    WisdomApplicability,
    WisdomProposalExtraction,
    generate_wisdom_proposal,
    migration_claim_fallback,
    removal_readiness_fallback,
)


def test_story_fallback_is_reviewable_not_active_wisdom():
    proposal = migration_claim_fallback()

    assert proposal.action == "create"
    assert proposal.promotion == "human_review"
    assert proposal.topic_key == "code-confirmed-api-migration"
    assert "bitbucket" in proposal.applicability.implementation_authority


def test_removal_readiness_fallback_is_a_reviewable_playbook():
    proposal = removal_readiness_fallback()

    assert proposal.wisdom_type == "Playbook"
    assert proposal.promotion == "human_review"
    assert proposal.topic_key == "consumer-verified-api-removal"
    assert "every dependency-graph consumer" in proposal.recommended_action


def test_wisdom_call_uses_findings_as_primary_grounded_input():
    parsed = WisdomProposalExtraction(
        action="create",
        wisdom_type="Policy",
        topic_key="code-confirmed-api-migration",
        title="Verify migrations from code",
        statement="Current code must confirm migration completion.",
        rationale="A finding recorded a claim/code mismatch.",
        recommended_action="Keep the finding open until code agrees.",
        applicability=WisdomApplicability(domains=["api-migration"]),
        confidence=0.8,
        promotion="human_review",
        review_reason="Organisation-wide policy requires review.",
    )

    class Responses:
        def __init__(self):
            self.input = None

        def parse(self, **kwargs):
            self.input = kwargs["input"]
            return SimpleNamespace(
                output_parsed=parsed,
                usage=SimpleNamespace(input_tokens=20, output_tokens=10, total_tokens=30),
            )

    responses = Responses()
    client = SimpleNamespace(responses=responses)
    result, usage = generate_wisdom_proposal(
        client,
        pattern_key="migration-claims-require-code-confirmation",
        findings=[{"id": "finding-1", "title": "Claim conflicts with code"}],
        existing_wisdom=[],
        model="test-model",
    )

    assert result.title == "Verify migrations from code"
    assert '"findings_with_lineage": [{"id": "finding-1"' in responses.input[1]["content"]
    assert usage.total_tokens == 30
