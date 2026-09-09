"""Deterministic enterprise identifier extraction for bridge candidates."""

from __future__ import annotations

import re


JIRA_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,11}-\d+\b")
URL_RE = re.compile(r"https?://[^\s<>\]\[\"']+")
GITHUB_REPOSITORY_RE = re.compile(
    r"(?:github\.com/|\bRepository:\s*)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)
GITHUB_COMMIT_RE = re.compile(
    r"(?:github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/commit/|\bCommit:\s*)([0-9a-f]{7,40})\b",
    re.IGNORECASE,
)


def normalize_url(value: str) -> str:
    return value.rstrip(".,;:!?)\"").rstrip("/")


def jira_keys(text: str) -> frozenset[str]:
    return frozenset(match.upper() for match in JIRA_KEY_RE.findall(text))


def urls(text: str) -> frozenset[str]:
    return frozenset(normalize_url(value) for value in URL_RE.findall(text))


def repository_names(text: str) -> frozenset[str]:
    return frozenset(value.lower().removesuffix(".git") for value in GITHUB_REPOSITORY_RE.findall(text))


def commit_shas(text: str) -> frozenset[str]:
    return frozenset(value.lower() for value in GITHUB_COMMIT_RE.findall(text))


def evidence_excerpt(text: str, anchor: str, radius: int = 150) -> str:
    flat = " ".join(text.split())
    position = flat.lower().find(anchor.lower())
    if position < 0:
        return flat[: radius * 2]
    start = max(0, position - radius)
    end = min(len(flat), position + len(anchor) + radius)
    prefix = "…" if start else ""
    suffix = "…" if end < len(flat) else ""
    return f"{prefix}{flat[start:end]}{suffix}"
