# Minimal standalone image for the dead-man prober sidecar (Phase 4 OB-1).
# No infra_brain package installed — this container must never be able to
# import it (see prober.py module docstring: standalone by design, outside
# the app's trust boundary).
FROM python:3.11-slim

WORKDIR /app

# stdlib-only (urllib, json) — no requirements.txt needed.
COPY prober.py /app/prober.py

RUN useradd --no-create-home --shell /usr/sbin/nologin prober
USER prober

ENTRYPOINT ["python", "/app/prober.py"]
