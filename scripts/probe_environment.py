#!/usr/bin/env python3
"""Run the environment readiness probe (P3.2) and report + file gaps.

    python scripts/probe_environment.py [--dry-run]

Probes every CONFIGURED integration (GitLab, Ansible inventory, the active
LLM gateway, container registries) for real reachability and files an
IntegrationProposal for each one that's configured but not actually working.
Never mutates config — see env_probe.py's module docstring.
"""

import argparse
import sys

from infra_brain.env_probe import probe_environment, propose_gaps


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Report gaps without filing proposals"
    )
    args = parser.parse_args()

    results = probe_environment()
    if not results:
        print("No integrations configured to probe.")
        return

    for r in results:
        status = "OK" if r.reachable else "GAP"
        print(f"[{status}] {r.name}: {r.detail}")

    gaps = [r for r in results if not r.reachable]
    if not gaps:
        print(f"\nAll {len(results)} configured integration(s) reachable.")
        return

    if args.dry_run:
        print(f"\nDry-run: would file {len(gaps)} IntegrationProposal(s).")
        return

    created = propose_gaps(results)
    print(f"\nFiled {len(created)} new IntegrationProposal(s): {created}")
    if len(created) < len(gaps):
        print(f"({len(gaps) - len(created)} gap(s) already had a pending proposal.)")


if __name__ == "__main__":
    sys.exit(main())
