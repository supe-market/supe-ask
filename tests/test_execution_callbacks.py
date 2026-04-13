import unittest
from unittest.mock import patch

from fastapi import HTTPException

from supe_ask.security import hash_callback_token
from supe_ask.services.execution_callbacks import ExecutionCallbackService


class ExecutionCallbackServiceTests(unittest.TestCase):
    def test_progress_callback_emits_event(self):
        service = ExecutionCallbackService()
        execution = {"tenant_id": "12", "callback_token_hash": hash_callback_token("secret"), "last_callback_sequence": 0}
        run = {"status": "running"}

        with patch("supe_ask.services.execution_callbacks.repository.get_run_execution", return_value=execution), patch(
            "supe_ask.services.execution_callbacks.repository.get_run", return_value=run
        ), patch("supe_ask.services.execution_callbacks.repository.update_run_execution") as update_execution, patch(
            "supe_ask.services.execution_callbacks.emit_live_run_event"
        ) as emit_event:
            response = service.handle_callback("run-1", "secret", "progress", 1, {"message": "Working"})

        self.assertEqual(response, {"success": True})
        update_execution.assert_called()
        emit_event.assert_called_once_with("12", "run-1", "run.execution.progress", {"message": "Working"})

    def test_duplicate_sequence_is_ignored(self):
        service = ExecutionCallbackService()
        execution = {"tenant_id": "12", "callback_token_hash": hash_callback_token("secret"), "last_callback_sequence": 2}

        with patch("supe_ask.services.execution_callbacks.repository.get_run_execution", return_value=execution), patch(
            "supe_ask.services.execution_callbacks.repository.get_run"
        ) as get_run:
            response = service.handle_callback("run-1", "secret", "heartbeat", 2, {})

        self.assertEqual(response, {"success": True, "ignored": True})
        get_run.assert_not_called()

    def test_invalid_token_is_rejected(self):
        service = ExecutionCallbackService()
        execution = {"tenant_id": "12", "callback_token_hash": hash_callback_token("secret"), "last_callback_sequence": 0}

        with patch("supe_ask.services.execution_callbacks.repository.get_run_execution", return_value=execution):
            with self.assertRaises(HTTPException) as context:
                service.handle_callback("run-1", "wrong", "heartbeat", 1, {})

        self.assertEqual(context.exception.status_code, 403)

    def test_artifact_callback_persists_artifact(self):
        service = ExecutionCallbackService()
        execution = {"tenant_id": "12", "callback_token_hash": hash_callback_token("secret"), "last_callback_sequence": 0}
        run = {"status": "running"}
        artifact = {"id": "artifact-1", "artifact_type": "plotly"}

        with patch("supe_ask.services.execution_callbacks.repository.get_run_execution", return_value=execution), patch(
            "supe_ask.services.execution_callbacks.repository.get_run", return_value=run
        ), patch("supe_ask.services.execution_callbacks.repository.update_run_execution"), patch(
            "supe_ask.services.execution_callbacks.artifact_service.persist_artifact", return_value=artifact
        ) as persist_artifact, patch("supe_ask.services.execution_callbacks.emit_live_run_event") as emit_event:
            response = service.handle_callback(
                "run-1",
                "secret",
                "artifact",
                1,
                {
                    "artifactType": "plotly",
                    "title": "Chart",
                    "previewPayload": {"data": []},
                    "storage": {"storageBackend": "s3", "objectKey": "a.json", "contentType": "application/json", "byteSize": 100},
                },
            )

        self.assertEqual(response, {"success": True})
        persist_artifact.assert_called_once()
        emit_event.assert_called_once_with("12", "run-1", "run.artifact", {"artifact": artifact}, force_flush=True)

    def test_failed_callback_uses_runner_supplied_stage(self):
        service = ExecutionCallbackService()
        execution = {"tenant_id": "12", "callback_token_hash": hash_callback_token("secret"), "last_callback_sequence": 0}
        run = {"status": "running"}

        with patch("supe_ask.services.execution_callbacks.repository.get_run_execution", return_value=execution), patch(
            "supe_ask.services.execution_callbacks.repository.get_run", return_value=run
        ), patch("supe_ask.services.execution_callbacks.repository.update_run_execution"), patch(
            "supe_ask.services.execution_callbacks.repository.update_run"
        ), patch("supe_ask.services.execution_callbacks.emit_live_run_event") as emit_event:
            response = service.handle_callback(
                "run-1",
                "secret",
                "failed",
                1,
                {
                    "message": "Manifest download failed",
                    "stage": "execution_bootstrap",
                },
            )

        self.assertEqual(response, {"success": True})
        emit_event.assert_called_once_with(
            "12",
            "run-1",
            "run.failed",
            {
                "message": "Manifest download failed",
                "traceback": None,
                "stage": "execution_bootstrap",
            },
            force_flush=True,
        )


if __name__ == "__main__":
    unittest.main()
