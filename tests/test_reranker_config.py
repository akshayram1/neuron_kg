"""Runtime Laya switch API and persistence."""

from __future__ import annotations

import asyncio

from demo_ui.backend import app as backend


def test_reranker_switch_persists_without_a_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "RUNTIME_SETTINGS_PATH", tmp_path / "settings.sqlite3")
    monkeypatch.setattr(
        backend,
        "reranker_status",
        lambda: {
            "mode": "laya", "enabled": True, "available": True, "ready": True,
            "source": "runtime", "modelDir": "/checkpoint", "device": "cpu",
            "packageAvailable": True, "checkpointReady": True, "reason": None,
        },
    )
    monkeypatch.setattr(backend, "set_reranker_enabled", lambda enabled: {
        **backend.reranker_status(), "mode": "laya" if enabled else "off", "enabled": enabled,
    })

    result = asyncio.run(
        backend.configure_reranker(backend.RerankerConfigRequest(enabled=False))
    )

    assert result["reranker"]["mode"] == "off"
    assert backend._runtime_setting("laya_enabled") == "false"
