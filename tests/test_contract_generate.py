"""TDD tests for scripts/contract/generate.py.

Task 5.6 — Phase 5 contract rework.

Coverage:
  1. Running generate_contract() twice produces byte-identical output (determinism).
  2. A model with a renamed field produces a DIFFERENT JSON schema snapshot than
     the original — confirming the generator is sensitive to schema changes and
     would fail the CI contract-check if someone edits a schema without
     regenerating.
  3. The generated types.ts contains a TypeScript interface for every *Out/*PageOut
     model class exported in schemas.__all__.
  4. No enums file is produced (schemas.py has no Enum-typed fields as of Task 5.6).
"""

import inspect
import json

import pytest

# The module under test — will fail with ImportError until implemented.
from scripts.contract.generate import (
    GENERATED_DIR,
    generate_contract,
    schema_for_models,
)

import infra_brain.api.schemas as schemas_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _all_out_classes():
    """Return all *Out / *PageOut / PageEnvelope classes from schemas.__all__."""
    result = []
    for name in schemas_module.__all__:
        cls = getattr(schemas_module, name, None)
        if cls is None:
            continue
        if inspect.isclass(cls) and (
            name.endswith("Out") or name.endswith("PageOut") or name == "PageEnvelope"
        ):
            result.append((name, cls))
    return result


# ---------------------------------------------------------------------------
# Test 1: determinism — two calls to schema_for_models() produce identical JSON
# ---------------------------------------------------------------------------


def test_schema_generation_is_deterministic():
    """Running the schema generator twice returns the same JSON bytes."""
    classes = _all_out_classes()
    run1 = schema_for_models(classes)
    run2 = schema_for_models(classes)
    assert run1 == run2, (
        "Schema generation is not deterministic — got different output on second call"
    )


# ---------------------------------------------------------------------------
# Test 2: sensitivity — renaming a field on a model changes the snapshot
# ---------------------------------------------------------------------------


def test_schema_changes_when_field_renamed():
    """A model with a renamed field produces a different schema snapshot.

    We construct a local variant of ResourceOut with 'hostname' renamed to
    'fqdn', run schema_for_models() against it, and confirm the JSON differs
    from the schema produced with the real ResourceOut.
    """
    from pydantic import BaseModel
    from datetime import datetime

    # Real class
    real_classes = [("ResourceOut", schemas_module.ResourceOut)]
    real_schema = schema_for_models(real_classes)

    # Modified variant: rename 'hostname' -> 'fqdn'
    class ResourceOutRenamed(BaseModel):
        id: str
        fqdn: str  # was 'hostname'
        domain: str
        resource_type: str
        zone: str
        status: str
        last_seen_at: datetime
        drift_count: int = 0
        meta: list = []

    modified_classes = [("ResourceOut", ResourceOutRenamed)]
    modified_schema = schema_for_models(modified_classes)

    assert real_schema != modified_schema, (
        "schema_for_models() returned identical output for a model with a "
        "renamed field — the generator is not sensitive to field-name changes"
    )

    # Confirm the rename is actually reflected in the modified schema
    modified_data = json.loads(modified_schema)
    resource_props = modified_data["models"]["ResourceOut"]["properties"]
    assert "fqdn" in resource_props, "Expected 'fqdn' field in renamed model schema"
    assert "hostname" not in resource_props, "Unexpected 'hostname' in renamed model schema"


# ---------------------------------------------------------------------------
# Test 3: TS types file contains an interface for every Out/PageOut class
# ---------------------------------------------------------------------------


def test_types_ts_contains_all_out_interfaces():
    """The generated types.ts has an 'interface <Name>' for every *Out class."""
    classes = _all_out_classes()
    from scripts.contract.generate import generate_ts_types

    ts_content = generate_ts_types(classes)

    for name, _cls in classes:
        assert f"interface {name}" in ts_content, (
            f"Missing TypeScript interface for {name!r} in generated types.ts"
        )


# ---------------------------------------------------------------------------
# Test 4: no enums file is generated (schemas.py has no Enum-typed fields)
# ---------------------------------------------------------------------------


def test_no_enums_in_schemas():
    """schemas.py should have no Enum classes — enums.ts generation is skipped."""
    from enum import Enum

    for name in schemas_module.__all__:
        cls = getattr(schemas_module, name, None)
        if cls is None:
            continue
        if inspect.isclass(cls) and issubclass(cls, Enum):
            pytest.fail(
                f"Found Enum class {name!r} in schemas.__all__ — "
                "enums.ts generation should now be implemented (Task 5.6 said to skip only if none exist)"
            )


# ---------------------------------------------------------------------------
# Test 5: generate_contract() writes files to GENERATED_DIR and they are stable
# ---------------------------------------------------------------------------


def test_generate_contract_writes_committed_files(tmp_path):
    """generate_contract() writes openapi.json and types.ts; second call is identical."""
    out_dir = tmp_path / "generated"
    out_dir.mkdir()

    # First generation
    generate_contract(output_dir=out_dir)
    files1 = {p.name: p.read_text(encoding="utf-8") for p in out_dir.iterdir()}

    assert "openapi.json" in files1, "generate_contract() did not produce openapi.json"
    assert "types.ts" in files1, "generate_contract() did not produce types.ts"

    # Second generation into same dir — must produce identical content
    generate_contract(output_dir=out_dir)
    files2 = {p.name: p.read_text(encoding="utf-8") for p in out_dir.iterdir()}

    assert files1 == files2, (
        "generate_contract() produced different content on second call — not deterministic"
    )

    # openapi.json must be valid JSON
    data = json.loads(files1["openapi.json"])
    assert "models" in data, "openapi.json missing top-level 'models' key"

    # types.ts must have at least one interface
    assert "interface " in files1["types.ts"], "types.ts has no TypeScript interfaces"


# ---------------------------------------------------------------------------
# Test 6: committed snapshot matches live generation (CI contract-check logic)
# ---------------------------------------------------------------------------


def test_committed_snapshot_matches_generation():
    """The committed scripts/contract/generated/ files match what generate() produces now.

    This is the same check the CI contract-check job runs: if someone edits a
    schema without regenerating, this test fails.
    """
    committed_dir = GENERATED_DIR
    if not committed_dir.exists():
        pytest.skip("scripts/contract/generated/ not yet committed — run generate_contract() first")

    classes = _all_out_classes()
    live_json = schema_for_models(classes)

    committed_json_path = committed_dir / "openapi.json"
    if not committed_json_path.exists():
        pytest.skip("openapi.json not yet committed")

    committed_json = committed_json_path.read_text(encoding="utf-8")
    assert live_json == committed_json, (
        "Committed openapi.json is stale — a schema was changed without regenerating.\n"
        "Fix: cd <workspace> && uv run python scripts/contract/generate.py && git add scripts/contract/generated/"
    )


# ---------------------------------------------------------------------------
# Test 7: no duplicate interface declarations in types.ts (TS2300 guard)
# ---------------------------------------------------------------------------


def test_types_ts_has_no_duplicate_interfaces():
    """Every 'export interface <Name>' declaration must appear EXACTLY ONCE.

    The generator used to emit $defs entries first, then top-level model
    interfaces — with no de-duplication when a name appeared in both.  Models
    that are both exported top-level AND referenced via $defs by another model
    (e.g. ResourceOut referenced by ResourcePageOut.items) were emitted twice,
    producing invalid TypeScript (TS2300 Duplicate identifier).

    This test counts occurrences of 'export interface <Name>' for every model
    name known to the generator and asserts each appears EXACTLY ONCE.
    """
    import re

    from scripts.contract.generate import generate_ts_types

    classes = _all_out_classes()
    ts_content = generate_ts_types(classes)

    # Find all declared interface names (order-preserving)
    declared = re.findall(r"^export interface (\w+)", ts_content, re.MULTILINE)

    # Count occurrences per name
    counts: dict[str, int] = {}
    for name in declared:
        counts[name] = counts.get(name, 0) + 1

    duplicates = {name: cnt for name, cnt in counts.items() if cnt > 1}
    assert not duplicates, (
        "Duplicate TypeScript interface declarations found (TS2300 would fire):\n"
        + "\n".join(f"  {name!r} appears {cnt} times" for name, cnt in sorted(duplicates.items()))
    )
