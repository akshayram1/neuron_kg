"""Suite-wide isolation from developer-machine runtime configuration."""

from __future__ import annotations

import pytest
import sys


@pytest.fixture(autouse=True)
def disable_runtime_reranker_by_default(monkeypatch):
    """Unit tests opt into a scorer explicitly; local .env must not do it."""
    monkeypatch.setenv("NEURON_RERANK", "off")
    chat = sys.modules.get("graph.chat")
    if chat is not None:
        monkeypatch.setattr(chat, "_reranker_enabled_override", None)
