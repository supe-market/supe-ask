import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from supe_ask.codebox_worker import CodeboxWorker


class CodeboxWorkerTests(unittest.TestCase):
    def test_poll_once_executes_message_and_deletes_it(self):
        fake_settings = SimpleNamespace(
            codebox_queue_url="https://sqs.ap-south-1.amazonaws.com/123456789012/ask-codebox",
            codebox_poll_wait_seconds=15,
            codebox_visibility_timeout_seconds=180,
            codebox_warm_pool_size=1,
            codebox_warm_pool_max_uses=50,
            codebox_warm_ready_timeout_seconds=30,
        )
        fake_pool = MagicMock()

        with patch("supe_ask.codebox_worker.settings", fake_settings), patch(
            "supe_ask.codebox_worker.sqs_service.receive_messages",
            return_value=[
                {
                    "Body": json.dumps(
                        {
                            "runId": "run-1",
                            "tenantId": "12",
                            "callbackUrl": "https://ask.internal.example/api/v1/ask/internal/runs/run-1/callbacks",
                            "callbackToken": "secret",
                            "inputS3Uri": "s3://runner-inputs/ask-runs/12/run-1/input.json",
                        }
                    ),
                    "ReceiptHandle": "receipt-1",
                }
            ],
        ) as receive_messages, patch("supe_ask.codebox_worker.run_job") as run_job, patch(
            "supe_ask.codebox_worker.sqs_service.delete_message"
        ) as delete_message:
            worker = CodeboxWorker(warm_pool=fake_pool)
            processed = worker.poll_once()

        self.assertTrue(processed)
        receive_messages.assert_called_once_with(
            "https://sqs.ap-south-1.amazonaws.com/123456789012/ask-codebox",
            max_number=1,
            wait_time_seconds=15,
            visibility_timeout=180,
        )
        self.assertIs(run_job.call_args.args[1], fake_pool)
        delete_message.assert_called_once_with(
            "https://sqs.ap-south-1.amazonaws.com/123456789012/ask-codebox",
            "receipt-1",
        )

    def test_invalid_message_is_deleted_without_execution(self):
        fake_settings = SimpleNamespace(
            codebox_queue_url="https://sqs.ap-south-1.amazonaws.com/123456789012/ask-codebox",
            codebox_poll_wait_seconds=15,
            codebox_visibility_timeout_seconds=180,
            codebox_warm_pool_size=1,
            codebox_warm_pool_max_uses=50,
            codebox_warm_ready_timeout_seconds=30,
        )

        with patch("supe_ask.codebox_worker.settings", fake_settings), patch(
            "supe_ask.codebox_worker.sqs_service.receive_messages",
            return_value=[{"Body": "not-json", "ReceiptHandle": "receipt-2"}],
        ), patch("supe_ask.codebox_worker.run_job") as run_job, patch(
            "supe_ask.codebox_worker.sqs_service.delete_message"
        ) as delete_message:
            worker = CodeboxWorker(warm_pool=MagicMock())
            processed = worker.poll_once()

        self.assertTrue(processed)
        run_job.assert_not_called()
        delete_message.assert_called_once_with(
            "https://sqs.ap-south-1.amazonaws.com/123456789012/ask-codebox",
            "receipt-2",
        )


if __name__ == "__main__":
    unittest.main()
