"""Tier-2 deterministic cross-source linking from verified exact anchors."""

from __future__ import annotations

from collections import defaultdict

from falkordb import Graph

from connectors.core.ledger import ConnectorLedger, RecordEdgeRef
from connectors.core.models import SourceRecord
from graph import writer as w
from graph.derived import materialize_around
from graph.bridge.anchors import (
    commit_shas, evidence_excerpt, jira_keys, pull_request_refs,
    repository_names, urls,
)


def anchor_properties(record: SourceRecord) -> dict[str, list[str]]:
    """Persist only deterministic identifiers, never an opaque source blob.

    These compact arrays let a target ingested later discover older records
    that already referenced it, making exact linking independent of connector
    ingestion order.
    """
    return {
        "anchor_jira_keys": sorted(jira_keys(record.content)),
        "anchor_repository_names": sorted(repository_names(record.content)),
        "anchor_commit_shas": sorted(commit_shas(record.content)),
        "anchor_pull_request_refs": sorted(pull_request_refs(record.content)),
        "anchor_urls": sorted(urls(record.content)),
    }


def _targets(graph: Graph, record: SourceRecord) -> list[dict]:
    found: dict[str, dict] = {}

    keys = sorted(jira_keys(record.content))
    if keys:
        rows = graph.query(
            """MATCH (n:WorkItem)-[:MENTIONED_IN]->(sr:SourceRecord)
               WHERE n.issue_key IN $values
               RETURN DISTINCT n.uid, labels(n)[0], n.name, n.issue_key,
                      collect(DISTINCT sr.provider)""",
            params={"values": keys},
        ).result_set
        for uid, label, name, anchor, providers in rows:
            found[uid] = {"uid": uid, "label": label, "name": name,
                          "anchor": anchor, "providers": providers or []}

    names = sorted(repository_names(record.content))
    if names:
        rows = graph.query(
            """MATCH (n:Repository)-[:MENTIONED_IN]->(sr:SourceRecord)
               WHERE toLower(n.name) IN $values
               RETURN DISTINCT n.uid, labels(n)[0], n.name, toLower(n.name),
                      collect(DISTINCT sr.provider)""",
            params={"values": names},
        ).result_set
        for uid, label, name, anchor, providers in rows:
            found[uid] = {"uid": uid, "label": label, "name": name,
                          "anchor": anchor, "providers": providers or []}

    shas = sorted(commit_shas(record.content))
    if shas:
        rows = graph.query(
            """MATCH (n:Commit)-[:MENTIONED_IN]->(sr:SourceRecord)
               WHERE any(value IN $values WHERE toLower(n.sha) STARTS WITH value)
               RETURN DISTINCT n.uid, labels(n)[0], n.name, n.sha,
                      collect(DISTINCT sr.provider)""",
            params={"values": shas},
        ).result_set
        for uid, label, name, anchor, providers in rows:
            found[uid] = {"uid": uid, "label": label, "name": name,
                          "anchor": anchor, "providers": providers or []}

    pr_refs = sorted(pull_request_refs(record.content))
    if pr_refs:
        qualified = [ref.lower() for ref in pr_refs if not ref.startswith("#")]
        bare = [ref.lower() for ref in pr_refs if ref.startswith("#")]
        if qualified:
            rows = graph.query(
                """MATCH (n:PullRequest)-[:MENTIONED_IN]->(sr:SourceRecord)
                   WHERE toLower(n.pr_ref) IN $values
                   RETURN DISTINCT n.uid, labels(n)[0], n.name, n.pr_ref,
                          collect(DISTINCT sr.provider)""",
                params={"values": qualified},
            ).result_set
            for uid, label, name, anchor, providers in rows:
                found[uid] = {"uid": uid, "label": label, "name": name,
                              "anchor": anchor, "providers": providers or []}
        if bare:
            rows = graph.query(
                """MATCH (n:PullRequest)-[:MENTIONED_IN]->(sr:SourceRecord)
                   WHERE any(value IN $values WHERE toLower(n.pr_ref) ENDS WITH value)
                   RETURN DISTINCT n.uid, labels(n)[0], n.name, n.pr_ref,
                          collect(DISTINCT sr.provider)""",
                params={"values": bare},
            ).result_set
            if len(rows) == 1:
                uid, label, name, anchor, providers = rows[0]
                found[uid] = {"uid": uid, "label": label, "name": name,
                              "anchor": anchor, "providers": providers or []}

    exact_urls = sorted(urls(record.content))
    if exact_urls:
        rows = graph.query(
            """MATCH (n)-[:MENTIONED_IN]->(sr:SourceRecord)
               WHERE n.url IN $values AND NOT n:SourceRecord
               RETURN DISTINCT n.uid, labels(n)[0], n.name, n.url,
                      collect(DISTINCT sr.provider)""",
            params={"values": exact_urls},
        ).result_set
        for uid, label, name, anchor, providers in rows:
            found[uid] = {"uid": uid, "label": label, "name": name,
                          "anchor": anchor, "providers": providers or []}

    return list(found.values())


def resolved_anchor_values(
    graph: Graph, record: SourceRecord, primary_uid: str,
) -> frozenset[str]:
    """Exact identifiers from this record that already resolve cross-source.

    The selective semantic queue uses this after ``resolve_exact_anchors`` to
    avoid paying an LLM to rediscover a relationship Pass 1 already wrote.
    """
    return frozenset(
        str(target["anchor"]).casefold()
        for target in _targets(graph, record)
        if target["uid"] != primary_uid and record.provider not in target["providers"]
    )


def _relation(from_label: str, to_label: str) -> str:
    if from_label in ("Commit", "PullRequest") and to_label == "WorkItem":
        return "IMPLEMENTS"
    if from_label == "Document" and to_label in {
        "WorkItem", "Commit", "PullRequest", "Repository", "SourceFile",
    }:
        return "DOCUMENTS"
    return "REFERENCES"


def resolve_exact_anchors(
    graph: Graph, ledger: ConnectorLedger, record: SourceRecord,
    primary_uid: str, primary_label: str,
) -> list[RecordEdgeRef]:
    grouped: defaultdict[tuple[str, str], list[dict]] = defaultdict(list)
    refs: list[RecordEdgeRef] = []
    for target in _targets(graph, record):
        if target["uid"] == primary_uid or record.provider in target["providers"]:
            continue
        relation = _relation(primary_label, target["label"])
        grouped[(relation, target["label"])].append({
            "from_uid": primary_uid, "to_uid": target["uid"],
            "source_record_keys": [record.record_key],
            "evidence": evidence_excerpt(record.content, str(target["anchor"])),
            "extraction_method": "exact_anchor", "confidence": 1.0,
            "extractor_version": "tier2-v1", "model": None,
            "chunk_id": None, "chunk_hash": None,
            "valid_at": record.reference_time.isoformat() if record.reference_time else None,
        })
        refs.append(RecordEdgeRef(relation, primary_uid, target["uid"]))
    for (relation, target_label), rows in grouped.items():
        w.upsert_fact_edges(graph, relation, primary_label, target_label, rows)
    if refs:
        ledger.record_edges_batch(record.record_key, refs)
        materialize_around(graph, primary_uid, record.record_key)
    return refs


def resolve_backlinks_for_target(
    graph: Graph,
    ledger: ConnectorLedger,
    *,
    target_uid: str,
    target_label: str,
    target_provider: str,
    jira_key: str | None = None,
    repository_name: str | None = None,
    commit_sha: str | None = None,
    pull_request_ref: str | None = None,
    url: str | None = None,
) -> list[RecordEdgeRef]:
    """Resolve records ingested *before* a newly available exact target.

    The referencing SourceRecord remains the sole support/provenance owner of
    the resulting edge. Same-provider matches are intentionally ignored here;
    provider-native structural writers own those relationships.
    """
    predicates: list[str] = []
    params: dict[str, object] = {"target_provider": target_provider}
    if jira_key:
        predicates.append("$jira_key IN coalesce(sr.anchor_jira_keys, [])")
        params["jira_key"] = jira_key.upper()
    if repository_name:
        predicates.append("$repository_name IN coalesce(sr.anchor_repository_names, [])")
        params["repository_name"] = repository_name.lower().removesuffix(".git")
    if commit_sha:
        predicates.append(
            "any(value IN coalesce(sr.anchor_commit_shas, []) "
            "WHERE toLower($commit_sha) STARTS WITH value)"
        )
        params["commit_sha"] = commit_sha.lower()
    if pull_request_ref:
        ref = pull_request_ref.lower()
        predicates.append(
            "$pr_ref IN coalesce(sr.anchor_pull_request_refs, []) "
            "OR any(value IN coalesce(sr.anchor_pull_request_refs, []) "
            "WHERE value STARTS WITH '#' AND $pr_ref ENDS WITH value)"
        )
        params["pr_ref"] = ref
    if url:
        predicates.append("$url IN coalesce(sr.anchor_urls, [])")
        params["url"] = url.rstrip("/")
    if not predicates:
        return []

    rows = graph.query(
        f"""
        MATCH (sr:SourceRecord)
        WHERE sr.deleted_at IS NULL AND sr.provider <> $target_provider
          AND ({' OR '.join(predicates)})
        RETURN DISTINCT sr.record_key, sr.name, sr.source_time
        """,
        params=params,
    ).result_set

    written: list[RecordEdgeRef] = []
    for record_key, record_name, source_time in rows:
        entry = ledger.get(record_key)
        if not entry or not entry.primary_node_uid or entry.primary_node_uid == target_uid:
            continue
        label_rows = graph.query(
            "MATCH (n {uid: $uid}) RETURN labels(n)[0]",
            params={"uid": entry.primary_node_uid},
        ).result_set
        if not label_rows:
            continue
        source_label = label_rows[0][0]
        relation = _relation(source_label, target_label)
        evidence_anchor = (
            jira_key or repository_name or commit_sha or pull_request_ref or url or target_uid
        )
        w.upsert_fact_edges(graph, relation, source_label, target_label, [{
            "from_uid": entry.primary_node_uid, "to_uid": target_uid,
            "source_record_keys": [record_key],
            "evidence": f"{record_name} contains exact reference {evidence_anchor}",
            "extraction_method": "exact_anchor", "confidence": 1.0,
            "extractor_version": "tier2-v1", "model": None,
            "chunk_id": None, "chunk_hash": None,
            "valid_at": source_time,
        }])
        ref = RecordEdgeRef(relation, entry.primary_node_uid, target_uid)
        ledger.record_edge(record_key, ref.rel_type, ref.from_uid, ref.to_uid)
        written.append(ref)
    if written:
        materialize_around(graph, target_uid, str(rows[0][0]))
    return written


def link_verified_person_identity(
    graph: Graph, ledger: ConnectorLedger, uid: str, email: str | None, record_key: str,
) -> list[RecordEdgeRef]:
    verified = (email or "").strip().lower()
    if not verified:
        return []
    source_rows = graph.query(
        "MATCH (sr:SourceRecord {record_key: $record_key}) RETURN sr.source_time LIMIT 1",
        params={"record_key": record_key},
    ).result_set
    source_time = source_rows[0][0] if source_rows else None
    rows = graph.query(
        """MATCH (other:Person)-[:MENTIONED_IN]->(sr:SourceRecord)
           WHERE other.uid <> $uid AND toLower(other.email) = $email
           RETURN DISTINCT other.uid""",
        params={"uid": uid, "email": verified},
    ).result_set
    refs = []
    for (other_uid,) in rows:
        from_uid, to_uid = sorted((uid, other_uid))
        w.upsert_fact_edges(graph, "SAME_AS", "Person", "Person", [{
            "from_uid": from_uid, "to_uid": to_uid,
            "source_record_keys": [record_key],
            "evidence": f"Verified email: {verified}",
            "extraction_method": "derived", "confidence": 1.0,
            "derived": True, "derived_rule": "verified_email",
            "extractor_version": "identity-v1", "model": None,
            "chunk_id": None, "chunk_hash": None,
            "valid_at": source_time,
        }])
        refs.append(RecordEdgeRef("SAME_AS", from_uid, to_uid))
    if refs:
        ledger.record_edges_batch(record_key, refs)
    return refs
