"""Doc-vs-code consistency checks for db/models/webhooks.py.

The delivery-record contract ("which statuses actually get persisted, and by
what") is documented in three places that drifted apart: the DELIVERY_PENDING
comment, the WebhookDelivery docstring, and the real inserting code in
tools/webhook_publish.py. These tests pin the comment to the code so the next
change to publish_event's insert paths cannot silently invalidate it again.
"""

import ast
import inspect

from infra_brain.db.models import webhooks as webhook_models
from infra_brain.tools import webhook_publish


def _model_source() -> str:
    return inspect.getsource(webhook_models)


def test_model_docs_do_not_reference_nonexistent_functions():
    """Comments/docstrings must not name a delivery entry point that does not
    exist. The stale comment referenced ``deliver_event``; the real public
    entry point is ``publish_event``."""
    source = _model_source()
    assert "deliver_event" not in source
    assert hasattr(webhook_publish, "publish_event")


def test_pending_comment_matches_publish_event_insert_statuses():
    """publish_event inserts WebhookDelivery rows on the success, retryable-
    failure, and permanently-denied (write-gate PermissionError) paths, and
    never with DELIVERY_PENDING — so the model must not claim that only
    failures land in the table."""
    tree = ast.parse(inspect.getsource(webhook_publish.publish_event))
    inserted_statuses = {
        kw.value.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "WebhookDelivery"
        for kw in node.keywords
        if kw.arg == "status" and isinstance(kw.value, ast.Name)
    }

    # All three real outcomes are recorded; none is "pending". DELIVERY_EXHAUSTED
    # covers the write-gate PermissionError path (a non-retryable denial —
    # see the except PermissionError block), same terminal status
    # retry_pending_deliveries() uses when its own retry budget is spent.
    assert inserted_statuses == {"DELIVERY_DELIVERED", "DELIVERY_RETRYING", "DELIVERY_EXHAUSTED"}
    assert "DELIVERY_PENDING" not in inserted_statuses

    source = _model_source()
    assert "only rows that FAIL land in the table" not in source
    # The comment must still be accurate about pending being unused in practice.
    assert "Column default only" in source


def test_webhook_delivery_status_default_is_pending():
    """DELIVERY_PENDING remains the column default even though no code path
    persists it — the comment describes exactly this."""
    default = webhook_models.WebhookDelivery.__table__.c.status.default
    assert default.arg == webhook_models.DELIVERY_PENDING
