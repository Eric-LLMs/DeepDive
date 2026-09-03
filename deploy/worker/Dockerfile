# Worker: arq async task runner for enrichment jobs (TTS / images / AI / indexing).
#
# Model inference (LLM / embedding / TTS) stays in the separate model containers; this
# service only orchestrates the enrichment jobs, talking to those containers over HTTP and
# to Redis (arq queue) + PostgreSQL (jobs table + session data).
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY packages ./packages
COPY apps ./apps
# Research OS plugin code: worker's research_drive task imports plugins.research.{driver,plugin}
# at runtime, and they are file-backed modules (not pip-packaged) — copy the tree verbatim.
COPY plugins ./plugins

RUN pip install --no-cache-dir "."

CMD ["python", "-m", "apps.worker.main"]
