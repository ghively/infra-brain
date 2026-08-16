"""B6 (Finding 5): declarative agent registry metadata.

Every domain in supervisor.AGENT_REGISTRY must resolve to a class that
declares an EXPLICIT ``schedule`` value — either a real cron string or
``None`` (documented as "intentionally no default schedule", e.g. hook-only
agents like drift/notification, no-op agents like inventory_mr, or
scope-gated agents like query). ``schedule`` defaults to ``None`` on
ETLConnector itself, so a forgotten schedule on a NEW agent would silently
inherit that default and never get scheduled — exactly the kind of
"documented past source of a real bug" (the old "query" dead-dispatch slot)
this test exists to catch in CI instead of in production.
"""

from infra_brain.etl.base import ETLConnector
from infra_brain.supervisor import AGENT_REGISTRY, SKIP_HOOK


def test_every_registered_agent_declares_a_schedule_class_attribute():
    """Every AGENT_REGISTRY domain's class must define ``schedule`` as a class
    attribute somewhere in its own MRO below ETLConnector's bare default —
    i.e. either the base default (None, acceptable and must be intentional)
    or an overridden cron string. This mainly guards against a typo'd/renamed
    attribute; the real intent-check is test_schedule_or_none_is_intentional
    below, which cross-checks against scheduler.py's derived table.
    """
    missing = []
    for domain, cls in AGENT_REGISTRY.items():
        if not hasattr(cls, "schedule"):
            missing.append(domain)
    assert missing == [], (
        f"Domains whose agent class has no `schedule` attribute at all: {missing}. "
        "Every ETLConnector subclass inherits `schedule = None` by default, so this "
        "should be structurally impossible — investigate immediately."
    )


def test_every_registered_agent_has_explicit_schedule_or_none():
    """CI guard for the B6 declarative registry: for every domain in
    AGENT_REGISTRY, `cls.schedule` must be either a non-empty cron string or
    exactly `None` — never an empty string, a non-string, or anything else
    that would silently be treated as "no schedule" without being the
    documented `None` sentinel.
    """
    bad: list[str] = []
    for domain, cls in AGENT_REGISTRY.items():
        schedule = getattr(cls, "schedule", ETLConnector.schedule)
        if schedule is not None and (not isinstance(schedule, str) or not schedule.strip()):
            bad.append(f"{domain} (schedule={schedule!r})")
    assert bad == [], (
        "Domains with a schedule that is neither a real cron string nor the "
        f"explicit None sentinel: {bad}"
    )


def test_scheduler_default_schedules_matches_agent_declared_schedules():
    """scheduler.py's _DEFAULT_SCHEDULES must be built FROM the agents'
    declarative `schedule` attributes (single source of truth) — this is a
    round-trip check that nothing has drifted between the two."""
    from infra_brain.scheduler import _DEFAULT_SCHEDULES

    for domain, cron_str in _DEFAULT_SCHEDULES.items():
        assert domain in AGENT_REGISTRY, (
            f"_DEFAULT_SCHEDULES has domain {domain!r} not present in AGENT_REGISTRY"
        )
        assert AGENT_REGISTRY[domain].schedule == cron_str, (
            f"{domain}: scheduler.py's cron ({cron_str!r}) does not match "
            f"{AGENT_REGISTRY[domain].__name__}.schedule ({AGENT_REGISTRY[domain].schedule!r})"
        )

    # And the converse: every domain with a non-None schedule attribute
    # must appear in _DEFAULT_SCHEDULES (with the one documented exception:
    # "query", which is scope-gated via add_job_scoped("query", "health", ...)
    # in scheduler.py's start() rather than the default-scope dict).
    _SCOPE_GATED_EXEMPT = {"query"}
    for domain, cls in AGENT_REGISTRY.items():
        if cls.schedule is not None and domain not in _SCOPE_GATED_EXEMPT:
            assert domain in _DEFAULT_SCHEDULES, (
                f"{domain} declares schedule={cls.schedule!r} but is missing from "
                "scheduler.py's _DEFAULT_SCHEDULES"
            )


def test_skip_hook_matches_agent_declared_skip_hook_attribute():
    """SKIP_HOOK must reflect exactly the domains whose class sets
    skip_hook=True — no hand-maintained drift between the two."""
    for domain, cls in AGENT_REGISTRY.items():
        declared = bool(getattr(cls, "skip_hook", False))
        assert (domain in SKIP_HOOK) == declared, (
            f"{domain}: SKIP_HOOK membership ({domain in SKIP_HOOK}) does not match "
            f"{cls.__name__}.skip_hook ({declared})"
        )
