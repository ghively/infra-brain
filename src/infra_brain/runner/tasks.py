"""Standing tasks for the headless runner (P6.4, convergence plan Phase 6).

Reworked for a headless, non-interactive runner rather than reusing the
infra-ops Claude Code plugin's ``/drift-check``/``/self-review``/
``/eol-report`` commands verbatim — those don't map cleanly onto this
runner (see CONVERGENCE-PLAN.md's P6.4 section for the full reasoning):

- **drift-triage** uses infra-brain's OWN already-collected drift data
  (``query_drift_events``/``query_drift_trend``) instead of re-running an
  Ansible ``--check --diff`` pass — the plugin's ``/drift-check`` is
  specifically about Ansible playbook drift, a different notion of "drift"
  than infra-brain's own collected-resource drift, and the runner has no
  natural way to shell out to ``ansible-playbook`` against a real inventory
  anyway.
- **self-review** reviews infra-brain's OWN audit trail (via the read
  tools + governance events it has itself recorded), not the plugin's
  ``knowledge/.infra-ops/state-store/observations.json`` — a headless
  runner never produces that file (it's written by the plugin's own
  ``observe_runner``/``session-debrief`` hooks during interactive Claude
  Code sessions), so reviewing it would silently review nothing.
- **eol-report** uses the existing ``query_eol_status`` tool directly
  instead of delegating to the ``eol-lifecycle-planner`` specialist agent
  (which the runner cannot load — P6.3 is deferred; see that section).

Each entry is ``(task_name, prompt)`` — ``scheduler.py`` registers one
cron job per entry via ``runner.agent_task.run_agent_task_sync``.
"""

DRIFT_TRIAGE_PROMPT = """Run the nightly drift-triage standing task.

1. Query recent drift events (query_drift_events) and the drift trend (query_drift_trend) for the last 24 hours.
2. Triage: for anything NEW or high-severity, summarize what changed and why it matters.
3. If there is anything worth a human's attention, file ONE GitLab issue (runner_create_gitlab_issue) titled "Drift triage: <today's date> - <N> event(s) need review" with the concrete details (host, field, old/new value) in the body.
4. Record a governance event (runner_record_governance_event, event_type="drift-triage-run") with a payload summarizing what you reviewed and whether you filed an issue.
5. If nothing needs attention, do NOT file an issue - just record the governance event and say so in your final message.
"""

SELF_REVIEW_PROMPT = """Run the weekly self-review standing task, reviewing your OWN (the headless runner's) recent activity and infra-brain's current state.

1. Use your read tools (drift events, fleet health, compliance, vulnerabilities, EOL status) to check for anything that has been flagged repeatedly across recent runs without resolution, or an area with no data at all.
2. If you find a real, actionable pattern worth capturing for humans to review, propose an instinct (runner_propose_instinct) describing it, or file a GitLab issue (runner_create_gitlab_issue) if it needs more attention than an instinct proposal.
3. Record a governance event (runner_record_governance_event, event_type="self-review-run") summarizing what you found.
4. If nothing notable stands out, say so in your final message and just record the governance event.
"""

EOL_REPORT_PROMPT = """Run the end-of-life (EOL) report standing task.

1. Query EOL status (query_eol_status) across the fleet.
2. Identify anything already past its EOL date, or within 90 days of reaching it.
3. If there is anything past or approaching EOL, file ONE GitLab issue (runner_create_gitlab_issue) titled "EOL report: <today's date> - <N> host(s) need attention", listing host/OS/EOL date/days past EOL for each.
4. Record a governance event (runner_record_governance_event, event_type="eol-report-run") with a payload summarizing hosts checked and whether you filed an issue.
5. If nothing is past or approaching EOL, do NOT file an issue - just record the governance event and say so in your final message.
"""

STANDING_TASKS = [
    ("drift-triage", DRIFT_TRIAGE_PROMPT),
    ("self-review", SELF_REVIEW_PROMPT),
    ("eol-report", EOL_REPORT_PROMPT),
]

__all__ = [
    "STANDING_TASKS",
    "DRIFT_TRIAGE_PROMPT",
    "SELF_REVIEW_PROMPT",
    "EOL_REPORT_PROMPT",
]
