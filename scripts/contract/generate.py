"""Generate OpenAPI/schema snapshot and TypeScript types from schemas.py.

Task 5.6 — Phase 5 contract rework.

## Approach: schema-only introspection (no FastAPI app boot)

We import ``infra_brain.api.schemas`` directly and call Pydantic v2's
``model_json_schema()`` on each exported ``*Out`` / ``*PageOut`` / ``PageEnvelope``
class — no FastAPI ``app`` object, no DB connection, no ``uvicorn`` runtime.

Why not boot the full app?  The CI ``preflight`` stage (where ``contract-check``
runs, mirroring ``design-sync-check``) does NOT spin up a ``pgvector/pgvector:pg15`` service —
only the ``test`` and ``migration-parity`` stages do (see ``.gitlab-ci.yml``).
Importing ``infra_brain.main`` triggers router imports that resolve SQLAlchemy
``sessionmaker`` bindings at module level, which requires a reachable DB URL.
Schema-only introspection side-steps this entirely and is also faster.

## Outputs

- ``scripts/contract/generated/openapi.json``  — deterministic JSON object
  ``{"generated_at": "<iso>", "models": {<name>: <json-schema>, ...}}``
  sorted at every level for byte-stable diffs.

- ``scripts/contract/generated/types.ts``  — TypeScript ``interface`` declarations
  for every exported ``*Out`` / ``*PageOut`` / ``PageEnvelope`` class.  Nested
  ``$defs`` models referenced in the schema are also emitted.

- ``scripts/contract/generated/enums.ts``  — SKIPPED.  As of Task 5.6,
  ``schemas.py`` contains no ``Enum``-typed fields.  If enums are added in a
  future task, add an ``_generate_enums()`` function here and emit the file.

## Usage

    uv run python scripts/contract/generate.py          # write to GENERATED_DIR
    uv run python scripts/contract/generate.py --check  # diff against committed; exit 1 on diff
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATED_DIR = _REPO_ROOT / "scripts" / "contract" / "generated"

# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------


def _collect_out_classes() -> list[tuple[str, type]]:
    """Return ``[(name, cls), ...]`` for all exported *Out/*PageOut/PageEnvelope classes.

    Imports ``infra_brain.api.schemas`` without booting the FastAPI app or
    establishing a DB connection — pure Pydantic model inspection.
    """
    # Local import so the module itself is importable in CI without infra_brain on path
    # (the CI install step does `pip install -e ".[dev]"` which puts infra_brain on path)
    import infra_brain.api.schemas as _schemas  # noqa: PLC0415 (local import intentional)

    result: list[tuple[str, type]] = []
    for name in _schemas.__all__:
        cls = getattr(_schemas, name, None)
        if cls is None:
            continue
        if inspect.isclass(cls) and (
            name.endswith("Out") or name.endswith("PageOut") or name == "PageEnvelope"
        ):
            result.append((name, cls))
    return result


# ---------------------------------------------------------------------------
# JSON schema generation
# ---------------------------------------------------------------------------


def _sort_schema(obj: Any) -> Any:
    """Recursively sort dict keys for deterministic JSON output."""
    if isinstance(obj, dict):
        return {k: _sort_schema(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        return [_sort_schema(item) for item in obj]
    return obj


def schema_for_models(classes: list[tuple[str, type]]) -> str:
    """Return a deterministic JSON string for the given model classes.

    Args:
        classes: List of ``(name, cls)`` pairs.  ``cls`` must be a Pydantic
                 ``BaseModel`` subclass.

    Returns:
        A deterministic JSON string (sorted keys, 2-space indent).
        Top-level shape: ``{"models": {<name>: <json-schema>, ...}}``.
        No ``generated_at`` timestamp — timestamps break determinism for the
        byte-identical CI diff check.
    """
    models: dict[str, Any] = {}
    for name, cls in classes:
        raw = cls.model_json_schema()
        models[name] = _sort_schema(raw)

    payload: dict[str, Any] = {"models": {k: models[k] for k in sorted(models)}}
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------------------
# TypeScript type generation
# ---------------------------------------------------------------------------

# Map of JSON schema ``"type"`` strings → TypeScript type names
_TS_SCALAR_MAP: dict[str, str] = {
    "string": "string",
    "integer": "number",
    "number": "number",
    "boolean": "boolean",
    "null": "null",
    "object": "Record<string, unknown>",
}


def _resolve_ts_type(prop_schema: dict[str, Any], defs: dict[str, Any]) -> str:
    """Convert a single JSON-schema property descriptor into a TypeScript type string."""
    # anyOf / oneOf (used for Optional[X] in Pydantic v2 → {"anyOf": [{"type": X}, {"type": "null"}]})
    if "anyOf" in prop_schema:
        parts = [_resolve_ts_type(p, defs) for p in prop_schema["anyOf"]]
        # Deduplicate while preserving order
        seen: set[str] = set()
        unique = []
        for p in parts:
            if p not in seen:
                seen.add(p)
                unique.append(p)
        return " | ".join(unique)

    # $ref to a $defs entry
    if "$ref" in prop_schema:
        ref = prop_schema["$ref"]
        # ref is like "#/$defs/KV"
        def_name = ref.split("/")[-1]
        return def_name  # interface name

    # array
    if prop_schema.get("type") == "array":
        items = prop_schema.get("items", {})
        item_type = _resolve_ts_type(items, defs) if items else "unknown"
        return f"{item_type}[]"

    # scalar
    if "type" in prop_schema:
        return _TS_SCALAR_MAP.get(prop_schema["type"], "unknown")

    # prefixItems (tuple types, unlikely here)
    if "prefixItems" in prop_schema:
        return "unknown[]"

    return "unknown"


def _ts_interface(name: str, schema: dict[str, Any], defs: dict[str, Any]) -> str:
    """Emit a single TypeScript interface for one model schema."""
    props = schema.get("properties", {})
    required_set = set(schema.get("required", []))
    lines: list[str] = [f"export interface {name} {{"]
    for field_name in sorted(props):
        prop_schema = props[field_name]
        ts_type = _resolve_ts_type(prop_schema, defs)
        optional = "" if field_name in required_set else "?"
        lines.append(f"  {field_name}{optional}: {ts_type};")
    lines.append("}")
    return "\n".join(lines)


def generate_ts_types(classes: list[tuple[str, type]]) -> str:
    """Return a TypeScript source string with interfaces for all given classes.

    Also emits interfaces for any ``$defs`` (nested models) referenced within
    the top-level schemas, so the output is self-contained.

    De-duplication: builds a single unified ``{name: schema}`` mapping —
    top-level models first, then ``$defs`` entries whose name is NOT already
    present — so each interface is emitted exactly once.  This prevents
    TS2300 (Duplicate identifier) when a model is both exported top-level and
    referenced via ``$defs`` by another model (e.g. ``ResourceOut`` inside
    ``ResourcePageOut``).
    """
    header_lines = [
        "// AUTO-GENERATED — do not edit by hand.",
        "// Source: infra_brain.api.schemas (Pydantic v2 model_json_schema)",
        "// Regenerate: uv run python scripts/contract/generate.py",
        "// Task 5.6 — Phase 5 contract rework.",
        "",
    ]

    # Pass 1: collect raw schemas for each top-level class and all $defs they reference.
    top_level_schemas: dict[str, Any] = {}
    all_defs: dict[str, Any] = {}
    for name, cls in classes:
        raw = cls.model_json_schema()
        top_level_schemas[name] = raw
        for def_name, def_schema in raw.get("$defs", {}).items():
            all_defs[def_name] = def_schema

    # Pass 2: build a single unified name→schema mapping.
    # Top-level models take priority; $defs entries are added only if not already present.
    unified: dict[str, Any] = dict(top_level_schemas)  # top-level first
    for def_name, def_schema in all_defs.items():
        if def_name not in unified:
            unified[def_name] = def_schema

    # Build a merged defs dict (all_defs + top-level overrides) for $ref resolution
    # inside _ts_interface — this ensures cross-references resolve correctly.
    merged_defs = {**all_defs, **top_level_schemas}

    # Pass 3: emit exactly one interface per unique name, sorted for determinism.
    interfaces: list[str] = []
    for name in sorted(unified):
        interfaces.append(_ts_interface(name, unified[name], merged_defs))
        interfaces.append("")

    parts = header_lines + interfaces
    return "\n".join(parts).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def generate_contract(output_dir: Path | None = None) -> None:
    """Generate all contract artifacts and write them to ``output_dir``.

    Args:
        output_dir: Directory to write generated files.  Defaults to
                    ``GENERATED_DIR`` (``scripts/contract/generated/``).
                    The directory is created if it does not exist.
    """
    out = output_dir or GENERATED_DIR
    out.mkdir(parents=True, exist_ok=True)

    classes = _collect_out_classes()

    # --- openapi.json ---
    json_content = schema_for_models(classes)
    (out / "openapi.json").write_text(json_content, encoding="utf-8")

    # --- types.ts ---
    ts_content = generate_ts_types(classes)
    (out / "types.ts").write_text(ts_content, encoding="utf-8")

    # NOTE: enums.ts is intentionally NOT generated.
    # schemas.py (as of Task 5.6) contains no Enum-typed classes.
    # When/if enums are added, implement _generate_enums() here.


def _check_mode() -> int:
    """Run in --check mode: regenerate in memory, diff against committed files.

    Returns 0 if committed files match generation; 1 if any file differs.
    Used by the CI ``contract-check`` job (pure Python diff, no git binary).
    """
    committed_dir = GENERATED_DIR
    classes = _collect_out_classes()

    live_json = schema_for_models(classes)
    live_ts = generate_ts_types(classes)

    failures: list[str] = []

    committed_json_path = committed_dir / "openapi.json"
    if not committed_json_path.exists():
        failures.append(f"  MISSING: {committed_json_path}")
    elif committed_json_path.read_text(encoding="utf-8") != live_json:
        failures.append(f"  STALE:   {committed_json_path}")

    committed_ts_path = committed_dir / "types.ts"
    if not committed_ts_path.exists():
        failures.append(f"  MISSING: {committed_ts_path}")
    elif committed_ts_path.read_text(encoding="utf-8") != live_ts:
        failures.append(f"  STALE:   {committed_ts_path}")

    if failures:
        print("ERROR: contract snapshot is stale — a schema was changed without regenerating.")
        print("")
        print("Stale/missing files:")
        for f in failures:
            print(f)
        print("")
        print("Fix: regenerate and commit the snapshot:")
        print("  uv run python scripts/contract/generate.py")
        print("  git add scripts/contract/generated/")
        print("  git commit --amend --no-edit  # or a new commit")
        return 1

    print("contract-check passed: committed snapshot matches schema generation")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate or check the infra-brain API contract snapshot."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check mode: compare live generation against committed files; exit 1 on diff.",
    )
    args = parser.parse_args()

    if args.check:
        sys.exit(_check_mode())
    else:
        generate_contract()
        print(f"Contract generated -> {GENERATED_DIR}/")
        print("  openapi.json")
        print("  types.ts")
        print("  (enums.ts: skipped — schemas.py has no Enum-typed fields)")
