"""
InventoryMRAgent — proposes Ansible inventory corrections via GitLab MR.
Called when drift detection identifies a host present in actual collection
but absent from the Ansible inventory (or vice versa).
Read-only on infra; writes only to GitLab (MR creation) and Postgres (drift status).
Registered in AGENT_REGISTRY under domain key "inventory_mr" (SKIP_HOOK member).
collect() returns [] — a plain dispatch() call is a no-op run; callers that need to
open an actual MR must invoke propose_inventory_fix() directly.
"""

import uuid
import logging
from infra_brain.agents.base import BaseAgent
from infra_brain.db.models import AgentActionLog
from infra_brain.db.session import get_session
from infra_brain.tools.gitlab_mr import create_inventory_mr
from infra_brain.etl.spec import AgentSpec, Tier

logger = logging.getLogger(__name__)


class InventoryMRAgent(BaseAgent):
    spec = AgentSpec(
        domain="inventory_mr",
        tier=Tier.ON_DEMAND,
        schedule=None,
        max_staleness=None,
        skip_hook=True,
    )

    def collect(self, scope: str = "all") -> list[dict]:
        """Not a data-collection agent; called directly via propose_inventory_fix()."""
        return []

    def propose_inventory_fix(
        self,
        drift_event_id: uuid.UUID,
        project_id: int,
        inventory_file_path: str,
        corrected_content: str,
        description: str,
    ) -> str:
        """
        Open a GitLab MR proposing a corrected inventory file.
        Returns the MR web URL.
        Follows the 'propose, never dispose' pattern — humans merge, not the agent.
        Branch name embeds the first 8 chars of the drift event ID for traceability
        and idempotency (re-running for the same drift ID reuses the same branch).
        """
        short_id = str(drift_event_id)[:8]
        branch_name = f"infra-brain/inventory-drift-{short_id}"
        mr_title = f"[Infra Brain] Inventory drift fix ({short_id})"
        mr_description = (
            f"**Auto-proposed by Infra Brain drift detection.**\n\n"
            f"Drift event ID: `{drift_event_id}`\n\n"
            f"{description}\n\n"
            f"Review the changed inventory file and merge if the proposed hosts/groups are correct."
        )
        args_summary = f"project={project_id} branch={branch_name} file={inventory_file_path}"
        url: str | None = None
        log_status = "ok"
        log_error: str | None = None
        log_verdict = "allow"
        # AA-R-17: the original exception object (traceback, type, args) must
        # survive past the `finally` audit-write so the final raise below can
        # chain it with `from`. Previously only `str(exc)` was kept in
        # log_error, and the final `raise RuntimeError(log_error)` had no
        # `from` clause — the real exception (and its traceback) was discarded,
        # leaving only a flattened string for anyone debugging the failure.
        original_exc: Exception | None = None
        try:
            url = create_inventory_mr(
                project_id=project_id,
                branch_name=branch_name,
                file_path=inventory_file_path,
                new_content=corrected_content,
                commit_message=f"fix(inventory): drift correction for event {short_id}",
                mr_title=mr_title,
                mr_description=mr_description,
            )
            args_summary += f" mr_url={url}"
            logger.info("InventoryMRAgent: opened MR %s for drift %s", url, drift_event_id)
        except PermissionError as exc:
            # F-004.3: write-gate denial — record the REAL verdict, not "allow".
            log_verdict = "deny"
            log_status = "error"
            log_error = str(exc)
            original_exc = exc
            logger.error(
                "InventoryMRAgent: MR for drift %s BLOCKED by write gate: %s",
                drift_event_id,
                exc,
            )
        except Exception as exc:
            log_status = "error"
            log_error = str(exc)
            original_exc = exc
            logger.error(
                "InventoryMRAgent: failed to create MR for drift %s: %s", drift_event_id, exc
            )
        finally:
            # C1: audit every create_inventory_mr call (pass, fail, or deny)
            with get_session() as audit_session:
                audit_session.add(
                    AgentActionLog(
                        agent="InventoryMRAgent",
                        tool="create_inventory_mr",
                        args_summary=args_summary,
                        verdict=log_verdict,
                        status=log_status,
                        error=log_error,
                    )
                )
                audit_session.commit()
        if log_status == "error":
            raise RuntimeError(log_error) from original_exc
        return url  # type: ignore[return-value]
