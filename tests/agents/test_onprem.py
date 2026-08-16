"""F-006: the onprem alias has been removed; importing it must fail.

Superseded by tests/agents/test_onprem_removed.py, kept here (renamed in spirit)
so history of the removal is visible in this file's own diff.
"""

import importlib

import pytest


def test_onprem_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("infra_brain.agents.onprem")
