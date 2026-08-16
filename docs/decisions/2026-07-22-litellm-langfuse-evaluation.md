# LiteLLM + Langfuse — capability evaluation for a future session

- **Date:** 2026-07-22
- **Status:** DEFERRED — evaluated, not implemented. Revisit in a future session.
- **Scope:** Evaluation only. No infra-brain code or infrastructure changed by this document.
- **Author:** A. Operator (via Claude Code session)

## Context

the maintainer has a personal, already-running LiteLLM + Langfuse + Presidio stack on `fedora-fleet`
(`~/projects/llm-stack`, `~/projects/litellm`, `~/projects/langfuse`). This session evaluated
whether infra-brain should integrate with it or a dedicated instance of the same tools, then
was explicitly redirected to evaluate the tools themselves — observability and guardrails
fit — independent of any deployment/sizing decision. Folded here for pickup later rather
than acted on now.

## Observability — Langfuse: real, low-friction win

infra-brain's existing observability (`audit_log`, `AgentDecisionLog`) is built for
compliance/audit, not for debugging or improving LLM behavior — no trace tree, no
prompt-version management, no dataset/eval tooling, no cost/session dashboards. Langfuse
fills exactly that gap and is complementary, not a replacement for the audit trail.

**Already built and ready to activate:** `src/infra_brain/callbacks/langfuse_handler.py` is
complete (lazy import, memoized handler, failure-tolerant, client-side `redact_pans()`
masking before export). Activation is 4 env vars once a Langfuse instance exists to point
at — `LANGFUSE_ENABLED`, `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` (→
Bitwarden per policy). Zero code work remaining.

**Verdict: worth doing.** The blocker was never capability, it's the same deploy-target
decision as everything below.

## Guardrails — LiteLLM's layer (Presidio + hide-secrets): real gap, not a drop-in

infra-brain's `callbacks/dlp.py` only detects PANs (Luhn+IIN, tuned via TRK-110). Presidio
adds a genuinely different entity class (EMAIL/PERSON/PHONE/SSN) at the prompt level before
the model sees it — complementary in principle.

**But real domain-fit risk:** Presidio's `PERSON` NER has no way to distinguish a person's
name from a hostname or service-account name, and infra-brain's prompts are full of exactly
that content. The existing llm-stack config runs guardrails `default_on: true` and fail-closed
— applied to infra-brain traffic as-is, this risks corrupting content the agent needs to
reason over, or (given Presidio's documented SIGKILL-under-load flakiness on the existing
instance) turning into mysteriously failed sweeps.

**Verdict: the entity-class gap is worth closing eventually, but Presidio off-the-shelf
needs domain tuning first** (an allowlist for infra-shaped tokens, or a false-positive-rate
test against real infra-brain prompt samples) before it could be trusted fail-closed — the
same maturity bar `dlp.py`'s own PAN detector had to clear via TRK-110. LiteLLM's guardrail
hook system is pluggable, so a custom infra-tuned guardrail is a legitimate alternative to
Presidio specifically, if/when this is picked back up.

## Resource sizing — corrected, for whenever deployment is reconsidered

`docker/langfuse/README.md`'s stated "~25 GiB RAM / ~11 vCPU" sizing and its "ClickHouse has
an 8 GiB floor" claim were investigated and found **not justified for infra-brain's actual
workload** — that number traces back to a Langfuse GitHub discussion about a 3-node HA
ClickHouse *cluster* sized for ~1,000 LLM calls/minute, mis-applied as a single-node floor.
Real `docker stats` against the maintainer's actively-used personal instance (single-node, higher
trace volume than infra-brain will produce) showed ClickHouse stable at ~1.05–1.25 GiB.
infra-brain's actual projected volume (even with all three reasoner-tier LLM flags flipped)
is on the order of 50-100 calls/day — several orders of magnitude below what the original
number was sized for.

**Corrected right-sized budget** (dedicated infra-brain instance, not shared with the
personal one): ~11 GiB / ~7.5 cpus in `mem_limit`/`cpus` ceilings, ~5.5–6 GiB actual expected
steady usage. See the full per-service table and reasoning in this session's research
(not yet transcribed into `docker/langfuse/README.md`/`docker-compose.yml` — those files
still carry the debunked 25 GiB figure as of this writing, at `README.md:26-43` and
`docker-compose.yml:62-67`).

**Host-sizing conclusion:** a dedicated big host is not warranted. Langfuse's own documented
whole-stack minimum (4 vCPU / 8–16 GiB) comfortably covers a combined LiteLLM+Langfuse
deployment. `deploy-host-01` itself is not a clean fit — not for CPU/RAM reasons, but because
of its disk posture (74% used, prior 100%-disk incident, and ClickHouse/MinIO volumes grow
unboundedly). A small dedicated VM (4 vCPU / 8–12 GiB / 100 GB disk) was the recommendation,
not the ~25 GiB figure originally implied.

## Open items for whoever picks this back up

1. Decide whether to actually provision a small dedicated VM (or reconsider `deploy-host-01`
   after clearing disk headroom) for a dedicated LiteLLM+Langfuse instance.
2. If Langfuse is deployed: flip the 4 env vars, done — no code work.
3. If LiteLLM guardrails are wanted: budget real tuning time against infra-brain's actual
   prompt shapes before enabling `default_on`/fail-closed — do not just turn Presidio on.
4. Correct `docker/langfuse/README.md`/`docker-compose.yml`'s sizing numbers to the
   corrected budget above (or re-derive fresh if meaningfully more time has passed).
5. Cross-reference [[infra-brain-trk-038-host-separation]] — a new dedicated VM for this
   is a separate decision from TRK-038's CI/deploy-host separation, but both are "do we
   provision new infrastructure" questions worth considering together if/when either comes
   back up, especially given the parallel note about a possible GitLab-instance migration.
