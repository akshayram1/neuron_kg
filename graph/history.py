"""Bi-temporal fact history reads with mandatory source ACL enforcement."""

from __future__ import annotations

from falkordb import Graph

from graph.access import AccessScope
from graph.time_axis import held_at, holds_at, parse_iso


def fetch_fact_history(
    graph: Graph, scope: AccessScope, fact_uid: str, *,
    valid_at: str | None = None, observed_at: str | None = None,
) -> list[dict]:
    """Return all intervals, or answer either bi-temporal as-of mode.

    `valid_at` asks what the source asserted at a business-valid time;
    `observed_at` asks what Neuron had recorded at a transaction time. Current
    connectors use source timestamps when supplied and ingestion time as the
    conservative fallback.
    """
    history_acl, history_params = scope.cypher("sr", "history_acl")
    rows = graph.query(
        f"""
        MATCH (h:FactHistory {{fact_uid: $fact_uid}})
        UNWIND coalesce(h.source_record_keys, []) AS source_key
        MATCH (sr:SourceRecord {{record_key: source_key}})
        WHERE sr.deleted_at IS NULL AND {history_acl}
        RETURN DISTINCT h.uid, h.from_uid, h.to_uid, h.relation, h.evidence,
               h.valid_from, h.valid_to, h.observed_from, h.observed_to,
               h.last_confirmed_at, h.confidence, h.chunk_id, h.chunk_hash,
               h.extractor_version, h.model, collect(DISTINCT sr.name)
        ORDER BY h.valid_from
        """,
        params={"fact_uid": fact_uid, **history_params},
    ).result_set
    output = [
        {
            "id": row[0], "factUid": fact_uid, "source": row[1], "target": row[2],
            "relation": row[3], "evidence": row[4], "validFrom": row[5],
            "validTo": row[6], "observedFrom": row[7], "observedTo": row[8],
            "lastConfirmedAt": row[9], "confidence": row[10], "chunkId": row[11],
            "chunkHash": row[12], "extractorVersion": row[13], "model": row[14],
            "documents": sorted(row[15] or []), "state": "historical",
        }
        for row in rows
    ]

    live_acl, live_params = scope.cypher("sr", "live_history_acl")
    live = graph.query(
        f"""
        MATCH (a)-[r]->(b) WHERE r.fact_uid = $fact_uid AND r.invalid_at IS NULL
        UNWIND coalesce(r.source_record_keys, []) AS source_key
        MATCH (sr:SourceRecord {{record_key: source_key}})
        WHERE sr.deleted_at IS NULL AND {live_acl}
        RETURN DISTINCT a.uid, b.uid, type(r), r.evidence, r.valid_at,
               r.first_seen_at, r.last_confirmed_at, r.confidence,
               r.chunk_id, r.chunk_hash, r.extractor_version, r.model,
               collect(DISTINCT sr.name)
        """,
        params={"fact_uid": fact_uid, **live_params},
    ).result_set
    for row in live:
        output.append({
            "id": fact_uid, "factUid": fact_uid, "source": row[0], "target": row[1],
            "relation": row[2], "evidence": row[3], "validFrom": row[4],
            "validTo": None, "observedFrom": row[5], "observedTo": None,
            "lastConfirmedAt": row[6], "confidence": row[7], "chunkId": row[8],
            "chunkHash": row[9], "extractorVersion": row[10], "model": row[11],
            "documents": sorted(row[12] or []), "state": "live",
        })
    valid_point, observed_point = parse_iso(valid_at), parse_iso(observed_at)
    return [
        item for item in output
        if (valid_point is None or holds_at(
            item["validFrom"], item["validTo"], valid_point,
            attested_from=item.get("attestedFrom") or item["validFrom"],
            ended_unknown=bool(item.get("endedUnknown")),
        ))
        and (observed_point is None or held_at(
            item["observedFrom"], item["observedTo"], observed_point,
        ))
    ]
