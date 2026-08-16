"""Tests for tenacity retry behaviour on HTTP helpers.

F-02 / F-12 (audit 2026-07-06): rapid7.py and octopus_tool.py lacked retry on
transient HTTP errors; gitlab.py had a hand-rolled time.sleep loop.

Tests verify:
  - 502-then-200: retry fires and the second attempt succeeds.
  - 404: NOT retried (permanent 4xx error).
  - Retry-After header on gitlab 429 respected (bounded at 60 s).
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok_resp(data=None):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = data or {"ok": True}
    r.raise_for_status = MagicMock()
    r.headers = {}
    return r


def _err_resp(status: int) -> httpx.HTTPStatusError:
    mock_r = MagicMock()
    mock_r.status_code = status
    mock_r.headers = {}
    return httpx.HTTPStatusError(f"HTTP {status}", request=MagicMock(), response=mock_r)


def _mock_r7_settings():
    s = MagicMock()
    s.rapid7_url = "http://r7.example.com"
    s.rapid7_username = "user"
    s.rapid7_api_key = "key"
    s.rapid7_ssl_verify = False
    s.api_timeout_seconds = 30
    return s


def _mock_octopus_settings():
    s = MagicMock()
    s.octopus_url = "http://octopus.example.com"
    s.octopus_api_key = "apikey"
    s.octopus_ssl_verify = False
    s.api_timeout_seconds = 30
    s.api_page_size = 30
    return s


def _mock_gitlab_settings():
    s = MagicMock()
    s.gitlab_url = "http://gitlab.example.com"
    s.gitlab_token = "glpat-test"
    s.gitlab_ssl_verify = False
    s.api_timeout_seconds = 30
    s.api_page_size = 20
    return s


# ---------------------------------------------------------------------------
# rapid7.py — _r7_get retry behaviour
# ---------------------------------------------------------------------------


class TestRapid7Retry:
    def _mock_client_cls(self, get_side_effects: list):
        """Return a patched ReadOnlyClient whose .get() yields side_effects in order."""
        mock_client = MagicMock()
        mock_client.get.side_effect = get_side_effects
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_cls = MagicMock(return_value=mock_client)
        return mock_cls, mock_client

    def test_502_retries_and_succeeds_on_200(self):
        """_r7_get retries on 502 and succeeds on the next attempt (no wait in tests)."""
        ok = _ok_resp({"resources": []})
        ok.raise_for_status = MagicMock()

        call_count = [0]

        def get_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise _err_resp(502)
            return ok

        mock_cls, mock_client = self._mock_client_cls(get_side_effect)

        with (
            patch("infra_brain.tools.rapid7.get_settings", return_value=_mock_r7_settings()),
            patch("infra_brain.tools.rapid7.ReadOnlyClient", mock_cls),
            patch("time.sleep"),  # skip tenacity wait
        ):
            from infra_brain.tools.rapid7 import _r7_get

            resp = _r7_get("/api/3/assets", params={"page": 0, "size": 10})

        assert call_count[0] == 2  # retried once
        assert resp.json() == {"resources": []}

    def test_404_not_retried(self):
        """_r7_get raises immediately on 404 without retry."""
        call_count = [0]

        def get_side_effect(*args, **kwargs):
            call_count[0] += 1
            raise _err_resp(404)

        mock_cls, mock_client = self._mock_client_cls(get_side_effect)

        with (
            patch("infra_brain.tools.rapid7.get_settings", return_value=_mock_r7_settings()),
            patch("infra_brain.tools.rapid7.ReadOnlyClient", mock_cls),
            patch("time.sleep"),
        ):
            from infra_brain.tools.rapid7 import _r7_get

            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                _r7_get("/api/3/assets/999")

        assert exc_info.value.response.status_code == 404
        assert call_count[0] == 1  # no retry

    def test_tool_level_502_retries_via_r7_get(self):
        """rapid7_assets_tool retries a 502 transparently and returns data."""
        ok = _ok_resp({"resources": [{"id": 1}], "page": {"totalPages": 1}})
        ok.raise_for_status = MagicMock()

        call_count = [0]

        def get_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise _err_resp(502)
            return ok

        mock_client = MagicMock()
        mock_client.get.side_effect = get_side_effect
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_cls = MagicMock(return_value=mock_client)

        with (
            patch("infra_brain.tools.rapid7.get_settings", return_value=_mock_r7_settings()),
            patch("infra_brain.tools.rapid7.ReadOnlyClient", mock_cls),
            patch("time.sleep"),
        ):
            from infra_brain.tools.rapid7 import rapid7_assets_tool

            result = rapid7_assets_tool.invoke({"page": 0, "size": 10})

        assert result["resources"][0]["id"] == 1
        assert call_count[0] == 2


# ---------------------------------------------------------------------------
# octopus_tool.py — _octopus_raw_get retry behaviour
# ---------------------------------------------------------------------------


class TestOctopusRetry:
    def test_502_retries_and_succeeds_on_200(self):
        """octopus_get retries on 502 and succeeds on the next attempt."""
        ok = _ok_resp({"api": "v1"})

        call_count = [0]

        def fake_readonly_get(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise _err_resp(502)
            return ok

        with (
            patch(
                "infra_brain.tools.octopus_tool.get_settings", return_value=_mock_octopus_settings()
            ),
            patch("infra_brain.tools.octopus_tool.readonly_get", side_effect=fake_readonly_get),
            patch("time.sleep"),
        ):
            from infra_brain.tools.octopus_tool import octopus_get

            result = octopus_get("/api/")

        assert call_count[0] == 2
        assert result == {"api": "v1"}

    def test_404_not_retried(self):
        """octopus_get raises immediately on 404 without retry."""
        call_count = [0]

        def fake_readonly_get(*args, **kwargs):
            call_count[0] += 1
            raise _err_resp(404)

        with (
            patch(
                "infra_brain.tools.octopus_tool.get_settings", return_value=_mock_octopus_settings()
            ),
            patch("infra_brain.tools.octopus_tool.readonly_get", side_effect=fake_readonly_get),
            patch("time.sleep"),
        ):
            from infra_brain.tools.octopus_tool import octopus_get

            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                octopus_get("/api/does-not-exist")

        assert exc_info.value.response.status_code == 404
        assert call_count[0] == 1


# ---------------------------------------------------------------------------
# gitlab.py — _gitlab_request retry behaviour (tenacity replaces time.sleep loop)
# ---------------------------------------------------------------------------


class TestGitlabRetry:
    def test_502_retries_and_succeeds_on_200(self):
        """_gitlab_request retries on 502 and succeeds on the next attempt."""
        ok = _ok_resp([{"id": 1}])
        ok.headers = {"X-Next-Page": ""}

        call_count = [0]

        def fake_readonly_get(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise _err_resp(502)
            return ok

        with (
            patch("infra_brain.tools.gitlab.get_settings", return_value=_mock_gitlab_settings()),
            patch("infra_brain.tools.gitlab.readonly_get", side_effect=fake_readonly_get),
            patch("time.sleep"),
        ):
            from infra_brain.tools.gitlab import gitlab_get

            result = gitlab_get("/api/v4/projects")

        assert call_count[0] == 2
        assert result[0]["id"] == 1

    def test_404_not_retried(self):
        """_gitlab_request raises immediately on 404 without retry."""
        call_count = [0]

        def fake_readonly_get(*args, **kwargs):
            call_count[0] += 1
            raise _err_resp(404)

        with (
            patch("infra_brain.tools.gitlab.get_settings", return_value=_mock_gitlab_settings()),
            patch("infra_brain.tools.gitlab.readonly_get", side_effect=fake_readonly_get),
            patch("time.sleep"),
        ):
            from infra_brain.tools.gitlab import gitlab_get

            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                gitlab_get("/api/v4/nonexistent")

        assert exc_info.value.response.status_code == 404
        assert call_count[0] == 1

    def test_429_respects_retry_after_header(self):
        """gitlab 429 with Retry-After header caps wait at 60 s (bounded)."""
        # The wait function reads Retry-After and returns min(value, 60)
        from infra_brain.tools.gitlab import _gitlab_wait

        mock_state = MagicMock()
        # Build a 429 HTTPStatusError with Retry-After: 120 (should be capped at 60)
        mock_r = MagicMock()
        mock_r.status_code = 429
        mock_r.headers = {"Retry-After": "120"}
        exc = httpx.HTTPStatusError("429", request=MagicMock(), response=mock_r)
        mock_state.outcome.exception.return_value = exc

        wait_seconds = _gitlab_wait(mock_state)
        assert wait_seconds <= 60.0, "Retry-After wait must be capped at 60 s"

    def test_429_no_retry_after_uses_exponential_jitter(self):
        """gitlab 429 without Retry-After falls back to exponential jitter."""
        from infra_brain.tools.gitlab import _gitlab_wait

        mock_state = MagicMock()
        mock_r = MagicMock()
        mock_r.status_code = 429
        mock_r.headers = {}
        exc = httpx.HTTPStatusError("429", request=MagicMock(), response=mock_r)
        mock_state.outcome.exception.return_value = exc
        mock_state.attempt_number = 1

        wait_seconds = _gitlab_wait(mock_state)
        # Exponential jitter: base = min(2^1, 60) + [0, 1) = [2, 3)
        assert 0 < wait_seconds <= 60.0
