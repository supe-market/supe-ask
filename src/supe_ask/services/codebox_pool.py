"""Warm process pool used by the long-lived ECS codebox worker."""

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
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
from threading import Lock, Thread
from typing import Any, Callable

from ..config import settings
from .runtime_env import apply_runtime_db_env

logger = logging.getLogger(__name__)

EVENT_PREFIX = "__SUPE_ASK_EVENT__"
ERROR_PREFIX = "__SUPE_ASK_ERROR__"
WARM_READY = "__WARM_READY__"
JOB_DONE = "__JOB_DONE__"
HEARTBEAT = "__SUPE_HEARTBEAT__"

CAN_FORK = platform.system() == "Linux"

_WORKER_SCRIPT = f"""
import sys, json, os, traceback, platform

_CAN_FORK = platform.system() == "Linux"

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
HEARTBEAT = {HEARTBEAT!r}

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
        _r_fd, _w_fd = os.pipe()
        _child_pid = os.fork()
        if _child_pid == 0:
            os.close(_r_fd)
            _pipe_out = os.fdopen(_w_fd, "w", buffering=1)
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
            os.close(_w_fd)
            import select as _sel
            _pipe_in = os.fdopen(_r_fd, "r")
            try:
                while True:
                    _ready, _, _ = _sel.select([_pipe_in], [], [], 30.0)
                    if _ready:
                        _fwd = _pipe_in.readline()
                        if not _fwd:
                            break
                        sys.stdout.write(_fwd)
                        sys.stdout.flush()
                    else:
                        sys.stdout.write(HEARTBEAT + "\\n")
                        sys.stdout.flush()
            finally:
                _pipe_in.close()
            os.waitpid(_child_pid, 0)
    else:
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
    process: subprocess.Popen
    output_queue: Queue = field(default_factory=Queue)
    busy: bool = False
    uses: int = 0


class WarmProcessPool:
    """Keep one or more warmed Python workers ready for Ask execution."""

    def __init__(self, pool_size: int = 1, max_uses: int = 50, ready_timeout_seconds: int = 30) -> None:
        self._pool_size = max(1, int(pool_size))
        self._max_uses = max(1, int(max_uses))
        self._ready_timeout_seconds = max(1, int(ready_timeout_seconds))
        self._lock = Lock()
        self._workers: list[WarmWorker] = []
        self._env: dict[str, str] | None = None
        self._warmed = False

    def warm_up(self, *, wait: bool = False) -> None:
        if self._warmed:
            return
        self._warmed = True
        self._env = self._build_env()
        if wait:
            self._spawn_all()
            return
        Thread(target=self._spawn_all, daemon=True).start()

    def run(
        self,
        run_id: str,
        tenant_id: str,
        code: str,
        on_event: Callable[[dict[str, Any]], None],
    ) -> tuple[int, list[str]]:
        env = dict(self._env or self._build_env())
        env["SUPE_ASK_TENANT_ID"] = str(tenant_id)
        saw_output = False

        def tracked_on_event(event: dict[str, Any]) -> None:
            nonlocal saw_output
            if isinstance(event, dict) and event.get("type"):
                saw_output = True
            on_event(event)

        worker = self._acquire_worker()
        if worker is not None:
            try:
                return self._run_on_worker(worker, run_id, code, tenant_id, tracked_on_event)
            except Exception as error:
                if isinstance(error, TimeoutError) or saw_output:
                    logger.exception(
                        "Codebox warm worker failed after execution had already started",
                        extra={"run_id": run_id},
                    )
                    raise
                logger.exception("Codebox warm worker failed, falling back to a cold subprocess")
            finally:
                self._release_worker(worker)

        logger.info("No idle warm workers available; using a cold subprocess", extra={"run_id": run_id})
        return self._run_cold(run_id, code, tracked_on_event, env)

    def shutdown(self) -> None:
        with self._lock:
            for worker in self._workers:
                try:
                    worker.process.terminate()
                except Exception:
                    pass
            self._workers.clear()

    def _spawn_all(self) -> None:
        while True:
            with self._lock:
                current = len(self._workers)
            if current >= self._pool_size:
                return
            worker = self._spawn_worker()
            with self._lock:
                self._workers.append(worker)
            logger.info("Warm codebox worker ready", extra={"pid": worker.process.pid})

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        project_src = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = f"{project_src}:{env.get('PYTHONPATH', '')}".strip(":")
        env["SUPE_ASK_EVENT_PREFIX"] = EVENT_PREFIX
        apply_runtime_db_env(env)
        return env

    def _spawn_worker(self) -> WarmWorker:
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
        if not process.stdout:
            raise RuntimeError("Warm worker stdout is not available")

        ready_line = ""
        ready, _, _ = select.select([process.stdout], [], [], float(self._ready_timeout_seconds))
        if ready:
            ready_line = process.stdout.readline().strip()
        if ready_line != WARM_READY:
            stderr_output = process.stderr.read().strip() if process.stderr else ""
            try:
                process.terminate()
            except Exception:
                pass
            detail = ready_line or stderr_output or "timed out waiting for worker imports"
            raise RuntimeError(f"Warm worker failed to become ready: {detail}")

        # Start a persistent reader thread that drains this worker's stdout into a
        # queue. Using select() directly on process.stdout is unreliable because
        # Python's buffered I/O can hold lines internally without the OS fd
        # appearing ready — causing JOB_DONE to be silently buffered and never read.
        output_queue: Queue = Queue()

        def _stdout_reader(stdout: Any, q: Queue) -> None:
            try:
                for line in stdout:
                    q.put(line)
            except Exception:
                pass
            finally:
                q.put(None)  # EOF / process died sentinel

        Thread(target=_stdout_reader, args=(process.stdout, output_queue), daemon=True).start()
        return WarmWorker(process=process, output_queue=output_queue)

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
            if not worker.busy:
                return  # already released (e.g. early release on JOB_DONE)
            should_retire = worker.uses >= self._max_uses or worker.process.poll() is not None
            if should_retire:
                try:
                    worker.process.terminate()
                except Exception:
                    pass
                try:
                    self._workers.remove(worker)
                except ValueError:
                    pass
                Thread(target=self._spawn_all, daemon=True).start()
            else:
                worker.busy = False

    def _run_on_worker(
        self,
        worker: WarmWorker,
        run_id: str,
        code: str,
        tenant_id: str,
        on_event: Callable[[dict[str, Any]], None],
    ) -> tuple[int, list[str]]:
        job = json.dumps({"code": code, "tenant_id": tenant_id})
        process = worker.process
        if not process.stdin:
            raise RuntimeError("Warm worker stdin not available")

        process.stdin.write(job + "\n")
        process.stdin.flush()

        stdout_logs: list[str] = []
        last_activity_at = time.monotonic()
        exit_reason = "eof"
        while True:
            idle_seconds = time.monotonic() - last_activity_at
            if idle_seconds > settings.run_timeout_seconds:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except Exception:
                    process.kill()
                raise TimeoutError(f"Run idle for {idle_seconds:.0f}s with no output — possible hang or infinite loop")
            try:
                raw = worker.output_queue.get(timeout=0.5)
            except Empty:
                if process.poll() is not None:
                    exit_reason = "process_exited"
                    break
                continue
            if raw is None:
                exit_reason = "eof"
                break
            last_activity_at = time.monotonic()
            line = raw.rstrip("\n")
            if line == JOB_DONE:
                exit_reason = "job_done"
                # Execution is confirmed done — release the worker back to the warm pool
                # now, before the caller does any post-execution DB writes.  The finally
                # block in run() will call _release_worker again but that is a no-op.
                self._release_worker(worker)
                break
            elif line == HEARTBEAT:
                pass  # idle timer already reset above; keep waiting
            elif line.startswith(EVENT_PREFIX):
                on_event(json.loads(line[len(EVENT_PREFIX):]))
            elif line.startswith(ERROR_PREFIX):
                on_event({"type": "error", "payload": json.loads(line[len(ERROR_PREFIX):])})
            else:
                stdout_logs.append(line)
                on_event({"type": "stdout", "payload": {"line": line}})
        logger.info("Warm worker loop exited", extra={"run_id": run_id, "exit_reason": exit_reason})
        if exit_reason != "job_done":
            raise RuntimeError(
                f"Warm worker terminated before JOB_DONE (exit_reason={exit_reason}); "
                "worker will be retired and replaced"
            )
        return 0, stdout_logs

    def _run_cold(
        self,
        run_id: str,
        code: str,
        on_event: Callable[[dict[str, Any]], None],
        env: dict[str, str],
    ) -> tuple[int, list[str]]:
        wrapper = (
            "import json, traceback\n"
            "from supe_lib.runtime import execute_user_code\n"
            f"USER_CODE = {json.dumps(code)!r}\n"
            "try:\n"
            "    execute_user_code(USER_CODE)\n"
            "except Exception as _error:\n"
            f"    print({ERROR_PREFIX!r} + json.dumps({{'message': str(_error), 'traceback': traceback.format_exc()}}), flush=True)\n"
            "    raise\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            prefix=f"supe-ask-cold-{run_id[:8]}-",
            delete=False,
            encoding="utf-8",
        ) as temp_file:
            temp_file.write(wrapper)
            temp_path = temp_file.name

        try:
            process = subprocess.Popen(
                [sys.executable, temp_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            return self._stream_process(process, on_event)
        finally:
            try:
                os.unlink(temp_path)
            except Exception:
                pass

    def _stream_process(
        self,
        process: subprocess.Popen,
        on_event: Callable[[dict[str, Any]], None],
    ) -> tuple[int, list[str]]:
        stdout_logs: list[str] = []
        last_activity_at = time.monotonic()
        try:
            while True:
                idle_seconds = time.monotonic() - last_activity_at
                if idle_seconds > settings.run_timeout_seconds:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except Exception:
                        process.kill()
                    raise TimeoutError(f"Run idle for {idle_seconds:.0f}s with no output — possible hang or infinite loop")
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
                last_activity_at = time.monotonic()
                line = line.rstrip("\n")
                if line.startswith(EVENT_PREFIX):
                    on_event(json.loads(line[len(EVENT_PREFIX):]))
                elif line.startswith(ERROR_PREFIX):
                    on_event({"type": "error", "payload": json.loads(line[len(ERROR_PREFIX):])})
                else:
                    stdout_logs.append(line)
                    on_event({"type": "stdout", "payload": {"line": line}})
            stderr_output = process.stderr.read().strip() if process.stderr else ""
            if stderr_output:
                stdout_logs.extend(line for line in stderr_output.splitlines() if line)
            return process.wait(timeout=settings.run_timeout_seconds), stdout_logs
        finally:
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()
