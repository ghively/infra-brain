"""Read-only GraphQL query layer over the knowledge-graph-shaped ORM.

See GitLab issue #114 / docs/superpowers/plans/2026-07-23-integration-graphql-api.md.

Structurally read-only: ``schema.py`` defines a ``Query`` type only — no
``Mutation`` type exists anywhere in this package. There is therefore no
write path to gate; the core read-only guarantee (docs/READONLY-MODEL.md)
holds by construction, not by a runtime check.
"""

from __future__ import annotations

from infra_brain.api.graphql.router import graphql_router
from infra_brain.api.graphql.schema import schema

__all__ = ["graphql_router", "schema"]
