"""Entrypoint for the isolated Ask runner container."""

from __future__ import annotations

import os
from .services.execution_subprocess import PythonSubprocessExecutor
from .services.runner_runtime import CallbackClient, RunnerJob, run_job


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def main() -> None:
    job = RunnerJob(
        run_id=_require_env("RUN_ID"),
        callback_url=_require_env("CALLBACK_URL"),
        callback_token=_require_env("CALLBACK_TOKEN"),
        input_s3_uri=_require_env("INPUT_S3_URI"),
    )
    run_job(job, PythonSubprocessExecutor())


if __name__ == "__main__":
    main()
