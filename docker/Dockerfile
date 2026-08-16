# Frontend build stage: dashboard-app (Vite+React) -> static2/ bundle served at
# /dashboard2 by FastAPI (src/infra_brain/main.py). Runs before the Python stages
# so its output can be copied into the runtime image below.
FROM node:20-slim AS frontend-builder

WORKDIR /app/dashboard-app

COPY dashboard-app/package.json dashboard-app/package-lock.json ./
RUN npm ci

COPY dashboard-app/ ./
# outDir is ../src/infra_brain/dashboard/static2 (see vite.config.ts) —
# resolves to /app/src/infra_brain/dashboard/static2 from this WORKDIR.
RUN npm run build

# Multi-stage build: builder stage with full dependencies
FROM python:3.11-slim AS builder

WORKDIR /app

# Install system dependencies for ansible-runner, psycopg2, and other requirements
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    ansible \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency manifests + src so hatchling can resolve package metadata.
COPY pyproject.toml uv.lock ./
COPY src/ ./src/
# Reproducible build (F-033): install EXACTLY what uv.lock resolves — never a
# fresh floating ">=" resolution at build time.
#   1. export the locked non-dev dependency set (with hashes) to requirements
#   2. pip-install that pinned, hash-checked set
#   3. install the project itself WITHOUT re-resolving dependencies
RUN pip install --no-cache-dir "uv>=0.5" \
    && uv export --locked --no-dev --no-emit-project --extra all \
         --format requirements-txt -o /tmp/requirements.lock.txt \
    && pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock.txt \
    && pip install --no-cache-dir --no-deps -e .

# Runtime stage: slim image with only runtime dependencies
FROM python:3.11-slim

WORKDIR /app

# Install only runtime system dependencies (no gcc or dev tools)
# nmap is required by NetDiscoveryAgent for Tier 1 (host sweep) and Tier 2
# (service/OS scan). The agent tolerates nmap absence at runtime (clean skip),
# but it must be present for active discovery to function.
# iproute2 provides `ip route`, used by NetDiscoveryAgent._reachable_subnets()
# to enumerate directly-connected subnets. Without it that helper always
# returns [], the Tier 1 sweep target list is empty, and Tier 2 service
# scanning never runs at all — net_discovery_services stays permanently empty
# (TRK-232). Debian trixie's python:3.11-slim base does NOT ship `ip`.
# libcap2-bin provides `setcap`, used immediately below to grant nmap
# CAP_NET_RAW (GitLab #174).
RUN apt-get update && apt-get install -y \
    libpq5 \
    ansible \
    openssh-client \
    nmap \
    iproute2 \
    libcap2-bin \
    && rm -rf /var/lib/apt/lists/*

# GitLab #174: the container runs as non-root (`USER appuser`, below), and
# unprivileged nmap silently degrades `-sn` to a TCP connect sweep — it never
# opens the raw socket needed for ARP, so it can never see layer 2 and
# `mac`/`mac_vendor` come back null for every host. The parse/persist plumbing
# in NetDiscoveryAgent is complete; only the capability is missing. CAP_NET_RAW
# alone is enough for ARP-based discovery and is in Docker's default bounding
# set, so the file capability takes effect without `--privileged` or extra
# `--cap-add`. Do NOT add cap_net_admin here: it is NOT in the default bounding
# set, and an unavailable capability in the effective set makes execve() fail
# outright — nmap would stop running at all rather than run unprivileged.
RUN setcap cap_net_raw+ep /usr/bin/nmap

# Create non-root user
RUN useradd -m -u 1000 appuser

# Copy installed Python packages from builder stage
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY src/ src/
COPY alembic/ alembic/
COPY scripts/ scripts/
# rules/ holds enforcement thresholds (e.g. rules/enforcement/compliance.yml)
# read at runtime by ComplianceAgent._load_thresholds() from /app/rules/...;
# without this COPY the image silently falls back to hardcoded defaults.
COPY rules/ rules/
COPY alembic.ini .
# TRK-122 R1: LocalDocsAgent ingests a curated allowlist of in-repo docs into
# the RAG store at runtime (docs/*.md top-level, docs/decisions|runbooks|specs|
# diagnostics/, and the root-level institutional docs). The image did not ship
# docs/ before, so without these COPYs repo_docs_root would point at nothing.
# The agent's own denylist still excludes docs/agents|audit|superpowers/ and
# CLAUDE.md at read time; copying the tree just makes the allowed files present.
COPY docs/ docs/
COPY AGENTS.md README.md CHANGELOG.md ./

# Built dashboard bundle (see frontend-builder stage above) — served at
# /dashboard2 by src/infra_brain/main.py; without this the mount is skipped
# and / (which redirects to /dashboard2) 404s.
COPY --from=frontend-builder /app/src/infra_brain/dashboard/static2 src/infra_brain/dashboard/static2

# Set Python environment variables
ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

# Switch to non-root user
USER appuser

# Expose port
EXPOSE 8000

# Run the application
CMD ["uvicorn", "infra_brain.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
