.PHONY: help install sync db-up db-down db-init db-migrate db-status db-verify db-reset \
        test test-int lint fmt typecheck check run-web shell clean

help:
	@echo "Stockscan dev targets:"
	@echo "  install       Install uv (if missing) and sync dependencies"
	@echo "  sync          uv sync (install/update from pyproject.toml)"
	@echo "  db-up         Start TimescaleDB container"
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
	@echo "  run-web       Run the FastAPI dev server"
	@echo "  shell         Open a Python shell with app context loaded"
	@echo "  clean         Remove caches and build artifacts"

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

run-web:
	uv run uvicorn stockscan.web.app:app --reload --host 0.0.0.0 --port 8000

shell:
	uv run python -i -c "from stockscan import config, db; print('stockscan dev shell ready')"

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info htmlcov .coverage
