# CI runner / deploy target / image store / secrets host separation

- **Date:** 2026-07-17
- **Status:** PROPOSED (pending ops decision — requires a second host)
- **Scope:** Ops/infra proposal only. **No code or infra change is made by this document.**
- **Author:** A. Operator (owner@example.com)

## Context

Today a **single host, `deploy-host-01`** (the DEV deployment — there is no production
environment yet, relabeled 2026-07-17), fills four roles at once:

| Role | How it is realized today |
|---|---|
| **CI runner** | `.gitlab-ci.yml` `default: tags: [deploy-host-01]` routes *every* job to this one runner. |
| **Deploy target** | The `deploy` job uses the runner's own `/var/run/docker.sock` — no SSH, no remote context — so "deploy" is just `docker compose up` on the runner host. |
| **Image store** | No external registry. `build` produces `infra-brain:$CI_COMMIT_SHA` / `:latest` in the host's Docker daemon; `deploy` consumes it with `--no-build` (no push/pull). |
| **Secrets store** | `INFRA_BRAIN_ENV` (GitLab File variable) is materialized to `/opt/infra-brain/.env` (chmod 600) on this host; `collector-secrets.env` lives host-only at `/opt/infra-brain/collector-secrets.env`; the Postgres data volume is also on this host. Bitwarden (`BWS_ACCESS_TOKEN` → `secrets.py::load_secrets_into_env`) is the upstream source but the materialized `.env` sits here. |

### Current single-host risk (blast radius)

One box is runner **and** deploy target **and** image store **and** secrets holder **and**
data volume. Consequences:

- **Compromise blast radius:** an attacker (or a malicious/buggy MR pipeline job that
  reaches the shared Docker socket) that lands on the runner simultaneously gets the
  deployed app, the image store, the live `.env`/collector secrets, and the Postgres
  volume. There is no trust boundary between "build untrusted branch code" and "hold
  production-equivalent secrets and data."
- **Availability blast radius:** host loss takes out CI, deploys, image history, and the
  running app/data together — no independent recovery path.
- **No promotion path:** because images never leave the host daemon, there is no artifact
  that a *separate* deploy target could pull, which blocks standing up any second
  environment.

## Target end-state (brief)

Four responsibilities, on separated infrastructure with explicit boundaries between them:

1. **Dedicated CI runner** — builds/tests only. Holds no long-lived secrets and is not the
   deploy target. Pushes images to the registry; never runs the app stack.
2. **Deploy / host target** — runs the compose stack, owns the Postgres data volume and the
   materialized `/opt/infra-brain/.env` + `collector-secrets.env`. Pulls images from the
   registry. Not reachable via a shared Docker socket from CI job code.
3. **Container image registry** — the GitLab Container Registry (free, already in the same
   GitLab project) becomes the image handoff between build and deploy, replacing the shared
   host daemon. Images move by authenticated `push`/`pull`.
4. **Secrets store** — Bitwarden remains the single upstream source of truth. The deploy
   host runs the Bitwarden bootstrap directly (`BWS_ACCESS_TOKEN` on the host), shrinking
   the `INFRA_BRAIN_ENV` CI File variable to only what is needed to boot. CI jobs stop
   carrying the full production-equivalent `.env`.

## Migration steps (ordered, each low-risk / reversible)

Each step is independently shippable and can be rolled back by reverting the one
`.gitlab-ci.yml` / compose / secrets change it introduces.

**Step 1 — Introduce the registry (no second host yet, fully reversible).**
- *CI change:* add `docker login` + `docker push` of `$IMAGE`/`$IMAGE_LATEST` to the GitLab
  Container Registry in the `build` stage, tagging with the registry path
  (`$CI_REGISTRY_IMAGE:$CI_COMMIT_SHA`). Keep the existing local tags too, so `deploy`
  still works off the shared daemon unchanged.
- *Compose/secrets:* none.
- *Reversibility:* drop the push lines — build is byte-for-byte the old behavior.

**Step 2 — Provision the second host and register a second runner (additive).**
- *Ops:* stand up the new box. Decision point for the maintainer: **which role the new box
  takes** — recommended is *new box = clean deploy target*, keeping `deploy-host-01` as the
  runner (smaller change: the runner keeps its build tooling; only the deploy destination
  moves). Register a GitLab runner tagged e.g. `deploy-target` (or `runner` on the new box,
  the mirror choice).
- *CI change:* none yet (new runner sits idle until Step 3 targets its tag).
- *Reversibility:* decommission the new runner; nothing references it.

**Step 3 — Point deploy at the remote host, pulling from the registry.**
- *CI change:* the `deploy` job stops using the local Docker socket and instead targets the
  deploy host — either by running on a runner *on* that host, or via a `docker context` /
  `DOCKER_HOST=ssh://…` remote connection using an SSH key delivered as a CI variable.
  Replace `up -d --no-build` with a registry `pull` + `up -d` (compose resolves images from
  `$CI_REGISTRY_IMAGE`). The build stage no longer needs to leave images in a shared daemon.
- *Compose:* `docker-compose.deploy.yml` `image:` refs point at the registry path; the
  `/opt/infra-brain/.env` and `/opt/infra-brain/repos` bind mounts now live on the deploy
  host (already host-pathed, so this is a host-move, not a compose rewrite).
- *Reversibility:* revert the deploy job to the socket/`--no-build` path; the Step-1 local
  tags still exist on `deploy-host-01`.

**Step 4 — Move secrets off CI onto the deploy host.**
- *Secrets change:* put `BWS_ACCESS_TOKEN` on the deploy host and let the stack pull the
  bulk of its config from Bitwarden at boot (`load_secrets_into_env` already does
  `setdefault`, so host-provided values win and Bitwarden fills the rest). Shrink the
  `INFRA_BRAIN_ENV` CI File variable to the minimal bootstrap set. `collector-secrets.env`
  is already host-only — it simply relocates to the new deploy host.
- *CI change:* smaller `INFRA_BRAIN_ENV`; the `cp $INFRA_BRAIN_ENV .env` step shrinks
  accordingly.
- *Reversibility:* restore the full `INFRA_BRAIN_ENV` variable.

**Step 5 — Split the tag routing / remove the shared-socket assumption.**
- *CI change:* remove `default: tags: [deploy-host-01]` and tag jobs per stage — `test`/`build`
  on the runner, `deploy` on the `deploy-target` runner. Drop the `DOCKER_HOST:
  unix:///var/run/docker.sock` reliance for cross-host deploys.
- *Compose/secrets:* none.
- *Reversibility:* restore the global `default: tags:` block.

## Consequences

- After Step 3 the trust boundary exists: untrusted MR/branch build code no longer shares a
  Docker socket with the live secrets and data volume.
- After Step 5 host loss is survivable independently — a lost runner does not take the app
  down, and a lost deploy host does not lose CI or image history (images are in the
  registry).
- Cost: one additional host to provision and maintain, plus registry storage/GC. This is the
  ops decision that gates the whole sequence and is **out of scope for a code change**.

## Decision needed

Approve provisioning a second host and choosing its role (deploy target vs. runner). Until
then the single-host collapse stays **deferred** — this document is the proposal, not
an implementation.
