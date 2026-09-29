"""Stage banners for ingest, reingest, retrieval, findings, and wisdom.

One box per stage so a synthetic demo log can be read as the pipeline,
not as a flat list of lines. Callers pass already-formatted rows; this
module does not inspect graph state.
"""

from __future__ import annotations

import logging

from connectors.core.actions import RecordAction

_WIDTH = 78

_ACTION = {
    RecordAction.INSERT: "first write",
    RecordAction.UPDATE: "reingest: content hash changed, rewrite",
    RecordAction.KEEP: "reingest: content hash unchanged, skip write",
    RecordAction.DELETE: "source removed",
}


def log_box(logger: logging.Logger, title: str, lines: list[str]) -> None:
    """Log one multiline box. `%` in `lines` is not treated as a format spec."""
    inner = _WIDTH - 2
    label = f" {title.strip()} "
    if len(label) > inner:
        label = label[: inner - 1] + " "
    top = "┌" + label + "─" * (inner - len(label)) + "┐"
    body = []
    for line in lines:
        text = line.replace("\n", " ")
        if len(text) > inner - 2:
            text = text[: inner - 5] + "..."
        body.append("│ " + text.ljust(inner - 2) + " │")
    if not body:
        body.append("│" + " " * inner + "│")
    bottom = "└" + "─" * inner + "┘"
    logger.info("%s", "\n".join((top, *body, bottom)))


def log_record_action(logger: logging.Logger, kind: str, name: str, action: RecordAction) -> None:
    """One line per source record: insert, update (reingest), keep, or delete."""
    logger.info(
        "  record         %-14s %-6s  %s  (%s)",
        kind, action.value, name, _ACTION[action],
    )
