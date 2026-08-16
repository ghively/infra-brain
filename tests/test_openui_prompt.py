from infra_brain.openui.prompt import get_system_prompt


def test_prompt_is_string():
    result = get_system_prompt()
    assert isinstance(result, str)
    assert len(result) > 100


def test_prompt_contains_component_names():
    prompt = get_system_prompt()
    assert "FleetStatCard" in prompt
    assert "DriftEventTable" in prompt
    assert "SweepTimeline" in prompt


def test_prompt_contains_props():
    prompt = get_system_prompt()
    assert "domain" in prompt
    assert "status" in prompt
    assert "title" in prompt
    assert "value" in prompt


def test_prompt_contains_all_8_component_names():
    prompt = get_system_prompt()
    components = [
        "FleetStatCard",
        "DriftEventTable",
        "SweepTimeline",
        "ResourceList",
        "VulnSummary",
        "ComplianceBoard",
        "AgentActivityFeed",
        "DomainHealthGrid",
    ]
    for name in components:
        assert name in prompt, f"Component '{name}' missing from system prompt"


def test_prompt_contains_new_component_props():
    prompt = get_system_prompt()
    # ResourceList props
    assert "resource_type" in prompt
    assert "zone" in prompt
    # VulnSummary props
    assert "severity" in prompt
    # AgentActivityFeed props
    assert "verdict" in prompt
    # DomainHealthGrid props
    assert "show_drift_count" in prompt
    # Combo examples present
    assert "Example combinations" in prompt


def test_drift_event_table_status_enum_matches_real_values():
    """DriftEventTable's documented `status` enum must match the real
    drift_events.status values (open/acknowledged/resolved) — the backing
    /api/dashboard/drift_events endpoint filters on this exact value, so a
    documented value that never occurs (e.g. "closed") silently returns an
    empty table instead of the events the user meant.
    """
    prompt = get_system_prompt()
    # Isolate the DriftEventTable prop block to avoid matching other components.
    start = prompt.index("### DriftEventTable")
    end = prompt.index("---", start)
    block = prompt[start:end]

    assert '"open"|"acknowledged"|"resolved"' in block
    assert "closed" not in block


def test_prompt_contains_all_13_component_names():
    prompt = get_system_prompt()
    components = [
        # Original 8
        "FleetStatCard",
        "DriftEventTable",
        "SweepTimeline",
        "ResourceList",
        "VulnSummary",
        "ComplianceBoard",
        "AgentActivityFeed",
        "DomainHealthGrid",
        # New 5 (Octopus removed — P7.1a)
        "LinuxPackageTable",
        "LinuxServiceStatus",
        "LinuxPortScan",
        "DriftTrendChart",
        "WindowsPatchState",
    ]
    for name in components:
        assert name in prompt, f"Component '{name}' missing from system prompt"
