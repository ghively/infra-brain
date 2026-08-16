"""Regression tests for mcp_server._row_to_dict serialization.

Guards the Resource.metadata_ trap: the DB column is named "metadata", which
collides with SQLAlchemy's reserved declarative MetaData attribute. Serializing
by raw column name returns the MetaData object (unserializable), which made
query_resources fail with "outputSchema defined but no structured output
returned" while every other query tool worked.
"""

import json

from sqlalchemy.sql.schema import MetaData

from infra_brain.db.models import CollectionRun, Resource
from infra_brain.mcp_server import _row_to_dict


def test_resource_row_to_dict_is_json_serializable():
    r = Resource(
        domain="cicd",
        type="gitlab_project",
        name="example-project",
        metadata_={"branch": "main", "pipeline_id": 42},
    )
    d = _row_to_dict(r)

    # No value may be a SQLAlchemy MetaData object (the bug).
    assert not any(isinstance(v, MetaData) for v in d.values())

    # The JSONB payload must survive under its documented key, intact.
    assert d["metadata_"] == {"branch": "main", "pipeline_id": 42}

    # The whole row must serialize to JSON without a coercion fallback.
    json.dumps(d)


def test_other_models_keys_unchanged():
    # Regression guard: non-colliding models keep their column-name keys.
    run = CollectionRun(domain="cicd", trigger_type="scheduled", status="completed")
    d = _row_to_dict(run)
    assert d["domain"] == "cicd"
    assert d["status"] == "completed"
    json.dumps(d)
