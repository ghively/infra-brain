from unittest.mock import patch, MagicMock

from infra_brain.tools.identity import _is_same_okta_host


def _mock_settings(okta_url="https://example.okta.com"):
    s = MagicMock()
    s.okta_url = okta_url
    s.okta_api_token = "token"
    s.okta_ssl_verify = True
    s.api_timeout_seconds = 30
    return s


def test_is_same_okta_host_accepts_identical_origin():
    with patch("infra_brain.tools.identity.get_settings", return_value=_mock_settings()):
        assert _is_same_okta_host("https://example.okta.com/api/v1/users?after=abc") is True


def test_is_same_okta_host_rejects_different_host():
    with patch("infra_brain.tools.identity.get_settings", return_value=_mock_settings()):
        assert _is_same_okta_host("https://attacker.example.com/api/v1/users") is False


def test_is_same_okta_host_rejects_different_scheme():
    with patch("infra_brain.tools.identity.get_settings", return_value=_mock_settings()):
        assert _is_same_okta_host("http://example.okta.com/api/v1/users") is False


def test_is_same_okta_host_rejects_same_host_different_port():
    """Regression test: a same-hostname Link header pointing at a different
    port must not be treated as the same Okta origin — otherwise the SSWS
    bearer token would be sent to an unexpected destination on that host."""
    with patch("infra_brain.tools.identity.get_settings", return_value=_mock_settings()):
        assert _is_same_okta_host("https://example.okta.com:8443/api/v1/users") is False


def test_is_same_okta_host_matches_when_configured_port_explicit():
    with patch(
        "infra_brain.tools.identity.get_settings",
        return_value=_mock_settings(okta_url="https://example.okta.com:8443"),
    ):
        assert _is_same_okta_host("https://example.okta.com:8443/api/v1/users") is True
        assert _is_same_okta_host("https://example.okta.com/api/v1/users") is False


def test_is_same_okta_host_rejects_malformed_port():
    with patch("infra_brain.tools.identity.get_settings", return_value=_mock_settings()):
        assert _is_same_okta_host("https://example.okta.com:notaport/api/v1/users") is False
