"""Warm process pool for fast Ask code execution.

Adapted from ScalarField's Codebox warm-pod pattern.

On Linux (ECS Fargate) each warm worker pre-imports pandas / numpy / plotly /
supe_lib and then blocks waiting for jobs sent over stdin.  When a job arrives
the worker *forks* a child — the child inherits all already-imported modules
via OS copy-on-write pages so import time is near zero.  The child streams
events back to the worker via a pipe; the worker relays them to the pool
coordinator over its stdout.

On macOS (developer machines) os.fork() is intentionally avoided because
forking after threads (numpy, pandas) is unreliable there.  We fall back to a
plain cold subprocess that still benefits from the pre-built env dict.

Worker lifecycle:
  1. Pool spawns N workers at service init (warm_up, non-blocking).
  2. Worker imports heavy libs, prints __WARM_READY__, then loops on stdin.
  3. Pool sends a JSON job line → worker forks → child executes code.
  4. Child events flow: child-stdout pipe → worker stdout → pool → on_event.
  5. Worker prints __JOB_DONE__ → pool marks worker idle.
  6. After max_uses runs the worker is recycled; pool refills in background.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import select
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Callable

from ..config import settings

logger = logging.getLogger(__name__)

EVENT_PREFIX = "__SUPE_ASK_EVENT__"
ERROR_PREFIX = "__SUPE_ASK_ERROR__"
WARM_READY = "__WARM_READY__"
JOB_DONE = "__JOB_DONE__"

CAN_FORK = platform.system() == "Linux"

# ---------------------------------------------------------------------------
# Worker script — sent via -c to the warm worker subprocess.
# On Linux the worker forks for each job (isolation + warm imports).
# On macOS it falls back to exec() in the same process (simpler, still warm).
# ---------------------------------------------------------------------------
_WORKER_SCRIPT = f"""
import sys, json, os, traceback, platform

_CAN_FORK = platform.system() == "Linux"

# Pre-import heavy libraries so forked children pay near-zero import cost.
try:
    import pandas as pd
    import numpy as np
    import plotly
    from supe_lib.runtime import execute_user_code as _execute
except ImportError as _e:
    sys.stderr.write(f"Warm worker import warning: {{_e}}\\n")
    sys.stderr.flush()
    def _execute(code):
        exec(compile(code, "supe_ask_generated.py", "exec"), {{}}, {{}})

EVENT_PREFIX = {EVENT_PREFIX!r}
ERROR_PREFIX = {ERROR_PREFIX!r}
JOB_DONE = {JOB_DONE!r}

sys.stdout.write("{WARM_READY}\\n")
sys.stdout.flush()

for _raw in sys.stdin:
    _raw = _raw.strip()
    if not _raw:
        continue
    try:
        _job = json.loads(_raw)
    except Exception:
        continue

    _code = _job.get("code", "")
    _tenant = _job.get("tenant_id", "")
    os.environ["SUPE_ASK_TENANT_ID"] = str(_tenant)

    if _CAN_FORK:
        # Fork: child inherits all imports via CoW pages.
        _r_fd, _w_fd = os.pipe()
        _child_pid = os.fork()
        if _child_pid == 0:
            # ---- child ----
            os.close(_r_fd)
            _pipe_out = os.fdopen(_w_fd, "w", buffering=1)
            _real_stdout = sys.stdout
            sys.stdout = _pipe_out
            try:
                _execute(_code)
            except Exception:
                _err = json.dumps({{"message": traceback.format_exc(), "traceback": traceback.format_exc()}})
                _pipe_out.write(ERROR_PREFIX + _err + "\\n")
                _pipe_out.flush()
            finally:
                _pipe_out.flush()
                _pipe_out.close()
            os._exit(0)
        else:
            # ---- parent (worker) ----
            os.close(_w_fd)
            with os.fdopen(_r_fd, "r") as _pipe_in:
                for _line in _pipe_in:
                    sys.stdout.write(_line)
                    sys.stdout.flush()
            os.waitpid(_child_pid, 0)
    else:
        # macOS fallback: exec in-process with exception isolation.
        try:
            _execute(_code)
        except Exception:
            _err = json.dumps({{"message": traceback.format_exc(), "traceback": traceback.format_exc()}})
            sys.stdout.write(ERROR_PREFIX + _err + "\\n")
        sys.stdout.flush()

    sys.stdout.write(JOB_DONE + "\\n")
    sys.stdout.flush()
"""


@dataclass
class WarmWorker:
    """One pre-forked subprocess sitting in the pool."""
    process: subprocess.Popen
    busy: bool = False
    uses: int = 0


class WarmProcessPool:
    """A pool of warm Python subprocesses for near-instant code execution."""

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
        """Pre-fork workers in background threads (non-blocking)."""
        if self._warmed:
            return
        self._warmed = True
        self._env = self._build_env()

        def _spawn_all():
            for _ in range(self._pool_size):
                try:
                    worker = self._spawn_worker()
                    with self._lock:
                        self._workers.append(worker)
                    logger.info("Warm pool: worker ready pid=%s", worker.process.pid)
                except Exception:
                    logger.exception("Warm pool: failed to spawn worker")

        Thread(target=_spawn_all, daemon=True).start()

    def run(
        self,
        run_id: str,
        tenant_id: str,
        code: str,
        on_event: Callable[[dict[str, Any]], None],
    ) -> tuple[int, list[str]]:
        """Execute generated code, streaming events via on_event."""
        env = dict(self._env or self._build_env())
        env["SUPE_ASK_TENANT_ID"] = str(tenant_id)

        worker = self._acquire_worker()
        if worker is not None:
            try:
                return self._run_on_worker(worker, run_id, code, tenant_id, on_event)
            except Exception:
                logger.exception("Warm pool: worker execution failed, falling back to cold")
            finally:
                self._release_worker(worker)

        # Fallback: cold subprocess
        logger.info("Warm pool: no idle workers, using cold subprocess for run %s", run_id)
        return self._run_cold(run_id, code, on_event, env)

    def cancel(self, run_id: str) -> bool:
        return False

    def shutdown(self) -> None:
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
        """Start a warm worker subprocess and wait for its __WARM_READY__ signal."""
        env = self._env or self._build_env()
        process = subprocess.Popen(
            [sys.executable, "-c", _WORKER_SCRIPT],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        # Wait up to 30 s for the worker to finish importing heavy libs.
        if process.stdout:
            try:
                ready, _, _ = select.select([process.stdout], [], [], 30.0)
                if ready:
                    line = process.stdout.readline().strip()
                    if line == WARM_READY:
                        logger.debug("Warm worker pid=%s ready", process.pid)
                    else:
                        logger.warning("Warm worker unexpected ready line: %r", line)
            except Exception:
                pass
        return WarmWorker(process=process)

    def _acquire_worker(self) -> WarmWorker | None:
        with self._lock:
            for worker in self._workers:
                if not worker.busy and worker.process.poll() is None:
                    worker.busy = True
                    worker.uses += 1
                    return worker
        return None

    def _release_worker(self, worker: WarmWorker) -> None:
        with self._lock:
            if worker.uses >= self._max_uses:
                try:
                    worker.process.terminate()
                except Exception:
                    pass
                try:
                    self._workers.remove(worker)
                except ValueError:
                    pass
                Thread(target=self._refill_pool, daemon=True).start()
            else:
                worker.busy = False

    def _refill_pool(self) -> None:
        with self._lock:
            current = len(self._workers)
        needed = self._pool_size - current
        for _ in range(needed):
            try:
                worker = self._spawn_worker()
                with self._lock:
                    self._workers.append(worker)
            except Exception:
                logger.exception("Warm pool: failed to refill worker")

    def _run_on_worker(
        self,
        worker: WarmWorker,
        run_id: str,
        code: str,
        tenant_id: str,
        on_event: Callable[[dict[str, Any]], None],
    ) -> tuple[int, list[str]]:
        """Send a job to a warm worker and stream its output."""
        job = json.dumps({"code": code, "tenant_id": tenant_id})
        proc = worker.process

        if not proc.stdin or not proc.stdout:
            raise RuntimeError("Worker stdin/stdout not available")

        proc.stdin.write(job + "\n")
        proc.stdin.flush()

        stdout_logs: list[str] = []
        started_at = time.monotonic()

        while True:
            if (time.monotonic() - started_at) > settings.run_timeout_seconds:
                proc.terminate()
                raise TimeoutError("Run exceeded the configured timeout")
            ready, _, _ = select.select([proc.stdout], [], [], 0.5)
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            line = proc.stdout.readline()
            if not line:
                break
            line = line.rstrip("\n")
            if line == JOB_DONE:
                break
            if line.startswith(EVENT_PREFIX):
                payload = json.loads(line[len(EVENT_PREFIX):])
                on_event(payload)
            elif line.startswith(ERROR_PREFIX):
                payload = json.loads(line[len(ERROR_PREFIX):])
                on_event({"type": "error", "payload": payload})
            else:
                stdout_logs.append(line)
                on_event({"type": "stdout", "payload": {"line": line}})

        return 0, stdout_logs

    def _run_cold(
        self,
        run_id: str,
        code: str,
        on_event: Callable[[dict[str, Any]], None],
        env: dict[str, str],
    ) -> tuple[int, list[str]]:
        """Cold subprocess fallback — identical contract to warm path."""
        wrapper = (
            "import json, traceback\n"
            "from supe_lib.runtime import execute_user_code\n"
            f"USER_CODE = {json.dumps(code)!r}\n"
            "try:\n"
            "    execute_user_code(USER_CODE)\n"
            "except Exception as _e:\n"
            f"    print({ERROR_PREFIX!r} + json.dumps({{'message': str(_e), 'traceback': traceback.format_exc()}}))\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            prefix=f"supe-ask-cold-{run_id[:8]}-",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            tmp.write(wrapper)
            tmp_path = tmp.name

        try:
            process = subprocess.Popen(
                [sys.executable, tmp_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            return self._stream_process(run_id, process, on_event)
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    def _stream_process(
        self,
        run_id: str,
        process: subprocess.Popen,
        on_event: Callable[[dict[str, Any]], None],
    ) -> tuple[int, list[str]]:
        stdout_logs: list[str] = []
        started_at = time.monotonic()
        try:
            while True:
                if (time.monotonic() - started_at) > settings.run_timeout_seconds:
                    process.terminate()
                    raise TimeoutError("Run exceeded the configured timeout")
                if not process.stdout:
                    break
                ready, _, _ = select.select([process.stdout], [], [], 0.5)
                if not ready:
                    if process.poll() is not None:
                        break
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
            if process.stderr:
                stderr_output = process.stderr.read().strip()
                if stderr_output:
                    stdout_logs.extend(ln for ln in stderr_output.splitlines() if ln)
            return_code = process.wait(timeout=5)
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
