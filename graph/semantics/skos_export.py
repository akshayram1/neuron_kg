"""Export the graph as SKOS (Turtle). Ported from
`graphiti_context_explorer/graph/skos_export.py` per plan.md §6b.1.

SKOS only defines generic semantic relations (skos:broader, skos:narrower,
skos:related) — no custom named predicates — so every fact becomes
skos:related plus a reified ctx:Fact carrying the real relation name and
evidence. Unlike the source version, the reified Fact also carries temporal
and provenance fields, since those are this project's actual value-add
(plan.md §2.4) and the original exporter dropped them entirely.

Takes the same node/edge dict shape `graph.graph_view.fetch_graph` returns —
no Graphiti types involved.
"""

from __future__ import annotations

from typing import Any

_PREFIXES = (
    "@prefix skos: <http://www.w3.org/2004/02/skos/core#> .\n"
    "@prefix ctx: <urn:neuron-context:> .\n"
    "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n"
)


def _escape(value: Any) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _slug(value: str) -> str:
    """Turtle local names cannot contain most punctuation."""
    return "".join(character if character.isalnum() else "_" for character in value)


def build_skos_turtle(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]], *, include_history: bool = False
) -> str:
    """include_history=False (default) exports only live facts
    (invalidAt is None) — otherwise a superseded fact would export as if it
    were still true, which is worse than not exporting it (plan.md §6b.1)."""
    lines: list[str] = [_PREFIXES, ""]

    schemes = sorted({node.get("group", "") for node in nodes if node.get("group")})
    for scheme in schemes:
        lines.append(f'ctx:scheme_{_slug(scheme)} a skos:ConceptScheme ;\n    skos:prefLabel "{_escape(scheme)}" .')
    lines.append("")

    known_ids = {node["id"] for node in nodes}
    for node in nodes:
        subject = f"ctx:concept_{_slug(node['id'])}"
        triples = [f'skos:prefLabel "{_escape(node["label"])}"', f'skos:notation "{_escape(node["type"])}"']
        if node.get("group"):
            triples.append(f"skos:inScheme ctx:scheme_{_slug(node['group'])}")
        if node.get("summary"):
            triples.insert(1, f'skos:definition "{_escape(node["summary"])}"')
        lines.append(f"{subject} a skos:Concept ;\n    " + " ;\n    ".join(triples) + " .")
    lines.append("")

    for edge in edges:
        if edge["source"] not in known_ids or edge["target"] not in known_ids:
            continue
        if not include_history and edge.get("invalidAt"):
            continue
        source = f"ctx:concept_{_slug(edge['source'])}"
        target = f"ctx:concept_{_slug(edge['target'])}"
        lines.append(f"{source} skos:related {target} .")
        fact_triples = [
            f'ctx:relationType "{_escape(edge["label"])}"',
            f"ctx:subject {source}",
            f"ctx:object {target}",
            f'skos:note "{_escape(edge.get("fact", ""))}"',
        ]
        if edge.get("validAt"):
            fact_triples.append(f'ctx:validFrom "{_escape(edge["validAt"])}"^^xsd:dateTime')
        if edge.get("invalidAt"):
            fact_triples.append(f'ctx:validTo "{_escape(edge["invalidAt"])}"^^xsd:dateTime')
        if edge.get("confidence") is not None:
            fact_triples.append(f'ctx:confidence "{edge["confidence"]}"^^xsd:double')
        lines.append(f"[] a ctx:Fact ;\n    " + " ;\n    ".join(fact_triples) + " .")
    return "\n".join(lines) + "\n"
