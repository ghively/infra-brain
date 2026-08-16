"""Module tests for infra_brain.api.schemas (MR-E TEST-4).

api/schemas.py is ~100 Pydantic response models with essentially no
hand-tested logic of its own (no field/model validators) — its real,
documented contract lives in PageEnvelope's docstring (api/_envelope.py):

    "Subclasses that carry extra sidecar fields (e.g. ``by_severity``,
    ``header``, ``summary``) must give those fields defaults so the degraded
    envelope ``{"items": [], "total": 0, "limit": 0, "offset": 0,
    "_degraded": True}`` deserializes without validation errors."

tests/test_envelope.py already proves this for a hand-picked list of classes
(the ones known to be exercised via HTTP in other tests). That list is
manually maintained — a new PageEnvelope subclass with a required sidecar
field and no default would only be caught if someone remembers to add it to
that parametrize list. This file closes that gap with a discovery-based
sweep: every PageEnvelope subclass actually defined in schemas.py, not a
hand-picked subset, is checked against the same invariant.
"""

import inspect

import pytest
from pydantic import BaseModel, ValidationError

from infra_brain.api import schemas
from infra_brain.api._envelope import PageEnvelope

_DEGRADED_BODY = {"items": [], "total": 0, "limit": 0, "offset": 0, "_degraded": True}


def _all_page_envelope_subclasses() -> list[type]:
    found = []
    for _name, obj in vars(schemas).items():
        if (
            inspect.isclass(obj)
            and issubclass(obj, PageEnvelope)
            and obj is not PageEnvelope
            and obj.__module__ == schemas.__name__
        ):
            found.append(obj)
    return found


ALL_PAGE_CLASSES = _all_page_envelope_subclasses()


def test_discovery_finds_a_reasonable_number_of_page_classes():
    """Sanity check on the discovery mechanism itself — if this drops to 0
    the sweep below would silently pass on nothing."""
    assert len(ALL_PAGE_CLASSES) >= 10


@pytest.mark.parametrize("model_cls", ALL_PAGE_CLASSES, ids=lambda c: c.__name__)
def test_every_page_class_accepts_the_minimal_degraded_body(model_cls):
    """Every PageEnvelope subclass in schemas.py must validate against the
    minimal degraded literal _degraded_body_for actually returns — i.e. every
    sidecar field beyond items/total/limit/offset must have a default.

    Fails loudly (naming the offending class) if a future schema adds a
    required sidecar field without a default — exactly the class of bug the
    PageEnvelope docstring warns about and the hand-picked list in
    test_envelope.py can't catch for classes it doesn't know about.
    """
    try:
        model_cls(**_DEGRADED_BODY)
    except ValidationError as exc:
        pytest.fail(
            f"{model_cls.__name__} does not accept the minimal degraded body — "
            f"a required sidecar field is missing a default: {exc}"
        )


@pytest.mark.parametrize("model_cls", ALL_PAGE_CLASSES, ids=lambda c: c.__name__)
def test_every_page_class_items_field_defaults_to_a_list_type(model_cls):
    """The base contract: `items` must be declared as a list on every
    PageEnvelope subclass (never overridden to something else)."""
    field = model_cls.model_fields["items"]
    origin = getattr(field.annotation, "__origin__", field.annotation)
    assert origin is list, (
        f"{model_cls.__name__}.items must be a list type, got {field.annotation!r}"
    )


# ---------------------------------------------------------------------------
# CountsOut — spot check tied to the FE-6/7/8/9 counts-integrity fixes: the
# schema's field set is the contract fleet.py's /counts endpoint fills in.
# ---------------------------------------------------------------------------


def test_counts_out_has_the_fields_the_counts_endpoint_populates():
    fields = set(schemas.CountsOut.model_fields.keys())
    assert fields == {
        "open_drift",
        "open_cves",
        "critical_cves",
        "severe_cves",
        "compliance_open",
        "compliance_resolved",
        "eol_overdue",
        "eol_total",
        "invrec_proposed",
        "invrec_total",
        "total_resources",
        "applied_instincts",
    }


def test_counts_out_fields_are_all_ints():
    for name, field in schemas.CountsOut.model_fields.items():
        assert field.annotation is int, f"CountsOut.{name} should be int, got {field.annotation!r}"


def test_counts_out_constructs_from_all_zero_counts():
    """The empty-fleet case (every count 0) must be a valid CountsOut — this
    is exactly what a fresh install's /counts response looks like."""
    zeros = dict.fromkeys(schemas.CountsOut.model_fields.keys(), 0)
    out = schemas.CountsOut(**zeros)
    assert out.open_cves == 0


# ---------------------------------------------------------------------------
# General schema hygiene
# ---------------------------------------------------------------------------


def test_all_public_schema_classes_are_pydantic_models():
    """Every public (non-underscore) name in schemas.py that is a class must
    be a BaseModel subclass — catches an accidental plain-class/dataclass
    slipping into a module whose entire purpose is FastAPI response models."""
    for name, obj in vars(schemas).items():
        if name.startswith("_") or not inspect.isclass(obj):
            continue
        if obj.__module__ != schemas.__name__:
            continue
        assert issubclass(obj, BaseModel), f"{name} is a class but not a BaseModel subclass"
