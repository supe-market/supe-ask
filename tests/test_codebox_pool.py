import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from supe_ask.services.codebox_pool import WarmProcessPool


class WarmProcessPoolTests(unittest.TestCase):
    def test_falls_back_to_cold_only_when_worker_failed_before_output(self):
        pool = WarmProcessPool.__new__(WarmProcessPool)
        pool._env = {}
        pool._build_env = MagicMock(return_value={})
        pool._acquire_worker = MagicMock(return_value=SimpleNamespace())
        pool._release_worker = MagicMock()
        pool._run_on_worker = MagicMock(side_effect=RuntimeError("worker boot failed"))
        pool._run_cold = MagicMock(return_value=(0, ["cold"]))

        events: list[dict] = []
        result = pool.run("run-1", "tenant-1", "print('ok')", events.append)

        self.assertEqual(result, (0, ["cold"]))
        pool._run_cold.assert_called_once()

    def test_timeout_after_output_is_not_retried_cold(self):
        pool = WarmProcessPool.__new__(WarmProcessPool)
        pool._env = {}
        pool._build_env = MagicMock(return_value={})
        pool._acquire_worker = MagicMock(return_value=SimpleNamespace())
        pool._release_worker = MagicMock()
        pool._run_cold = MagicMock(return_value=(0, ["cold"]))

        def fail_after_output(worker, run_id, code, tenant_id, on_event):
            on_event({"type": "artifact", "payload": {"artifact_type": "metric"}})
            raise TimeoutError("Run exceeded the configured timeout")

        pool._run_on_worker = MagicMock(side_effect=fail_after_output)

        with self.assertRaises(TimeoutError):
            pool.run("run-1", "tenant-1", "print('ok')", lambda event: None)

        pool._run_cold.assert_not_called()


if __name__ == "__main__":
    unittest.main()
