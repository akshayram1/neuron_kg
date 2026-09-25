"""Load ``story/`` fixtures through the real connector ingestion pipeline.

Only remote OAuth/API fetching is replaced. Dataclass payloads, SourceRecord
normalization, hashes, chunks, embeddings, semantic extraction, Falkor writes,
temporal invalidation and edge-support bookkeeping are the production code.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from falkordb import Graph
from openai import OpenAI

from connectors.bitbucket.api import (
    BitbucketCommit,
    BitbucketFile,
    BitbucketFileChange,
    BitbucketPullRequest,
    BitbucketRepository,
)
from connectors.core.actions import RecordAction
from connectors.core.models import SourceRecord
from connectors.jira.api import JiraChange, JiraIssue, JiraPerson, JiraProject, JiraSite
from connectors.notion.api import NotionPage
from graph import bitbucket_pipeline as bp
from graph import jira_pipeline as jp
from graph import notion_pipeline as np
from graph import vector_store
from graph import writer as w
from graph.embed_batch import close_batch, open_batch
from graph.semantic_pass import SemanticContext, run_semantic_pass
from graph.token_usage import TokenUsage
from graph.wisdom import (
    generate_wisdom_proposal,
    migration_claim_fallback,
    removal_readiness_fallback,
)
from storage.ledger import PostgresLedger
from storage.postgres import PostgresStore

logger = logging.getLogger("neuron.story")
_MIGRATION_WISDOM_PATTERN = "migration-claims-require-code-confirmation"
_REMOVAL_WISDOM_PATTERN = "api-removal-requires-consumer-verification"

PHASES = {
    "baseline": "baseline",
    "deprecation": "events/01_auth_v1_deprecation",
    "migration-claim": "events/02_mcp_migration_claim",
    "code-catches-up": "events/03_mcp_code_catches_up",
    "v1-removal": "events/04_auth_v1_removed",
}

_API_RE = re.compile(r"\bAuth API v([12])\b", re.IGNORECASE)
_ENDPOINT_RE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/v[12]/[a-z0-9_/-]+)", re.IGNORECASE)


@dataclass
class StoryStats:
    records_total: int = 0
    records_written: int = 0
    records_kept: int = 0
    semantic_chunks: int = 0
    semantic_entities: int = 0
    semantic_facts: int = 0
    llm_calls: int = 0
    llm_findings: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    chunks_total: int = 0
    chunks_deterministic: int = 0
    chunks_partial: int = 0
    chunks_unresolved: int = 0
    pass1_records_linked: int = 0
    pass1_links_written: int = 0
    chunks_llm_skipped: int = 0
    chunks_hybrid: int = 0
    chunks_llm_only: int = 0
    wisdom_llm_calls: int = 0
    wisdom_proposals: int = 0
    wisdom_input_tokens: int = 0
    wisdom_output_tokens: int = 0

    def as_dict(self) -> dict:
        return {
            "records_total": self.records_total,
            "records_written": self.records_written,
            "records_kept": self.records_kept,
            "chunks_ingested": self.semantic_chunks,
            "entities_written": self.semantic_entities,
            "facts_written": self.semantic_facts,
            "llm_calls": self.llm_calls,
            "llm_findings": self.llm_findings,
            "ingestion_input_tokens": self.llm_input_tokens + self.wisdom_input_tokens,
            "ingestion_output_tokens": self.llm_output_tokens + self.wisdom_output_tokens,
            "ingestion_total_tokens": (
                self.llm_input_tokens + self.llm_output_tokens
                + self.wisdom_input_tokens + self.wisdom_output_tokens
            ),
            "chunks_total": self.chunks_total,
            "chunks_deterministic": self.chunks_deterministic,
            "chunks_partial": self.chunks_partial,
            "chunks_unresolved": self.chunks_unresolved,
            "pass1_records_linked": self.pass1_records_linked,
            "pass1_links_written": self.pass1_links_written,
            "chunks_llm_skipped": self.chunks_llm_skipped,
            "chunks_hybrid": self.chunks_hybrid,
            "chunks_llm_only": self.chunks_llm_only,
            "wisdom_llm_calls": self.wisdom_llm_calls,
            "wisdom_proposals": self.wisdom_proposals,
            "wisdom_input_tokens": self.wisdom_input_tokens,
            "wisdom_output_tokens": self.wisdom_output_tokens,
            "wisdom_total_tokens": self.wisdom_input_tokens + self.wisdom_output_tokens,
        }


class StoryIngestor:
    def __init__(
        self, *, root: Path, graph: Graph, store: PostgresStore,
        ledger: PostgresLedger, graph_id: str, collection: str,
    ):
        self.root = root
        self.graph = graph
        self.store = store
        self.ledger = ledger
        self.graph_id = graph_id
        self.collection = collection
        manifest = json.loads((root / "manifest.json").read_text())
        self.projects = {item["slug"]: item for item in manifest["projects"]}
        self._jira_identity = self._baseline_jira_identities()
        self._written_record_keys: set[str] = set()

    def _baseline_jira_identities(self) -> dict[str, tuple[str, str, str]]:
        output = {}
        for slug in self.projects:
            data = json.loads((self.root / "baseline" / slug / "jira.json").read_text())
            output[slug] = (
                data["connection_id"], data["site"]["cloud_id"], data["project"]["project_id"],
            )
        return output

    def project_uid(self, project_ref: str) -> str:
        connection_id, cloud_id, project_id = self._jira_identity[project_ref]
        return jp.project_uid(connection_id, f"{cloud_id}:{project_id}")

    def ingest(
        self, phase: str, *, run_llm: bool = True,
        progress: Callable[[dict], None] | None = None,
    ) -> StoryStats:
        if phase not in PHASES:
            raise ValueError(f"unknown story phase: {phase}")
        self._written_record_keys.clear()
        phase_root = self.root / PHASES[phase]
        event_started_at = datetime.now(UTC)
        bundles = sorted({
            path.parent
            for filename in ("jira.json", "notion.json", "bitbucket.json")
            for path in phase_root.rglob(filename)
        })
        source_files_total = sum(
            (parent / filename).exists()
            for parent in bundles
            for filename in ("jira.json", "notion.json", "bitbucket.json")
        )
        stats = StoryStats()
        embedding_client = OpenAI(timeout=30.0, max_retries=2)
        open_batch(
            embedding_client, os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
            self.collection,
        )
        try:
            done = 0
            for parent in bundles:
                project_ref = self._project_ref(parent)
                for filename, handler in (
                    ("jira.json", self._ingest_jira),
                    ("notion.json", self._ingest_notion),
                    ("bitbucket.json", self._ingest_bitbucket),
                ):
                    path = parent / filename
                    if not path.exists():
                        continue
                    written, kept = handler(path, project_ref)
                    stats.records_total += written + kept
                    stats.records_written += written
                    stats.records_kept += kept
                    done += 1
                    if progress:
                        progress({**stats.as_dict(), "phase": "ingesting", "current": str(path.relative_to(self.root)), "bundles_done": done, "bundles_total": source_files_total})
            close_batch()

            context_provider = self._index_evidence_chunks(
                embedding_client, before=event_started_at,
            )
            pass1_links = self._pass1_link_counts()
            resolution = self.store.chunk_resolution_counts(
                self.graph_id, since=event_started_at,
                linked_record_keys=pass1_links.keys(),
            )
            stats.chunks_total = resolution["chunks_total"]
            stats.chunks_deterministic = resolution["chunks_deterministic"]
            stats.chunks_partial = resolution["chunks_partial"]
            stats.chunks_unresolved = resolution["chunks_unresolved"]
            stats.chunks_llm_skipped = resolution["chunks_llm_skipped"]
            stats.chunks_hybrid = resolution["chunks_hybrid"]
            stats.chunks_llm_only = resolution["chunks_llm_only"]
            stats.pass1_records_linked = len(pass1_links)
            stats.pass1_links_written = sum(pass1_links.values())

            if run_llm:
                semantic = run_semantic_pass(
                    self.graph, self.ledger, collection=self.collection,
                    context_provider=context_provider,
                    on_progress=(
                        lambda done_chunks, total_chunks, record_key, current: progress({
                            **stats.as_dict(), "phase": "semantic", "current": record_key,
                            "chunks_ingested": done_chunks, "chunks_total": total_chunks,
                            "entities_written": current.entities_written,
                            "facts_written": current.facts_written,
                        }) if progress else None
                    ),
                )
                stats.semantic_chunks = semantic.chunks_processed
                stats.semantic_entities = semantic.entities_written
                stats.semantic_facts = semantic.facts_written
                stats.llm_calls = semantic.llm_calls
                stats.llm_findings = semantic.findings_written
                stats.llm_input_tokens = semantic.token_usage.input_tokens
                stats.llm_output_tokens = semantic.token_usage.output_tokens
            # The dedicated story graph contains synthetic fixtures only and
            # must be visible in a CTO demo without Jira/Notion/Bitbucket
            # OAuth cookies. Normal connector graphs retain their source ACLs.
            self.graph.query(
                "MATCH (sr:SourceRecord) "
                "SET sr.public=true, sr.policy_version='story-public-v1'"
            )
            self._recompute_current_consumption()
            self.recompute_findings()
            self._project_findings()
            wisdom_runs = (
                self._sync_wisdom(embedding_client, run_llm=run_llm),
                self._sync_removal_wisdom(embedding_client, run_llm=run_llm),
            )
            stats.wisdom_llm_calls = sum(item["llm_calls"] for item in wisdom_runs)
            stats.wisdom_proposals = sum(item["proposals"] for item in wisdom_runs)
            stats.wisdom_input_tokens = sum(item["input_tokens"] for item in wisdom_runs)
            stats.wisdom_output_tokens = sum(item["output_tokens"] for item in wisdom_runs)
            self.project_wisdom()
            return stats
        finally:
            close_batch()

    def _index_evidence_chunks(self, client: OpenAI, *, before: datetime):
        """Embed canonical chunks and return bounded prior-evidence retrieval.

        The cutoff is captured before this phase writes anything. This is the
        key guardrail: the new event may query older related evidence, but its
        own records cannot leak into the comparison set.
        """
        model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
        while chunks := self.store.unembedded_chunks(self.graph_id, limit=200):
            texts = [vector_store.truncate_for_embedding(item["text"]) for item in chunks]
            response = client.embeddings.create(model=model, input=texts)
            self.store.set_chunk_embeddings(self.graph_id, [{
                "record_key": item["record_key"], "chunk_id": item["chunk_id"],
                "embedding": embedded.embedding,
            } for item, embedded in zip(chunks, response.data)], model=model)

        def related(chunk):
            response = client.embeddings.create(
                model=model, input=[vector_store.truncate_for_embedding(chunk.text)],
            )
            matches = self.store.search_related_chunks(
                self.graph_id, response.data[0].embedding,
                exclude_record_key=chunk.record_key, before=before,
                limit=int(os.getenv("STORY_RELATED_EVIDENCE_LIMIT", "5")),
                min_similarity=float(os.getenv("STORY_RELATED_EVIDENCE_MIN_SCORE", "0.55")),
            )
            nodes = self.store.search_related_nodes(
                self.collection, response.data[0].embedding, before=before,
                limit=int(os.getenv("STORY_RELATED_NODE_LIMIT", "5")),
                min_similarity=float(os.getenv("STORY_RELATED_NODE_MIN_SCORE", "0.55")),
            )
            if not matches and not nodes:
                return None
            sections = [
                f"[{item['provider']} | {item['name']} | similarity={item['similarity']:.3f}]\n"
                f"{item['text']}"
                for item in matches
            ]
            sections.extend(
                f"[CANDIDATE NODE uid={item['uid']} kind={item['label']} "
                f"similarity={item['similarity']:.3f}]\n{item['text']}"
                for item in nodes
            )
            return SemanticContext(
                text="\n\n".join(sections),
                candidate_uids=frozenset(item["uid"] for item in nodes),
            )

        return related

    def _project_ref(self, path: Path) -> str:
        for part in reversed(path.parts):
            if part in self.projects:
                return part
        raise ValueError(f"fixture path does not identify a project: {path}")

    def _track(self, action: RecordAction, record_key: str | None = None) -> tuple[int, int]:
        if action != RecordAction.KEEP and record_key:
            self._written_record_keys.add(record_key)
        return (0, 1) if action == RecordAction.KEEP else (1, 0)

    def _pass1_link_counts(self) -> dict[str, int]:
        """Current links supported by records written during this Pass-1 run.

        This is captured before the semantic pass, so it cannot accidentally
        include relationships subsequently proposed by the LLM.
        """
        return {
            record_key: len(edges)
            for record_key in self._written_record_keys
            if (edges := self.ledger.edges_for_record(record_key))
        }

    def _stage(self, record: SourceRecord, project_ref: str | None) -> None:
        self.ledger.stage(record, project_ref)

    def _ingest_jira(self, path: Path, project_ref: str) -> tuple[int, int]:
        raw = json.loads(path.read_text())
        site = JiraSite(**raw["site"])
        project = JiraProject(**raw["project"])
        issues = []
        for value in raw["issues"]:
            payload = dict(value)
            payload["assignee"] = JiraPerson(**payload["assignee"]) if payload.get("assignee") else None
            payload["reporter"] = JiraPerson(**payload["reporter"]) if payload.get("reporter") else None
            payload["labels"] = tuple(payload.get("labels") or ())
            payload["blocks"] = tuple(payload.get("blocks") or ())
            payload["changes"] = tuple(JiraChange(**item) for item in payload.get("changes") or ())
            issues.append(JiraIssue(**payload))
        connection_id = raw["connection_id"]
        written = kept = 0
        project_record = jp.project_record(project, site, connection_id)
        self._stage(project_record, project_ref)
        action = jp.write_project(self.graph, self.ledger, project, site, connection_id)
        a, b = self._track(action, project_record.record_key); written += a; kept += b
        for issue in issues:
            record = jp.issue_record(issue, project, site, connection_id)
            self._stage(record, project_ref)
            action = jp.write_issue(
                self.graph, self.ledger, issue, project, site, connection_id,
                collection=self.collection,
            )
            a, b = self._track(action, record.record_key); written += a; kept += b
            if action != RecordAction.KEEP:
                self._enrich(record, project_ref, jp.work_item_uid(connection_id, record.external_id), "WorkItem")
        return written, kept

    def _ingest_notion(self, path: Path, project_ref: str) -> tuple[int, int]:
        raw = json.loads(path.read_text())
        workspace_id, workspace_name = raw["workspace_id"], raw["workspace_name"]
        pages = [NotionPage(**value) for value in raw["pages"]]
        written = kept = 0
        workspace_record = np.workspace_record(workspace_id, workspace_name)
        self._stage(workspace_record, None)
        action = np.write_workspace(self.graph, self.ledger, workspace_id, workspace_name)
        a, b = self._track(action, workspace_record.record_key); written += a; kept += b
        for page in pages:
            record = np.page_record(page, workspace_id, workspace_name)
            self._stage(record, project_ref)
            action = np.write_page(self.graph, self.ledger, page, workspace_id, workspace_name)
            a, b = self._track(action, record.record_key); written += a; kept += b
            if action != RecordAction.KEEP:
                uid = np.document_uid(workspace_id, page.page_id)
                self._link_project(project_ref, "HAS_DOCUMENT", "Document", uid, record)
                self._enrich(record, project_ref, uid, "Document")
        return written, kept

    def _ingest_bitbucket(self, path: Path, project_ref: str) -> tuple[int, int]:
        raw = json.loads(path.read_text())
        repository = BitbucketRepository(**raw["repository"])
        files = [BitbucketFile(**value) for value in raw["files"]]
        commits = []
        for value in raw["commits"]:
            payload = dict(value)
            payload["files"] = tuple(BitbucketFileChange(**item) for item in payload.get("files") or ())
            commits.append(BitbucketCommit(**payload))
        pull_requests = [BitbucketPullRequest(**value) for value in raw.get("pull_requests") or ()]
        connection_id = raw["connection_id"]
        written = kept = 0
        record = bp.repository_record(repository, connection_id)
        self._stage(record, project_ref)
        action = bp.write_repository(self.graph, self.ledger, repository, connection_id)
        a, b = self._track(action, record.record_key); written += a; kept += b
        repo_uid = bp.repository_uid(connection_id, repository.uuid)
        if action != RecordAction.KEEP:
            self._link_project(project_ref, "HAS_REPOSITORY", "Repository", repo_uid, record)
            self._enrich(record, project_ref, repo_uid, "Repository")
        present_paths = {file.path for file in files}
        for file in files:
            record = bp.file_record(repository, file, connection_id)
            self._stage(record, project_ref)
            action = bp.write_file(
                self.graph, self.ledger, repository, file, connection_id,
                collection=self.collection,
            )
            a, b = self._track(action, record.record_key); written += a; kept += b
            if action != RecordAction.KEEP:
                self._enrich(
                    record, project_ref,
                    bp.file_uid(connection_id, repository.uuid, file.path), "SourceFile",
                )
        for commit in commits:
            record = bp.commit_record(repository, commit, connection_id)
            self._stage(record, project_ref)
            action = bp.write_commit(
                self.graph, self.ledger, repository, commit, connection_id,
                collection=self.collection, present_paths=present_paths,
            )
            a, b = self._track(action, record.record_key); written += a; kept += b
            if action != RecordAction.KEEP:
                self._enrich(
                    record, project_ref,
                    bp.commit_uid(connection_id, repository.uuid, commit.commit_hash), "Commit",
                )
        for pr in pull_requests:
            record = bp.pull_request_record(repository, pr, connection_id)
            self._stage(record, project_ref)
            action = bp.write_pull_request(
                self.graph, self.ledger, repository, pr, connection_id,
                collection=self.collection,
            )
            a, b = self._track(action, record.record_key); written += a; kept += b
            if action != RecordAction.KEEP:
                self._enrich(
                    record, project_ref,
                    bp.pull_request_uid(connection_id, repository.uuid, pr.id), "PullRequest",
                )
        return written, kept

    def _link_project(
        self, project_ref: str, relation: str, target_label: str,
        target_uid: str, record: SourceRecord,
    ) -> None:
        self._write_fact(
            relation, "Project", target_label, self.project_uid(project_ref), target_uid,
            record, evidence=f"{record.name} belongs to project {project_ref}",
            method="fixture_binding",
        )

    def _semantic_node(self, label: str, name: str, record: SourceRecord, **props) -> str:
        uid = w.make_uid(label, name.strip().lower())
        w.upsert_entities(self.graph, label, [{
            "uid": uid, "props": {"name": name, "search_text": name, **props},
        }])
        w.link_mentioned_in(self.graph, label, [{"uid": uid, "record_key": record.record_key}])
        return uid

    def _write_fact(
        self, relation: str, from_label: str, to_label: str,
        from_uid: str, to_uid: str, record: SourceRecord, *,
        evidence: str, method: str = "deterministic_signal", derived: bool = False,
        derived_rule: str | None = None, premise_fact_uids: list[str] | None = None,
    ) -> str:
        fact_uid = w.make_uid("Fact", from_uid, relation, to_uid)
        valid_at = record.reference_time.isoformat() if record.reference_time else None
        row = {
            "from_uid": from_uid, "to_uid": to_uid,
            "source_record_keys": [record.record_key], "evidence": evidence,
            "extraction_method": method, "confidence": 1.0,
            "valid_at": valid_at, "fact_uid": fact_uid,
            "derived": derived, "derived_rule": derived_rule,
            "premise_fact_uids": premise_fact_uids or [],
        }
        w.upsert_fact_edges(self.graph, relation, from_label, to_label, [row])
        self.ledger.record_edge(record.record_key, relation, from_uid, to_uid)
        self.store.upsert_facts(self.graph_id, [{
            "fact_uid": fact_uid, "subject_uid": from_uid, "subject_label": from_label,
            "predicate": relation, "object_uid": to_uid, "object_label": to_label,
            "source_record_keys": [record.record_key], "evidence": evidence,
            "extraction_method": method, "confidence": 1.0,
            "valid_from": valid_at, "derived": derived, "derived_rule": derived_rule,
            "premise_fact_uids": premise_fact_uids or [],
        }])
        return fact_uid

    def _enrich(self, record: SourceRecord, project_ref: str, primary_uid: str, primary_label: str) -> None:
        text = record.content
        versions = sorted(set(_API_RE.findall(text)))
        endpoints = sorted(set((method.upper(), path.rstrip(".,`")) for method, path in _ENDPOINT_RE.findall(text)))
        if (
            project_ref == "auth-service" and primary_label == "SourceFile"
            and record.name.endswith("src/api.py")
        ):
            # The provider router is authoritative for which endpoints still
            # exist. End the old exposure set before writing this snapshot.
            rows = self.graph.query(
                """MATCH (:Project {uid: $project_uid})-[:PROVIDES_API]->(api:Api)
                   RETURN DISTINCT api.uid""",
                params={"project_uid": self.project_uid(project_ref)},
            ).result_set
            api_uids = [row[0] for row in rows]
            w.supersede_fact_edges(
                self.graph, "EXPOSES_ENDPOINT", "Api", "Endpoint", api_uids,
            )
            self.store.invalidate_facts(
                self.graph_id, "EXPOSES_ENDPOINT", subject_uids=api_uids,
            )
        if primary_label == "SourceFile":
            # A source file is a current snapshot. Its endpoint-call set may
            # contain several endpoints, but every old call absent from this
            # snapshot must end before current calls are written. This also
            # protects authority from a later semantic pass attempting to
            # revive a retrieved historical endpoint candidate.
            w.supersede_fact_edges(
                self.graph, "CALLS_ENDPOINT", "SourceFile", "Endpoint", [primary_uid],
            )
            self.store.invalidate_facts(
                self.graph_id, "CALLS_ENDPOINT", subject_uids=[primary_uid],
            )
        api_uids: dict[str, str] = {}
        endpoint_uids: dict[str, str] = {}
        for version in versions:
            name = f"Auth API v{version}"
            api_uids[version] = self._semantic_node("Api", name, record, version=f"v{version}")
        for method, endpoint_path in endpoints:
            name = f"{method} {endpoint_path}"
            endpoint_uids[endpoint_path] = self._semantic_node(
                "Endpoint", name, record, method=method, path=endpoint_path,
            )
            version_match = re.search(r"/v([12])/", endpoint_path)
            if version_match:
                version = version_match.group(1)
                api_uid = api_uids.get(version) or self._semantic_node(
                    "Api", f"Auth API v{version}", record, version=f"v{version}",
                )
                self._write_fact(
                    "EXPOSES_ENDPOINT", "Api", "Endpoint", api_uid,
                    endpoint_uids[endpoint_path], record, evidence=name,
                )

        lower = text.lower()
        negates_removal = bool(re.search(
            r"\b(?:does not|do not|not|without|no)\b.{0,60}\b(?:remov\w*|deprecat\w*)\b",
            lower,
        ))
        project_uid = self.project_uid(project_ref)
        for version, api_uid in api_uids.items():
            api_name = f"Auth API v{version}"
            if project_ref == "auth-service" and ("provides" in lower or primary_label in {"SourceFile", "Repository"}):
                self._write_fact(
                    "PROVIDES_API", "Project", "Api", project_uid, api_uid, record,
                    evidence=f"{record.name}: {api_name}",
                )
            if project_ref != "auth-service" and any(word in lower for word in ("consume", "depend", "calls", "uses")):
                self._write_fact(
                    "CONSUMES_API", "Project", "Api", project_uid, api_uid, record,
                    evidence=f"{record.name}: {api_name}",
                )
            if (
                project_ref == "auth-service" and primary_label == "WorkItem"
                and version == "1" and not negates_removal
                and any(word in lower for word in ("deprecat", "remove", "removal"))
            ):
                self._write_fact(
                    "DEPRECATES", "WorkItem", "Api", primary_uid, api_uid, record,
                    evidence=f"{record.name} deprecates {api_name}",
                )
                if (
                    any(phrase in lower for phrase in ("status: done", "removed auth api v1", "removal was completed"))
                    and "2026-10-01" in lower
                ):
                    self._write_fact(
                        "REMOVES", "WorkItem", "Api", primary_uid, api_uid, record,
                        evidence=f"{record.name} records completed removal of {api_name}",
                    )
            if project_ref != "auth-service" and version == "2" and "migrat" in lower:
                self._write_fact(
                    "MIGRATES_TO", "Project", "Api", project_uid, api_uid, record,
                    evidence=f"{record.name}: migration to {api_name}",
                )

        if primary_label == "SourceFile":
            for endpoint_path, endpoint_uid in endpoint_uids.items():
                if project_ref != "auth-service":
                    self._write_fact(
                        "CALLS_ENDPOINT", "SourceFile", "Endpoint", primary_uid,
                        endpoint_uid, record, evidence=f"code references {endpoint_path}",
                        method="code_signal",
                    )

    def recompute_findings(self) -> None:
        # Recompute the blast radius from current code, not from a migration
        # ticket's claim. This is the authority policy demonstrated by phase 3.
        change_rows = self.graph.query(
            """MATCH (change:WorkItem)-[d:DEPRECATES]->(:Api)
               WHERE d.invalid_at IS NULL RETURN DISTINCT change.uid"""
        ).result_set
        change_uids = [row[0] for row in change_rows]
        w.supersede_fact_edges(
            self.graph, "MAY_IMPACT", "WorkItem", "Project", change_uids,
        )
        self.store.invalidate_facts(
            self.graph_id, "MAY_IMPACT", subject_uids=change_uids,
        )

        planned_rows = self.graph.query(
            """
            MATCH (change:WorkItem)-[d:DEPRECATES]->(api:Api)-[:EXPOSES_ENDPOINT]->(ep:Endpoint)
                  <-[call:CALLS_ENDPOINT]-(file:SourceFile)<-[:CONTAINS]-(repo:Repository)
                  <-[:HAS_REPOSITORY]-(project:Project)
            WHERE d.invalid_at IS NULL AND call.invalid_at IS NULL
            RETURN DISTINCT change.uid, change.name, api.name, project.uid, project.name,
                   d.fact_uid, call.fact_uid, d.source_record_keys, call.source_record_keys
            """
        ).result_set
        removed_rows = self.graph.query(
            """
            MATCH (change:WorkItem)-[d:REMOVES]->(api:Api),
                  (file:SourceFile)-[call:CALLS_ENDPOINT]->(ep:Endpoint)
                  <-[:EXPOSES_ENDPOINT]-(:Api)
            MATCH (file)<-[:CONTAINS]-(repo:Repository)<-[:HAS_REPOSITORY]-(project:Project)
            WHERE d.invalid_at IS NULL AND call.invalid_at IS NULL
              AND api.version = 'v1' AND ep.path STARTS WITH '/v1/'
            RETURN DISTINCT change.uid, change.name, api.name, project.uid, project.name,
                   d.fact_uid, call.fact_uid, d.source_record_keys, call.source_record_keys
            """
        ).result_set
        # The v1 EXPOSES_ENDPOINT edge is historical after removal, so the
        # removed query above may not find it. Match versioned endpoint paths
        # directly as a second, authoritative route.
        removed_rows += self.graph.query(
            """
            MATCH (change:WorkItem)-[d:REMOVES]->(api:Api),
                  (file:SourceFile)-[call:CALLS_ENDPOINT]->(ep:Endpoint)
            MATCH (file)<-[:CONTAINS]-(repo:Repository)<-[:HAS_REPOSITORY]-(project:Project)
            WHERE d.invalid_at IS NULL AND call.invalid_at IS NULL
              AND api.version = 'v1' AND ep.path STARTS WITH '/v1/'
            RETURN DISTINCT change.uid, change.name, api.name, project.uid, project.name,
                   d.fact_uid, call.fact_uid, d.source_record_keys, call.source_record_keys
            """
        ).result_set
        impact_rows: dict[str, tuple] = {}
        for state, candidates in (("planned", planned_rows), ("removed", removed_rows)):
            for row in candidates:
                key = f"{row[0]}:{row[3]}"
                current = impact_rows.get(key)
                if current is None or state == "removed":
                    impact_rows[key] = (*row, state)
        live_impact_keys: set[str] = set()
        by_change: dict[str, list[dict]] = {}
        for (
            change_uid, change_name, api_name, project_uid, project_name,
            dep_fact, call_fact, trigger_keys, code_keys, impact_state,
        ) in impact_rows.values():
            key = f"impact:{change_uid}:{project_uid}"
            live_impact_keys.add(key)
            materialized = impact_state == "removed"
            project_ref = self._slug_for_project_name(project_name)
            finding_id = self.store.upsert_finding(
                self.graph_id, finding_key=key,
                kind="removed_dependency_still_called" if materialized else "cross_project_impact",
                severity="critical" if materialized else "high",
                title=(
                    f"{project_name} calls removed {api_name}"
                    if materialized else f"{project_name} may be affected"
                ),
                summary=(
                    f"{change_name} removed {api_name}, while current code in {project_name} still calls its v1 endpoint."
                    if materialized else
                    f"{change_name} changes {api_name}, which current code in {project_name} still calls."
                ),
                reasoning=(
                    "A completed REMOVES fact intersects a live code-level CALLS_ENDPOINT dependency; the predicted impact has materialized."
                    if materialized else
                    "A live code-level CALLS_ENDPOINT fact intersects a DEPRECATES fact for the same API."
                ),
                confidence=1.0, project_ref=project_ref,
                trigger_record_key=(trigger_keys or [None])[0],
                properties={
                    "changeUid": change_uid, "projectUid": project_uid, "api": api_name,
                    "materialized": materialized,
                },
            )
            if trigger_keys:
                self.store.add_finding_evidence(
                    self.graph_id, finding_id, trigger_keys[0], "trigger", fact_uid=dep_fact,
                    excerpt=f"{change_name} deprecates {api_name}",
                )
            if code_keys:
                self.store.add_finding_evidence(
                    self.graph_id, finding_id, code_keys[0], "affected_code", fact_uid=call_fact,
                    excerpt=f"Current code in {project_name} still calls an endpoint exposed by {api_name}",
                )
            if materialized:
                incident = next((record for record in self.store.current_records(
                    self.graph_id, providers=["jira"], project_ref=project_ref,
                ) if "returns 404" in str((record.get("metadata") or {}).get("content") or "").lower()), None)
                if incident:
                    self.store.add_finding_evidence(
                        self.graph_id, finding_id, incident["record_key"], "observed_incident",
                        excerpt="Workflows are blocked because POST /v1/auth now returns 404.",
                    )
            by_change.setdefault(change_uid, []).append({
                "from_uid": change_uid, "to_uid": project_uid,
                "source_record_keys": sorted(set((trigger_keys or []) + (code_keys or []))),
                "evidence": f"Current code in {project_name} calls an endpoint exposed by {api_name}",
                "extraction_method": "derived", "confidence": 1.0,
                "derived": True, "derived_rule": "changed_api_has_code_consumer",
                "premise_fact_uids": [dep_fact, call_fact],
            })
        for change_uid, payload in by_change.items():
            w.upsert_fact_edges(self.graph, "MAY_IMPACT", "WorkItem", "Project", payload)
            self.store.upsert_facts(self.graph_id, [{
                "fact_uid": w.make_uid("Fact", item["from_uid"], "MAY_IMPACT", item["to_uid"]),
                "subject_uid": item["from_uid"], "subject_label": "WorkItem",
                "predicate": "MAY_IMPACT", "object_uid": item["to_uid"],
                "object_label": "Project", "source_record_keys": item["source_record_keys"],
                "evidence": item["evidence"], "extraction_method": "derived",
                "confidence": 1.0, "derived": True,
                "derived_rule": item["derived_rule"],
                "premise_fact_uids": item["premise_fact_uids"],
            } for item in payload])

        existing = self.store.findings(self.graph_id)
        for finding in existing:
            if (
                finding["kind"] in {"cross_project_impact", "removed_dependency_still_called"}
                and finding["key"] not in live_impact_keys
            ):
                self.store.mark_finding_stale(
                    self.graph_id, finding["key"],
                    reason="Current code no longer supports the dependency path that produced this impact.",
                )

        self._recompute_mismatches()

    def _recompute_current_consumption(self) -> None:
        """Make CONSUMES_API a current-code snapshot, not a claim union.

        Jira/Notion statements and historical code remain in SourceRecords and
        temporal fact history. Only live SourceFile CALLS_ENDPOINT facts define
        the current project-to-API consumption edge.
        """
        project_uids = [self.project_uid(slug) for slug in self.projects if slug != "auth-service"]
        w.supersede_fact_edges(
            self.graph, "CONSUMES_API", "Project", "Api", project_uids,
        )
        self.store.invalidate_facts(
            self.graph_id, "CONSUMES_API", subject_uids=project_uids,
        )
        rows = self.graph.query(
            """
            MATCH (project:Project)-[:HAS_REPOSITORY]->(:Repository)-[:CONTAINS]->
                  (:SourceFile)-[call:CALLS_ENDPOINT]->(endpoint:Endpoint)
            WHERE call.invalid_at IS NULL AND endpoint.path IS NOT NULL
            RETURN project.uid, endpoint.path, call.fact_uid,
                   call.source_record_keys, endpoint.name
            """
        ).result_set
        api_rows = self.graph.query(
            "MATCH (api:Api) WHERE api.version IS NOT NULL RETURN api.version, api.uid, api.name"
        ).result_set
        api_by_version: dict[str, tuple[str, str]] = {}
        for version, uid, name in api_rows:
            key = str(version).lower()
            # Semantic extraction may create project-specific API nodes that
            # share a version. Prefer the canonical versioned contract node.
            if key not in api_by_version or name == f"Auth API {version}":
                api_by_version[key] = (uid, name)
        grouped: dict[tuple[str, str], dict] = {}
        for project_uid, path, call_fact_uid, source_keys, endpoint_name in rows:
            match = re.search(r"/v(\d+)/", path or "")
            if not match:
                continue
            api = api_by_version.get(f"v{match.group(1)}")
            if not api:
                continue
            api_uid, api_name = api
            item = grouped.setdefault((project_uid, api_uid), {
                "from_uid": project_uid, "to_uid": api_uid,
                "source_record_keys": set(), "premise_fact_uids": set(),
                "endpoints": set(), "api_name": api_name,
            })
            item["source_record_keys"].update(source_keys or [])
            if call_fact_uid:
                item["premise_fact_uids"].add(call_fact_uid)
            item["endpoints"].add(endpoint_name or path)

        graph_rows = []
        fact_rows = []
        for item in grouped.values():
            evidence = "Current production code calls " + ", ".join(sorted(item["endpoints"]))
            graph_row = {
                "from_uid": item["from_uid"], "to_uid": item["to_uid"],
                "source_record_keys": sorted(item["source_record_keys"]),
                "evidence": evidence, "extraction_method": "derived",
                "confidence": 1.0, "derived": True,
                "derived_rule": "current_code_calls_api_endpoint",
                "premise_fact_uids": sorted(item["premise_fact_uids"]),
            }
            graph_rows.append(graph_row)
            fact_rows.append({
                "fact_uid": w.make_uid(
                    "Fact", item["from_uid"], "CONSUMES_API", item["to_uid"],
                ),
                "subject_uid": item["from_uid"], "subject_label": "Project",
                "predicate": "CONSUMES_API", "object_uid": item["to_uid"],
                "object_label": "Api",
                "source_record_keys": graph_row["source_record_keys"],
                "evidence": evidence, "extraction_method": "derived",
                "confidence": 1.0, "derived": True,
                "derived_rule": graph_row["derived_rule"],
                "premise_fact_uids": graph_row["premise_fact_uids"],
            })
        if graph_rows:
            w.upsert_fact_edges(
                self.graph, "CONSUMES_API", "Project", "Api", graph_rows,
            )
            # A revived historical edge keeps its original evidence in the
            # generic writer. For this authoritative snapshot, replace claim
            # provenance with the current code premises instead of unioning it.
            self.graph.query(
                """
                UNWIND $rows AS row
                MATCH (:Project {uid: row.from_uid})-[r:CONSUMES_API]->
                      (:Api {uid: row.to_uid})
                WHERE r.invalid_at IS NULL
                SET r.evidence=row.evidence,
                    r.source_record_keys=row.source_record_keys,
                    r.extraction_method='derived', r.confidence=1.0,
                    r.derived=true, r.derived_rule=row.derived_rule,
                    r.premise_fact_uids=row.premise_fact_uids
                """,
                params={"rows": graph_rows},
            )
            self.store.upsert_facts(self.graph_id, fact_rows)

    def _recompute_mismatches(self) -> None:
        current_code = self.graph.query(
            """
            MATCH (project:Project)-[:HAS_REPOSITORY]->(:Repository)-[:CONTAINS]->(:SourceFile)
                  -[call:CALLS_ENDPOINT]->(endpoint:Endpoint)
            WHERE call.invalid_at IS NULL
            RETURN DISTINCT project.name, endpoint.path, call.source_record_keys
            """
        ).result_set
        code_by_project: dict[str, set[str]] = {}
        code_sources: dict[tuple[str, str], list[str]] = {}
        for project_name, path, source_keys in current_code:
            code_by_project.setdefault(self._slug_for_project_name(project_name), set()).add(path)
            code_sources[(self._slug_for_project_name(project_name), path)] = source_keys or []

        live_keys: set[str] = set()
        for project_ref in self.projects:
            records = self.store.current_records(
                self.graph_id, providers=["jira", "notion"], project_ref=project_ref,
            )
            claim = next((record for record in records if self._claims_completed_v2(record)), None)
            endpoints = code_by_project.get(project_ref, set())
            if claim and "/v1/auth" in endpoints:
                key = f"documentation-code-mismatch:{project_ref}:auth-api"
                live_keys.add(key)
                finding_id = self.store.upsert_finding(
                    self.graph_id, finding_key=key, kind="documentation_code_mismatch",
                    severity="high", title=f"{self._project_title(project_ref)} migration claim conflicts with code",
                    summary="Jira/Notion says the project migrated to Auth API v2, but current Bitbucket code still calls POST /v1/auth.",
                    reasoning="Current production-branch code is authoritative for CALLS_ENDPOINT; the claim is preserved but cannot close the v1 dependency.",
                    confidence=1.0, project_ref=project_ref,
                    trigger_record_key=claim["record_key"],
                    properties={
                        "claimedEndpoint": "/v2/auth", "codeEndpoint": "/v1/auth",
                        "projectUid": self.project_uid(project_ref),
                    },
                )
                self.store.add_finding_evidence(
                    self.graph_id, finding_id, claim["record_key"], "claim",
                    excerpt="Migration completion claim to Auth API v2",
                )
                for record_key in code_sources.get((project_ref, "/v1/auth"), []):
                    self.store.add_finding_evidence(
                        self.graph_id, finding_id, record_key, "conflicting_code",
                        excerpt="Current production code calls POST /v1/auth",
                    )
        for finding in self.store.findings(self.graph_id):
            if finding["kind"] == "documentation_code_mismatch" and finding["key"] not in live_keys:
                self.store.mark_finding_stale(
                    self.graph_id, finding["key"],
                    reason="Current Bitbucket code now agrees with the Jira/Notion migration claim.",
                )

    def _project_findings(self) -> None:
        """Project durable Postgres findings into the navigable graph.

        Open findings remain dark-red alert paths in the UI. Stale findings
        and their relationships stay visible as dashed history instead of
        being deleted, preserving what was believed, when, and from where.
        """
        for finding in self.store.findings(self.graph_id):
            source_keys = sorted({
                source["recordKey"] for source in finding.get("sources", [])
                if source.get("recordKey")
            })
            self.graph.query(
                """
                MERGE (f:Finding {uid: $uid})
                SET f.name=$title, f.search_text=$search_text, f.finding_key=$finding_key,
                    f.kind=$kind, f.status=$status, f.severity=$severity,
                    f.confidence=$confidence, f.created_at=$created_at,
                    f.updated_at=$updated_at, f.last_seen_at=$last_seen_at,
                    f.status_changed_at=$status_changed_at, f.stale_at=$stale_at,
                    f.stale_reason=$stale_reason
                """,
                params={
                    "uid": finding["id"], "title": finding["title"],
                    "search_text": f"{finding['title']} — {finding['summary']} — {finding['reasoning']}",
                    "finding_key": finding["key"], "kind": finding["kind"],
                    "status": finding["status"], "severity": finding["severity"],
                    "confidence": finding["confidence"], "created_at": finding["createdAt"],
                    "updated_at": finding["updatedAt"], "last_seen_at": finding["lastSeenAt"],
                    "status_changed_at": finding["statusChangedAt"],
                    "stale_at": finding["staleAt"], "stale_reason": finding["staleReason"],
                },
            )
            self.graph.query(
                "MATCH (f:Finding {uid: $uid})-[old:FLAGS|CONTEXT_FROM]->() DELETE old",
                params={"uid": finding["id"]},
            )
            if source_keys:
                self.graph.query(
                    """
                    MATCH (f:Finding {uid: $uid})
                    UNWIND $record_keys AS record_key
                    MATCH (sr:SourceRecord {record_key: record_key})
                    MERGE (f)-[:MENTIONED_IN]->(sr)
                    """,
                    params={"uid": finding["id"], "record_keys": source_keys},
                )

            properties = finding.get("properties") or {}
            context_uids = set(properties.get("relatedCandidateUids") or [])
            target_uids: set[str] = set()
            target_uids.update(
                value for key in ("projectUid", "changeUid", "affectedUid")
                if (value := properties.get(key))
            )
            if finding.get("projectRef") in self.projects:
                target_uids.add(self.project_uid(finding["projectRef"]))
            if not target_uids:
                continue
            self.graph.query(
                """
                MATCH (f:Finding {uid: $uid})
                UNWIND $target_uids AS target_uid
                MATCH (target {uid: target_uid})
                WHERE NOT target:Finding
                MERGE (f)-[r:FLAGS]->(target)
                SET r.source_record_keys=$source_record_keys,
                    r.evidence=$evidence, r.extraction_method='finding_projection',
                    r.confidence=$confidence, r.finding_status=$status,
                    r.finding_severity=$severity, r.finding_id=$uid,
                    r.valid_at=$created_at, r.invalid_at=null
                """,
                params={
                    "uid": finding["id"], "target_uids": sorted(target_uids),
                    "source_record_keys": source_keys, "evidence": finding["summary"],
                    "confidence": finding["confidence"], "status": finding["status"],
                    "severity": finding["severity"], "created_at": finding["createdAt"],
                },
            )
            if context_uids:
                self.graph.query(
                    """
                    MATCH (f:Finding {uid: $uid})
                    UNWIND $target_uids AS target_uid
                    MATCH (target {uid: target_uid})
                    WHERE NOT target:Finding
                    MERGE (f)-[r:CONTEXT_FROM]->(target)
                    SET r.source_record_keys=$source_record_keys,
                        r.evidence='Retrieved context used during finding adjudication',
                        r.extraction_method='finding_context', r.confidence=$confidence,
                        r.finding_id=$uid, r.valid_at=$created_at, r.invalid_at=null
                    """,
                    params={
                        "uid": finding["id"], "target_uids": sorted(context_uids),
                        "source_record_keys": source_keys,
                        "confidence": finding["confidence"], "created_at": finding["createdAt"],
                    },
                )

    def _sync_wisdom(self, client: OpenAI, *, run_llm: bool) -> dict[str, int]:
        """Turn grounded mismatch findings into signals, then one proposal.

        Findings are the primary aggregation input. Their attached source
        excerpts and timestamps provide grounding; the wisdom call never
        scans arbitrary raw connector content.
        """
        findings = [
            item for item in self.store.findings(self.graph_id)
            if item["kind"] == "documentation_code_mismatch"
        ]
        if not findings:
            return {"llm_calls": 0, "proposals": 0, "input_tokens": 0, "output_tokens": 0}

        prior_signals = self.store.wisdom_signals(
            self.graph_id, pattern_key=_MIGRATION_WISDOM_PATTERN,
        )
        for finding in findings:
            self.store.upsert_wisdom_signal(
                self.graph_id,
                signal_key=f"{_MIGRATION_WISDOM_PATTERN}:{finding['id']}",
                pattern_key=_MIGRATION_WISDOM_PATTERN,
                signal_type="supports_existing" if prior_signals else "new_pattern",
                statement="A migration completion claim preceded code-confirmed implementation.",
                reasoning=(
                    "The finding compares a Jira/Notion completion claim with authoritative "
                    "current repository evidence and preserves when the mismatch became stale."
                ),
                suggested_action=(
                    "Require current code evidence before treating an API migration as complete."
                ),
                confidence=min(0.95, max(0.65, float(finding["confidence"]) * 0.85)),
                project_ref=finding.get("projectRef"), finding_id=finding["id"],
                properties={
                    "findingStatus": finding["status"],
                    "findingCreatedAt": finding["createdAt"],
                    "findingStaleAt": finding.get("staleAt"),
                },
            )

        signals = self.store.wisdom_signals(
            self.graph_id, pattern_key=_MIGRATION_WISDOM_PATTERN,
        )
        proposals = self.store.wisdom_proposals(self.graph_id)
        proposal = next(
            (item for item in proposals if item["key"] == f"wisdom:{_MIGRATION_WISDOM_PATTERN}"),
            None,
        )
        llm_calls = input_tokens = output_tokens = 0
        if proposal is None:
            grounded_findings = []
            finding_by_id = {item["id"]: item for item in self.store.findings(self.graph_id)}
            for signal in signals:
                finding = finding_by_id.get(signal["findingId"])
                if not finding:
                    continue
                grounded_findings.append({
                    "id": finding["id"], "title": finding["title"],
                    "summary": finding["summary"], "reasoning": finding["reasoning"],
                    "status": finding["status"], "confidence": finding["confidence"],
                    "createdAt": finding["createdAt"], "staleAt": finding.get("staleAt"),
                    "staleReason": finding.get("staleReason"),
                    "sources": [{
                        "provider": source.get("provider"), "name": source.get("name"),
                        "role": source.get("role"), "sourceTime": source.get("sourceTime"),
                        "excerpt": source.get("excerpt"),
                    } for source in finding.get("sources", [])],
                })
            extraction = migration_claim_fallback()
            method = "deterministic_fallback"
            if run_llm:
                try:
                    extraction, usage = generate_wisdom_proposal(
                        client, pattern_key=_MIGRATION_WISDOM_PATTERN,
                        findings=grounded_findings, existing_wisdom=proposals,
                    )
                    llm_calls = 1
                    input_tokens = usage.input_tokens
                    output_tokens = usage.output_tokens
                    method = "llm"
                    if extraction.action == "insufficient_evidence":
                        extraction = migration_claim_fallback()
                        method = "llm_insufficient_fallback"
                except Exception:
                    logger.exception("wisdom aggregation failed; using grounded story fallback")
                    method = "llm_error_fallback"
            proposal_id = self.store.upsert_wisdom_proposal(
                self.graph_id, proposal_key=f"wisdom:{_MIGRATION_WISDOM_PATTERN}",
                wisdom_type=extraction.wisdom_type, topic_key=extraction.topic_key,
                title=extraction.title, statement=extraction.statement,
                rationale=extraction.rationale,
                recommended_action=extraction.recommended_action,
                confidence=extraction.confidence, scope=extraction.applicability.model_dump(),
                properties={
                    "patternKey": _MIGRATION_WISDOM_PATTERN,
                    "promotion": extraction.promotion,
                    "reviewReason": extraction.review_reason,
                    "aggregationAction": extraction.action,
                },
                generation_method=method,
            )
            proposal = next(
                item for item in self.store.wisdom_proposals(self.graph_id)
                if item["id"] == proposal_id
            )
            if run_llm:
                try:
                    embedding_model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
                    embedding_response = client.embeddings.create(
                        model=embedding_model,
                        input=[
                            vector_store.truncate_for_embedding(
                                f"{proposal['statement']} {proposal['rationale']}"
                            ),
                            vector_store.truncate_for_embedding(proposal["title"]),
                        ],
                    )
                    embedding_usage = TokenUsage()
                    embedding_usage.add(embedding_response.usage)
                    input_tokens += embedding_usage.input_tokens
                    output_tokens += embedding_usage.output_tokens
                    self.store.vector_upsert(self.collection, [{
                        "uid": proposal["id"], "label": "Wisdom",
                        "embedding": embedding_response.data[0].embedding,
                        "name_embedding": embedding_response.data[1].embedding,
                        "embedded_text": proposal["statement"],
                        "embedded_model": embedding_model,
                    }])
                except Exception:
                    logger.exception("wisdom proposal embedding failed; proposal remains usable")

        for signal in signals:
            self.store.add_wisdom_proposal_evidence(
                self.graph_id, proposal["id"], signal["id"], signal["findingId"],
            )
        return {
            "llm_calls": llm_calls, "proposals": 1,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
        }

    def _sync_removal_wisdom(self, client: OpenAI, *, run_llm: bool) -> dict[str, int]:
        """Propose a removal-readiness playbook when predicted impact materialises."""
        findings = [
            item for item in self.store.findings(self.graph_id)
            if item["kind"] == "removed_dependency_still_called"
        ]
        if not findings:
            return {"llm_calls": 0, "proposals": 0, "input_tokens": 0, "output_tokens": 0}

        prior_signals = self.store.wisdom_signals(
            self.graph_id, pattern_key=_REMOVAL_WISDOM_PATTERN,
        )
        for finding in findings:
            self.store.upsert_wisdom_signal(
                self.graph_id,
                signal_key=f"{_REMOVAL_WISDOM_PATTERN}:{finding['id']}",
                pattern_key=_REMOVAL_WISDOM_PATTERN,
                signal_type="supports_existing" if prior_signals else "new_pattern",
                statement=(
                    "A known consumer still called a deprecated endpoint when the provider "
                    "removed it, materialising the predicted blast-radius risk."
                ),
                reasoning=(
                    "A completed removal fact, a live repository dependency, and an observed "
                    "failure are connected through the finding's evidence lineage."
                ),
                suggested_action=(
                    "Gate API removal on a current-code check of every known consumer."
                ),
                confidence=min(0.98, max(0.75, float(finding["confidence"]) * 0.92)),
                project_ref=finding.get("projectRef"), finding_id=finding["id"],
                properties={
                    "findingStatus": finding["status"],
                    "findingCreatedAt": finding["createdAt"],
                    "materialized": True,
                },
            )

        signals = self.store.wisdom_signals(
            self.graph_id, pattern_key=_REMOVAL_WISDOM_PATTERN,
        )
        proposals = self.store.wisdom_proposals(self.graph_id)
        proposal_key = f"wisdom:{_REMOVAL_WISDOM_PATTERN}"
        proposal = next((item for item in proposals if item["key"] == proposal_key), None)
        llm_calls = input_tokens = output_tokens = 0
        if proposal is None:
            finding_by_id = {item["id"]: item for item in self.store.findings(self.graph_id)}
            grounded_findings = []
            for signal in signals:
                finding = finding_by_id.get(signal["findingId"])
                if not finding:
                    continue
                grounded_findings.append({
                    "id": finding["id"], "kind": finding["kind"],
                    "title": finding["title"], "summary": finding["summary"],
                    "reasoning": finding["reasoning"], "status": finding["status"],
                    "severity": finding["severity"], "confidence": finding["confidence"],
                    "createdAt": finding["createdAt"],
                    "sources": [{
                        "provider": source.get("provider"), "name": source.get("name"),
                        "role": source.get("role"), "sourceTime": source.get("sourceTime"),
                        "excerpt": source.get("excerpt"),
                    } for source in finding.get("sources", [])],
                })
            extraction = removal_readiness_fallback()
            method = "deterministic_fallback"
            if run_llm:
                try:
                    extraction, usage = generate_wisdom_proposal(
                        client, pattern_key=_REMOVAL_WISDOM_PATTERN,
                        findings=grounded_findings, existing_wisdom=proposals,
                    )
                    llm_calls = 1
                    input_tokens = usage.input_tokens
                    output_tokens = usage.output_tokens
                    method = "llm"
                    if extraction.action == "insufficient_evidence":
                        extraction = removal_readiness_fallback()
                        method = "llm_insufficient_fallback"
                except Exception:
                    logger.exception(
                        "removal-readiness wisdom aggregation failed; using grounded fallback"
                    )
                    method = "llm_error_fallback"

            proposal_id = self.store.upsert_wisdom_proposal(
                self.graph_id, proposal_key=proposal_key,
                wisdom_type=extraction.wisdom_type, topic_key=extraction.topic_key,
                title=extraction.title, statement=extraction.statement,
                rationale=extraction.rationale,
                recommended_action=extraction.recommended_action,
                confidence=extraction.confidence, scope=extraction.applicability.model_dump(),
                properties={
                    "patternKey": _REMOVAL_WISDOM_PATTERN,
                    "promotion": extraction.promotion,
                    "reviewReason": extraction.review_reason,
                    "aggregationAction": extraction.action,
                },
                generation_method=method,
            )
            proposal = next(
                item for item in self.store.wisdom_proposals(self.graph_id)
                if item["id"] == proposal_id
            )
            if run_llm:
                try:
                    embedding_model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
                    embedding_response = client.embeddings.create(
                        model=embedding_model,
                        input=[
                            vector_store.truncate_for_embedding(
                                f"{proposal['statement']} {proposal['rationale']}"
                            ),
                            vector_store.truncate_for_embedding(proposal["title"]),
                        ],
                    )
                    embedding_usage = TokenUsage()
                    embedding_usage.add(embedding_response.usage)
                    input_tokens += embedding_usage.input_tokens
                    output_tokens += embedding_usage.output_tokens
                    self.store.vector_upsert(self.collection, [{
                        "uid": proposal["id"], "label": "Wisdom",
                        "embedding": embedding_response.data[0].embedding,
                        "name_embedding": embedding_response.data[1].embedding,
                        "embedded_text": proposal["statement"],
                        "embedded_model": embedding_model,
                    }])
                except Exception:
                    logger.exception(
                        "removal-readiness wisdom embedding failed; proposal remains usable"
                    )

        for signal in signals:
            self.store.add_wisdom_proposal_evidence(
                self.graph_id, proposal["id"], signal["id"], signal["findingId"],
            )
        return {
            "llm_calls": llm_calls, "proposals": 1,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
        }

    def project_wisdom(self) -> None:
        """Project proposals and their finding/evidence lineage into Falkor."""
        for proposal in self.store.wisdom_proposals(self.graph_id):
            source_keys = sorted({
                source["recordKey"]
                for finding in proposal["supportingFindings"]
                for source in finding.get("sources", []) if source.get("recordKey")
            })
            self.graph.query(
                """
                MERGE (wisdom:Wisdom {uid: $uid})
                SET wisdom.name=$title, wisdom.search_text=$search_text,
                    wisdom.proposal_key=$proposal_key, wisdom.wisdom_type=$wisdom_type,
                    wisdom.topic_key=$topic_key, wisdom.statement=$statement,
                    wisdom.rationale=$rationale,
                    wisdom.recommended_action=$recommended_action,
                    wisdom.status=$status, wisdom.confidence=$confidence,
                    wisdom.version=$version, wisdom.created_at=$created_at,
                    wisdom.updated_at=$updated_at, wisdom.reviewed_at=$reviewed_at
                """,
                params={
                    "uid": proposal["id"], "title": proposal["title"],
                    "search_text": (
                        f"{proposal['title']} — {proposal['statement']} — "
                        f"{proposal['recommendedAction']}"
                    ),
                    "proposal_key": proposal["key"], "wisdom_type": proposal["type"],
                    "topic_key": proposal["topicKey"], "statement": proposal["statement"],
                    "rationale": proposal["rationale"],
                    "recommended_action": proposal["recommendedAction"],
                    "status": proposal["status"], "confidence": proposal["confidence"],
                    "version": proposal["version"], "created_at": proposal["createdAt"],
                    "updated_at": proposal["updatedAt"], "reviewed_at": proposal["reviewedAt"],
                },
            )
            self.graph.query(
                "MATCH (wisdom:Wisdom {uid: $uid})-[old:DERIVED_FROM|APPLIES_TO]->() DELETE old",
                params={"uid": proposal["id"]},
            )
            if source_keys:
                self.graph.query(
                    """
                    MATCH (wisdom:Wisdom {uid: $uid})
                    UNWIND $record_keys AS record_key
                    MATCH (sr:SourceRecord {record_key: record_key})
                    MERGE (wisdom)-[:MENTIONED_IN]->(sr)
                    """,
                    params={"uid": proposal["id"], "record_keys": source_keys},
                )
            finding_ids = [item["id"] for item in proposal["supportingFindings"]]
            if finding_ids:
                self.graph.query(
                    """
                    MATCH (wisdom:Wisdom {uid: $uid})
                    UNWIND $finding_ids AS finding_id
                    MATCH (finding:Finding {uid: finding_id})
                    MERGE (wisdom)-[r:DERIVED_FROM]->(finding)
                    SET r.source_record_keys=$source_record_keys,
                        r.evidence='Wisdom proposal derived from grounded finding',
                        r.extraction_method='wisdom_aggregation',
                        r.confidence=$confidence, r.valid_at=$created_at,
                        r.wisdom_status=$status, r.invalid_at=null
                    """,
                    params={
                        "uid": proposal["id"], "finding_ids": finding_ids,
                        "source_record_keys": source_keys,
                        "confidence": proposal["confidence"],
                        "created_at": proposal["createdAt"], "status": proposal["status"],
                    },
                )
            project_uids = sorted({
                self.project_uid(finding["projectRef"])
                for finding in proposal["supportingFindings"]
                if finding.get("projectRef") in self.projects
            })
            if project_uids:
                self.graph.query(
                    """
                    MATCH (wisdom:Wisdom {uid: $uid})
                    UNWIND $project_uids AS project_uid
                    MATCH (project:Project {uid: project_uid})
                    MERGE (wisdom)-[r:APPLIES_TO]->(project)
                    SET r.source_record_keys=$source_record_keys,
                        r.evidence=$statement, r.extraction_method='wisdom_scope',
                        r.confidence=$confidence, r.valid_at=$created_at,
                        r.wisdom_status=$status, r.invalid_at=null
                    """,
                    params={
                        "uid": proposal["id"], "project_uids": project_uids,
                        "source_record_keys": source_keys, "statement": proposal["statement"],
                        "confidence": proposal["confidence"], "created_at": proposal["createdAt"],
                        "status": proposal["status"],
                    },
                )

    @staticmethod
    def _claims_completed_v2(record: dict) -> bool:
        text = str((record.get("metadata") or {}).get("content") or "").lower()
        negated = bool(re.search(
            r"\b(?:no|not|never|without)\b.{0,100}\b(?:migrat\w*|complet\w*|retir\w*)\b",
            text,
        ))
        conditional = any(phrase in text for phrase in (
            "complete only after", "completed only after", "complete when",
            "completed when", "until the", "until src/",
        ))
        return (
            not negated and not conditional and "v2" in text and "migrat" in text
            and any(phrase in text for phrase in ("completed", "complete", "no longer calls", "dependency is retired"))
        )

    def _slug_for_project_name(self, name: str) -> str:
        folded = (name or "").strip().lower()
        for slug in self.projects:
            if folded == self._project_title(slug).lower():
                return slug
        return folded.replace(" ", "-")

    def _project_title(self, slug: str) -> str:
        mapping = {
            "auth-service": "Auth Service",
            "mcp-gateway": "MCP Gateway",
            "argo-orchestrator": "Argo Orchestrator",
        }
        return mapping.get(slug, slug.replace("-", " ").title())
