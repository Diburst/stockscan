.PHONY: help install sync db-up db-down db-init db-migrate db-status db-verify db-reset \
        test test-int lint fmt typecheck check run-web run-web-local run-mcp-local shell clean \
        css docker-build docker-up docker-down docker-logs

# Local uvicorn bind for run-web; tailscale serve proxies HTTPS:443 to this.
WEB_HOST ?= 127.0.0.1
WEB_PORT ?= 8000

help:
	@echo "Stockscan dev targets:"
	@echo "  install       Install uv (if missing) and sync dependencies"
	@echo "  sync          uv sync (install/update from pyproject.toml)"
	@echo "  db-up         Start TimescaleDB container (DB-only dev path)"
	@echo "  db-down       Stop TimescaleDB container"
	@echo "  db-init       Bootstrap database (extension + apply all migrations)"
	@echo "  db-migrate    Apply pending SQL migrations"
	@echo "  db-status     Show applied + pending migrations"
	@echo "  db-verify     Detect checksum drift between disk and DB"
	@echo "  db-reset      Drop and recreate the database (DANGEROUS)"
	@echo "  test          Run unit tests"
	@echo "  test-int      Run integration tests (requires db-up)"
	@echo "  lint          Run ruff lint"
	@echo "  fmt           Run ruff format"
	@echo "  typecheck     Run mypy"
	@echo "  check         Run lint + typecheck + tests"
	@echo "  run-web       Run web + MCP, exposed over HTTPS on your tailnet (for AI clients)"
	@echo "  run-web-local Run the plain FastAPI dev server (no MCP, no tailscale)"
	@echo "  run-mcp-local Run web + MCP on http://localhost:8000 (no tailscale, no auth)"
	@echo "  css           Rebuild the Tailwind stylesheet (web/static/app.css)"
	@echo "  shell         Open a Python shell with app context loaded"
	@echo "  clean         Remove caches and build artifacts"
	@echo "Full-stack Docker (see DEPLOY.md):"
	@echo "  docker-build  Build the app image (web + scheduler)"
	@echo "  docker-up     Start db + migrate + web + scheduler"
	@echo "  docker-down   Stop the full stack"
	@echo "  docker-logs   Tail logs from all services"

install:
	@command -v uv >/dev/null 2>&1 || (echo "Installing uv..." && curl -LsSf https://astral.sh/uv/install.sh | sh)
	uv sync --all-extras

sync:
	uv sync --all-extras

db-up:
	cd infra && docker compose up -d

db-down:
	cd infra && docker compose down

db-init: db-up
	@echo "Waiting for postgres to accept connections..."
	@until docker exec stockscan-db pg_isready -U stockscan -d stockscan >/dev/null 2>&1; do sleep 1; done
	@echo "Postgres ready."
	bash infra/setup_db.sh
	uv run stockscan db migrate

db-migrate:
	uv run stockscan db migrate

db-status:
	uv run stockscan db status

db-verify:
	uv run stockscan db verify

db-reset:
	@echo "This will DROP and recreate the stockscan database. Press ^C to cancel, Enter to continue."
	@read confirm
	docker exec stockscan-db psql -U stockscan -d postgres -c "DROP DATABASE IF EXISTS stockscan;"
	docker exec stockscan-db psql -U stockscan -d postgres -c "CREATE DATABASE stockscan;"
	bash infra/setup_db.sh
	uv run stockscan db migrate

test:
	uv run pytest -m "not integration"

test-int:
	uv run pytest -m integration

lint:
	uv run ruff check src tests

fmt:
	uv run ruff format src tests

typecheck:
	uv run mypy src

check: lint typecheck test

# Run the web app AND the MCP server, exposed over HTTPS on your tailnet so AI
# clients (e.g. Claude Desktop) can connect. uvicorn binds to localhost; Tailscale
# Serve terminates TLS with a valid *.ts.net cert and proxies to it. The tailnet
# hostname (and thus the OAuth issuer URL) is derived automatically — override by
# exporting STOCKSCAN_MCP_BASE_URL. Set STOCKSCAN_MCP_ALLOW_WRITES=true to also
# expose the mutating tools. For plain local dev without any of this, use
# `make run-web-local`.
run-web:
	@command -v tailscale >/dev/null 2>&1 || { \
	  echo "ERROR: tailscale not found. Install it, or use 'make run-web-local' for plain local dev."; exit 1; }
	@TS_HOST=$$(tailscale status --json 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))" 2>/dev/null); \
	if [ -z "$$TS_HOST" ]; then \
	  echo "ERROR: could not read your tailnet hostname (is tailscaled running and logged in?)."; \
	  echo "       Run 'tailscale up', or use 'make run-web-local'."; exit 1; \
	fi; \
	BASE_URL=$${STOCKSCAN_MCP_BASE_URL:-https://$$TS_HOST}; \
	echo "→ Exposing $(WEB_HOST):$(WEB_PORT) over HTTPS on your tailnet via Tailscale Serve…"; \
	tailscale serve --bg http://$(WEB_HOST):$(WEB_PORT); \
	echo "→ stockscan UI + MCP live at $$BASE_URL"; \
	echo "  Add this URL as a custom connector in Claude:  $$BASE_URL/mcp"; \
	echo "  (view/stop sharing later: 'tailscale serve status' / 'tailscale serve reset')"; \
	STOCKSCAN_MCP_ENABLED=true STOCKSCAN_MCP_BASE_URL=$$BASE_URL \
	  uv run uvicorn stockscan.web.app:app --reload --host $(WEB_HOST) --port $(WEB_PORT)

# Plain dev server: no MCP, no tailscale. Forces MCP off so a leftover
# STOCKSCAN_MCP_ENABLED=true in .env can't pull in the MCP server here.
run-web-local:
	STOCKSCAN_MCP_ENABLED=false uv run uvicorn stockscan.web.app:app --reload --host 0.0.0.0 --port 8000

# Web + MCP for a same-machine setup: no tailscale, no HTTPS, no OAuth. Binds to
# 127.0.0.1 only (localhost is exempt from the OAuth HTTPS rule, and binding to
# loopback keeps the unauthenticated endpoint off your LAN). Connect a client on
# THIS machine to http://localhost:8000/mcp. Set STOCKSCAN_MCP_ALLOW_WRITES=true
# to also expose the write tools. Needs the mcp extra: `uv sync --all-extras`.
run-mcp-local:
	STOCKSCAN_MCP_ENABLED=true STOCKSCAN_MCP_AUTH=none \
	  uv run uvicorn stockscan.web.app:app --reload --host 127.0.0.1 --port 8000

# Rebuild the self-hosted Tailwind stylesheet after template/theme changes.
# Uses npx so no permanent node_modules is needed; the Docker build runs the
# same compile in its assets stage.
css:
	npx --yes tailwindcss@3.4.19 -c tailwind.config.js \
	  -i src/stockscan/web/static/input.css \
	  -o src/stockscan/web/static/app.css --minify

docker-build:
	docker compose build

docker-up:
	docker compose up -d
	@echo "Web UI: http://localhost:8000 — logs: make docker-logs"

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f

shell:
	uv run python -i -c "from stockscan import config, db; print('stockscan dev shell ready')"

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info htmlcov .coverage
