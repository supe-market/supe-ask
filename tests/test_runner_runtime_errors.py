import unittest
from unittest.mock import MagicMock, patch

from supe_ask.services.runner import WarmPoolRunner


class WarmPoolRunnerRuntimeErrorTests(unittest.TestCase):
    def test_runtime_error_event_marks_run_failed_and_persists_log_artifact(self):
        runner = WarmPoolRunner.__new__(WarmPoolRunner)

        def fake_run(run_id, tenant_id, code, on_event):
            on_event(
                {
                    "type": "error",
                    "payload": {
                        "message": "ValueError: bad chart config",
                        "traceback": "Traceback...\nValueError: bad chart config",
                    },
                }
            )
            return 0, []

        runner._pool = MagicMock()
        runner._pool.run.side_effect = fake_run

        artifact = {"id": "artifact-1", "artifact_type": "log"}

        with patch("supe_ask.services.runner.repository.update_run_execution") as update_execution, patch(
            "supe_ask.services.runner.repository.update_run"
        ) as update_run, patch(
            "supe_ask.services.artifacts.artifact_service.persist_artifact", return_value=artifact
        ) as persist_artifact, patch(
            "supe_ask.services.run_stream.emit_live_run_event"
        ) as emit_live_run_event:
            runner._execute("run-1", "tenant-1", "print('hello')")

        persist_artifact.assert_called_once()
        persisted_lines = persist_artifact.call_args.kwargs["preview_payload"]["lines"]
        self.assertIn("Error: ValueError: bad chart config", persisted_lines)
        self.assertIn("ValueError: bad chart config", "\n".join(persisted_lines))
        update_execution.assert_called_once_with(
            "run-1",
            status="failed",
            runner_completed=True,
            stop_reason="ValueError: bad chart config",
        )
        update_run.assert_called_once_with(
            "run-1",
            status="failed",
            error_message="ValueError: bad chart config",
            completed=True,
        )
        self.assertEqual(emit_live_run_event.call_args_list[-1].args[:3], ("tenant-1", "run-1", "run.failed"))


if __name__ == "__main__":
    unittest.main()
