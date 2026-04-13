"""Long-lived ECS worker that keeps a warm Ask execution process ready."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from .aws_clients import sqs_service
from .config import settings
from .services.codebox_pool import WarmProcessPool
from .services.runner_runtime import RunnerJob, run_job

logger = logging.getLogger(__name__)


class CodeboxWorker:
    def __init__(self, *, warm_pool: WarmProcessPool | None = None) -> None:
        queue_url = settings.codebox_queue_url.strip()
        if not queue_url:
            raise RuntimeError("ASK_CODEBOX_QUEUE_URL must be configured for the codebox worker")
        self._queue_url = queue_url
        self._warm_pool = warm_pool or WarmProcessPool(
            pool_size=settings.codebox_warm_pool_size,
            max_uses=settings.codebox_warm_pool_max_uses,
            ready_timeout_seconds=settings.codebox_warm_ready_timeout_seconds,
        )

    def warm_up(self) -> None:
        logger.info("Warming codebox pool", extra={"queue_url": self._queue_url})
        self._warm_pool.warm_up(wait=True)

    def poll_once(self) -> bool:
        messages = sqs_service.receive_messages(
            self._queue_url,
            max_number=1,
            wait_time_seconds=settings.codebox_poll_wait_seconds,
            visibility_timeout=settings.codebox_visibility_timeout_seconds,
        )
        if not messages:
            return False
        for message in messages:
            self._handle_message(message)
        return True

    def run_forever(self) -> None:
        self.warm_up()
        logger.info("Codebox worker ready", extra={"queue_url": self._queue_url})
        while True:
            try:
                self.poll_once()
            except Exception:
                logger.exception("Codebox worker poll failed")
                time.sleep(2)

    def _handle_message(self, message: dict[str, Any]) -> None:
        receipt_handle = str(message.get("ReceiptHandle") or "")
        try:
            payload = json.loads(str(message.get("Body") or "{}"))
            job = RunnerJob.from_payload(payload)
            logger.info("Processing codebox job", extra={"run_id": job.run_id, "tenant_id": job.tenant_id})
            run_job(job, self._warm_pool)
        except SystemExit as error:
            logger.warning("Codebox job exited non-zero", extra={"exit_code": int(error.code or 1)})
        except Exception:
            logger.exception("Codebox job failed")
        finally:
            if receipt_handle:
                try:
                    sqs_service.delete_message(self._queue_url, receipt_handle)
                except Exception:
                    logger.exception("Failed to delete codebox queue message")


def main() -> int:
    worker = CodeboxWorker()
    worker.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
