"""Outbound webhook subscription management routes (GitLab #112).

Dashboard-management-page backend for the subscription table half of the
webhook-out system: session/admin-gated CRUD over ``WebhookSubscription``,
a manual "send test delivery" action (goes through the same
``publish_event`` path — and therefore the same ``gate_external_write``
DLP/audit gate — as a real event), and a read-only delivery-history view
for the retry worker's rows. Mirrors ``api/routers/mcp_keys.py``'s
session/admin auth pattern. The raw ``secret_token`` is accepted on
create/update but never echoed back — list/get responses only report
``has_secret``.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from infra_brain.api.schemas import (
    WebhookDeliveryOut,
    WebhookDeliveryPageOut,
    WebhookSubscriptionCreateBody,
    WebhookSubscriptionDeleteResult,
    WebhookSubscriptionOut,
    WebhookSubscriptionPageOut,
    WebhookSubscriptionUpdateBody,
    WebhookTestDeliveryBody,
    WebhookTestDeliveryResult,
)
from infra_brain.dashboard_auth import current_user, require_admin, require_session
from infra_brain.db.models import WebhookDelivery, WebhookSubscription
from infra_brain.db.session import get_session

logger = logging.getLogger(__name__)

webhook_subscriptions_router = APIRouter(
    prefix="/api/dashboard/webhook-subscriptions",
    tags=["webhook-subscriptions"],
    dependencies=[Depends(require_session)],
)


def _parse_uuid(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="id must be a UUID")


def _sub_out(row: WebhookSubscription) -> WebhookSubscriptionOut:
    return WebhookSubscriptionOut(
        id=str(row.id),
        name=row.name,
        target_url=row.target_url,
        has_secret=bool(row.secret_token),
        event_pattern=row.event_pattern,
        domain_filter=row.domain_filter,
        active=row.active,
        description=row.description or "",
        created_by=row.created_by or "",
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _delivery_out(row: WebhookDelivery) -> WebhookDeliveryOut:
    return WebhookDeliveryOut(
        id=str(row.id),
        subscription_id=str(row.subscription_id),
        category=row.category,
        domain=row.domain,
        dedup_key=row.dedup_key,
        status=row.status,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        last_error=row.last_error,
        next_attempt_at=row.next_attempt_at,
        created_at=row.created_at,
        delivered_at=row.delivered_at,
    )


@webhook_subscriptions_router.get("", response_model=WebhookSubscriptionPageOut)
def list_webhook_subscriptions(_: None = Depends(require_admin)):
    with get_session() as s:
        rows = (
            s.query(WebhookSubscription).order_by(WebhookSubscription.created_at.desc()).all()
        )
        items = [_sub_out(r) for r in rows]
    return WebhookSubscriptionPageOut(items=items, total=len(items))


@webhook_subscriptions_router.post("", response_model=WebhookSubscriptionOut)
def create_webhook_subscription(
    body: WebhookSubscriptionCreateBody,
    request: Request,
    _: None = Depends(require_admin),
):
    user = current_user(request) or {}
    created_by = user.get("username") or "dashboard"
    with get_session() as s:
        row = WebhookSubscription(
            name=body.name,
            target_url=body.target_url,
            secret_token=body.secret_token or None,
            event_pattern=body.event_pattern or "*",
            domain_filter=body.domain_filter or None,
            description=body.description or "",
            created_by=created_by,
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        result = _sub_out(row)
    return result


@webhook_subscriptions_router.patch("/{sub_id}", response_model=WebhookSubscriptionOut)
def update_webhook_subscription(
    sub_id: str,
    body: WebhookSubscriptionUpdateBody,
    _: None = Depends(require_admin),
):
    sid = _parse_uuid(sub_id)
    with get_session() as s:
        row = s.get(WebhookSubscription, sid)
        if row is None:
            raise HTTPException(status_code=404, detail="subscription not found")

        if body.name is not None:
            row.name = body.name
        if body.target_url is not None:
            row.target_url = body.target_url
        if body.secret_token is not None:
            row.secret_token = body.secret_token or None
        if body.event_pattern is not None:
            row.event_pattern = body.event_pattern or "*"
        if body.domain_filter is not None:
            row.domain_filter = body.domain_filter or None
        if body.active is not None:
            row.active = body.active
        if body.description is not None:
            row.description = body.description

        s.commit()
        s.refresh(row)
        result = _sub_out(row)
    return result


@webhook_subscriptions_router.delete("/{sub_id}", response_model=WebhookSubscriptionDeleteResult)
def delete_webhook_subscription(sub_id: str, _: None = Depends(require_admin)):
    sid = _parse_uuid(sub_id)
    with get_session() as s:
        row = s.get(WebhookSubscription, sid)
        if row is None:
            raise HTTPException(status_code=404, detail="subscription not found")
        s.delete(row)
        s.commit()
    return WebhookSubscriptionDeleteResult(id=sub_id, deleted=True)


@webhook_subscriptions_router.post(
    "/{sub_id}/test", response_model=WebhookTestDeliveryResult
)
def send_test_delivery(
    sub_id: str,
    body: WebhookTestDeliveryBody,
    _: None = Depends(require_admin),
):
    """Manual one-off delivery to a single subscription, bypassing the
    event_pattern/domain_filter match (this is an explicit operator test,
    not a real event fan-out) — but still going through the same
    gate_external_write DLP/audit gate every other outbound write does."""
    from infra_brain.config import get_settings
    from infra_brain.tools.webhook_publish import _deliver

    sid = _parse_uuid(sub_id)
    with get_session() as s:
        row = s.get(WebhookSubscription, sid)
        if row is None:
            raise HTTPException(status_code=404, detail="subscription not found")

        try:
            delivered, error = _deliver(
                row,
                body.category,
                [body.message],
                None,
                "dashboard-test-delivery",
                get_settings().api_timeout_seconds,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    return WebhookTestDeliveryResult(id=sub_id, delivered=delivered, error=error)


@webhook_subscriptions_router.get(
    "/{sub_id}/deliveries", response_model=WebhookDeliveryPageOut
)
def list_deliveries(sub_id: str, limit: int = 50, _: None = Depends(require_admin)):
    sid = _parse_uuid(sub_id)
    limit = max(1, min(limit, 200))
    with get_session() as s:
        base_query = s.query(WebhookDelivery).filter_by(subscription_id=sid)
        total = base_query.count()
        rows = (
            base_query.order_by(WebhookDelivery.created_at.desc()).limit(limit).all()
        )
        items = [_delivery_out(r) for r in rows]
    return WebhookDeliveryPageOut(items=items, total=total)
