import os
import unittest
from unittest.mock import MagicMock, patch

from supe_ask import runner_entrypoint


class RunnerEntrypointTests(unittest.TestCase):
    def test_manifest_download_failure_posts_bootstrap_failed_callback(self):
        env = {
            "RUN_ID": "run-1",
            "CALLBACK_URL": "https://control-plane.local/callback",
            "CALLBACK_TOKEN": "secret",
            "INPUT_S3_URI": "s3://bucket/input.json",
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "supe_ask.runner_entrypoint.s3_storage.get_json",
            side_effect=RuntimeError("manifest missing"),
        ), patch("supe_ask.runner_entrypoint.CallbackClient._post") as post_callback:
            with self.assertRaises(RuntimeError):
                runner_entrypoint.main()

        # The heartbeat fires immediately before the manifest download, so
        # _post is called for the heartbeat AND the failure callback.
        post_callback.assert_any_call(
            "failed",
            {
                "message": "manifest missing",
                "stage": "execution_bootstrap",
            },
        )

    def test_malformed_manifest_posts_bootstrap_failed_callback(self):
        env = {
            "RUN_ID": "run-1",
            "CALLBACK_URL": "https://control-plane.local/callback",
            "CALLBACK_TOKEN": "secret",
            "INPUT_S3_URI": "s3://bucket/input.json",
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "supe_ask.runner_entrypoint.s3_storage.get_json",
            return_value={"tenantId": "12"},
        ), patch("supe_ask.runner_entrypoint.CallbackClient._post") as post_callback:
            with self.assertRaises(RuntimeError):
                runner_entrypoint.main()

        post_callback.assert_any_call(
            "failed",
            {
                "message": "Execution manifest is missing tenantId or pythonCode",
                "stage": "execution_bootstrap",
            },
        )

    def test_runtime_error_posts_execution_failed_callback(self):
        env = {
            "RUN_ID": "run-1",
            "CALLBACK_URL": "https://control-plane.local/callback",
            "CALLBACK_TOKEN": "secret",
            "INPUT_S3_URI": "s3://bucket/input.json",
        }
        fake_thread = MagicMock()
        with patch.dict(os.environ, env, clear=True), patch(
            "supe_ask.runner_entrypoint.s3_storage.get_json",
            return_value={"tenantId": "12", "pythonCode": "print('ok')"},
        ), patch("supe_ask.runner_entrypoint.CallbackClient.start_heartbeat", return_value=fake_thread), patch(
            "supe_ask.runner_entrypoint.LocalRunner"
        ) as runner_cls, patch("supe_ask.runner_entrypoint.CallbackClient._post") as post_callback:
            runner_cls.return_value.run.side_effect = RuntimeError("runtime blew up")
            with self.assertRaises(RuntimeError):
                runner_entrypoint.main()

        self.assertIn(
            unittest.mock.call(
                "failed",
                {
                    "message": "runtime blew up",
                    "stage": "execution",
                },
            ),
            post_callback.call_args_list,
        )


if __name__ == "__main__":
    unittest.main()
