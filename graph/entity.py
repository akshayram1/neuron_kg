"""Entity-level Relations / History / Derived reads (Utopia entity dock)."""

from __future__ import annotations

from falkordb import Graph

from graph.access import AccessScope
from graph.axioms import DEFAULT_AXIOMS
from graph.time_axis import as_fact_dict, held_at, holds_at, parse_iso
from graph.writer import make_uid


def fetch_entity_detail(
    graph: Graph,
    scope: AccessScope,
    uid: str,
    *,
    at: str | None = None,
    at_end: str | None = None,
    as_of: str | None = None,
    providers: list[str] | None = None,
) -> dict | None:
    entity = _entity(graph, scope, uid, providers)
    if entity is None:
        return None
    at_point, as_of_point = parse_iso(at), parse_iso(as_of)
    # `at_end` makes the read a window rather than a snapshot; an event
    # relation must land inside it, a state need only overlap it, so the
    # relation's temporal class comes from the axiom store.
    end_point = parse_iso(at_end)
    axioms = DEFAULT_AXIOMS if end_point is not None else None
    facts = _facts(graph, scope, uid, providers)
    visible = [
        fact for fact in facts
        if holds_at(
            fact["validFrom"], fact["validTo"], at_point,
            at_end=end_point,
            temporal="state" if axioms is None else axioms.temporal_of(fact["relation"]),
            attested_from=fact.get("attestedFrom"),
            ended_unknown=bool(fact.get("endedUnknown")),
        ) and (as_of_point is None or held_at(fact["observedFrom"], fact["observedTo"], as_of_point))
    ]
    current, past, derived = [], [], []
    for fact in visible:
        if fact.get("derived") or fact.get("derivedRule"):
            derived.append(fact)
            continue
        if fact["state"] == "live" and not fact.get("validTo"):
            current.append(fact)
        else:
            past.append(fact)
    return {
        "entity": entity,
        "facts": current,
        "past": past,
        "derived": derived,
        "history": _history_events(visible),
        "at": at,
        "asOf": as_of,
    }


def _entity(
    graph: Graph, scope: AccessScope, uid: str, providers: list[str] | None,
) -> dict | None:
    acl, params = scope.cypher("sr", "entity_acl")
    provider_filter = "AND sr.provider IN $providers" if providers else ""
    rows = graph.query(
        f"""
        MATCH (n {{uid: $uid}})-[:MENTIONED_IN]->(sr:SourceRecord)
        WHERE sr.deleted_at IS NULL AND {acl} {provider_filter}
        RETURN n.uid, labels(n)[0], n.name, n.search_text, n.definition,
               n.statement, n.purpose, n.status, n.issue_key, n.url,
               n.email, n.sha, n.path, n.language, n.issue_type, n.authored_at,
               n.last_edited_time, n.state, n.pr_ref, n.source_branch,
               n.destination_branch, n.default_branch,
               collect(DISTINCT sr.provider), collect(DISTINCT sr.name)
        """,
        params={"uid": uid, **params, **({"providers": providers} if providers else {})},
    ).result_set
    if not rows:
        return None
    row = rows[0]
    search_text = row[3] or ""
    summary = row[4] or row[5] or row[6] or (search_text[:200] if search_text else "") or ""
    fields = {
        key: value for key, value in {
            "email": row[10],
            "sha": row[11],
            "path": row[12],
            "language": row[13],
            "issueType": row[14],
            "authoredAt": row[15],
            "lastEditedTime": row[16],
            "state": row[17],
            "prRef": row[18],
            "sourceBranch": row[19],
            "destinationBranch": row[20],
            "defaultBranch": row[21],
        }.items() if value
    }
    return {
        "id": row[0], "type": row[1], "label": row[2] or row[0],
        "summary": summary, "status": row[7], "issueKey": row[8], "url": row[9],
        "body": search_text,
        "fields": fields,
        "providers": row[22] or [], "documents": sorted(row[23] or []),
    }


def _facts(
    graph: Graph, scope: AccessScope, uid: str, providers: list[str] | None,
) -> list[dict]:
    provider_filter = "AND sr.provider IN $providers" if providers else ""
    out: list[dict] = []
    live_acl, live_params = scope.cypher("sr", "entity_live_acl")
    for direction, match, names in (
        ("out", "MATCH (n {uid: $uid})-[r]->(other)", "n.name, other.name, other.uid"),
        ("in", "MATCH (other)-[r]->(n {uid: $uid})", "other.name, n.name, other.uid"),
    ):
        rows = graph.query(
            f"""
            {match}
            WHERE type(r) <> 'MENTIONED_IN'
            UNWIND coalesce(r.source_record_keys, []) AS source_key
            MATCH (sr:SourceRecord {{record_key: source_key}})
            WHERE sr.deleted_at IS NULL AND {live_acl} {provider_filter}
            RETURN type(r), {names}, r.evidence, r.valid_at, r.invalid_at,
                   r.first_seen_at, r.fact_uid, r.derived, r.derived_rule,
                   r.premise_fact_uids, r.ended_unknown, r.attested_from,
                   r.extraction_method, collect(DISTINCT sr.record_key),
                   collect(DISTINCT sr.name)
            """,
            params={"uid": uid, **live_params, **({"providers": providers} if providers else {})},
        ).result_set
        for row in rows:
            (rel, subj, obj, other_uid, evidence, valid_at, invalid_at, first_seen,
             fact_uid, derived, rule, premises, ended_unknown, attested, method,
             record_keys, documents) = row
            live = invalid_at is None
            out.append(as_fact_dict(
                source=subj, target=obj, relation=rel, evidence=evidence,
                valid_from=valid_at, valid_to=None if live else invalid_at,
                observed_from=first_seen, observed_to=None if live else invalid_at,
                documents=sorted(documents or []),
                state="live" if live else "historical",
                derived=bool(derived) or method == "derived",
                derived_rule=rule, premises=list(premises or []),
                ended_unknown=bool(ended_unknown), attested_from=attested,
                fact_uid=fact_uid or make_uid("Fact", uid if direction == "out" else other_uid, rel,
                                              other_uid if direction == "out" else uid),
                from_uid=uid if direction == "out" else other_uid,
                to_uid=other_uid if direction == "out" else uid,
                direction=direction,
                extra={"otherUid": other_uid, "otherName": obj if direction == "out" else subj,
                       "recordKeys": list(record_keys or [])},
            ))

    hist_acl, hist_params = scope.cypher("sr", "entity_hist_acl")
    rows = graph.query(
        f"""
        MATCH (h:FactHistory)
        WHERE h.from_uid = $uid OR h.to_uid = $uid
        UNWIND coalesce(h.source_record_keys, []) AS source_key
        MATCH (sr:SourceRecord {{record_key: source_key}})
        WHERE sr.deleted_at IS NULL AND {hist_acl} {provider_filter}
        RETURN h.from_uid, h.to_uid, h.relation, h.evidence, h.valid_from, h.valid_to,
               h.observed_from, h.observed_to, h.fact_uid, h.ended_unknown,
               h.attested_from, h.name, collect(DISTINCT sr.record_key),
               collect(DISTINCT sr.name)
        """,
        params={"uid": uid, **hist_params, **({"providers": providers} if providers else {})},
    ).result_set
    for row in rows:
        (from_uid, to_uid, rel, evidence, valid_from, valid_to, observed_from,
         observed_to, fact_uid, ended_unknown, attested, name, record_keys, documents) = row
        direction = "out" if from_uid == uid else "in"
        parts = (name or "").split(f" {rel} ") if rel and name else []
        source_name = parts[0] if len(parts) == 2 else from_uid
        target_name = parts[1] if len(parts) == 2 else to_uid
        out.append(as_fact_dict(
            source=source_name, target=target_name, relation=rel, evidence=evidence,
            valid_from=valid_from, valid_to=valid_to,
            observed_from=observed_from, observed_to=observed_to,
            documents=sorted(documents or []), state="historical",
            ended_unknown=bool(ended_unknown), attested_from=attested,
            fact_uid=fact_uid, from_uid=from_uid, to_uid=to_uid, direction=direction,
            extra={"otherUid": to_uid if direction == "out" else from_uid,
                   "otherName": target_name if direction == "out" else source_name,
                   "recordKeys": list(record_keys or [])},
        ))
    return out


def _history_events(facts: list[dict]) -> list[dict]:
    """Record-axis feed: when we asserted or closed each interval."""
    events = []
    for fact in facts:
        kind = "asserted" if fact["state"] == "live" else "corrected"
        events.append({
            "kind": kind,
            "relation": fact["relation"],
            "source": fact["source"],
            "target": fact["target"],
            "at": fact["observedTo"] or fact["observedFrom"] or fact["validFrom"],
            "interval": fact.get("interval"),
            "documents": fact["documents"],
            "derived": fact.get("derived") or False,
            "derivedRule": fact.get("derivedRule"),
            "evidence": fact.get("evidence"),
            "direction": fact.get("direction"),
        })
    events.sort(key=lambda item: item["at"] or "", reverse=True)
    return events
