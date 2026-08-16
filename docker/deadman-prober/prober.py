#!/usr/bin/env python3
"""Standalone dead-man prober for infra-brain (Phase 4 OB-1).

STANDALONE BY DESIGN: this module must never import anything from
infra_brain. It runs as its own container, outside the app's trust boundary,
and only ever sends synthetic health strings (endpoint name + up/down,
heartbeat age numbers) to the ops webhook — never real infrastructure data.
That is also why it bypasses infra-brain's write-gate layer
(callbacks/write_gate.py): the gate exists to police the LLM/agent layer's
outbound writes, and this process is not part of that layer at all. See
docker/docker-compose.yml's prober service comment and README.md.

Loop, every PROBE_INTERVAL seconds (default 120):
  - GET  {APP_URL}/healthz
  - GET  {APP_URL}/health
  - GET  {APP_URL}/api/ops/heartbeat   -> {heartbeat_age_seconds, max_age_seconds, stale}

Alerting rules:
  - Each of healthz/health is an independent "endpoint" whose failures are
    counted; an alert fires only after 3 CONSECUTIVE failures (no flapping
    spam on a single blip).
  - The heartbeat endpoint alerts ONLY when the endpoint itself reports
    stale=True — never merely because the prober couldn't compute freshness
    itself, and never on a few minutes of staleness (the heartbeat is written
    hourly; server-side threshold already carries slack — see
    callbacks/freshness.py::get_scheduler_heartbeat_status).
  - Alerts fire once per incident (edge-triggered) with a matching recovery
    alert when the condition clears — no repeat spam every loop iteration.

Webhook payload shape mirrors infra_brain.tools.ops_webhook.send_ops_alert:
    {"category": ..., "messages": [...]}
plus an optional `Authorization: Bearer {OPS_WEBHOOK_TOKEN}` header.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import urllib.error
import urllib.request
import json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] deadman-prober: %(message)s",
)
log = logging.getLogger("deadman-prober")

FAILURE_THRESHOLD = 3


@dataclass
class EndpointState:
    """Consecutive-failure counter + edge-triggered alert state for one endpoint."""

    name: str
    consecutive_failures: int = 0
    alerting: bool = False

    def record_success(self) -> str | None:
        """Returns a recovery message if this clears an active alert, else None."""
        self.consecutive_failures = 0
        if self.alerting:
            self.alerting = False
            return f"{self.name}: recovered"
        return None

    def record_failure(self, detail: str) -> str | None:
        """Returns an alert message once the threshold is crossed, else None
        (already alerting, or not yet at threshold — no repeat spam)."""
        self.consecutive_failures += 1
        if self.consecutive_failures >= FAILURE_THRESHOLD and not self.alerting:
            self.alerting = True
            return f"{self.name}: {self.consecutive_failures} consecutive failures — {detail}"
        return None


@dataclass
class HeartbeatState:
    """Edge-triggered alert state for the heartbeat staleness condition."""

    alerting: bool = False

    def observe(self, stale: bool, age_seconds: float | None, max_age_seconds: float) -> str | None:
        """Returns an alert/recovery message on a state transition, else None.

        Alerts ONLY on stale=True (never on the mere existence of some age in
        minutes) and only once per incident.
        """
        if stale and not self.alerting:
            self.alerting = True
            age_desc = "never recorded" if age_seconds is None else f"{age_seconds:.0f}s old"
            return f"scheduler heartbeat: stale — {age_desc} (max {max_age_seconds:.0f}s)"
        if not stale and self.alerting:
            self.alerting = False
            return "scheduler heartbeat: recovered"
        return None


def _http_get_json(url: str, timeout: float) -> tuple[int, dict[str, Any] | None]:
    """GET url, returning (status_code, parsed_json_or_None).

    Raises on connection-level failure (DNS, refused, timeout) — callers treat
    that as a failure the same as a non-2xx status.
    """
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = resp.status
        body = resp.read()
    parsed = None
    if body:
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            parsed = None
    return status, parsed


def probe_endpoint(base_url: str, path: str, timeout: float) -> tuple[bool, str]:
    """Probe a plain health endpoint. Returns (ok, detail)."""
    url = base_url.rstrip("/") + path
    try:
        status, _ = _http_get_json(url, timeout)
        if 200 <= status < 300:
            return True, f"HTTP {status}"
        return False, f"HTTP {status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 — any connection failure counts as down
        return False, f"{type(exc).__name__}: {exc}"


def probe_heartbeat(base_url: str, timeout: float) -> tuple[bool, str, dict[str, Any] | None]:
    """Probe the heartbeat endpoint. Returns (ok, detail, parsed_body).

    ok=False means the ENDPOINT ITSELF failed (down/error/malformed) — this is
    counted toward the endpoint failure-threshold like healthz/health.
    Heartbeat staleness (body['stale']) is a SEPARATE, independent condition
    handled by HeartbeatState, not by this function's ok/failure counting.
    """
    url = base_url.rstrip("/") + "/api/ops/heartbeat"
    try:
        status, body = _http_get_json(url, timeout)
        if not (200 <= status < 300):
            return False, f"HTTP {status}", None
        if not isinstance(body, dict) or "stale" not in body:
            return False, "malformed response body", None
        return True, f"HTTP {status}", body
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}", None
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}", None


def build_alert_payload(category: str, messages: list[str]) -> dict[str, Any]:
    """Build the ops_webhook-shaped payload: {"category": ..., "messages": [...]}."""
    return {"category": category, "messages": messages}


def send_alert(
    webhook_url: str, token: str | None, category: str, messages: list[str], timeout: float
) -> bool:
    """POST an alert to the ops webhook. Best-effort: logs and returns False on failure."""
    if not webhook_url:
        log.info("OPS_WEBHOOK_URL not configured; skipping delivery: %s", messages)
        return True

    payload = build_alert_payload(category, messages)
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(webhook_url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ok = 200 <= resp.status < 300
            if not ok:
                log.error("ops webhook returned HTTP %s", resp.status)
            return ok
    except Exception:
        log.exception("failed to deliver alert to ops webhook")
        return False


@dataclass
class Prober:
    """Holds all per-endpoint state across loop iterations."""

    app_url: str
    webhook_url: str
    webhook_token: str | None
    request_timeout: float = 10.0

    healthz_state: EndpointState = field(default_factory=lambda: EndpointState("healthz"))
    health_state: EndpointState = field(default_factory=lambda: EndpointState("health"))
    heartbeat_endpoint_state: EndpointState = field(
        default_factory=lambda: EndpointState("heartbeat-endpoint")
    )
    heartbeat_staleness_state: HeartbeatState = field(default_factory=HeartbeatState)

    def run_once(self) -> list[str]:
        """Run one probe cycle; returns the list of alert/recovery messages sent."""
        sent: list[str] = []

        ok, detail = probe_endpoint(self.app_url, "/healthz", self.request_timeout)
        sent.extend(self._handle_endpoint(self.healthz_state, ok, detail))

        ok, detail = probe_endpoint(self.app_url, "/health", self.request_timeout)
        sent.extend(self._handle_endpoint(self.health_state, ok, detail))

        ok, detail, body = probe_heartbeat(self.app_url, self.request_timeout)
        sent.extend(self._handle_endpoint(self.heartbeat_endpoint_state, ok, detail))
        if ok and body is not None:
            msg = self.heartbeat_staleness_state.observe(
                stale=bool(body.get("stale")),
                age_seconds=body.get("heartbeat_age_seconds"),
                max_age_seconds=float(body.get("max_age_seconds", 0.0)),
            )
            if msg:
                self._deliver("heartbeat", [msg])
                sent.append(msg)

        return sent

    def _handle_endpoint(self, state: EndpointState, ok: bool, detail: str) -> list[str]:
        if ok:
            msg = state.record_success()
        else:
            msg = state.record_failure(detail)
        if msg:
            self._deliver("endpoint_health", [msg])
            return [msg]
        return []

    def _deliver(self, category: str, messages: list[str]) -> None:
        send_alert(self.webhook_url, self.webhook_token, category, messages, self.request_timeout)


def main() -> None:
    app_url = os.environ.get("APP_URL", "http://app:8000")
    webhook_url = os.environ.get("OPS_WEBHOOK_URL", "")
    webhook_token = os.environ.get("OPS_WEBHOOK_TOKEN") or None
    interval = float(os.environ.get("PROBE_INTERVAL", "120"))
    timeout = float(os.environ.get("PROBE_TIMEOUT", "10"))

    log.info(
        "starting: app_url=%s interval=%ss webhook_configured=%s",
        app_url,
        interval,
        bool(webhook_url),
    )

    prober = Prober(
        app_url=app_url,
        webhook_url=webhook_url,
        webhook_token=webhook_token,
        request_timeout=timeout,
    )

    while True:
        try:
            messages = prober.run_once()
            for m in messages:
                log.warning("alert/recovery: %s", m)
        except Exception:
            log.exception("probe cycle failed unexpectedly")
        time.sleep(interval)


if __name__ == "__main__":
    main()
