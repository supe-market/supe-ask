import os
import unittest
from unittest.mock import MagicMock, patch

from supe_ask import runner_preflight


class RunnerPreflightTests(unittest.TestCase):
    def test_main_runs_database_http_and_bucket_checks(self):
        fake_connection = MagicMock()
        fake_cursor = MagicMock()
        fake_cursor.fetchone.return_value = ("analytics", "readonly")
        fake_connection.cursor.return_value.__enter__.return_value = fake_cursor

        with patch("supe_ask.runner_preflight.Database") as database_cls, patch(
            "supe_ask.runner_preflight.settings"
        ) as fake_settings, patch("supe_ask.runner_preflight.s3_storage.put_json") as put_json, patch(
            "supe_ask.runner_preflight.s3_storage.get_json",
            side_effect=lambda bucket, key: {"probe": "ask-runner-smoke", "hostname": "runner-host"},
        ), patch("supe_ask.runner_preflight.s3_storage.delete_object"), patch(
            "supe_ask.runner_preflight.httpx.Client"
        ) as client_cls, patch("supe_ask.runner_preflight.socket.gethostname", return_value="runner-host"):
            fake_settings.control_plane_internal_url = "https://ask.internal.example"
            fake_settings.runner_input_bucket = "input-bucket"
            fake_settings.runner_artifact_bucket = "artifact-bucket"

            database_cls.return_value.connection.return_value.__enter__.return_value = fake_connection
            put_json.side_effect = [
                type("Stored", (), {"key": "ask-runner-smoke/one.json"})(),
                type("Stored", (), {"key": "ask-runner-smoke/two.json"})(),
            ]
            client = client_cls.return_value.__enter__.return_value
            response = MagicMock()
            response.json.return_value = {"service": "supe-ask", "runnerBackend": "ecs"}
            client.get.return_value = response

            with patch.dict(os.environ, {}, clear=False):
                exit_code = runner_preflight.main()

        self.assertEqual(exit_code, 0)
        fake_connection.set_session.assert_called_once_with(readonly=True, autocommit=True)
        fake_cursor.execute.assert_called_once_with("select current_database(), current_user")
        self.assertEqual(client.get.call_args.args[0], "https://ask.internal.example/api/v1/ask/internal/health")

    def test_secret_probe_runs_when_configured(self):
        fake_connection = MagicMock()
        fake_cursor = MagicMock()
        fake_cursor.fetchone.return_value = ("analytics", "readonly")
        fake_connection.cursor.return_value.__enter__.return_value = fake_cursor

        with patch("supe_ask.runner_preflight.Database") as database_cls, patch(
            "supe_ask.runner_preflight.settings"
        ) as fake_settings, patch("supe_ask.runner_preflight.s3_storage.put_json") as put_json, patch(
            "supe_ask.runner_preflight.s3_storage.get_json",
            return_value={"probe": "ask-runner-smoke", "hostname": "runner-host"},
        ), patch("supe_ask.runner_preflight.s3_storage.delete_object"), patch(
            "supe_ask.runner_preflight.httpx.Client"
        ) as client_cls, patch("supe_ask.runner_preflight.secrets_service.describe_secret") as describe_secret, patch(
            "supe_ask.runner_preflight.socket.gethostname",
            return_value="runner-host",
        ):
            fake_settings.control_plane_internal_url = "https://ask.internal.example"
            fake_settings.runner_input_bucket = "input-bucket"
            fake_settings.runner_artifact_bucket = "input-bucket"

            database_cls.return_value.connection.return_value.__enter__.return_value = fake_connection
            put_json.return_value = type("Stored", (), {"key": "ask-runner-smoke/one.json"})()
            client = client_cls.return_value.__enter__.return_value
            response = MagicMock()
            response.json.return_value = {"service": "supe-ask", "runnerBackend": "ecs"}
            client.get.return_value = response
            describe_secret.return_value = {"ARN": "arn:aws:secretsmanager:ap-south-1:123:secret:test", "Name": "test"}

            with patch.dict(os.environ, {"ASK_RUNNER_SECRET_PROBE_ID": "secret-id"}, clear=False):
                exit_code = runner_preflight.main()

        self.assertEqual(exit_code, 0)
        describe_secret.assert_called_once_with("secret-id")


if __name__ == "__main__":
    unittest.main()
