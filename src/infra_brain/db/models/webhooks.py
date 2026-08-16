"""Outbound webhook subscription/event-publish models (GitLab #112).

Bucket: WebhookSubscription, WebhookDelivery.

Before this, ``config.py``'s ``ops_webhook_url`` was a single configured
destination for ALL ``send_ops_alert`` calls — no per-event-type registry,
no multiple subscribers. This module is the subscription-table half of the
fix described in GitLab issue #112: multiple third parties register their
own endpoint + event-type filter (e.g. "notify me only on ``drift.critical``
for domain=vsphere") instead of one hardcoded destination for everything.

``tools/webhook_publish.py`` is the delivery-with-retry half, reusing
``gate_external_write`` per destination (same DLP/audit choke point every
other sanctioned external write goes through).
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ._base import Base, _now, _uuid

# Delivery status values for WebhookDelivery.status.
DELIVERY_PENDING = "pending"  # queued for first attempt. Column default only: no row
# is persisted with this status today, because ``publish_event`` in
# tools/webhook_publish.py attempts delivery synchronously and always inserts the
# row with the resolved outcome — DELIVERY_DELIVERED on success, DELIVERY_RETRYING
# on failure. Reserved for a future enqueue-then-deliver path.
DELIVERY_DELIVERED = "delivered"
DELIVERY_RETRYING = "retrying"  # failed at least once, more attempts remain
DELIVERY_EXHAUSTED = "exhausted"  # failed max_attempts times, given up

# Backoff schedule (minutes) applied to WebhookDelivery.attempt_count when a
# delivery fails and is scheduled for retry. Index 0 = delay before 2nd
# attempt, index 1 = delay before 3rd, etc. Beyond the list, the last value
# repeats. Mirrors the Datadog/PagerDuty outbound-webhook retry cadence
# named in the issue.
RETRY_BACKOFF_MINUTES = [1, 5, 15, 60]
DEFAULT_MAX_ATTEMPTS = 5


class WebhookSubscription(Base):
    """A third-party-registered outbound webhook destination + event filter.

    ``event_pattern`` matches against the dotted ``category`` string passed
    to ``publish_event()`` (e.g. ``"drift.critical"``). Matching is a simple
    prefix/glob check done in ``tools/webhook_publish.py`` — ``"*"`` matches
    everything, ``"drift.*"`` matches any category starting with ``"drift."``,
    an exact string matches only itself. ``domain_filter`` is an optional
    additional AND-condition (e.g. only vsphere-domain events) — null/empty
    means "any domain".
    """

    __tablename__ = "webhook_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128))
    target_url: Mapped[str] = mapped_column(String(1024))
    # Optional bearer token sent as Authorization: Bearer <secret> — mirrors
    # ops_webhook_token's shape for the single legacy destination.
    secret_token: Mapped[str | None] = mapped_column(String(256), nullable=True)
    event_pattern: Mapped[str] = mapped_column(String(128), default="*")
    domain_filter: Mapped[str | None] = mapped_column(String(64), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    deliveries: Mapped[list["WebhookDelivery"]] = relationship(
        back_populates="subscription", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_webhook_subscriptions_active", "active"),
        Index("ix_webhook_subscriptions_event_pattern", "event_pattern"),
    )


class WebhookDelivery(Base):
    """One delivery attempt record for one (subscription, event) pair.

    Only created for a subscription once its first delivery attempt is made
    (successful rows are recorded too, for audit/history — see
    ``get_recent_deliveries``). Failed rows drive the retry worker: they
    carry ``next_attempt_at`` (exponential backoff via
    ``RETRY_BACKOFF_MINUTES``) and ``attempt_count`` vs ``max_attempts``.
    """

    __tablename__ = "webhook_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("webhook_subscriptions.id", ondelete="CASCADE")
    )
    category: Mapped[str] = mapped_column(String(128))
    domain: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dedup_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    payload_summary: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default=DELIVERY_PENDING)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=DEFAULT_MAX_ATTEMPTS)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    subscription: Mapped["WebhookSubscription"] = relationship(back_populates="deliveries")

    __table_args__ = (
        Index("ix_webhook_deliveries_status_next_attempt", "status", "next_attempt_at"),
        Index("ix_webhook_deliveries_subscription_id", "subscription_id"),
    )
