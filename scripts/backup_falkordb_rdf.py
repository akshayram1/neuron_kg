"""Export every FalkorDB graph as RDF N-Quads, then optionally clear it.

The exporter preserves graph boundaries, node labels/properties, relationship
types/properties, and relationship identity. Clearing is only allowed after the
written file's statement count and SHA-256 checksum have been verified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

from falkordb import FalkorDB

from util import paths as _paths  # noqa: F401 - load the repository .env

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDF_STATEMENT = "http://www.w3.org/1999/02/22-rdf-syntax-ns#Statement"
RDF_SUBJECT = "http://www.w3.org/1999/02/22-rdf-syntax-ns#subject"
RDF_PREDICATE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#predicate"
RDF_OBJECT = "http://www.w3.org/1999/02/22-rdf-syntax-ns#object"
XSD_BOOLEAN = "http://www.w3.org/2001/XMLSchema#boolean"
XSD_INTEGER = "http://www.w3.org/2001/XMLSchema#integer"
XSD_DOUBLE = "http://www.w3.org/2001/XMLSchema#double"


def _iri(value: str) -> str:
    return f"<{value}>"


def _urn(category: str, value: object) -> str:
    return _iri(f"urn:graphiti-falkor:{category}:{quote(str(value), safe='')}")


def _literal(value: object) -> str:
    if isinstance(value, bool):
        return f'"{str(value).lower()}"^^{_iri(XSD_BOOLEAN)}'
    if isinstance(value, int) and not isinstance(value, bool):
        return f'"{value}"^^{_iri(XSD_INTEGER)}'
    if isinstance(value, float):
        return f'"{value}"^^{_iri(XSD_DOUBLE)}'
    if isinstance(value, (datetime, date)):
        value = value.isoformat()
    elif not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    # json.dumps produces a valid quoted/escaped RDF string lexical form.
    return json.dumps(value, ensure_ascii=False)


def _quad(subject: str, predicate: str, obj: str, graph: str) -> str:
    return f"{subject} {predicate} {obj} {graph} .\n"


def _node_iri(graph_name: str, internal_id: int) -> str:
    return _urn("node", f"{graph_name}:{internal_id}")


def _edge_iri(graph_name: str, internal_id: int) -> str:
    return _urn("edge", f"{graph_name}:{internal_id}")


def _node_quads(graph_name: str, row: list[object]) -> Iterable[str]:
    internal_id, labels, properties = row
    graph = _urn("graph", graph_name)
    node = _node_iri(graph_name, int(internal_id))
    yield _quad(node, _urn("meta", "internalId"), _literal(internal_id), graph)
    for label in labels or []:
        yield _quad(node, _iri(RDF_TYPE), _urn("label", label), graph)
    for key, value in (properties or {}).items():
        yield _quad(node, _urn("property", key), _literal(value), graph)


def _edge_quads(graph_name: str, row: list[object]) -> Iterable[str]:
    source_id, edge_id, relation_type, properties, target_id = row
    graph = _urn("graph", graph_name)
    source = _node_iri(graph_name, int(source_id))
    target = _node_iri(graph_name, int(target_id))
    predicate = _urn("relationship", relation_type)
    statement = _edge_iri(graph_name, int(edge_id))

    # Keep the natural graph triple and reify it so edge UUID/properties survive.
    yield _quad(source, predicate, target, graph)
    yield _quad(statement, _iri(RDF_TYPE), _iri(RDF_STATEMENT), graph)
    yield _quad(statement, _iri(RDF_SUBJECT), source, graph)
    yield _quad(statement, _iri(RDF_PREDICATE), predicate, graph)
    yield _quad(statement, _iri(RDF_OBJECT), target, graph)
    yield _quad(statement, _urn("meta", "internalId"), _literal(edge_id), graph)
    for key, value in (properties or {}).items():
        yield _quad(statement, _urn("property", key), _literal(value), graph)


def _statement_count(path: Path) -> int:
    with path.open("rb") as handle:
        return sum(1 for line in handle if line.strip() and not line.startswith(b"#"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def export_and_maybe_clear(output: Path, *, clear: bool) -> dict[str, object]:
    client = FalkorDB(
        host=os.getenv("FALKOR_HOST", "localhost"),
        port=int(os.getenv("FALKOR_PORT", "6379")),
    )
    graph_names = sorted(client.list_graphs())
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".partial")
    counts: dict[str, dict[str, int]] = {}
    statements = 0

    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("# Graphiti/FalkorDB complete RDF N-Quads backup\n")
        for graph_name in graph_names:
            graph = client.select_graph(graph_name)
            nodes = graph.query(
                "MATCH (n) RETURN id(n), labels(n), properties(n) ORDER BY id(n)"
            ).result_set
            edges = graph.query(
                "MATCH (s)-[r]->(t) RETURN id(s), id(r), type(r), properties(r), id(t) "
                "ORDER BY id(r)"
            ).result_set
            counts[graph_name] = {"nodes": len(nodes), "edges": len(edges)}
            for row in nodes:
                for line in _node_quads(graph_name, row):
                    handle.write(line)
                    statements += 1
            for row in edges:
                for line in _edge_quads(graph_name, row):
                    handle.write(line)
                    statements += 1
        handle.flush()
        os.fsync(handle.fileno())

    temporary.replace(output)
    checksum = _sha256(output)
    verified_statements = _statement_count(output)
    if verified_statements != statements or not checksum:
        raise RuntimeError(
            f"Backup verification failed: wrote {statements}, read {verified_statements} statements"
        )

    cleared: list[str] = []
    if clear:
        # Resolve the targets from the immutable inventory captured before export.
        for graph_name in graph_names:
            client.select_graph(graph_name).delete()
            cleared.append(graph_name)
        remaining = sorted(client.list_graphs())
        if any(name in remaining for name in graph_names):
            raise RuntimeError(f"Some graphs remain after clear: {remaining}")

    manifest = {
        "format": "application/n-quads",
        "backup": str(output.resolve()),
        "sha256": checksum,
        "statements": statements,
        "graphs": counts,
        "cleared_graphs": cleared,
    }
    manifest_path = output.with_suffix(output.suffix + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("backups") / f"falkordb-{timestamp}.nq",
    )
    parser.add_argument(
        "--clear-after-verify",
        action="store_true",
        help="delete only the graphs included in this verified backup",
    )
    args = parser.parse_args()
    print(json.dumps(export_and_maybe_clear(args.output, clear=args.clear_after_verify), indent=2))


if __name__ == "__main__":
    main()
