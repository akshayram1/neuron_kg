"""Mandatory source-record access predicates for every user-facing read path."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class AccessScope:
    """Connections and principals proven by the caller's authenticated session.

    `unrestricted` is reserved for trusted offline maintenance/evaluation code;
    browser/API handlers must always build a scoped instance.
    """

    connections: tuple[tuple[str, tuple[str, ...]], ...] = ()
    principals: tuple[str, ...] = ()
    allow_public: bool = True
    unrestricted: bool = False

    @classmethod
    def from_connections(
        cls, values: Mapping[str, list[str] | tuple[str, ...]], *,
        principals: list[str] | tuple[str, ...] = (), allow_public: bool = True,
    ) -> "AccessScope":
        return cls(
            connections=tuple(
                (provider.lower(), tuple(sorted(set(map(str, connection_ids)))))
                for provider, connection_ids in sorted(values.items()) if connection_ids
            ),
            principals=tuple(sorted(set(map(str, principals)))),
            allow_public=allow_public,
        )

    @classmethod
    def trusted_internal(cls) -> "AccessScope":
        return cls(unrestricted=True)

    def cypher(self, alias: str, prefix: str = "acl") -> tuple[str, dict]:
        if self.unrestricted:
            return "true", {}
        clauses: list[str] = []
        params: dict = {}
        if self.allow_public:
            clauses.append(f"coalesce({alias}.public, false) = true")
        for index, (provider, connection_ids) in enumerate(self.connections):
            provider_key = f"{prefix}_provider_{index}"
            connections_key = f"{prefix}_connections_{index}"
            params[provider_key] = provider
            params[connections_key] = list(connection_ids)
            # record_key fallback protects records written before connection_id
            # became a persisted SourceRecord property.
            clauses.append(
                f"({alias}.provider = ${provider_key} AND ("
                f"{alias}.connection_id IN ${connections_key} OR "
                f"any(connection IN ${connections_key} WHERE "
                f"{alias}.record_key STARTS WITH ${provider_key} + ':' + connection + ':')))"
            )
        if self.principals:
            principals_key = f"{prefix}_principals"
            params[principals_key] = list(self.principals)
            clauses.append(
                f"any(principal IN coalesce({alias}.principals, []) "
                f"WHERE principal IN ${principals_key})"
            )
        return "(" + " OR ".join(clauses) + ")" if clauses else "false", params

