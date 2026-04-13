import unittest
from types import SimpleNamespace
from unittest.mock import patch

from supe_ask.services.execution_subprocess import PythonSubprocessExecutor


class _FakeStdout:
    def __init__(self) -> None:
        self.closed = True

    def close(self) -> None:
        self.closed = True


class _FakeStderr:
    def __init__(self) -> None:
        self.closed = False

    def read(self) -> str:
        return ""

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self) -> None:
        self.stdout = _FakeStdout()
        self.stderr = _FakeStderr()

    def poll(self) -> int:
        return 0

    def wait(self, timeout: int | None = None) -> int:
        return 0


class RuntimeDbEnvTests(unittest.TestCase):
    def test_subprocess_executor_uses_resolved_db_settings_for_subprocess(self):
        fake_settings = SimpleNamespace(
            db_host="analytics.internal",
            db_port=6543,
            db_name="analytics",
            db_user="readonly",
            db_password="secret",
            db_ssl=True,
            run_timeout_seconds=90,
        )

        with patch("supe_ask.services.execution_subprocess.settings", fake_settings), patch(
            "supe_ask.services.runtime_env.settings",
            fake_settings,
        ), patch("subprocess.Popen", return_value=_FakeProcess()) as popen:
            return_code, logs = PythonSubprocessExecutor().run("run-1", "42", "print('ok')", lambda event: None)

        env = popen.call_args.kwargs["env"]
        self.assertEqual(return_code, 0)
        self.assertEqual(logs, [])
        self.assertEqual(env["SUPE_ASK_DB_HOST"], "analytics.internal")
        self.assertEqual(env["SUPE_ASK_DB_PORT"], "6543")
        self.assertEqual(env["SUPE_ASK_DB_NAME"], "analytics")
        self.assertEqual(env["SUPE_ASK_DB_USER"], "readonly")
        self.assertEqual(env["SUPE_ASK_DB_PASSWORD"], "secret")
        self.assertEqual(env["SUPE_ASK_DB_SSL"], "true")


if __name__ == "__main__":
    unittest.main()
