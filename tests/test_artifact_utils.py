import unittest

from supe_ask.artifact_utils import build_preview_payload


class ArtifactUtilsTests(unittest.TestCase):
    def test_table_preview_is_truncated(self):
        payload = {
            "columns": ["region"],
            "rows": [{"region": f"R{index}"} for index in range(100)],
            "rowCount": 100,
        }

        preview = build_preview_payload("table", payload)

        self.assertEqual(preview["rowCount"], 100)
        self.assertLessEqual(len(preview["rows"]), 50)

    def test_log_preview_is_truncated(self):
        payload = {"lines": [f"line-{index}" for index in range(500)]}

        preview = build_preview_payload("log", payload)

        self.assertEqual(len(preview["lines"]), 200)

    def test_plotly_preview_truncates_trace_lists(self):
        payload = {
            "data": [
                {
                    "type": "scatter",
                    "x": list(range(500)),
                    "y": list(range(500)),
                }
            ],
            "layout": {"title": "Large"},
        }

        preview = build_preview_payload("plotly", payload)

        self.assertEqual(len(preview["data"][0]["x"]), 100)
        self.assertEqual(len(preview["data"][0]["y"]), 100)


if __name__ == "__main__":
    unittest.main()
