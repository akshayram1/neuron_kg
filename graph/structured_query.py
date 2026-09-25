"""Rule-based structured reads for questions hybrid search cannot answer.

Hybrid search ranks similar text. It cannot enumerate, negate, or sort:
"unassigned tickets", "all tickets assigned to X", "last commit in argus".
Those are Cypher. This module detects those intents and returns a complete
hit list so chat can skip retrieval.

Verified live on `neuron__less_token` (2026-09-10):
- "assigned to Aashish" picked the Bitbucket Person (67 commits, 0
  ASSIGNED_TO) because `_find_named_person` first-wins on equal fuzzy
  ratio. Jira Aashish — SAME_AS, 4 live assignments — was never injected.
- "which tickets are not assigned" retrieved a Notion release-notes page
  whose body mentions DATAOS keys that are not WorkItems in the graph.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from falkordb import Graph

from graph.access import AccessScope
from graph.search import SearchHit

_WORD_RE = re.compile(r"[A-Za-z]+")
_STOP = {
    "the", "all", "and", "for", "not", "any", "are", "was", "who", "what",
    "which", "how", "did", "does", "assigned", "assignee", "ticket", "tickets",
    "issue", "issues", "anyone", "nobody", "someone", "commit", "commits",
    "last", "latest", "list", "everything", "working", "about", "from",
    "have", "has", "been", "with", "without", "there", "their", "this",
}

_UNASSIGNED = re.compile(
    r"\b("
    r"unassigned|"
    r"not assigned|"
    r"no assignee|"
    r"without (an? )?assignee|"
    r"assigned to (anyone|anybody|nobody|no one)"
    r")\b",
    re.I,
)
_ASSIGNED_TO = re.compile(
    r"\b(?:(?:tickets?|issues?|work ?items?)\s+)?"
    r"(?:assigned to|assignee(?:\s+is|\s*:)?)\s+"
    r"([A-Za-z][A-Za-z .'-]{0,60})",
    re.I,
)
_LATEST_COMMIT = re.compile(
    r"\b(second[- ]last|2nd last|latest|most recent|last)\b.*\bcommits?\b|"
    r"\bcommits?\b.*\b(second[- ]last|2nd last|latest|most recent|last)\b",
    re.I,
)
_SECOND = re.compile(r"\b(second[- ]last|2nd last)\b", re.I)
_IN_REPO = re.compile(
    r"\b(?:in|on|for)\s+(?:the\s+)?([A-Za-z0-9_./-]+)(?:\s+(?:repo|repository))?",
    re.I,
)
# Git short SHA is 7 hex; full is 40. Hybrid cannot retrieve by hex
# (verified: "what did ce9b313 do" returned unrelated hits — `commit*`
# matches every Commit node's "[SOURCE] Kind: Commit" line).
_SHA = re.compile(r"\b([0-9a-f]{7,40})\b", re.I)
# Jira keys (`DATAOS-4346`). Hybrid splits on `-`, so BM25 is `dataos*|4346*`
# and every DATAOS ticket ties; the named ticket often never makes top-k
# (verified: "what has been done for DATAOS-4346" → 0 sources).
_ISSUE_KEY = re.compile(r"\b([A-Za-z][A-Za-z0-9]+-\d+)\b")
_CURRENT_API_USAGE = re.compile(
    r"(?=.*\b(?:current|currently|actual|actually|now|still)\b)"
    r"(?=.*\b(?:consume|consumes|using|uses|call|calls|depend|depends)\b)"
    r"(?=.*\b(?:api|endpoint)\b)|"
    r"\b(?:which|what)\s+projects?\b.*\b(?:consume|consumes|call|calls|use|uses)\b.*"
    r"\b(?:api|endpoint)\b",
    re.I,
)
_CURRENT_REMOVAL_IMPACT = re.compile(
    r"(?=.*\b(?:current|currently|now|still)\b)"
    r"(?=.*\b(?:affect|affected|impact|impacted|broken|failing)\w*\b)"
    r"(?=.*\b(?:remove|removed|removal|sunset|deprecated?)\w*\b)",
    re.I,
)


@dataclass(frozen=True)
class StructuredHits:
    kind: str
    hits: list[SearchHit]
    preamble: str


def resolve_structured(
    graph: Graph, question: str, scope: AccessScope, providers: list[str] | None,
) -> StructuredHits | None:
    if _CURRENT_REMOVAL_IMPACT.search(question):
        return _current_removal_findings(graph, scope, providers)
    if _CURRENT_API_USAGE.search(question):
        return _current_api_consumers(graph, scope, providers)
    if _UNASSIGNED.search(question):
        return _unassigned_work_items(graph, scope, providers)
    assigned = _ASSIGNED_TO.search(question)
    if assigned and not _UNASSIGNED.search(question):
        return _assigned_to(graph, assigned.group(1), scope, providers)
    if _LATEST_COMMIT.search(question):
        limit = 2 if _SECOND.search(question) else 1
        repo = _repo_hint(question)
        return _latest_commits(graph, scope, providers, repo=repo, limit=limit)
    prefixes = _sha_prefixes(question)
    if prefixes:
        return _commits_by_sha(graph, prefixes, scope, providers)
    keys = _issue_keys(question)
    if keys:
        return _work_items_by_key(graph, keys, scope, providers)
    return None


def _current_removal_findings(
    graph: Graph, scope: AccessScope, providers: list[str] | None,
) -> StructuredHits:
    """Complete set of open, materialised removed-dependency Findings."""
    acl, provider_filter, params = _acl_bits(scope, providers, "removal_impact_acl")
    rows = graph.query(
        f"""
        MATCH (finding:Finding)-[:MENTIONED_IN]->(sr:SourceRecord)
        WHERE finding.status = 'open'
          AND finding.kind = 'removed_dependency_still_called'
          AND sr.deleted_at IS NULL AND {acl} {provider_filter}
        RETURN DISTINCT finding.uid, finding.name, finding.search_text,
               finding.severity
        ORDER BY finding.name
        """,
        params=params,
    ).result_set
    hits = [
        SearchHit(
            uid, "Finding", name, search_text or "",
            1.0, ["structured", "current-finding"],
        )
        for uid, name, search_text, _severity in rows
    ]
    names = "; ".join(row[1] for row in rows) or "none"
    preamble = (
        "STRUCTURED RESULT — complete list of open materialised removal-impact "
        f"Findings: {names}. Report only these projects as currently affected. "
        "A stale Finding is historical and must not be reported as current."
    )
    return StructuredHits("current_removal_findings", hits, preamble)


def _current_api_consumers(
    graph: Graph, scope: AccessScope, providers: list[str] | None,
) -> StructuredHits:
    """Complete current implementation view from live repository calls.

    Jira/Notion may claim a migration, and historical CONSUMES_API edges may
    explain what used to be true. Neither is authoritative for the endpoint
    current production code actually calls.
    """
    acl, provider_filter, params = _acl_bits(scope, providers, "api_usage_acl")
    rows = graph.query(
        f"""
        MATCH (project:Project)-[:HAS_REPOSITORY]->(:Repository)-[:CONTAINS]->
              (file:SourceFile)-[call:CALLS_ENDPOINT]->(endpoint:Endpoint)
        WHERE call.invalid_at IS NULL AND endpoint.path IS NOT NULL
        UNWIND coalesce(call.source_record_keys, []) AS source_key
        MATCH (sr:SourceRecord {{record_key: source_key}})
        WHERE sr.deleted_at IS NULL AND {acl} {provider_filter}
        RETURN DISTINCT file.uid, file.name, file.search_text, project.name,
               endpoint.name, endpoint.path
        ORDER BY project.name, endpoint.path, file.name
        """,
        params=params,
    ).result_set
    hits = [
        SearchHit(
            uid, "SourceFile", name,
            f"CURRENT IMPLEMENTATION: {project} calls {endpoint_name or path}. "
            "This is a live Bitbucket/GitHub CALLS_ENDPOINT fact.",
            1.0, ["structured", "code-authority"],
        )
        for uid, name, search_text, project, endpoint_name, path in rows
    ]
    usage = []
    seen: set[tuple[str, str]] = set()
    for _uid, _name, _text, project, endpoint_name, path in rows:
        key = (project, path)
        if key in seen:
            continue
        seen.add(key)
        version = re.search(r"/v(\d+)/", path or "")
        api_name = f"Auth API v{version.group(1)}" if version else "the API"
        usage.append(f"{project} currently calls {endpoint_name or path} ({api_name})")
    complete = "; ".join(usage) or "no live repository CALLS_ENDPOINT facts were found"
    preamble = (
        "STRUCTURED RESULT — complete current implementation view from live "
        "Bitbucket/GitHub SourceFile CALLS_ENDPOINT facts. " + complete + ". "
        "Current code is authoritative. Jira/Notion text is a claim, not proof "
        "of implementation, and historical calls must not be reported as current."
    )
    return StructuredHits("current_api_consumers", hits, preamble)


def find_named_persons(
    graph: Graph, text: str, scope: AccessScope, providers: list[str] | None,
) -> list[SearchHit]:
    """Every Person whose name fuzzy-matches, plus SAME_AS neighbors.

    First-wins on equal ratio is wrong: two 'Aashish Verma' nodes (Jira vs
    Bitbucket) score identically; the Bitbucket one happened to come first
    and hid four live Jira assignments.
    """
    seeds = _fuzzy_people(graph, text, scope, providers)
    if not seeds:
        return []
    seen: dict[str, SearchHit] = {}
    for uid, name, ratio in seeds:
        for member_uid, member_name in _same_as_cluster(graph, uid, name):
            seen.setdefault(
                member_uid,
                SearchHit(member_uid, "Person", member_name, "", ratio, ["named_entity"]),
            )
    return list(seen.values())


def _commit_summary(when, sha, repo_name, search_text) -> str:
    """Dates + the Files: list. MODIFIES is only .py/.md still on HEAD."""
    head = f"authored_at={when} sha={sha} repo={repo_name or '?'}"
    text = (search_text or "").strip()
    if "Files:" in text:
        text = text[text.index("Files:"):]
    elif "\n\n" in text:
        text = text.split("\n\n", 1)[1]
    text = text.strip()
    if len(text) > 2500:
        text = text[:2500] + "\n…"
    return f"{head}\n{text}" if text else head


def _issue_keys(question: str) -> list[str]:
    found = [m.group(1).upper() for m in _ISSUE_KEY.finditer(question)]
    return list(dict.fromkeys(found))


def _work_item_summary(key, status, search_text) -> str:
    head = f"issue_key={key} status={status or '?'}"
    text = (search_text or "").strip()
    if len(text) > 3500:
        text = text[:3500] + "\n…"
    return f"{head}\n{text}" if text else head


def _sha_prefixes(question: str) -> list[str]:
    """Longest unique hex prefixes from the question. `DATAOS-3833` is not hex."""
    found = [m.group(1).lower() for m in _SHA.finditer(question)]
    unique: list[str] = []
    for sha in sorted(set(found), key=len, reverse=True):
        if any(kept.startswith(sha) for kept in unique):
            continue
        unique.append(sha)
    return unique


def _repo_hint(question: str) -> str | None:
    match = _IN_REPO.search(question)
    if not match:
        return None
    token = match.group(1).strip(".,")
    if token.lower() in {"the", "a", "this", "that", "our"}:
        return None
    if token.lower() in {"branch", "repo", "repository"}:
        return None
    return token


def _fuzzy_people(
    graph: Graph, text: str, scope: AccessScope, providers: list[str] | None,
) -> list[tuple[str, str, float]]:
    provider_filter = "AND sr.provider IN $providers" if providers else ""
    acl, acl_params = scope.cypher("sr", "person_name_acl")
    rows = graph.query(
        f"""
        MATCH (p:Person)-[:MENTIONED_IN]->(sr:SourceRecord)
        WHERE sr.deleted_at IS NULL AND {acl} {provider_filter}
        RETURN DISTINCT p.uid, p.name
        """,
        params={**acl_params, **({"providers": providers} if providers else {})},
    ).result_set
    query_words = [
        w.lower() for w in _WORD_RE.findall(text)
        if len(w) >= 3 and w.lower() not in _STOP
    ]
    if not rows or not query_words:
        return []
    found: list[tuple[str, str, float]] = []
    for uid, name in rows:
        best = 0.0
        for part in _WORD_RE.findall(name or ""):
            if len(part) < 3:
                continue
            part_lower = part.lower()
            for word in query_words:
                best = max(best, difflib.SequenceMatcher(None, part_lower, word).ratio())
        if best >= 0.82:
            found.append((uid, name, best))
    found.sort(key=lambda row: row[2], reverse=True)
    return found


def _same_as_cluster(graph: Graph, uid: str, name: str) -> list[tuple[str, str]]:
    rows = graph.query(
        """
        MATCH (p:Person {uid: $uid})
        OPTIONAL MATCH (p)-[:SAME_AS*0..4]-(o:Person)
        RETURN DISTINCT coalesce(o.uid, p.uid), coalesce(o.name, p.name)
        """,
        params={"uid": uid},
    ).result_set
    if not rows:
        return [(uid, name)]
    return [(row[0], row[1] or name) for row in rows]


def _acl_bits(
    scope: AccessScope, providers: list[str] | None, prefix: str,
) -> tuple[str, str, dict]:
    acl, params = scope.cypher("sr", prefix)
    provider_filter = "AND sr.provider IN $providers" if providers else ""
    if providers:
        params = {**params, "providers": providers}
    return acl, provider_filter, params


def _unassigned_work_items(
    graph: Graph, scope: AccessScope, providers: list[str] | None,
) -> StructuredHits:
    acl, provider_filter, params = _acl_bits(scope, providers, "unassigned_acl")
    rows = graph.query(
        f"""
        MATCH (w:WorkItem)-[:MENTIONED_IN]->(sr:SourceRecord)
        WHERE sr.deleted_at IS NULL AND w.issue_key IS NOT NULL
          AND {acl} {provider_filter}
        OPTIONAL MATCH (w)-[r:ASSIGNED_TO]->(:Person)
        WHERE r.invalid_at IS NULL
        WITH w, count(r) AS live
        WHERE live = 0
        RETURN DISTINCT w.uid, w.name, w.issue_key
        ORDER BY w.issue_key
        """,
        params=params,
    ).result_set
    hits = [
        SearchHit(
            uid, "WorkItem", name, "No live ASSIGNED_TO — currently unassigned.",
            1.0, ["structured"],
        )
        for uid, name, _key in rows
    ]
    keys = ", ".join(row[2] for row in rows) or "(none)"
    preamble = (
        "STRUCTURED RESULT — complete list, not a sample. "
        f"{len(hits)} WorkItem(s) have no live ASSIGNED_TO edge: {keys}. "
        "An ended historical assignment is not a current assignee. "
        "Ignore DATAOS keys that appear only inside Document text."
    )
    return StructuredHits("unassigned", hits, preamble)


def _assigned_to(
    graph: Graph, raw_name: str, scope: AccessScope, providers: list[str] | None,
) -> StructuredHits:
    people = find_named_persons(graph, raw_name, scope, providers)
    if not people:
        return StructuredHits(
            "assigned_to",
            [],
            f"STRUCTURED RESULT — no Person matching {raw_name.strip()!r}.",
        )
    uids = [hit.uid for hit in people]
    acl, provider_filter, params = _acl_bits(scope, providers, "assigned_acl")
    params = {**params, "uids": uids}
    rows = graph.query(
        f"""
        MATCH (w:WorkItem)-[r:ASSIGNED_TO]->(p:Person)
        WHERE p.uid IN $uids AND r.invalid_at IS NULL AND w.issue_key IS NOT NULL
        UNWIND coalesce(r.source_record_keys, []) AS source_key
        MATCH (sr:SourceRecord {{record_key: source_key}})
        WHERE sr.deleted_at IS NULL AND {acl} {provider_filter}
        RETURN DISTINCT w.uid, w.name, w.issue_key, p.name
        ORDER BY w.issue_key
        """,
        params=params,
    ).result_set
    assignee = rows[0][3] if rows else people[0].name
    hits = [
        SearchHit(
            uid, "WorkItem", name, f"Live assignee: {who}.",
            1.0, ["structured"],
        )
        for uid, name, _key, who in rows
    ]
    keys = ", ".join(row[2] for row in rows) or "(none)"
    preamble = (
        "STRUCTURED RESULT — complete list, not a sample. "
        f"{len(hits)} WorkItem(s) currently ASSIGNED_TO {assignee}: {keys}. "
        "Do not include tickets that only appear in Document text. "
        "Do not treat commit AUTHORED_BY as a Jira assignment."
    )
    return StructuredHits("assigned_to", hits, preamble)


def _latest_commits(
    graph: Graph, scope: AccessScope, providers: list[str] | None,
    *, repo: str | None, limit: int,
) -> StructuredHits:
    acl, provider_filter, params = _acl_bits(scope, providers, "commit_acl")
    repo_clause = ""
    if repo:
        repo_clause = "AND toLower(repo.name) CONTAINS $repo"
        params = {**params, "repo": repo.lower()}
    params = {**params, "limit": limit}
    rows = graph.query(
        f"""
        MATCH (repo:Repository)-[:CONTAINS]->(c:Commit)-[:MENTIONED_IN]->(sr:SourceRecord)
        WHERE sr.deleted_at IS NULL AND {acl} {provider_filter} {repo_clause}
        RETURN DISTINCT c.uid, c.name, c.authored_at, repo.name, c.sha, c.search_text
        ORDER BY c.authored_at DESC
        LIMIT $limit
        """,
        params=params,
    ).result_set
    hits = [
        SearchHit(
            uid, "Commit", name,
            _commit_summary(when, sha, repo_name, search_text),
            1.0, ["structured"],
        )
        for uid, name, when, repo_name, sha, search_text in rows
    ]
    scope_note = f" in repositories matching {repo!r}" if repo else ""
    rank = "latest" if limit == 1 else f"latest {limit} (row 2 is second-last)"
    preamble = (
        "STRUCTURED RESULT — complete, sorted by authored_at descending. "
        f"{rank}{scope_note}: {len(hits)} commit(s). "
        "Commits were ingested from the synced branch head, not every branch. "
        "There is no per-branch parent walk on Commit nodes."
    )
    return StructuredHits("latest_commits", hits, preamble)


def _commits_by_sha(
    graph: Graph, prefixes: list[str], scope: AccessScope, providers: list[str] | None,
) -> StructuredHits:
    acl, provider_filter, params = _acl_bits(scope, providers, "sha_acl")
    params = {**params, "prefixes": prefixes}
    rows = graph.query(
        f"""
        MATCH (c:Commit)-[:MENTIONED_IN]->(sr:SourceRecord)
        WHERE sr.deleted_at IS NULL AND {acl} {provider_filter}
          AND any(p IN $prefixes WHERE toLower(c.sha) STARTS WITH p)
        OPTIONAL MATCH (repo:Repository)-[:CONTAINS]->(c)
        RETURN DISTINCT c.uid, c.name, c.authored_at, c.sha, repo.name, c.search_text
        ORDER BY c.authored_at DESC
        """,
        params=params,
    ).result_set
    hits = [
        SearchHit(
            uid, "Commit", name,
            _commit_summary(when, sha, repo_name, search_text),
            1.0, ["structured"],
        )
        for uid, name, when, sha, repo_name, search_text in rows
    ]
    wanted = ", ".join(prefixes)
    if not hits:
        preamble = (
            "STRUCTURED RESULT — no Commit whose sha starts with "
            f"{wanted}. Only commits from the synced branch head are stored "
            f"(last N, default 100). A short hex that is not in that window "
            "is not in the graph."
        )
        return StructuredHits("commit_sha", [], preamble)
    found = ", ".join(row[3][:12] for row in rows)
    preamble = (
        "STRUCTURED RESULT — exact sha prefix lookup, not hybrid search. "
        f"Asked {wanted}; matched {found}. "
        "MODIFIES edges are the files this commit touched that still exist "
        "as ingested HEAD SourceFiles. Paths only in the Files: list were "
        "changed but are not linked (deleted or not .py/.md)."
    )
    return StructuredHits("commit_sha", hits, preamble)


def _work_items_by_key(
    graph: Graph, keys: list[str], scope: AccessScope, providers: list[str] | None,
) -> StructuredHits:
    acl, provider_filter, params = _acl_bits(scope, providers, "issue_acl")
    params = {**params, "keys": keys}
    rows = graph.query(
        f"""
        MATCH (w:WorkItem)-[:MENTIONED_IN]->(sr:SourceRecord)
        WHERE sr.deleted_at IS NULL AND {acl} {provider_filter}
          AND toUpper(w.issue_key) IN $keys
        RETURN DISTINCT w.uid, w.name, w.issue_key, w.status, w.search_text
        ORDER BY w.issue_key
        """,
        params=params,
    ).result_set
    hits = [
        SearchHit(
            uid, "WorkItem", name,
            _work_item_summary(key, status, search_text),
            1.0, ["structured"],
        )
        for uid, name, key, status, search_text in rows
    ]
    wanted = ", ".join(keys)
    if not hits:
        preamble = (
            "STRUCTURED RESULT — no WorkItem whose issue_key is "
            f"{wanted}. Only tickets from the synced Jira project or "
            "subtree are stored."
        )
        return StructuredHits("issue_key", [], preamble)
    found = ", ".join(row[2] for row in rows)
    preamble = (
        "STRUCTURED RESULT — exact issue_key lookup, not hybrid search. "
        f"Asked {wanted}; matched {found}. "
        "The block text is the ingested ticket (summary, description, "
        "comments). Incoming IMPLEMENTS edges are commits/PRs that named "
        "this key. PARENT_OF points at the parent ticket, not children."
    )
    return StructuredHits("issue_key", hits, preamble)


def find_window_activity(
    graph: Graph, scope: AccessScope, providers: list[str] | None,
    *, at: str, at_end: str, limit: int = 8,
) -> list[SearchHit]:
    """Entities whose facts land inside `[at, at_end)`, busiest first.

    A dated question names a window that no amount of text similarity can
    find: "what was worked on in August 2026" is not lexically closer to an
    August commit than to a July one, so ranking alone returned roadmap
    documents and two commits from June and July while 39 August commits sat
    unretrieved. Time is a filter the index cannot express, so it is asked
    of the graph directly and the results are merged into the text hits.
    """
    provider_filter = "AND sr.provider IN $providers" if providers else ""
    acl, acl_params = scope.cypher("sr", "window_acl")
    rows = graph.query(
        f"""
        MATCH (n)-[r]->(m)
        WHERE r.valid_at >= $at AND r.valid_at < $at_end
          AND type(r) <> 'MENTIONED_IN'
        MATCH (n)-[:MENTIONED_IN]->(sr:SourceRecord)
        WHERE sr.deleted_at IS NULL AND {acl} {provider_filter}
        RETURN n.uid, labels(n)[0], n.name, n.search_text, count(r) AS weight
        ORDER BY weight DESC
        LIMIT $limit
        """,
        params={
            "at": at, "at_end": at_end, "limit": limit, **acl_params,
            **({"providers": providers} if providers else {}),
        },
    ).result_set
    return [
        SearchHit(uid, label, name or uid, (text or "")[:2500], float(weight), ["time_window"])
        for uid, label, name, text, weight in rows
    ]
