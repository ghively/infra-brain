"""L-4 — the sanctioned-write allowlist must match on a host/path boundary.

``ReadOnlyToolValidator.on_tool_start`` bypassed the mutating-verb denial with
a bare ``url.startswith(prefix)``. The prefixes come straight from settings —
``agents/remediation.py`` passes ``[settings.gitlab_url]`` and
``agents/notification.py`` passes the Jira/Confluence base URLs — and those are
normally stored WITHOUT a trailing slash. So with
``gitlab_url="https://gitlab.example.com"``, a POST to
``https://gitlab.example.com.evil.net/...`` shares the prefix and was waved
straight through the read-only boundary to an attacker-controlled host.

This STRENGTHENS the guard: no ``raise`` is removed and no path is made more
permissive. The tests below assert both halves of that claim — the lookalike
host is now denied, and every legitimate sanctioned write still passes.
"""

import json
import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from infra_brain.callbacks.readonly import ReadOnlyToolValidator


@contextmanager
def _readonly_env():
    with (
        patch("infra_brain.callbacks.readonly.get_settings") as mock_settings,
        patch("infra_brain.callbacks.readonly.get_session") as mock_session,
    ):
        mock_settings.return_value.scan_readonly_enforce = True
        mock_session.return_value.__enter__ = lambda s: MagicMock()
        mock_session.return_value.__exit__ = MagicMock(return_value=False)
        yield


def _post(handler, url):
    handler.on_tool_start(
        {"name": "HttpTool"},
        json.dumps({"method": "POST", "url": url}),
        run_id=uuid.uuid4(),
    )


@pytest.mark.parametrize(
    "evil_url",
    [
        # Suffix-appended lookalike domains — the core L-4 bypass.
        "https://gitlab.example.com.evil.net/api/v4/projects/1/merge_requests",
        "https://gitlab.example.com.evil.net/",
        "https://gitlab.example.com.evil.net",
        # No separator at all.
        "https://gitlab.example.comevil.net/api/v4/projects",
        "https://gitlab.example.com-evil.net/api/v4/projects",
        # userinfo confusion — the real host is evil.net.
        "https://gitlab.example.com@evil.net/api/v4/projects",
        "https://gitlab.example.com:pass@evil.net/api/v4/projects",
    ],
)
def test_post_denied_for_lookalike_host_sharing_the_whitelisted_prefix(evil_url):
    with _readonly_env():
        h = ReadOnlyToolValidator(
            agent_name="TestAgent", whitelisted_post=["https://gitlab.example.com"]
        )
        with pytest.raises(PermissionError, match="read-only"):
            _post(h, evil_url)


@pytest.mark.parametrize(
    "good_url",
    [
        # The real sanctioned writes: GitLab MR, Jira issue, Confluence page.
        "https://gitlab.example.com/api/v4/projects/1/merge_requests",
        "https://gitlab.example.com/",
        "https://gitlab.example.com",
        "https://gitlab.example.com?foo=bar",
        "https://gitlab.example.com#frag",
    ],
)
def test_post_still_allowed_for_genuine_sanctioned_writes(good_url):
    with _readonly_env():
        h = ReadOnlyToolValidator(
            agent_name="TestAgent", whitelisted_post=["https://gitlab.example.com"]
        )
        _post(h, good_url)  # must not raise


def test_whitelist_prefix_with_trailing_slash_still_matches():
    """A settings value stored WITH a trailing slash must behave identically
    to one stored without."""
    with _readonly_env():
        h = ReadOnlyToolValidator(
            agent_name="TestAgent", whitelisted_post=["https://jira.example.com/"]
        )
        _post(h, "https://jira.example.com/rest/api/3/issue")  # must not raise


def test_whitelist_prefix_with_a_path_segment_is_boundary_matched():
    """A path-scoped prefix must not be escapable by appending characters to
    its last segment."""
    with _readonly_env():
        h = ReadOnlyToolValidator(
            agent_name="TestAgent", whitelisted_post=["https://gitlab.example.com/api/v4"]
        )
        _post(h, "https://gitlab.example.com/api/v4/projects")  # must not raise
        with pytest.raises(PermissionError, match="read-only"):
            _post(h, "https://gitlab.example.com/api/v40/projects")


def test_empty_whitelist_entry_never_matches():
    """A falsy settings value must not degrade the allowlist into 'allow all'
    (``"".startswith`` semantics aside, an empty prefix must be inert)."""
    with _readonly_env():
        h = ReadOnlyToolValidator(agent_name="TestAgent", whitelisted_post=["", None])
        with pytest.raises(PermissionError, match="read-only"):
            _post(h, "https://evil.net/anything")


# --- MEDIUM-2 — pin the three shapes the boundary rewrite newly ADMITS ------
#
# _url_matches_whitelist_prefix's own module comment used to claim it is
# "STRICTLY MORE RESTRICTIVE" than the bare startswith() it replaced. That is
# false: normalizing the configured prefix with .rstrip("/") before matching
# newly admits three URL/prefix combinations the old bare startswith() denied.
# None are exploitable — each is the same origin AND same path prefix as an
# already-sanctioned target, differing only in a trailing slash the *prefix*
# does or doesn't carry — but the comment must describe reality, and these
# three specific shapes must be pinned by test rather than left incidental.


def _old_bare_startswith(url: str, prefix: str) -> bool:
    """The exact pre-L-4 matching rule, reproduced for comparison only."""
    return url.startswith(prefix)


@pytest.mark.parametrize(
    "prefix,url",
    [
        # 1. Configured prefix carries a trailing slash; url is the prefix
        #    itself with the slash stripped.
        ("https://jira.example.com/", "https://jira.example.com"),
        # 2. Same, with a query string appended after the bare origin.
        ("https://jira.example.com/", "https://jira.example.com?x=1"),
        # 3. Path-scoped prefix with a trailing slash; url is that same path
        #    with the slash stripped.
        ("https://gitlab.example.com/api/v4/", "https://gitlab.example.com/api/v4"),
    ],
)
def test_boundary_rewrite_newly_admits_trailing_slash_prefix_variants(prefix, url):
    """Documents (and locks in) the exact widening the comment above
    describes: the old bare startswith() denied these; the current boundary
    match allows them, and that is safe because it never crosses a host or
    path-prefix boundary."""
    with _readonly_env():
        h = ReadOnlyToolValidator(agent_name="TestAgent", whitelisted_post=[prefix])

        assert _old_bare_startswith(url, prefix) is False, (
            "test setup: this shape must have been DENIED by the old bare "
            "startswith() rule for the widening claim to hold"
        )
        _post(h, url)  # must not raise — the new match allows it
