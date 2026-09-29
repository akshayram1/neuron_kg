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
# `PR #12` alone is stored as `#12` and only matches if exactly one
# ingested PullRequest has that id (see resolver). Repo-qualified refs
# come from Bitbucket/GitHub PR URLs.
_BITBUCKET_PR_RE = re.compile(
    r"bitbucket\.org/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull-requests/(\d+)",
    re.IGNORECASE,
)
_GITHUB_PR_RE = re.compile(
    r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)",
    re.IGNORECASE,
)
_PR_NUM_RE = re.compile(r"\bPR\s*#\s*(\d+)\b", re.IGNORECASE)


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


def pull_request_refs(text: str) -> frozenset[str]:
    """`rubik_/argus#12` from a PR URL, or `#12` from `PR #12`."""
    refs: set[str] = set()
    for repo, number in _BITBUCKET_PR_RE.findall(text):
        refs.add(f"{repo.lower()}#{number}")
    for repo, number in _GITHUB_PR_RE.findall(text):
        refs.add(f"{repo.lower().removesuffix('.git')}#{number}")
    for number in _PR_NUM_RE.findall(text):
        refs.add(f"#{number}")
    return frozenset(refs)


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
