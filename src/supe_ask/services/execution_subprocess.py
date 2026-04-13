from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

from ..config import settings
from .runtime_env import apply_runtime_db_env

EVENT_PREFIX = "__SUPE_ASK_EVENT__"
ERROR_PREFIX = "__SUPE_ASK_ERROR__"

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


class PythonSubprocessExecutor:
    """Execute generated Python inside a dedicated subprocess."""

    def run(self, run_id: str, tenant_id: str, code: str, on_event: Callable[[dict], None]) -> tuple[int, list[str]]:
        stdout_logs: list[str] = []
        with tempfile.TemporaryDirectory(prefix=f"supe-ask-{run_id[:8]}-") as temp_dir:
            wrapper_path = Path(temp_dir) / "runner_wrapper.py"
            wrapper_path.write_text(
                WRAPPER_TEMPLATE.format(code_json=json.dumps(code), error_prefix=ERROR_PREFIX),
                encoding="utf-8",
            )
            env = os.environ.copy()
            project_src = str(Path(__file__).resolve().parents[2])
            env["PYTHONPATH"] = f"{project_src}:{env.get('PYTHONPATH', '')}".strip(":")
            env["SUPE_ASK_EVENT_PREFIX"] = EVENT_PREFIX
            apply_runtime_db_env(env)
            env["SUPE_ASK_TENANT_ID"] = str(tenant_id)

            process = subprocess.Popen(
                [sys.executable, str(wrapper_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            try:
                started_at = time.monotonic()
                while True:
                    if process.poll() is not None and process.stdout and process.stdout.closed:
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
                    stdout_logs.extend([line for line in stderr_output.splitlines() if line])
                return process.wait(timeout=settings.run_timeout_seconds), stdout_logs
            finally:
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()
