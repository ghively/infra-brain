"""Contract generation tooling for the infra-brain API.

Task 5.6 — Phase 5 contract rework.

Generates deterministic OpenAPI/schema snapshots and TypeScript type stubs
from ``infra_brain.api.schemas`` Pydantic models.  CI fails on any undiffed
change (``contract-check`` job in ``.gitlab-ci.yml``).
"""
