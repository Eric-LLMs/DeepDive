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

RUN pip install --no-cache-dir -e "." \
    # Editable install: /app/packages (+ /app/apps, /app/packages/shared/proto) become the
    # ONE import source — no frozen copies in site-packages to double-write against.
    # Hatchling's generated .pth only lists the package parents and puts /app/apps before
    # /app/packages, which would shadow the proto `retrieval` with the apps/retrieval app.
    # Pin the order explicitly (proto first) so every top-level name resolves to source.
    && printf '/app/packages/shared/proto\n/app/apps\n/app/packages\n' \
       > /usr/local/lib/python3.12/site-packages/_editable_impl_deepdive.pth

CMD ["python", "-m", "apps.worker.main"]
