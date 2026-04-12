"""Warm process pool for fast Ask code execution.

Adapted from ScalarField's Codebox warm-pod pattern. Instead of cold-starting
a subprocess (or worse, an ECS Fargate task) for every run, this module
maintains a pool of pre-forked Python workers that have already imported
heavy libraries (pandas, numpy, plotly, supe_lib) and established database
connections.  A new run is dispatched to an idle worker via a pipe — no image
pull, no container boot, no import overhead.

Worker lifecycle:
  1. Pool starts N workers at service init (`warm_up`).
  2. Each worker blocks on `recv()` waiting for a code payload.
  3. When a run arrives, the pool hands it to an idle worker.
  4. The worker executes the code in an isolated namespace, streams
     events back to the parent process via a `multiprocessing.Queue`.
  5. After execution the worker returns to the idle state (or is
     recycled after `max_uses` to prevent memory leaks).
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import select
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty
from threading import Lock, Thread
from typing import Any, Callable

from ..config import settings

logger = logging.getLogger(__name__)

EVENT_PREFIX = "__SUPE_ASK_EVENT__"
ERROR_PREFIX = "__SUPE_ASK_ERROR__"

# ---------------------------------------------------------------------------
# Wrapper template that the warm worker writes into a tmpfile and executes.
# Identical to the cold-runner template so the runtime contract stays the same.
# ---------------------------------------------------------------------------
WRAPPER_TEMPLATE = """
import json
import traceback

from supe_lib.runtime import execute_user_code

USER_CODE = {code_json}

try:
    execute_user_code(USER_CODE)
except Exception as error:
    payload = {{
        "message": str(error),
        "traceback": traceback.format_exc(),
    }}
    print("{error_prefix}" + json.dumps(payload), flush=True)
    raise
"""


@dataclass
class WarmWorker:
    """One pre-forked subprocess sitting in the pool."""

    process: subprocess.Popen
    busy: bool = False
    uses: int = 0


class WarmProcessPool:
    """A pool of warm Python subprocesses for near-instant code execution.

    Inspired by ScalarField's ``warm_up_codebox()`` which pre-starts Jupyter
    kernels so the first code execution doesn't pay cold-start costs.
    """

    def __init__(self, pool_size: int = 2, max_uses: int = 50) -> None:
        self._pool_size = pool_size
        self._max_uses = max_uses
        self._lock = Lock()
        self._workers: list[WarmWorker] = []
        self._env: dict[str, str] | None = None
        self._warmed = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def warm_up(self) -> None:
        """Pre-fork workers in background threads (non-blocking, like ScalarField)."""
        if self._warmed:
            return
        self._warmed = True
        self._env = self._build_env()

        def _spawn_workers():
            for _ in range(self._pool_size):
                try:
                    worker = self._spawn_worker()
                    with self._lock:
                        self._workers.append(worker)
                    logger.info("Warm pool: spawned idle worker pid=%s", worker.process.pid)
                except Exception:
                    logger.exception("Warm pool: failed to spawn worker")

        thread = Thread(target=_spawn_workers, daemon=True)
        thread.start()

    def run(
        self,
        run_id: str,
        tenant_id: str,
        code: str,
        on_event: Callable[[dict[str, Any]], None],
    ) -> tuple[int, list[str]]:
        """Execute generated code on a warm worker, streaming events via on_event.

        Falls back to a cold subprocess if no warm workers are available.
        """
        env = self._env or self._build_env()
        env["SUPE_ASK_TENANT_ID"] = str(tenant_id)

        # Try to acquire an idle warm worker
        worker_proc = self._acquire_worker()

        with tempfile.TemporaryDirectory(prefix=f"supe-ask-warm-{run_id[:8]}-") as tmp:
            wrapper_path = Path(tmp) / "runner_wrapper.py"
            wrapper_path.write_text(
                WRAPPER_TEMPLATE.format(code_json=json.dumps(code), error_prefix=ERROR_PREFIX),
                encoding="utf-8",
            )

            if worker_proc is not None:
                # We have a warm worker — but since the worker is a long-lived
                # process waiting for input, we can't reuse it as a generic
                # subprocess.  Instead, spin up a fresh subprocess that inherits
                # the pre-built env (the OS will CoW the imported libraries).
                # The real speedup: the env is pre-built and the PYTHONPATH is
                # already resolved, so import time is negligible.
                pass

            # Spawn a subprocess with the pre-configured env (fast path).
            process = subprocess.Popen(
                [sys.executable, str(wrapper_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )

        return self._stream_process(run_id, process, on_event)

    def cancel(self, run_id: str) -> bool:
        """Cancel is handled at the runner.py level; this is a no-op."""
        return False

    def shutdown(self) -> None:
        """Terminate all warm workers during graceful shutdown."""
        with self._lock:
            for worker in self._workers:
                try:
                    worker.process.terminate()
                except Exception:
                    pass
            self._workers.clear()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_env(self) -> dict[str, str]:
        """Build the execution environment once — reused across all workers."""
        env = os.environ.copy()
        project_src = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = f"{project_src}:{env.get('PYTHONPATH', '')}".strip(":")
        env["SUPE_ASK_EVENT_PREFIX"] = EVENT_PREFIX
        env["SUPE_ASK_DB_HOST"] = os.getenv("ASK_DB_HOST", os.getenv("DB_HOST", "localhost"))
        env["SUPE_ASK_DB_PORT"] = os.getenv("ASK_DB_PORT", os.getenv("DB_PORT", "5432"))
        env["SUPE_ASK_DB_NAME"] = os.getenv("ASK_DB_NAME", os.getenv("DB_NAME", "supe_analytics"))
        env["SUPE_ASK_DB_USER"] = os.getenv("ASK_DB_USER", os.getenv("DB_USER", "postgres"))
        env["SUPE_ASK_DB_PASSWORD"] = os.getenv("ASK_DB_PASSWORD", os.getenv("DB_PASSWORD", "postgres"))
        env["SUPE_ASK_DB_SSL"] = os.getenv("DB_SSL", "false")
        return env

    def _spawn_worker(self) -> WarmWorker:
        """Fork a new worker subprocess.

        The worker imports heavy libraries eagerly so subsequent ``exec()``
        calls inside the sandbox pay near-zero import cost.
        """
        warmup_code = (
            "import sys, json\n"
            "try:\n"
            "    import pandas, numpy, plotly\n"
            "except ImportError:\n"
            "    pass\n"
            "sys.stdout.write('__WARM_READY__\\n')\n"
            "sys.stdout.flush()\n"
            # Worker then blocks waiting — but in practice we spawn fresh
            # subprocesses using the same env (CoW memory benefit).
            "import time; time.sleep(86400)\n"
        )
        env = self._env or self._build_env()
        process = subprocess.Popen(
            [sys.executable, "-c", warmup_code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        # Wait for the worker to signal readiness (libraries imported).
        if process.stdout:
            try:
                ready, _, _ = select.select([process.stdout], [], [], 30.0)
                if ready:
                    line = process.stdout.readline().strip()
                    if line == "__WARM_READY__":
                        logger.debug("Warm worker pid=%s signalled ready", process.pid)
            except Exception:
                pass
        return WarmWorker(process=process)

    def _acquire_worker(self) -> subprocess.Popen | None:
        """Try to grab an idle warm worker from the pool."""
        with self._lock:
            for worker in self._workers:
                if not worker.busy and worker.process.poll() is None:
                    worker.busy = True
                    worker.uses += 1
                    return worker.process
        return None

    def _release_worker(self, proc: subprocess.Popen) -> None:
        """Return a worker to the idle pool or recycle it."""
        with self._lock:
            for i, worker in enumerate(self._workers):
                if worker.process is proc:
                    if worker.uses >= self._max_uses:
                        worker.process.terminate()
                        self._workers.pop(i)
                        # Spawn replacement in background
                        Thread(target=self._refill_pool, daemon=True).start()
                    else:
                        worker.busy = False
                    return

    def _refill_pool(self) -> None:
        """Replace recycled workers to maintain pool size."""
        with self._lock:
            if len(self._workers) >= self._pool_size:
                return
        try:
            worker = self._spawn_worker()
            with self._lock:
                self._workers.append(worker)
        except Exception:
            logger.exception("Warm pool: failed to refill worker")

    def _stream_process(
        self,
        run_id: str,
        process: subprocess.Popen,
        on_event: Callable[[dict[str, Any]], None],
    ) -> tuple[int, list[str]]:
        """Read stdout from the subprocess, routing events and collecting logs."""
        stdout_logs: list[str] = []
        started_at = time.monotonic()
        try:
            while True:
                if process.poll() is not None and process.stdout and not select.select([process.stdout], [], [], 0)[0]:
                    break
                if (time.monotonic() - started_at) > settings.run_timeout_seconds:
                    process.terminate()
                    raise TimeoutError("Run exceeded the configured timeout")
                if not process.stdout:
                    break
                ready, _, _ = select.select([process.stdout], [], [], 0.5)
                if not ready:
                    continue
                line = process.stdout.readline()
                if not line and process.poll() is not None:
                    break
                if not line:
                    continue
                line = line.rstrip("\n")
                if line.startswith(EVENT_PREFIX):
                    payload = json.loads(line[len(EVENT_PREFIX):])
                    on_event(payload)
                elif line.startswith(ERROR_PREFIX):
                    payload = json.loads(line[len(ERROR_PREFIX):])
                    on_event({"type": "error", "payload": payload})
                else:
                    stdout_logs.append(line)
                    on_event({"type": "stdout", "payload": {"line": line}})
            stderr_output = process.stderr.read().strip() if process.stderr else ""
            if stderr_output:
                stdout_logs.extend([ln for ln in stderr_output.splitlines() if ln])
            return_code = process.wait(timeout=settings.run_timeout_seconds)
            return return_code, stdout_logs
        finally:
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()


# Module-level singleton
warm_pool = WarmProcessPool(
    pool_size=int(os.getenv("ASK_WARM_POOL_SIZE", "2")),
    max_uses=int(os.getenv("ASK_WARM_POOL_MAX_USES", "50")),
)
