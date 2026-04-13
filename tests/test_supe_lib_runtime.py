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

    def test_emit_highlights_is_captured_as_highlights_artifact(self):
        events, plain_lines = capture_runtime_events(
            """
from supe_lib.report import emit_highlights

emit_highlights([
    {"title": "MTD Revenue", "detail": "Month-to-date secondary revenue", "value": "₹12.4L", "tone": "positive"},
    {"title": "Top Distributor", "detail": "Largest contributor this month", "value": "Sharma Distributors", "tone": "positive"},
], subtitle="What needs attention")
"""
        )
        self.assertEqual(plain_lines, [])
        highlight_artifacts = [
            event for event in events if event["type"] == "artifact" and event["payload"]["artifact_type"] == "highlights"
        ]
        self.assertEqual(len(highlight_artifacts), 1)
        payload = highlight_artifacts[0]["payload"]["payload"]
        self.assertEqual(highlight_artifacts[0]["payload"]["title"], "Key Highlights")
        self.assertEqual(payload["subtitle"], "What needs attention")
        self.assertEqual(len(payload["items"]), 2)
        self.assertEqual(payload["items"][0]["value"], "₹12.4L")

    def test_emit_highlights_drops_malformed_rows_and_normalizes_values(self):
        events, plain_lines = capture_runtime_events(
            """
from supe_lib.report import emit_highlights

emit_highlights([
    None,
    {"title": "MTD Revenue", "detail": "Month-to-date revenue", "value": 1240000, "tone": None},
    {"detail": "Only detail", "value": "", "tone": "warning"},
], title="Executive Highlights")
"""
        )
        self.assertEqual(plain_lines, [])
        highlight_artifacts = [
            event for event in events if event["type"] == "artifact" and event["payload"]["artifact_type"] == "highlights"
        ]
        self.assertEqual(len(highlight_artifacts), 1)
        payload = highlight_artifacts[0]["payload"]["payload"]
        self.assertEqual(highlight_artifacts[0]["payload"]["title"], "Executive Highlights")
        self.assertEqual(len(payload["items"]), 2)
        self.assertEqual(payload["items"][0]["value"], "1240000")
        self.assertEqual(payload["items"][0]["tone"], "neutral")
        self.assertEqual(payload["items"][1]["detail"], "Only detail")

    def test_emit_highlights_accepts_simple_string_items(self):
        events, plain_lines = capture_runtime_events(
            """
from supe_lib.report import emit_highlights

emit_highlights([
    "Secondary revenue reached ₹12.4L this month-to-date.",
    "This is 8.2% above the same period last month."
])
"""
        )
        self.assertEqual(plain_lines, [])
        highlight_artifacts = [
            event for event in events if event["type"] == "artifact" and event["payload"]["artifact_type"] == "highlights"
        ]
        self.assertEqual(len(highlight_artifacts), 1)
        items = highlight_artifacts[0]["payload"]["payload"]["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "Insight 1")
        self.assertEqual(items[0]["detail"], "Secondary revenue reached ₹12.4L this month-to-date.")

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
    def test_chart_helpers_accept_plotly_express_style_kwargs(self):
        events, plain_lines = capture_runtime_events(
            """
import pandas as pd
from supe_lib.charts import line_chart, bar_chart

trend_df = pd.DataFrame([
    {"order_sale_date": "2026-04-01", "daily_revenue": 125000},
    {"order_sale_date": "2026-04-02", "daily_revenue": 118000},
])
dist_df = pd.DataFrame([
    {"distributor_name": "A", "total_revenue": 200000},
    {"distributor_name": "B", "total_revenue": 150000},
])

line_chart(
    data_frame=trend_df,
    x="order_sale_date",
    y="daily_revenue",
    title="Daily Secondary Revenue (MTD)",
    labels={"order_sale_date": "Date", "daily_revenue": "Revenue"},
)
bar_chart(
    data_frame=dist_df,
    x="total_revenue",
    y="distributor_name",
    orientation="h",
    title="Top 10 Distributors by Secondary Revenue",
    labels={"total_revenue": "Total Revenue", "distributor_name": "Distributor"},
)
"""
        )
        self.assertEqual(plain_lines, [])
        plotly_artifacts = [
            event for event in events if event["type"] == "artifact" and event["payload"]["artifact_type"] == "plotly"
        ]
        self.assertEqual(len(plotly_artifacts), 2)
        self.assertEqual(plotly_artifacts[0]["payload"]["title"], "Daily Secondary Revenue (MTD)")
        self.assertEqual(plotly_artifacts[1]["payload"]["title"], "Top 10 Distributors by Secondary Revenue")

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
