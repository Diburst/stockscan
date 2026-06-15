# stockscan application image — used by both the `web` and `scheduler`
# services in docker-compose.yml.
#
# Build:    docker compose build        (or: make docker-build)
# Stages:
#   assets  — rebuilds the Tailwind stylesheet from the templates so the
#             image never ships a stale app.css (the repo also carries a
#             checked-in build for no-Docker dev use).
#   base    — Python 3.12 + uv-installed dependencies + the app.
#
# The image includes postgresql-client for the scheduler's pg_dump backup
# job, and supercronic (a container-friendly cron) for the scheduler
# service's crontab (infra/crontab).

# ---------------------------------------------------------------- assets --
FROM node:22-slim AS assets
WORKDIR /build
RUN npm install --no-fund --no-audit tailwindcss@3.4.19
COPY tailwind.config.js ./
COPY src/stockscan/web/templates ./src/stockscan/web/templates
COPY src/stockscan/web/static/input.css ./src/stockscan/web/static/input.css
# Python sources also feed the content scan (HTMX snippets built in routes).
COPY src/stockscan/web/routes ./src/stockscan/web/routes
COPY src/stockscan/web/deps.py ./src/stockscan/web/deps.py
RUN ./node_modules/.bin/tailwindcss \
      -c tailwind.config.js \
      -i src/stockscan/web/static/input.css \
      -o /build/app.css --minify

# ---------------------------------------------------------------- base ----
FROM python:3.12-slim AS base

# uv — fast, lockfile-faithful installs.
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /bin/

# postgresql-client: pg_dump/psql for the scheduler's backup job.
# supercronic: cron-for-containers (PID-1-safe, logs to stdout).
ARG SUPERCRONIC_VERSION=v0.2.33
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client curl ca-certificates \
    && curl -fsSL -o /usr/local/bin/supercronic \
       "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-$(dpkg --print-architecture)" \
    && chmod +x /usr/local/bin/supercronic \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Layer-cache the dependency install: lockfile first, source later.
COPY pyproject.toml uv.lock ./
# --no-dev: runtime only. Add --extra ml at build time for meta-labeling:
#   docker compose build --build-arg INSTALL_EXTRAS="--extra ml"
ARG INSTALL_EXTRAS=""
RUN uv sync --frozen --no-dev --no-install-project ${INSTALL_EXTRAS}

# The application.
COPY src ./src
COPY migrations ./migrations
COPY infra/crontab ./infra/crontab
COPY infra/scripts ./infra/scripts
# Docs rendered by the in-app /docs hub.
COPY README.md DESIGN.md USER_STORIES.md TODO.md MIGRATION.md DEPLOY.md ./
COPY market_regime_detection.md signal_scoring_spec.md ./
RUN uv sync --frozen --no-dev --no-editable ${INSTALL_EXTRAS}

# Freshly-built stylesheet wins over the checked-in copy.
COPY --from=assets /build/app.css ./src/stockscan/web/static/app.css
COPY src/stockscan/web/static/htmx.min.js ./src/stockscan/web/static/htmx.min.js

# Non-root user; writable dirs for logs and ML model pickles.
RUN useradd --create-home --uid 1000 stockscan \
    && mkdir -p /app/logs /app/models /backups \
    && chown -R stockscan:stockscan /app /backups
USER stockscan

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    STOCKSCAN_LOG_DIR=/app/logs \
    STOCKSCAN_MODELS_DIR=/app/models

EXPOSE 8000

# Default command = web server. The scheduler service overrides this with
# supercronic (see docker-compose.yml). No --reload in containers.
CMD ["uvicorn", "stockscan.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
