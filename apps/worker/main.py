"""Worker entrypoint.

The canonical way to start the worker is ``arq apps.worker.settings.WorkerSettings``; this
module offers the equivalent ``python -m apps.worker.main`` form for container entrypoints.
"""
from arq import run_worker

from apps.worker.settings import WorkerSettings


def main() -> None:
    run_worker(WorkerSettings)


if __name__ == "__main__":
    main()
