"""Run the Framework API and persistent Task Worker as one container unit."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path


API_COMMAND = (
    sys.executable,
    "-m",
    "uvicorn",
    "backend.local_code_chat_app:create_app",
    "--factory",
    "--host",
    "0.0.0.0",
    "--port",
    "8894",
)
WORKER_COMMAND = (
    sys.executable,
    "-m",
    "task_manager.runtime.worker",
)


def _configure_python_path() -> None:
    """Give local and container child processes the same import roots."""

    project_root = Path(__file__).resolve().parents[1]
    required_paths = (
        project_root,
        project_root / "backend",
        project_root / "backend" / "single-agent",
        project_root / "services" / "contract" / "src",
    )
    existing_paths = [
        item for item in os.environ.get("PYTHONPATH", "").split(os.pathsep) if item
    ]
    resolved = [str(item) for item in required_paths]
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [*resolved, *(item for item in existing_paths if item not in resolved)]
    )
    # The supervised runtime always has a durable Worker. Let the API enqueue
    # Runs and subscribe to their persisted event stream instead of executing
    # the same Run inline while the Worker is also eligible to claim it.
    os.environ.setdefault("TASK_EXECUTION_MODE", "worker")


def run_processes(
    commands: Sequence[Sequence[str]] = (API_COMMAND, WORKER_COMMAND),
) -> int:
    """Supervise API and Worker; either process exiting restarts the container."""

    _configure_python_path()
    processes = [subprocess.Popen(list(command)) for command in commands]
    stopping = False

    def stop(_signum=None, _frame=None) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        for process in processes:
            if process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopping:
            for process in processes:
                return_code = process.poll()
                if return_code is not None:
                    stop()
                    return return_code or 1
            time.sleep(0.2)
    finally:
        stop()
        deadline = time.monotonic() + 10
        for process in processes:
            if process.poll() is None:
                try:
                    process.wait(timeout=max(0.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    process.kill()
        for process in processes:
            if process.poll() is None:
                process.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(run_processes())
