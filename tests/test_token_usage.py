from types import SimpleNamespace

from graph.token_usage import TokenUsage


def test_token_usage_accepts_responses_and_chat_shapes():
    usage = TokenUsage()

    usage.add(SimpleNamespace(input_tokens=120, output_tokens=30, total_tokens=150))
    usage.add(SimpleNamespace(prompt_tokens=80, completion_tokens=20, total_tokens=100))

    assert usage.input_tokens == 200
    assert usage.output_tokens == 50
    assert usage.total_tokens == 250


def test_token_usage_accepts_embedding_shape_and_serializes_prefix():
    usage = TokenUsage()

    usage.add({"prompt_tokens": 42, "total_tokens": 42})

    assert usage.as_dict("ingestion") == {
        "ingestion_input_tokens": 42,
        "ingestion_output_tokens": 0,
        "ingestion_total_tokens": 42,
    }
