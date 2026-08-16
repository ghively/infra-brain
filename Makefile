.PHONY: lint lint-check test setup doctor

# The Vite+React dashboard-app is built via `cd dashboard-app && npm run build`.

# First-time dev setup. Does NOT touch .env or bring up docker compose — see
# README.md for that (config needs secrets only a human should set). This is
# the "get a working local venv + frontend deps" half.
setup:
	uv sync --extra dev
	cd dashboard-app && npm install
	@echo "Backend: .venv/bin/python -m pytest tests/ -v"
	@echo "Frontend: cd dashboard-app && npx vitest run"
	@echo "Real bring-up (docker compose, secrets): see README.md"

# P3.3: health check. Runs the existing full dev-tooling audit
# (.claude/scripts/dev_status.py, 10 checks — git/venv-aware, run this from a
# checkout) plus the runtime self-check (selfcheck.py's 3 checks, exercised
# here via direct import rather than the live HTTP endpoint since a running
# app/mcp stack is not a `make doctor` precondition). For the live, deployed
# instance's health, use GET /api/dashboard/selfcheck or /health instead.
doctor:
	-python .claude/scripts/dev_status.py
	python -c "from infra_brain.selfcheck import run_selfcheck; import json; r = run_selfcheck(); print(json.dumps(r, indent=2)); import sys; sys.exit(1 if r['overall'] == 'error' else 0)"

lint:
	ruff check --fix src/ tests/
	ruff format src/ tests/

lint-check:
	ruff check src/ tests/
	ruff format --check src/ tests/

test:
	python -m pytest tests/ -v
