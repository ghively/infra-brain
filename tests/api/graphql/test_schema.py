"""Task 1: schema is query-only, and a trivial query executes end-to-end."""

import pytest

from infra_brain.api.graphql.schema import schema


def test_schema_is_query_only():
    """No Mutation type exists anywhere on the schema — the structural
    read-only guarantee for this surface (GitLab #114)."""
    assert schema.mutation is None


@pytest.mark.asyncio
async def test_health_query_executes():
    result = await schema.execute("{ health }")
    assert result.errors is None
    assert result.data == {"health": "ok"}
