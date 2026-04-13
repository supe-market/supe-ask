import io
import json
import unittest
from contextlib import redirect_stdout

from supe_lib.runtime import execute_user_code


EVENT_PREFIX = "__SUPE_ASK_EVENT__"
try:
    import plotly.graph_objects as _go  # noqa: F401

    HAS_PLOTLY = True
except ImportError:
    HAS_PLOTLY = False


def capture_runtime_events(code: str) -> tuple[list[dict], list[str]]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        execute_user_code(code)

    events: list[dict] = []
    plain_lines: list[str] = []
    for line in buffer.getvalue().splitlines():
        if line.startswith(EVENT_PREFIX):
            events.append(json.loads(line[len(EVENT_PREFIX) :]))
        elif line.strip():
            plain_lines.append(line)
    return events, plain_lines


class RuntimeCaptureTests(unittest.TestCase):
    def test_print_prefixes_become_progress_and_logs(self):
        events, plain_lines = capture_runtime_events(
            """
print("Progress: Loading data")
print("Result: Analysis complete")
"""
        )
        self.assertEqual(plain_lines, [])
        progress_events = [event for event in events if event["type"] == "progress"]
        log_artifacts = [
            event for event in events if event["type"] == "artifact" and event["payload"]["artifact_type"] == "log"
        ]
        self.assertEqual(progress_events[0]["payload"]["message"], "Loading data")
        self.assertIn("Result: Analysis complete", log_artifacts[0]["payload"]["payload"]["lines"])

    def test_display_captures_dataframes_and_series(self):
        events, plain_lines = capture_runtime_events(
            """
import pandas as pd

display(pd.DataFrame([{"region": "South", "sales": 12}]), title="Region table")
display(pd.Series([10, 20], name="sales"))
"""
        )
        self.assertEqual(plain_lines, [])
        table_artifacts = [
            event for event in events if event["type"] == "artifact" and event["payload"]["artifact_type"] == "table"
        ]
        self.assertEqual(len(table_artifacts), 2)
        self.assertEqual(table_artifacts[0]["payload"]["title"], "Region table")

    @unittest.skipUnless(HAS_PLOTLY, "plotly is not installed in the local test environment")
    def test_fig_show_is_captured_as_plotly_artifact(self):
        events, plain_lines = capture_runtime_events(
            """
import plotly.graph_objects as go

fig = go.Figure(data=[go.Bar(x=["South"], y=[12])])
fig.update_layout(title="Coverage")
fig.show()
"""
        )
        self.assertEqual(plain_lines, [])
        plotly_artifacts = [
            event for event in events if event["type"] == "artifact" and event["payload"]["artifact_type"] == "plotly"
        ]
        self.assertEqual(len(plotly_artifacts), 1)
        self.assertEqual(plotly_artifacts[0]["payload"]["title"], "Coverage")

    @unittest.skipUnless(HAS_PLOTLY, "plotly is not installed in the local test environment")
    def test_plotly_payload_is_json_native_for_dates_and_numpy(self):
        events, plain_lines = capture_runtime_events(
            """
import datetime as dt
import numpy as np
import plotly.graph_objects as go

fig = go.Figure(
    data=[
        go.Scatter(
            x=[dt.date(2026, 4, 1), dt.date(2026, 4, 2)],
            y=np.array([12.5, 18.0]),
            mode="lines+markers",
        )
    ]
)
fig.update_layout(title="Normalized")
fig.show()
"""
        )
        self.assertEqual(plain_lines, [])
        plotly_artifacts = [
            event for event in events if event["type"] == "artifact" and event["payload"]["artifact_type"] == "plotly"
        ]
        self.assertEqual(len(plotly_artifacts), 1)
        trace = plotly_artifacts[0]["payload"]["payload"]["data"][0]
        self.assertEqual(trace["x"], ["2026-04-01", "2026-04-02"])
        self.assertEqual(trace["y"], [12.5, 18.0])


if __name__ == "__main__":
    unittest.main()
