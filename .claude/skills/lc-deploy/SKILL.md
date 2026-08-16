---
name: lc-deploy
description: Use when deploying a LangChain or LangGraph application — local dev server, LangGraph Platform, LangServe, Docker, or Kubernetes. Covers langgraph.json config, langgraph dev, RemoteGraph client, PostgresSaver, Dockerfile, docker-compose, and K8s manifests. Use when the user asks where/how to run their graph in production, needs persistence beyond MemorySaver, wants HITL or scheduling, or is containerizing a LangGraph app.
---

# lc:deploy — LangGraph / LangChain Deployment

## Overview

Scaffold the right deployment for LangGraph and LangChain applications.
Progressive complexity: local dev → LangGraph Platform → Docker → Kubernetes.

---

## Trigger Phrases

- "deploy my graph"
- "run in production"
- "how do I host this"
- "langgraph dev"
- "containerize my agent"
- "LangGraph Platform"
- `/deploy`

---

## Discovery Questions (ask before scaffolding)

Ask all four in one message. Do not scaffold until answered.

```
1. TARGET ENVIRONMENT
   (a) Local dev / exploration only
   (b) Staging / team demo
   (c) Production

2. EXPECTED SCALE
   (a) Single user / personal
   (b) Small team (< 20 users)
   (c) Enterprise / public-facing

3. HUMAN-IN-THE-LOOP required?
   (a) No — fully automated
   (b) Yes — need interrupts, approvals, or async wait

4. PERSISTENCE required?
   (a) No — ephemeral runs only
   (b) Yes — need conversation history, long-running threads
```

**Routing table:**

| Target | Scale | HITL | Persistence | Recommended path |
|--------|-------|------|-------------|-----------------|
| Local | Any | No | No | Local dev server |
| Staging/Prod | Small | Yes | Yes | LangGraph Platform |
| Staging/Prod | Small | No | Yes | Docker + PostgresSaver |
| Prod | Enterprise | Any | Yes | Kubernetes |
| Any | Any | No | No | LangServe (legacy) |

---

## Option 1 — Local Development Server

**When to use:** Building and debugging a graph locally; connect Graph Studio v2 for visual inspection.

### Prerequisites

```bash
pip install --upgrade "langgraph-cli[inmem]"   # requires Python >= 3.11
```

### `langgraph.json` (required config file)

```json
{
  "dependencies": ["."],
  "graphs": {
    "my_agent": "./src/agent.py:graph"
  },
  "env": ".env"
}
```

**Fields:**
- `dependencies` — list of packages or local paths to install
- `graphs` — map of graph name → `module_path:variable`
- `env` — path to `.env` file for secrets

**Multi-graph example:**
```json
{
  "dependencies": ["langchain_openai", "."],
  "graphs": {
    "chat_agent":   "./src/chat.py:graph",
    "research_agent": "./src/research.py:graph"
  },
  "env": ".env"
}
```

### Starting the server

```bash
langgraph dev                  # hot-reload, in-memory state
langgraph dev --port 2024      # custom port (default: 2024)
langgraph dev --no-browser     # skip auto-opening Studio
```

Server starts three URLs:
- `http://localhost:2024` — LangGraph API
- `https://smith.langchain.com/studio/?baseUrl=http://localhost:2024` — Graph Studio v2
- `http://localhost:2024/docs` — OpenAPI docs

### Connecting from Python client

```python
from langgraph_sdk import get_client

client = get_client(url="http://localhost:2024")

# List available graphs
assistants = await client.assistants.search()

# Create a thread and run
thread = await client.threads.create()
async for chunk in client.runs.stream(
    thread["thread_id"],
    "my_agent",
    input={"messages": [{"role": "user", "content": "Hello"}]},
    stream_mode="values",
):
    print(chunk)
```

### `.env` for local dev

```bash
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=ls__...
LANGCHAIN_PROJECT=my-agent-dev
```

---

## Option 2 — LangGraph Platform (Recommended for Production)

**When to use:** Need managed hosting with built-in persistence, memory, HITL,
scheduling, webhooks, and horizontal scaling — without managing infrastructure.

**Advantages over self-hosting:**
- Built-in PostgreSQL-backed checkpointing (no setup needed)
- Native human-in-the-loop support (`interrupt_before`/`interrupt_after`)
- Long-term memory store across threads
- Cron-based and webhook-triggered runs
- LangSmith observability pre-wired

### Prerequisites

1. LangSmith account at [smith.langchain.com](https://smith.langchain.com)
2. `langgraph-cli` installed: `pip install langgraph-cli`
3. Same `langgraph.json` as local dev (above)

### Build for deployment

```bash
langgraph build -t my-agent:latest    # builds Docker image
```

### Deploy (LangSmith UI)

1. Go to smith.langchain.com → Deployments → New Deployment
2. Connect your GitHub repo or upload the built image
3. Set environment variables in the UI
4. Deploy — Platform provides the API endpoint URL

### RemoteGraph — connect from Python

```python
from langgraph.pregel.remote import RemoteGraph

# Replace with your deployment URL from LangSmith
DEPLOYMENT_URL = "https://my-agent-xyz.us.langgraph.app"

remote_graph = RemoteGraph(
    "my_agent",
    url=DEPLOYMENT_URL,
    api_key="ls__...",   # LangSmith API key
)

# Use exactly like a local compiled graph
result = await remote_graph.ainvoke(
    {"messages": [{"role": "user", "content": "Hello"}]},
    config={"configurable": {"thread_id": "thread-123"}},
)
```

### Key API endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/runs` | POST | Start a new run |
| `/runs/stream` | POST | Streaming run |
| `/threads` | POST/GET | Create / list threads |
| `/threads/{id}/state` | GET | Read thread state |
| `/assistants` | GET | List deployed graphs |
| `/store/items` | GET/PUT | Long-term memory store |
| `/runs/crons` | POST | Schedule recurring runs |

### Human-in-the-loop (HITL) example

```python
# In your graph definition — interrupt before a sensitive node
graph = builder.compile(interrupt_before=["human_review"])

# In client code — resume after human approves
thread = await client.threads.create()

# Start run (will pause at interrupt)
run = await client.runs.create(
    thread["thread_id"],
    "my_agent",
    input={"messages": [{"role": "user", "content": "Transfer $5000"}]},
)

# Human reviews thread state...
state = await client.threads.get_state(thread["thread_id"])
print(state["values"])  # show pending action to human

# Human approves — resume with None input (continues from checkpoint)
async for chunk in client.runs.stream(
    thread["thread_id"],
    "my_agent",
    input=None,   # resume
    stream_mode="values",
):
    print(chunk)
```

---

## Option 3 — LangServe (Legacy)

**When to use:** Wrapping a simple LCEL chain or Runnable as a REST API;
existing deployments not yet migrated; no need for persistence or HITL.

**Use LangGraph Platform instead if:** you need threads, memory, interrupts,
scheduling, or complex graph state.

### Setup

```bash
pip install langserve[all] fastapi uvicorn
```

### `server.py`

```python
"""LangServe FastAPI server — wraps any LangChain Runnable."""
from fastapi import FastAPI
from langserve import add_routes
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

app = FastAPI(
    title="My LangChain API",
    version="1.0",
    description="LangServe deployment",
)

# ── define your chain ──────────────────────────────────────────────────────────
prompt = ChatPromptTemplate.from_template("Answer the question: {question}")
llm    = ChatOpenAI(model="gpt-4o-mini", temperature=0)
chain  = prompt | llm | StrOutputParser()

# ── mount at a path ────────────────────────────────────────────────────────────
add_routes(
    app,
    chain,
    path="/chat",
    # Optional: enable playground UI
    # playground_type="default",
)

# ── health check ───────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

### Generated endpoints

| Path | Description |
|------|-------------|
| `POST /chat/invoke` | Single invocation |
| `POST /chat/stream` | SSE streaming |
| `POST /chat/batch` | Batch invocation |
| `GET  /chat/playground` | Interactive UI |
| `GET  /chat/input_schema` | Input JSON schema |
| `GET  /chat/output_schema` | Output JSON schema |

### Start server

```bash
uvicorn server:app --reload --port 8000
```

### Migration path to LangGraph Platform

1. Wrap your chain as a LangGraph node
2. Create `langgraph.json` pointing to the graph
3. Replace `add_routes()` calls with `langgraph dev` locally
4. Deploy via LangGraph Platform instead of uvicorn

---

## Option 4 — Docker Deployment

**When to use:** Self-hosted production; need portable container; using
a cloud provider without LangGraph Platform support.

### `Dockerfile`

```dockerfile
# ── Stage 1: build ─────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: runtime ────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY . .

# Non-root user for security
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
```

### `requirements.txt`

```
langchain>=0.3
langchain-openai>=0.3
langgraph>=0.2
langserve[all]>=0.3
fastapi>=0.115
uvicorn[standard]>=0.32
psycopg[binary]>=3.1          # for PostgresSaver
langgraph-checkpoint-postgres>=2.0
python-dotenv>=1.0
pydantic-settings>=2.0
```

### `docker-compose.yml` (with PostgreSQL checkpointing)

```yaml
version: "3.9"

services:
  app:
    build: .
    ports:
      - "8000:8000"
    environment:
      - OPENAI_API_KEY=${OPENAI_API_KEY}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - LANGCHAIN_TRACING_V2=${LANGCHAIN_TRACING_V2:-true}
      - LANGCHAIN_API_KEY=${LANGCHAIN_API_KEY}
      - DATABASE_URL=postgresql://langgraph:langgraph@postgres:5432/langgraph
    depends_on:
      postgres:
        condition: service_healthy
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c",
             "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"]
      interval: 30s
      timeout: 10s
      retries: 3

  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: langgraph
      POSTGRES_PASSWORD: langgraph
      POSTGRES_DB: langgraph
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U langgraph"]
      interval: 10s
      timeout: 5s
      retries: 5
    restart: unless-stopped

volumes:
  pgdata:
```

### PostgresSaver in your graph

```python
import os
from langgraph.checkpoint.postgres import PostgresSaver

DB_URI = os.environ["DATABASE_URL"]

def create_graph():
    with PostgresSaver.from_conn_string(DB_URI) as checkpointer:
        checkpointer.setup()   # run once to create tables
        builder = StateGraph(MyState)
        # ... add nodes and edges ...
        return builder.compile(checkpointer=checkpointer)
```

### Build and run

```bash
# Build image
docker build -t my-langgraph-app:latest .

# Run with docker compose
docker compose up -d

# Tail logs
docker compose logs -f app

# Run DB migration (first time)
docker compose exec app python -c "
from langgraph.checkpoint.postgres import PostgresSaver
import os
with PostgresSaver.from_conn_string(os.environ['DATABASE_URL']) as cp:
    cp.setup()
print('DB ready')
"
```

---

## Option 5 — Kubernetes Deployment

**When to use:** Enterprise scale; need horizontal autoscaling, managed ingress,
GitOps workflows, or multi-region deployment.

### File layout

```
k8s/
  namespace.yaml
  configmap.yaml
  secret.yaml
  deployment.yaml
  service.yaml
  hpa.yaml
  ingress.yaml
  postgres/
    statefulset.yaml
    service.yaml
    pvc.yaml
```

### `namespace.yaml`

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: langgraph
```

### `configmap.yaml`

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: langgraph-config
  namespace: langgraph
data:
  LANGCHAIN_TRACING_V2: "true"
  LANGCHAIN_PROJECT: "production"
  DATABASE_URL: "postgresql://langgraph:$(POSTGRES_PASSWORD)@postgres-svc:5432/langgraph"
```

### `secret.yaml` (apply manually — never commit with real values)

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: langgraph-secrets
  namespace: langgraph
type: Opaque
stringData:
  OPENAI_API_KEY: "sk-..."
  ANTHROPIC_API_KEY: "sk-ant-..."
  LANGCHAIN_API_KEY: "ls__..."
  POSTGRES_PASSWORD: "change-me-in-production"
```

### `deployment.yaml`

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: langgraph-app
  namespace: langgraph
  labels:
    app: langgraph-app
spec:
  replicas: 2
  selector:
    matchLabels:
      app: langgraph-app
  template:
    metadata:
      labels:
        app: langgraph-app
    spec:
      containers:
        - name: app
          image: my-langgraph-app:latest   # replace with your registry
          ports:
            - containerPort: 8000
          envFrom:
            - configMapRef:
                name: langgraph-config
            - secretRef:
                name: langgraph-secrets
          resources:
            requests:
              cpu: "250m"
              memory: "512Mi"
            limits:
              cpu: "1000m"
              memory: "2Gi"
          readinessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 10
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 30
            periodSeconds: 30
          lifecycle:
            preStop:
              exec:
                command: ["sleep", "5"]   # graceful drain
      terminationGracePeriodSeconds: 30
```

### `service.yaml`

```yaml
apiVersion: v1
kind: Service
metadata:
  name: langgraph-svc
  namespace: langgraph
spec:
  selector:
    app: langgraph-app
  ports:
    - protocol: TCP
      port: 80
      targetPort: 8000
```

### `hpa.yaml` (horizontal pod autoscaling)

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: langgraph-hpa
  namespace: langgraph
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: langgraph-app
  minReplicas: 2
  maxReplicas: 20
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
    - type: Resource
      resource:
        name: memory
        target:
          type: Utilization
          averageUtilization: 80
```

### `ingress.yaml` (nginx-ingress)

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: langgraph-ingress
  namespace: langgraph
  annotations:
    nginx.ingress.kubernetes.io/rewrite-target: /
    cert-manager.io/cluster-issuer: "letsencrypt-prod"
spec:
  ingressClassName: nginx
  tls:
    - hosts:
        - my-agent.example.com
      secretName: langgraph-tls
  rules:
    - host: my-agent.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: langgraph-svc
                port:
                  number: 80
```

### `postgres/statefulset.yaml`

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: postgres
  namespace: langgraph
spec:
  serviceName: postgres-svc
  replicas: 1
  selector:
    matchLabels:
      app: postgres
  template:
    metadata:
      labels:
        app: postgres
    spec:
      containers:
        - name: postgres
          image: postgres:16-alpine
          env:
            - name: POSTGRES_USER
              value: langgraph
            - name: POSTGRES_DB
              value: langgraph
            - name: POSTGRES_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: langgraph-secrets
                  key: POSTGRES_PASSWORD
            - name: PGDATA
              value: /var/lib/postgresql/data/pgdata
          ports:
            - containerPort: 5432
          volumeMounts:
            - name: pgdata
              mountPath: /var/lib/postgresql/data
          readinessProbe:
            exec:
              command: ["pg_isready", "-U", "langgraph"]
            periodSeconds: 10
  volumeClaimTemplates:
    - metadata:
        name: pgdata
      spec:
        accessModes: ["ReadWriteOnce"]
        resources:
          requests:
            storage: 20Gi
---
apiVersion: v1
kind: Service
metadata:
  name: postgres-svc
  namespace: langgraph
spec:
  selector:
    app: postgres
  ports:
    - port: 5432
      targetPort: 5432
  clusterIP: None   # headless service for StatefulSet
```

### Deploy to cluster

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/postgres/
kubectl apply -f k8s/
# Wait for pods
kubectl rollout status deployment/langgraph-app -n langgraph
# Tail logs
kubectl logs -f deployment/langgraph-app -n langgraph
```

---

## Environment Configuration

### Development — python-dotenv

```bash
# .env (never commit)
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=ls__...
LANGCHAIN_PROJECT=my-agent-dev
DATABASE_URL=postgresql://langgraph:langgraph@localhost:5432/langgraph
```

```python
from dotenv import load_dotenv
load_dotenv()  # call at top of entry point
```

### `settings.py` — Pydantic Settings for all environments

```python
"""settings.py — single source of truth for all configuration."""
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache

class Settings(BaseSettings):
    # LLM keys
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    # LangSmith
    langchain_tracing_v2: bool = True
    langchain_api_key: str = ""
    langchain_project: str = "default"

    # Database
    database_url: str = "postgresql://langgraph:langgraph@localhost:5432/langgraph"

    # App
    app_env: str = "development"   # development | staging | production
    log_level: str = "INFO"
    rate_limit_per_minute: int = 60

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

@lru_cache
def get_settings() -> Settings:
    return Settings()

# Usage anywhere in the app:
# from settings import get_settings
# cfg = get_settings()
# print(cfg.database_url)
```

### AWS Secrets Manager integration

```python
import json
import boto3
from settings import Settings

def load_from_aws_secrets(secret_name: str, region: str = "us-east-1") -> Settings:
    """Overlay AWS Secrets Manager values on top of env/defaults."""
    client = boto3.client("secretsmanager", region_name=region)
    raw = client.get_secret_value(SecretId=secret_name)["SecretString"]
    secrets = json.loads(raw)
    # Override env with secrets (Pydantic Settings accepts _env_overrides)
    return Settings(**secrets)
```

### GCP Secret Manager integration

```python
from google.cloud import secretmanager
import json
from settings import Settings

def load_from_gcp_secrets(project_id: str, secret_id: str) -> Settings:
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    secrets = json.loads(response.payload.data.decode("utf-8"))
    return Settings(**secrets)
```

---

## Production Checklist

**Persistence:**
- [ ] Replace `MemorySaver` with `PostgresSaver` — `MemorySaver` is process-local and loses all state on restart
- [ ] Run `checkpointer.setup()` once on first deploy to create checkpoint tables

**Observability:**
- [ ] `LANGCHAIN_TRACING_V2=true` and `LANGCHAIN_API_KEY` set in all environments
- [ ] Set a descriptive `LANGCHAIN_PROJECT` per environment (dev/staging/prod)

**Resilience:**
- [ ] Add `tenacity` retry wrapper around LLM calls
- [ ] Set `max_retries` on ChatOpenAI / ChatAnthropic constructors
- [ ] Handle `RateLimitError` with exponential backoff

**Secrets:**
- [ ] Never commit `.env` — add to `.gitignore`
- [ ] Use K8s Secrets or cloud secret manager in production (not ConfigMap for sensitive values)
- [ ] Rotate API keys quarterly

**Health and shutdown:**
- [ ] `/health` endpoint returns 200 when DB is reachable
- [ ] Graceful shutdown: drain in-flight requests before SIGTERM (`preStop` sleep + `terminationGracePeriodSeconds`)

**Rate limiting (FastAPI/LangServe):**
```python
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

@app.post("/chat/invoke")
@limiter.limit("30/minute")
async def invoke(request: Request, body: dict):
    ...
```

**Retry wrapper:**
```python
from tenacity import retry, stop_after_attempt, wait_exponential
from langchain_core.runnables import RunnableLambda

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def invoke_with_retry(chain, input_):
    return chain.invoke(input_)
```

---

## Quick Reference

| Need | Command / File |
|------|---------------|
| Start local dev | `langgraph dev` |
| Config file | `langgraph.json` |
| Build for Platform | `langgraph build -t name:tag` |
| Connect remotely | `RemoteGraph("graph_name", url=..., api_key=...)` |
| PostgreSQL checkpointing | `PostgresSaver.from_conn_string(DB_URI)` |
| Container | `docker build . && docker compose up -d` |
| K8s deploy | `kubectl apply -f k8s/` |
| Scale K8s | `kubectl scale deployment langgraph-app --replicas=5 -n langgraph` |

---

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Using `MemorySaver` in production | Switch to `PostgresSaver` — state is lost on pod restart with MemorySaver |
| Committing `.env` to git | Add `.env` to `.gitignore`; use secrets manager |
| Not calling `checkpointer.setup()` | Run once on first deploy to create DB tables |
| Single replica with stateful in-memory | PostgresSaver + multiple replicas is the correct pattern |
| LangServe for a LangGraph app with threads | Use LangGraph Platform; LangServe has no thread/state API |
| Hardcoding secrets in `configmap.yaml` | Secrets go in `secret.yaml` (type: Opaque), not ConfigMap |
| No health check on K8s | Add `readinessProbe` and `livenessProbe` — K8s will send traffic to broken pods without them |

---

## Section 6 — FastAPI Integration (Production Streaming API)

**When to use:** You need a custom REST/SSE API in front of a LangGraph graph —
e.g. to add proprietary auth, custom request shaping, Prometheus metrics, or
multi-graph routing — without the full LangGraph Platform managed service.

### Dependencies

```
fastapi>=0.115
uvicorn[standard]>=0.32
sse-starlette>=2.1          # EventSourceResponse
prometheus-client>=0.21
psycopg[binary]>=3.1
langgraph-checkpoint-postgres>=2.0
pydantic>=2.7
httpx>=0.27                 # for /health/ready LLM probe
python-dotenv>=1.0
```

### Complete `src/api.py`

```python
"""
src/api.py — Production FastAPI wrapper for a LangGraph graph.

Design principles:
  - One compiled graph shared across all requests (created in lifespan).
  - /invoke   → synchronous JSON response (short tasks, < 30 s).
  - /stream   → Server-Sent Events via astream_events v2 (long tasks, token streaming).
  - /health/* → liveness + readiness probes for K8s / load-balancers.
  - /metrics  → Prometheus exposition format.
  - CORS pre-configured for browser clients.
  - Background tasks for fire-and-forget side-effects (logging, webhooks).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Optional
from uuid import uuid4

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from langgraph.checkpoint.postgres import PostgresSaver
from src.graph import build_graph   # your StateGraph builder function
from src.settings import get_settings

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

REQUEST_COUNT = Counter(
    "langgraph_requests_total",
    "Total API requests",
    ["endpoint", "status_code"],
)
REQUEST_LATENCY = Histogram(
    "langgraph_request_duration_seconds",
    "Request latency in seconds",
    ["endpoint"],
    buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
)
STREAM_TOKENS = Counter(
    "langgraph_stream_tokens_total",
    "Total tokens streamed via SSE",
)

# ---------------------------------------------------------------------------
# Lifespan — graph is built ONCE and shared across all requests
# ---------------------------------------------------------------------------

_graph = None          # module-level ref; populated in lifespan
_checkpointer = None   # kept open for the process lifetime


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Startup: open DB connection pool, run checkpointer migrations, compile graph.
    Shutdown: close the connection pool cleanly.

    Using asynccontextmanager means FastAPI calls the code before `yield` on
    startup and the code after `yield` on shutdown — no separate on_event hooks.
    """
    global _graph, _checkpointer

    cfg = get_settings()

    # Open a persistent async connection to Postgres
    _checkpointer = PostgresSaver.from_conn_string(cfg.database_url)
    _checkpointer.setup()          # idempotent: creates tables if not present

    # Compile the graph with the shared checkpointer
    _graph = build_graph(checkpointer=_checkpointer)

    print("Graph compiled and ready.")
    yield

    # Teardown — release DB connection pool
    await _checkpointer.aclose()
    print("Checkpointer closed.")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    cfg = get_settings()

    app = FastAPI(
        title="LangGraph API",
        version="1.0.0",
        description="Production LangGraph REST + SSE API",
        lifespan=lifespan,
    )

    # ── CORS ──────────────────────────────────────────────────────────────────
    # Restrict origins in production; use ["*"] only for fully public APIs.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,      # e.g. ["https://app.example.com"]
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    # ── Prometheus middleware ─────────────────────────────────────────────────
    @app.middleware("http")
    async def prometheus_middleware(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start
        path = request.url.path
        REQUEST_COUNT.labels(endpoint=path, status_code=response.status_code).inc()
        REQUEST_LATENCY.labels(endpoint=path).observe(duration)
        return response

    return app


app = create_app()

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class InvokeRequest(BaseModel):
    """Body for POST /invoke."""
    input: dict[str, Any] = Field(..., description="Graph input matching your StateGraph input schema.")
    thread_id: Optional[str] = Field(
        default=None,
        description="Existing thread to resume. Omit to create a new thread.",
    )
    config: Optional[dict[str, Any]] = Field(
        default=None,
        description="Extra configurable fields passed through to the graph.",
    )


class InvokeResponse(BaseModel):
    thread_id: str
    output: dict[str, Any]
    run_id: str


class StreamRequest(BaseModel):
    """Body for POST /stream."""
    input: dict[str, Any]
    thread_id: Optional[str] = None
    config: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Fire-and-forget background task
# ---------------------------------------------------------------------------

async def _record_run_async(thread_id: str, run_id: str, duration_ms: float) -> None:
    """
    Example fire-and-forget task: log run metadata to an external analytics sink.

    Add any side-effect work here — webhook calls, audit logging, billing events.
    Runs in the background after the response is already sent to the client.
    Failures here do NOT affect the HTTP response.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                os.getenv("ANALYTICS_WEBHOOK_URL", "http://localhost:9999/noop"),
                json={"thread_id": thread_id, "run_id": run_id, "duration_ms": duration_ms},
            )
    except Exception:
        pass   # analytics failure must never bubble up to the user


# ---------------------------------------------------------------------------
# /invoke — synchronous response
# ---------------------------------------------------------------------------

@app.post("/invoke", response_model=InvokeResponse, summary="Invoke graph, return full output")
async def invoke(body: InvokeRequest, background_tasks: BackgroundTasks) -> InvokeResponse:
    """
    Run the graph to completion and return the final state as JSON.

    Best for: short tasks (tool calls, classifications, quick Q&A) where the
    client can wait for the full result. Use /stream for long generation tasks.
    """
    if _graph is None:
        raise HTTPException(status_code=503, detail="Graph not initialised")

    thread_id = body.thread_id or str(uuid4())
    run_id = str(uuid4())

    configurable: dict[str, Any] = {"thread_id": thread_id, "run_id": run_id}
    if body.config:
        configurable.update(body.config)

    start = time.perf_counter()

    try:
        result = await _graph.ainvoke(
            body.input,
            config={"configurable": configurable},
        )
    except Exception as exc:
        REQUEST_COUNT.labels(endpoint="/invoke", status_code=500).inc()
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    duration_ms = (time.perf_counter() - start) * 1000

    # Schedule fire-and-forget analytics — response already on its way
    background_tasks.add_task(_record_run_async, thread_id, run_id, duration_ms)

    return InvokeResponse(thread_id=thread_id, output=result, run_id=run_id)


# ---------------------------------------------------------------------------
# /stream — Server-Sent Events (SSE) using astream_events v2
# ---------------------------------------------------------------------------

@app.post("/stream", summary="Stream graph events via SSE")
async def stream(body: StreamRequest, request: Request):
    """
    Stream graph execution as Server-Sent Events.

    Event types emitted to the client:
      - data: {"type": "token",  "content": "<text>"}   — LLM token
      - data: {"type": "node",   "name": "<node>"}       — node entry
      - data: {"type": "output", "value": {...}}          — final state
      - data: {"type": "error",  "detail": "<msg>"}      — error
      - data: {"type": "done"}                            — stream complete

    The client disconnects cleanly via the EventSourceResponse close mechanism.
    """
    if _graph is None:
        raise HTTPException(status_code=503, detail="Graph not initialised")

    thread_id = body.thread_id or str(uuid4())
    configurable: dict[str, Any] = {"thread_id": thread_id}
    if body.config:
        configurable.update(body.config)

    async def event_generator() -> AsyncGenerator[dict, None]:
        try:
            async for event in _graph.astream_events(
                body.input,
                config={"configurable": configurable},
                version="v2",           # astream_events v2 is the stable API
            ):
                # Check if client disconnected mid-stream
                if await request.is_disconnected():
                    break

                kind = event.get("event", "")
                name = event.get("name", "")

                # ── LLM token chunk ──────────────────────────────────────────
                if kind == "on_chat_model_stream":
                    chunk = event["data"].get("chunk")
                    content = getattr(chunk, "content", "") if chunk else ""
                    if content:
                        STREAM_TOKENS.inc()
                        yield {
                            "data": json.dumps({"type": "token", "content": content}),
                        }

                # ── Node entry ───────────────────────────────────────────────
                elif kind == "on_chain_start" and name:
                    yield {
                        "data": json.dumps({"type": "node", "name": name}),
                    }

                # ── Final graph output ───────────────────────────────────────
                elif kind == "on_chain_end" and name == _graph.name:
                    output = event["data"].get("output", {})
                    yield {
                        "data": json.dumps({"type": "output", "value": output}),
                    }

        except asyncio.CancelledError:
            pass   # clean client disconnect
        except Exception as exc:
            yield {"data": json.dumps({"type": "error", "detail": str(exc)})}
        finally:
            yield {"data": json.dumps({"type": "done"})}

    return EventSourceResponse(event_generator())


# ---------------------------------------------------------------------------
# /health/live — Kubernetes liveness probe
# ---------------------------------------------------------------------------

@app.get(
    "/health/live",
    status_code=status.HTTP_200_OK,
    summary="Liveness probe — always 200 if process is alive",
    tags=["Health"],
)
async def health_live() -> JSONResponse:
    """
    Returns 200 as long as the process is running.
    K8s restarts the pod only if this returns non-2xx or times out.
    Never add heavy logic here — it must be near-instant.
    """
    return JSONResponse({"status": "alive"})


# ---------------------------------------------------------------------------
# /health/ready — Kubernetes readiness probe
# ---------------------------------------------------------------------------

@app.get(
    "/health/ready",
    status_code=status.HTTP_200_OK,
    summary="Readiness probe — checks DB + LLM reachability",
    tags=["Health"],
)
async def health_ready() -> JSONResponse:
    """
    Returns 200 only when ALL dependencies are reachable:
      1. PostgreSQL checkpointer connection is open.
      2. LLM API responds (lightweight HEAD/models probe).

    K8s stops routing traffic to the pod if this returns non-2xx.
    The /health/live probe remains separate so a temporarily degraded
    dependency does not cause an unnecessary pod restart.
    """
    checks: dict[str, str] = {}
    overall_ok = True
    cfg = get_settings()

    # ── 1. Database check ────────────────────────────────────────────────────
    try:
        if _checkpointer is None:
            raise RuntimeError("checkpointer not initialised")
        # Lightweight round-trip: list zero checkpoints
        async with _checkpointer.conn.cursor() as cur:
            await cur.execute("SELECT 1")
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {exc}"
        overall_ok = False

    # ── 2. LLM API reachability (OpenAI models endpoint, 3 s timeout) ────────
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {cfg.openai_api_key}"},
            )
            if resp.status_code not in (200, 401):
                # 401 is fine — it means the network path is open
                raise RuntimeError(f"HTTP {resp.status_code}")
        checks["llm_api"] = "ok"
    except Exception as exc:
        checks["llm_api"] = f"error: {exc}"
        overall_ok = False

    http_status = status.HTTP_200_OK if overall_ok else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse({"status": "ready" if overall_ok else "degraded", "checks": checks}, status_code=http_status)


# ---------------------------------------------------------------------------
# /metrics — Prometheus scrape endpoint
# ---------------------------------------------------------------------------

@app.get(
    "/metrics",
    summary="Prometheus metrics",
    tags=["Observability"],
    include_in_schema=False,   # hide from public OpenAPI docs
)
async def metrics():
    """
    Exposes all Prometheus metrics registered in this process.
    Scrape with:  prometheus.yml → scrape_configs → targets: ['my-host:8000']
    """
    from fastapi.responses import Response
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "src.api:app",
        host="0.0.0.0",
        port=8000,
        workers=1,          # use 1 worker; lifespan runs once per process
        log_level="info",
        access_log=True,
    )
```

### Key design notes

| Pattern | Rationale |
|---------|-----------|
| `asynccontextmanager` lifespan | Graph is compiled once at startup; `PostgresSaver` connection pool is shared across requests; no per-request DB connect/disconnect overhead. |
| `astream_events(..., version="v2")` | v2 is the stable, recommended API. It emits fine-grained events for every model call, tool call, and chain step. |
| `EventSourceResponse` from `sse-starlette` | Handles SSE framing, keep-alive pings, and clean client-disconnect detection. |
| `await request.is_disconnected()` | Cancels the async generator early when the browser tab closes, avoiding wasted LLM token spend. |
| `/health/live` vs `/health/ready` | Liveness = is the process alive? Readiness = can it serve traffic? Keep them separate so a slow DB does not restart healthy pods. |
| Background tasks for side-effects | `BackgroundTasks.add_task()` runs after the response is sent, so analytics/webhook latency never adds to API latency. |
| `workers=1` in uvicorn | The lifespan context (and therefore the compiled graph + DB pool) lives in one process. For multi-worker scale, put the graph behind a process-safe connection pool or use LangGraph Platform. |

### `settings.py` additions for Section 6

```python
# Add these fields to the Settings class in settings.py:
cors_origins: list[str] = ["http://localhost:3000"]
openai_api_key: str = ""
analytics_webhook_url: str = ""
```

### Running

```bash
uvicorn src.api:app --reload --port 8000          # dev
uvicorn src.api:app --host 0.0.0.0 --port 8000    # prod (single worker)
```

---

## Section 7 — JWT Authentication for LangGraph Platform

**When to use:** Deploying on LangGraph Platform (managed or self-hosted) and
need to restrict access by user identity, tenant, or role — replacing the
`"auth": "<path>"` placeholder in `langgraph.json` with a real implementation.

### How LangGraph Platform auth works

```
Browser / Client
      │
      │  Authorization: Bearer <JWT>
      ▼
LangGraph Platform API gateway
      │
      │  calls @auth.authenticate(token) → Identity
      │  calls @auth.on(action, resource) → allow / deny
      ▼
Graph execution (config["configurable"] includes user_id, tenant_id, roles)
```

### `langgraph.json` — wire in the auth module

```json
{
  "dependencies": ["."],
  "graphs": {
    "my_agent": "./src/agent.py:graph"
  },
  "auth": {
    "path": "./src/auth.py:auth",
    "disable_studio_auth": false
  },
  "env": ".env"
}
```

`disable_studio_auth: false` means Graph Studio also requires a valid JWT.
Set to `true` during local development if you want Studio to bypass auth.

### Dependencies

```
python-jose[cryptography]>=3.3    # JWT decode + JWKS support
httpx>=0.27                        # async JWKS fetch
cachetools>=5.3                    # TTL cache for JWKS
langgraph-sdk>=0.1                 # Auth, on decorators
```

### Complete `src/auth.py`

```python
"""
src/auth.py — JWT authentication + authorization for LangGraph Platform.

Supports:
  - RS256 JWTs from any OIDC provider (Okta, Azure AD, Auth0, Cognito, custom).
  - HS256 JWTs for simpler setups (shared secret, no JWKS).
  - Claims extraction: user_id, tenant_id, roles.
  - Thread-level scoping: users can only access their own threads.
  - Role-based authorization on graph operations.

Replace JWKS_URI and AUDIENCE with your IdP values.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
from cachetools import TTLCache
from jose import JWTError, jwt
from jose.backends import RSAKey

from langgraph_sdk import Auth

# ---------------------------------------------------------------------------
# Configuration — override via environment variables
# ---------------------------------------------------------------------------

# For RS256 (OIDC providers): set JWKS_URI to your provider's JWKS endpoint.
#   Okta:     https://<domain>/oauth2/default/v1/keys
#   Azure AD: https://login.microsoftonline.com/<tenant>/discovery/v2.0/keys
#   Auth0:    https://<domain>/.well-known/jwks.json
#   AWS Cognito: https://cognito-idp.<region>.amazonaws.com/<pool_id>/.well-known/jwks.json
JWKS_URI: str = os.environ.get("JWKS_URI", "")

# For HS256 (shared secret): set JWT_SECRET instead of JWKS_URI.
JWT_SECRET: str = os.environ.get("JWT_SECRET", "")

JWT_ALGORITHM: str = os.environ.get("JWT_ALGORITHM", "RS256")   # RS256 | HS256
JWT_AUDIENCE: str  = os.environ.get("JWT_AUDIENCE", "")         # e.g. "https://api.example.com"
JWT_ISSUER: str    = os.environ.get("JWT_ISSUER", "")           # e.g. "https://myapp.okta.com"

# Claim names — customise for your IdP's token structure
CLAIM_USER_ID:   str = os.environ.get("CLAIM_USER_ID",   "sub")
CLAIM_TENANT_ID: str = os.environ.get("CLAIM_TENANT_ID", "tenant_id")
CLAIM_ROLES:     str = os.environ.get("CLAIM_ROLES",     "roles")

# ---------------------------------------------------------------------------
# JWKS cache — fetched once, refreshed every 10 minutes
# ---------------------------------------------------------------------------

_jwks_cache: TTLCache = TTLCache(maxsize=1, ttl=600)   # 10-minute TTL


async def _get_jwks() -> dict[str, Any]:
    """Fetch JWKS from the IdP with a TTL cache to avoid hammering the endpoint."""
    cached = _jwks_cache.get("jwks")
    if cached:
        return cached
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(JWKS_URI)
        resp.raise_for_status()
        data = resp.json()
    _jwks_cache["jwks"] = data
    return data


async def _decode_rs256(token: str) -> dict[str, Any]:
    """Validate an RS256 JWT against the IdP's public keys."""
    jwks = await _get_jwks()
    # python-jose handles kid matching automatically when passed the full JWKS
    return jwt.decode(
        token,
        jwks,
        algorithms=["RS256"],
        audience=JWT_AUDIENCE or None,
        issuer=JWT_ISSUER or None,
    )


def _decode_hs256(token: str) -> dict[str, Any]:
    """Validate an HS256 JWT against a shared secret."""
    if not JWT_SECRET:
        raise ValueError("JWT_SECRET env var required for HS256")
    return jwt.decode(
        token,
        JWT_SECRET,
        algorithms=["HS256"],
        audience=JWT_AUDIENCE or None,
        issuer=JWT_ISSUER or None,
    )


# ---------------------------------------------------------------------------
# LangGraph Auth object
# ---------------------------------------------------------------------------

auth = Auth()


# ---------------------------------------------------------------------------
# @auth.authenticate — called on every request before the graph runs
# ---------------------------------------------------------------------------

@auth.authenticate
async def authenticate(authorization: str | None) -> Auth.types.MinimalUserDict:
    """
    Validate the bearer JWT and return an identity dict.

    LangGraph Platform passes the raw Authorization header value here.
    Return a MinimalUserDict with at minimum {"identity": "<user_id>"}.
    Any extra keys are forwarded to config["configurable"] automatically.

    Raise Auth.exceptions.HTTPException to reject the request.
    """
    if not authorization:
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="Missing Authorization header",
        )

    # Strip "Bearer " prefix
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="Authorization header must be 'Bearer <token>'",
        )

    try:
        if JWT_ALGORITHM == "HS256":
            claims = _decode_hs256(token)
        else:
            claims = await _decode_rs256(token)
    except JWTError as exc:
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail=f"Invalid token: {exc}",
        ) from exc

    # ── Extract standard claims ──────────────────────────────────────────────
    user_id: str = claims.get(CLAIM_USER_ID, "")
    if not user_id:
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail=f"Token missing required claim '{CLAIM_USER_ID}'",
        )

    tenant_id: str = claims.get(CLAIM_TENANT_ID, "default")
    roles: list[str] = claims.get(CLAIM_ROLES, [])

    # ── Return identity — everything here lands in config["configurable"] ────
    # The "identity" key is required by LangGraph; all other keys are optional
    # but available to your graph via config["configurable"]["tenant_id"] etc.
    return {
        "identity": user_id,          # required — used as the authenticated user
        "tenant_id": tenant_id,        # your custom claim
        "roles": roles,                # your custom claim
        "display_name": claims.get("name", user_id),
        "email": claims.get("email", ""),
    }


# ---------------------------------------------------------------------------
# @auth.on — authorization rules applied after authentication
# ---------------------------------------------------------------------------

@auth.on
async def add_owner_filter(
    ctx: Auth.types.AuthContext,
    value: dict[str, Any],
) -> dict[str, Any]:
    """
    Global authorization handler: inject an owner filter on all resources
    so users can only see threads/runs that belong to their identity.

    ctx.user     — the MinimalUserDict returned by @auth.authenticate
    ctx.action   — the operation: "create", "read", "update", "delete", "search"
    ctx.resource — the resource type: "threads", "runs", "assistants", "store"
    value        — the request body / filter dict (mutate and return)

    Return the (optionally mutated) value dict to allow, or raise
    Auth.exceptions.HTTPException to deny.
    """
    user_id = ctx.user["identity"]
    filters = {"owner": user_id}
    # Merge owner filter so all queries are automatically scoped to this user
    value.update(filters)
    return value


@auth.on.threads.create
async def on_thread_create(
    ctx: Auth.types.AuthContext,
    value: dict[str, Any],
) -> dict[str, Any]:
    """
    On thread creation: stamp the thread with the owner's identity and tenant.

    This runs after the global @auth.on handler. Use resource-specific
    handlers when you need finer control than the global rule.
    """
    user_id   = ctx.user["identity"]
    tenant_id = ctx.user.get("tenant_id", "default")

    # Metadata written to the thread record in Postgres
    value.setdefault("metadata", {})
    value["metadata"]["owner"]     = user_id
    value["metadata"]["tenant_id"] = tenant_id
    return value


@auth.on.threads.read
async def on_thread_read(
    ctx: Auth.types.AuthContext,
    value: dict[str, Any],
) -> dict[str, Any]:
    """Ensure users can only read threads they own."""
    user_id = ctx.user["identity"]
    roles   = ctx.user.get("roles", [])

    # Admins can read any thread
    if "admin" in roles:
        return value

    # Regular users: enforce owner filter (global handler already set it,
    # but this is an explicit secondary guard)
    if value.get("owner") != user_id:
        raise Auth.exceptions.HTTPException(
            status_code=403,
            detail="Access denied to this thread",
        )
    return value


@auth.on.assistants
async def on_assistants(
    ctx: Auth.types.AuthContext,
    value: dict[str, Any],
) -> dict[str, Any]:
    """
    Assistants (deployed graphs) are read-only for regular users.
    Only admins and deployers may create or update them.
    """
    roles  = ctx.user.get("roles", [])
    action = ctx.action

    if action in ("create", "update", "delete") and not any(
        r in roles for r in ("admin", "deployer")
    ):
        raise Auth.exceptions.HTTPException(
            status_code=403,
            detail=f"Role 'admin' or 'deployer' required to {action} assistants",
        )
    return value


# ---------------------------------------------------------------------------
# Graph-side: reading auth context from config
# ---------------------------------------------------------------------------
# In your graph nodes, access the authenticated identity via config:
#
#   from langchain_core.runnables import RunnableConfig
#
#   def my_node(state: MyState, config: RunnableConfig) -> MyState:
#       configurable = config.get("configurable", {})
#       user_id   = configurable.get("identity")
#       tenant_id = configurable.get("tenant_id", "default")
#       roles     = configurable.get("roles", [])
#       # Use user_id to scope DB queries, personalise responses, etc.
#       ...
```

### OIDC provider configuration reference

| Provider | `JWKS_URI` | `JWT_AUDIENCE` | `JWT_ISSUER` | Notes |
|----------|-----------|----------------|--------------|-------|
| Okta | `https://<domain>/oauth2/default/v1/keys` | API audience from Okta app | `https://<domain>/oauth2/default` | Use custom auth server for role claims |
| Azure AD | `https://login.microsoftonline.com/<tenant>/discovery/v2.0/keys` | `api://<client-id>` | `https://login.microsoftonline.com/<tenant>/v2.0` | Add roles as app roles in manifest |
| Auth0 | `https://<domain>/.well-known/jwks.json` | API identifier from Auth0 | `https://<domain>/` | Use Actions to add `roles` claim |
| AWS Cognito | `https://cognito-idp.<region>.amazonaws.com/<pool>/.well-known/jwks.json` | `<pool-client-id>` | `https://cognito-idp.<region>.amazonaws.com/<pool>` | Groups map to roles |
| Custom / HS256 | Not needed | Set or leave blank | Set or leave blank | Set `JWT_SECRET` + `JWT_ALGORITHM=HS256` |

### Environment variables for `src/auth.py`

```bash
# .env additions for Section 7
JWT_ALGORITHM=RS256
JWKS_URI=https://myapp.okta.com/oauth2/default/v1/keys
JWT_AUDIENCE=https://api.example.com
JWT_ISSUER=https://myapp.okta.com/oauth2/default
CLAIM_USER_ID=sub
CLAIM_TENANT_ID=tenant_id
CLAIM_ROLES=roles
```

### Thread-id scoping pattern (graph node example)

```python
# src/agent.py — using auth context inside a node
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, MessagesState


def chat_node(state: MessagesState, config: RunnableConfig) -> MessagesState:
    """Node that personalises responses and scopes DB queries per user."""
    c = config.get("configurable", {})
    user_id   = c.get("identity", "anonymous")
    tenant_id = c.get("tenant_id", "default")
    roles     = c.get("roles", [])

    # Example: restrict tool use to premium users
    allowed_tools = []
    if "premium" in roles or "admin" in roles:
        allowed_tools = ["web_search", "code_exec"]

    # The thread_id is also in configurable — automatically scoped by auth.on
    thread_id = c.get("thread_id")

    # ... invoke LLM, call tools, etc.
    return state


builder = StateGraph(MessagesState)
builder.add_node("chat", chat_node)
builder.set_entry_point("chat")
builder.set_finish_point("chat")

graph = builder.compile()   # checkpointer injected in lifespan (Section 6)
```

### Common auth mistakes

| Mistake | Fix |
|---------|-----|
| `"auth": "./src/auth.py"` (string, not object) | Use `"auth": {"path": "./src/auth.py:auth"}` — the value must be an object with a `path` key |
| Putting secrets in `langgraph.json` | All secrets go in `.env` / secrets manager; `langgraph.json` only holds the module path |
| Using `disable_studio_auth: true` in production | Only disable in local dev; always require tokens in staging and prod |
| Forgetting to cache JWKS | Fetching JWKS on every request DDoS's your IdP and adds 100-300 ms latency; the `TTLCache` pattern above fetches at most once per 10 min |
| Not scoping threads by owner | Without the `@auth.on` owner filter, any authenticated user can read any thread — always set `owner` metadata on create |
| Ignoring `tenant_id` in multi-tenant apps | Scope all DB queries and memory store keys by `tenant_id`, not just `user_id` |

---

## Section 8 — K8s Health Probes (Liveness / Readiness / Startup)

**When to use:** Any Kubernetes deployment (Option 5). Replace the single
`/health` probe in the base `deployment.yaml` with a proper three-probe split.

### Why the split matters

| Probe | Question answered | Failure action |
|-------|-------------------|----------------|
| `livenessProbe` | Is the process still alive? | K8s **restarts** the pod |
| `readinessProbe` | Can the pod serve traffic right now? | K8s **removes pod from Service endpoints** (no restart) |
| `startupProbe` | Has the app finished starting up? | K8s **delays** liveness/readiness until startup passes |

Putting dependency checks (DB, LLM API) only in the readiness probe means a
temporarily degraded downstream never triggers an unnecessary restart loop.

---

### FastAPI — `src/health.py`

Create this as a standalone router so it can be imported into `api.py` or any
other FastAPI application without coupling to the full `api.py` module.

```python
"""
src/health.py — Split liveness and readiness probe endpoints.

Mount in api.py:
    from src.health import health_router
    app.include_router(health_router)
"""

from __future__ import annotations

import threading
from typing import Any

import httpx
from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from src.settings import get_settings

health_router = APIRouter(tags=["Health"])

# Module-level reference injected by the lifespan in api.py
# e.g.  from src.health import set_checkpointer; set_checkpointer(cp)
_checkpointer = None
_thread_pool: threading.ThreadPoolExecutor | None = None


def set_checkpointer(cp) -> None:
    """Called from api.py lifespan after the checkpointer is opened."""
    global _checkpointer
    _checkpointer = cp


def set_thread_pool(pool: threading.ThreadPoolExecutor) -> None:
    """Called from api.py lifespan after the thread pool is created."""
    global _thread_pool
    _thread_pool = pool


# ---------------------------------------------------------------------------
# /health/live — liveness probe
# ---------------------------------------------------------------------------

@health_router.get(
    "/health/live",
    status_code=status.HTTP_200_OK,
    summary="Liveness probe — always 200 unless process is dead",
)
async def health_live() -> JSONResponse:
    """
    K8s calls this to decide whether to RESTART the pod.

    Rule: return 200 immediately. Do NOT check databases, caches, or LLM APIs
    here — a slow dependency would cause needless restarts.
    Only add logic here if you need to detect an unrecoverable internal
    deadlock (e.g. a poisoned global state that can only be fixed by restart).
    """
    return JSONResponse({"status": "alive"})


# ---------------------------------------------------------------------------
# /health/ready — readiness probe
# ---------------------------------------------------------------------------

@health_router.get(
    "/health/ready",
    summary="Readiness probe — checks DB + LLM API + thread pool",
)
async def health_ready() -> JSONResponse:
    """
    K8s calls this to decide whether to ROUTE traffic to the pod.

    Returns 200 (ready) when all checks pass.
    Returns 503 (degraded) when any check fails — pod is removed from the
    Service load-balancer until it recovers, with NO restart.

    Checks performed:
      1. PostgreSQL — lightweight SELECT 1.
      2. LLM API reachability — HEAD to the models endpoint (3 s timeout).
      3. Thread pool — ensure the executor is alive and not saturated.
    """
    checks: dict[str, str] = {}
    overall_ok = True
    cfg = get_settings()

    # ── 1. Database ─────────────────────────────────────────────────────────
    try:
        if _checkpointer is None:
            raise RuntimeError("checkpointer not initialised")
        # Use the checkpointer's underlying psycopg connection pool
        async with _checkpointer.conn.cursor() as cur:
            await cur.execute("SELECT 1")
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {exc}"
        overall_ok = False

    # ── 2. LLM API reachability ──────────────────────────────────────────────
    # We probe the /v1/models listing endpoint with a 3-second timeout.
    # HTTP 401 is acceptable — it proves the network path is open.
    # Anything else (connection refused, 5xx, timeout) marks the pod degraded.
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {cfg.openai_api_key}"},
            )
        if resp.status_code not in (200, 401):
            raise RuntimeError(f"unexpected HTTP {resp.status_code}")
        checks["llm_api"] = "ok"
    except Exception as exc:
        checks["llm_api"] = f"error: {exc}"
        overall_ok = False

    # ── 3. Thread pool ───────────────────────────────────────────────────────
    # Verify the executor is alive.  A saturated pool causes queued requests to
    # pile up; removing the pod from rotation buys it time to drain.
    try:
        if _thread_pool is None:
            raise RuntimeError("thread pool not initialised")
        # _threads is an internal CPython attribute; fall back gracefully
        active = len(getattr(_thread_pool, "_threads", set()))
        max_workers = getattr(_thread_pool, "_max_workers", -1)
        if max_workers > 0 and active >= max_workers:
            raise RuntimeError(f"pool saturated ({active}/{max_workers} threads)")
        checks["thread_pool"] = f"ok ({active} active)"
    except Exception as exc:
        checks["thread_pool"] = f"error: {exc}"
        overall_ok = False

    http_status = status.HTTP_200_OK if overall_ok else status.HTTP_503_SERVICE_UNAVAILABLE
    body = {
        "status": "ready" if overall_ok else "degraded",
        "checks": checks,
    }
    return JSONResponse(body, status_code=http_status)
```

### Wire into `api.py`

```python
# Add to imports in src/api.py
from src.health import health_router, set_checkpointer, set_thread_pool

# Add to create_app():
app.include_router(health_router)

# Add to lifespan(), after checkpointer is opened:
set_checkpointer(_checkpointer)

# If you create a ThreadPoolExecutor in lifespan for CPU-bound work:
import concurrent.futures
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)
set_thread_pool(_pool)
```

---

### Updated `k8s/deployment.yaml` — three-probe split

Replace the single probe block in Option 5 with this:

```yaml
          # ── Startup probe ─────────────────────────────────────────────────
          # Gives the container up to 3 min (30 × 6 s) to finish loading
          # models or running migrations before liveness/readiness kick in.
          # Once startupProbe succeeds, it is never checked again.
          startupProbe:
            httpGet:
              path: /health/live
              port: 8000
            initialDelaySeconds: 5
            periodSeconds: 6
            failureThreshold: 30     # 30 × 6 s = 3-minute budget
            successThreshold: 1

          # ── Liveness probe ────────────────────────────────────────────────
          # Restarts pod only if the process itself is unresponsive.
          # Never checks external dependencies.
          livenessProbe:
            httpGet:
              path: /health/live
              port: 8000
            initialDelaySeconds: 10
            periodSeconds: 15
            timeoutSeconds: 5
            failureThreshold: 3      # 3 consecutive failures → restart
            successThreshold: 1

          # ── Readiness probe ───────────────────────────────────────────────
          # Removes pod from load-balancer if DB or LLM API is degraded.
          # Checked every 10 s; pod is added back automatically on recovery.
          readinessProbe:
            httpGet:
              path: /health/ready
              port: 8000
            initialDelaySeconds: 5
            periodSeconds: 10
            timeoutSeconds: 5
            failureThreshold: 3
            successThreshold: 1
```

### Probe decision tree

```
Pod starts
  │
  └─► startupProbe /health/live
        passes?
          YES → enable liveness + readiness
          NO (after failureThreshold) → restart pod

Running:
  ├─► livenessProbe  /health/live  every 15 s
  │     fails?  → restart pod
  └─► readinessProbe /health/ready every 10 s
        fails?  → remove from Service (no restart)
        recovers? → add back to Service
```

### Probe configuration reference

| Field | Liveness | Readiness | Notes |
|-------|----------|-----------|-------|
| `path` | `/health/live` | `/health/ready` | Never share the same path |
| `initialDelaySeconds` | `10` | `5` | Readiness can start sooner than liveness |
| `periodSeconds` | `15` | `10` | Readiness checks more frequently |
| `timeoutSeconds` | `5` | `5` | Must be less than `periodSeconds` |
| `failureThreshold` | `3` | `3` | 3 failures = ~45 s / ~30 s before action |
| `successThreshold` | `1` | `1` | Only `1` is valid for liveness |

### Production checklist additions

- [ ] Liveness probe hits `/health/live` (no dependency checks)
- [ ] Readiness probe hits `/health/ready` (DB + LLM + thread pool)
- [ ] `startupProbe` covers slow startup (model loading, DB migrations)
- [ ] `timeoutSeconds` < `periodSeconds` on all probes
- [ ] `failureThreshold` x `periodSeconds` gives enough time for transient errors to recover before action

---

## Section 9 — Structured Logging with structlog

**When to use:** Any production deployment. Replace all `print()` calls in
scaffolded graph code with structured JSON logs that include correlation IDs,
LangGraph node context, and per-request user metadata.

### Why structured logging

- JSON logs are natively parseable by CloudWatch, Datadog, GCP Cloud Logging, Loki, Splunk.
- Every log entry carries `run_id`, `thread_id`, `user_id` — grep by ID instead of timestamp.
- `log.bind(node=node_name)` stamps all logs inside a node without repeating fields.
- Dev mode uses colored `ConsoleRenderer`; prod uses `JSONRenderer` — same code, different env var.

### Install

```
structlog>=24.1
python-dotenv>=1.0   # already in requirements.txt
```

---

### `src/logging_config.py` — one-time configuration

```python
"""
src/logging_config.py — Configure structlog for the whole application.

Call configure_logging() once at application startup (before importing
any module that logs).  After that, every module does:

    import structlog
    log = structlog.get_logger()

and gets a pre-configured logger with all processors applied.
"""

from __future__ import annotations

import logging
import os
import sys

import structlog
from structlog.types import EventDict, WrappedLogger


# ---------------------------------------------------------------------------
# Custom processor: add standard service fields to every log entry
# ---------------------------------------------------------------------------

_SERVICE   = os.environ.get("SERVICE_NAME", "langgraph-app")
_VERSION   = os.environ.get("APP_VERSION",  "0.0.0")
_ENV       = os.environ.get("APP_ENV",       "development")


def add_service_fields(
    logger: WrappedLogger,
    method: str,
    event_dict: EventDict,
) -> EventDict:
    """
    Inject service-level metadata into every log entry.

    These fields are stamped at the processor level so they appear in every
    JSON line without the caller needing to pass them explicitly.
    """
    event_dict.setdefault("service", _SERVICE)
    event_dict.setdefault("version", _VERSION)
    event_dict.setdefault("env",     _ENV)
    return event_dict


# ---------------------------------------------------------------------------
# Shared processor chain
# ---------------------------------------------------------------------------

SHARED_PROCESSORS: list = [
    structlog.contextvars.merge_contextvars,        # pull in bound context vars
    structlog.stdlib.add_log_level,                 # level="info" etc.
    structlog.stdlib.add_logger_name,               # logger="src.graph" etc.
    structlog.processors.TimeStamper(fmt="iso"),    # timestamp in ISO-8601
    add_service_fields,                             # service/version/env
    structlog.processors.StackInfoRenderer(),       # include stack_info if passed
    structlog.processors.format_exc_info,           # format exc_info tracebacks
]


def configure_logging(log_level: str = "INFO") -> None:
    """
    Configure structlog.  Call once at the top of your entrypoint.

    Renderer selection:
      APP_ENV=production  → JSONRenderer (machine-readable, one line per event)
      anything else       → ConsoleRenderer (colored, human-readable)

    Stdlib `logging` is also wired up so third-party libraries (httpx, uvicorn,
    sqlalchemy) emit their records through the same structlog pipeline.
    """
    env = os.environ.get("APP_ENV", "development")
    is_prod = env == "production"

    level = getattr(logging, log_level.upper(), logging.INFO)

    # ── stdlib → structlog bridge ────────────────────────────────────────────
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    # ── renderer ─────────────────────────────────────────────────────────────
    if is_prod:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=SHARED_PROCESSORS + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Wire stdlib log records through the structlog pipeline
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=SHARED_PROCESSORS,
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(level)
```

---

### `src/graph.py` — LangGraph node logging pattern

```python
"""
src/graph.py — LangGraph graph with structured logging in every node.

Pattern:
  1. At graph entry, extract run_id from RunnableConfig and bind it.
  2. Each node binds its own name: log.bind(node="node_name").
  3. Use log.info / log.warning / log.error — never print().
  4. structlog.contextvars.bind_contextvars() makes bound keys thread-safe
     and propagates them to any child logger created in the same async task.
"""

from __future__ import annotations

import structlog
import structlog.contextvars

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph, MessagesState
from langgraph.checkpoint.base import BaseCheckpointSaver

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Helper: bind run-level context from RunnableConfig
# ---------------------------------------------------------------------------

def bind_run_context(config: RunnableConfig) -> structlog.BoundLogger:
    """
    Extract LangGraph run metadata from config and bind to the context-var
    store so that all log calls in the current async task automatically
    include run_id and thread_id.

    Call once at graph entry (entry node or __call__), not per-node.

    Returns a logger already bound with the run context.
    """
    c = config.get("configurable", {})
    run_id    = str(c.get("run_id",    "unknown"))
    thread_id = str(c.get("thread_id", "unknown"))
    user_id   = str(c.get("identity",  "anonymous"))  # populated by Section 7 auth

    # bind_contextvars is async-task-safe: values are stored in a contextvar,
    # not a global dict, so concurrent requests don't bleed into each other.
    structlog.contextvars.bind_contextvars(
        run_id=run_id,
        thread_id=thread_id,
        user_id=user_id,
    )

    return log.bind(run_id=run_id, thread_id=thread_id, user_id=user_id)


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------

def entry_node(state: MessagesState, config: RunnableConfig) -> MessagesState:
    """
    Entry point: bind run context, then log node_start.
    Every subsequent node inherits run_id / thread_id via contextvars.
    """
    run_log = bind_run_context(config)
    node_log = run_log.bind(node="entry")
    node_log.info("node_start", input_keys=list(state.keys()))

    # ... actual node logic ...

    node_log.info("node_end", output_keys=list(state.keys()))
    return state


def llm_node(state: MessagesState, config: RunnableConfig) -> MessagesState:
    """
    Intermediate node: bind only the node name.
    run_id / thread_id arrive automatically from contextvars.
    """
    node_log = log.bind(node="llm_call")
    node_log.info("node_start")

    messages = state.get("messages", [])
    node_log.debug("messages_snapshot", count=len(messages))

    try:
        # ... call LLM ...
        node_log.info("llm_response_received", token_count=0)   # fill from response
    except Exception as exc:
        node_log.error("llm_call_failed", error=str(exc), exc_info=True)
        raise

    node_log.info("node_end")
    return state


def tool_node(state: MessagesState, config: RunnableConfig) -> MessagesState:
    node_log = log.bind(node="tool_execution")
    node_log.info("node_start")

    # ... execute tools ...

    node_log.info("node_end")
    return state


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def build_graph(checkpointer: BaseCheckpointSaver | None = None):
    builder = StateGraph(MessagesState)
    builder.add_node("entry",   entry_node)
    builder.add_node("llm",     llm_node)
    builder.add_node("tools",   tool_node)

    builder.set_entry_point("entry")
    builder.add_edge("entry", "llm")
    builder.add_edge("llm",   "tools")
    builder.add_edge("tools", END)

    return builder.compile(checkpointer=checkpointer)
```

---

### FastAPI request-ID middleware — `src/middleware.py`

Each inbound HTTP request gets a UUID stamped into all log lines for the
lifetime of that request, without the caller needing to thread it manually.

```python
"""
src/middleware.py — Request-ID middleware for FastAPI.

Assigns a UUID to every inbound request and binds it to structlog's
context-var store so every log call made during request handling (including
inside LangGraph nodes) automatically includes request_id.

Usage in api.py:
    from src.middleware import RequestIDMiddleware
    app.add_middleware(RequestIDMiddleware)
"""

from __future__ import annotations

import uuid

import structlog.contextvars
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

import structlog

log = structlog.get_logger(__name__)


class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    Assigns a unique request_id UUID to every HTTP request.

    The ID is:
      1. Bound to structlog contextvars — appears in every log line.
      2. Echoed back to the caller in the X-Request-ID response header.
      3. Accepted from the caller via X-Request-ID request header
         (useful for client-side correlation / replay debugging).
    """

    def __init__(self, app: ASGIApp, header_name: str = "X-Request-ID") -> None:
        super().__init__(app)
        self.header_name = header_name

    async def dispatch(self, request: Request, call_next) -> Response:
        # Honour a client-supplied ID (e.g. from a load-balancer trace header),
        # otherwise generate a fresh UUID.
        request_id = request.headers.get(self.header_name) or str(uuid.uuid4())

        # Clear any context left over from a previous request on this task/thread
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        log.info(
            "request_start",
            method=request.method,
            path=request.url.path,
            request_id=request_id,
        )

        response = await call_next(request)

        log.info(
            "request_end",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            request_id=request_id,
        )

        # Return the ID to the caller for client-side log correlation
        response.headers[self.header_name] = request_id
        return response
```

### Wire middleware and logging into `api.py`

```python
# src/api.py — additions for Section 9

# 1. Top of file — import before anything that logs
from src.logging_config import configure_logging
from src.middleware import RequestIDMiddleware

configure_logging(log_level=os.environ.get("LOG_LEVEL", "INFO"))

# 2. In create_app(), add middleware (order matters — RequestID before Prometheus):
app.add_middleware(RequestIDMiddleware)

# 3. Replace the print() calls in lifespan with structured logging:
import structlog as _structlog
_log = _structlog.get_logger(__name__)

# In lifespan — replace  print("Graph compiled and ready.")  with:
_log.info("startup_complete", graph=_graph.name if _graph else None)

# Replace  print("Checkpointer closed.")  with:
_log.info("shutdown_complete")
```

---

### Environment variables for Section 9

```bash
# .env additions for Section 9
APP_ENV=production          # production → JSONRenderer; anything else → ConsoleRenderer
APP_VERSION=1.2.3           # stamped on every log line
SERVICE_NAME=langgraph-app  # stamped on every log line
LOG_LEVEL=INFO              # DEBUG | INFO | WARNING | ERROR
```

---

### Sample log output

**Development (ConsoleRenderer):**
```
2026-06-18T09:12:34.001Z [info     ] request_start   request_id=a1b2c3 method=POST path=/invoke
2026-06-18T09:12:34.005Z [info     ] node_start      request_id=a1b2c3 run_id=r-xyz thread_id=t-abc user_id=u-001 node=entry
2026-06-18T09:12:34.210Z [info     ] llm_response_received  request_id=a1b2c3 run_id=r-xyz node=llm_call token_count=312
2026-06-18T09:12:34.215Z [info     ] request_end     request_id=a1b2c3 method=POST path=/invoke status_code=200
```

**Production (JSONRenderer, one line per event):**
```json
{"timestamp":"2026-06-18T09:12:34.001Z","level":"info","event":"request_start","service":"langgraph-app","version":"1.2.3","env":"production","request_id":"a1b2c3","method":"POST","path":"/invoke"}
{"timestamp":"2026-06-18T09:12:34.005Z","level":"info","event":"node_start","service":"langgraph-app","version":"1.2.3","env":"production","request_id":"a1b2c3","run_id":"r-xyz","thread_id":"t-abc","user_id":"u-001","node":"entry"}
{"timestamp":"2026-06-18T09:12:34.210Z","level":"info","event":"llm_response_received","service":"langgraph-app","version":"1.2.3","env":"production","request_id":"a1b2c3","run_id":"r-xyz","node":"llm_call","token_count":312}
{"timestamp":"2026-06-18T09:12:34.215Z","level":"info","event":"request_end","service":"langgraph-app","version":"1.2.3","env":"production","request_id":"a1b2c3","method":"POST","path":"/invoke","status_code":200}
```

---

### Standard field glossary

| Field | Source | Purpose |
|-------|--------|---------|
| `service` | `SERVICE_NAME` env var | Identify which microservice emitted the line |
| `version` | `APP_VERSION` env var | Correlate bugs to a specific deploy |
| `env` | `APP_ENV` env var | Separate prod/staging/dev in log aggregators |
| `request_id` | `RequestIDMiddleware` | Correlate all lines for one HTTP request |
| `run_id` | `RunnableConfig["configurable"]["run_id"]` | Correlate all lines for one graph run |
| `thread_id` | `RunnableConfig["configurable"]["thread_id"]` | Correlate across resumptions of the same thread |
| `user_id` | `RunnableConfig["configurable"]["identity"]` (Section 7 auth) | Per-user audit trail |
| `node` | `log.bind(node=node_name)` inside each LangGraph node | Pinpoint which node emitted each line |

### Logging production checklist additions

- [ ] `configure_logging()` called before any import that uses `structlog.get_logger()`
- [ ] `RequestIDMiddleware` added before other middleware in `create_app()`
- [ ] `bind_run_context(config)` called in the graph's entry node
- [ ] All `print()` calls replaced with `log.info()` / `log.warning()` / `log.error()`
- [ ] `APP_ENV=production` set in K8s `configmap.yaml` for JSON output
- [ ] Log aggregator (Datadog / CloudWatch / Loki) configured to parse JSON
- [ ] `LOG_LEVEL=WARNING` or higher in production to reduce volume (use `DEBUG` in staging)
