import unittest

from supe_ask.services.execution_subprocess import PythonSubprocessExecutor


class ExecutionSubprocessTests(unittest.TestCase):
    def test_executor_emits_runtime_events(self):
        events: list[dict] = []
        code = """
import pandas as pd

print("Progress: Loading")
display(pd.DataFrame([{"region": "South", "sales": 12}]), title="Sales table")
print("Result: Completed")
"""

        return_code, logs = PythonSubprocessExecutor().run("test-runner-runtime", "42", code, events.append)

        self.assertEqual(return_code, 0)
        self.assertEqual(logs, [])
        progress_events = [event for event in events if event["type"] == "progress"]
        artifact_types = [event["payload"]["artifact_type"] for event in events if event["type"] == "artifact"]
        self.assertEqual(progress_events[0]["payload"]["message"], "Loading")
        self.assertIn("table", artifact_types)
        self.assertIn("log", artifact_types)


if __name__ == "__main__":
    unittest.main()
