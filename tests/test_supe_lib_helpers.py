from datetime import date
import unittest

import pandas as pd

from supe_lib.dataframes import bottom_n, frame_records, summarize_frame, top_n
from supe_lib.metrics import dense_rank, growth_rate, percent_delta, safe_percent
from supe_lib.supe import build_kpi_summary, build_period_filter, build_scope_filter, sql_and
from supe_lib.time import period_bounds


class SupeLibHelpersTests(unittest.TestCase):
    def test_period_bounds_supports_common_labels(self):
        start_date, end_date = period_bounds("mtd", today=date(2026, 4, 2))
        self.assertEqual(start_date.isoformat(), "2026-04-01")
        self.assertEqual(end_date.isoformat(), "2026-04-02")

        start_date, end_date = period_bounds("last_7_days", today=date(2026, 4, 2))
        self.assertEqual(start_date.isoformat(), "2026-03-27")
        self.assertEqual(end_date.isoformat(), "2026-04-02")

    def test_period_bounds_recovers_from_swapped_generated_arguments(self):
        start_date, end_date = period_bounds("2026-04-13", today="mtd")
        self.assertEqual(start_date.isoformat(), "2026-04-01")
        self.assertEqual(end_date.isoformat(), "2026-04-13")

    def test_scope_and_period_filters_are_reusable(self):
        clause, params = build_scope_filter("region", "South")
        self.assertEqual(clause, "region = %(scope_value)s")
        self.assertEqual(params, {"scope_value": "South"})

        period_clause, period_params = build_period_filter("order_date", "qtd", today=date(2026, 4, 2))
        self.assertIn("order_date >=", period_clause)
        self.assertEqual(period_params["period_start"], "2026-04-01")
        self.assertEqual(period_params["period_end"], "2026-04-02")
        self.assertEqual(sql_and(clause, period_clause), "(region = %(scope_value)s) and (" + period_clause + ")")

    def test_dataframe_helpers_and_metrics(self):
        frame = pd.DataFrame(
            [
                {"region": "North", "sales": 10},
                {"region": "South", "sales": 30},
                {"region": "West", "sales": 20},
            ]
        )
        self.assertEqual(top_n(frame, "sales", n=1).iloc[0]["region"], "South")
        self.assertEqual(bottom_n(frame, "sales", n=1).iloc[0]["region"], "North")

        records = frame_records(frame, max_rows=2)
        self.assertEqual(records["rowCount"], 3)
        self.assertEqual(len(records["rows"]), 2)
        self.assertEqual(summarize_frame(frame)["numericColumnCount"], 1)

        self.assertEqual(safe_percent(2, 8), 25.0)
        self.assertEqual(percent_delta(120, 100), 20.0)
        self.assertEqual(growth_rate(120, 100), 20.0)
        self.assertEqual(list(dense_rank(pd.Series([10, 30, 20]))), [3, 1, 2])
        self.assertEqual(build_kpi_summary("Revenue", 120, 100)["tone"], "positive")


if __name__ == "__main__":
    unittest.main()
