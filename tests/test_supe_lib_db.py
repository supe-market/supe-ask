import os
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from supe_lib.db import query_df, query_records, query_scalar


class QueryDfTests(unittest.TestCase):
    @patch("supe_lib.db.pd.read_sql_query")
    @patch("supe_lib.db.psycopg2.connect")
    def test_query_df_injects_tenant_filter(self, mock_connect, mock_read_sql_query):
        mock_connection = MagicMock()
        mock_connect.return_value = mock_connection
        mock_read_sql_query.return_value = []
        os.environ["SUPE_ASK_TENANT_ID"] = "42"

        query_df(
            "select * from orders where {{tenant_filter}} and status = %s",
            params=["open"],
        )

        executed_sql = mock_read_sql_query.call_args.args[0]
        executed_params = mock_read_sql_query.call_args.kwargs["params"]
        self.assertIn("tenant_id = %s", executed_sql)
        self.assertEqual(executed_params, ["open", "42"])
        mock_connection.set_session.assert_called_once_with(readonly=True, autocommit=True)
        mock_connection.close.assert_called_once()

    @patch("supe_lib.db.pd.read_sql_query")
    @patch("supe_lib.db.psycopg2.connect")
    def test_query_df_auto_injects_tenant_filter_when_placeholder_is_missing(self, mock_connect, mock_read_sql_query):
        mock_connection = MagicMock()
        mock_connect.return_value = mock_connection
        mock_read_sql_query.return_value = []
        os.environ["SUPE_ASK_TENANT_ID"] = "42"

        query_df("select * from orders order by created_at desc")

        executed_sql = mock_read_sql_query.call_args.args[0]
        executed_params = mock_read_sql_query.call_args.kwargs["params"]
        self.assertIn("tenant_id = %s", executed_sql)
        self.assertIn("order by created_at desc", executed_sql.lower())
        self.assertEqual(executed_params, ["42"])

    @patch("supe_lib.db.pd.read_sql_query")
    @patch("supe_lib.db.psycopg2.connect")
    def test_query_records_and_query_scalar_normalize_values(self, mock_connect, mock_read_sql_query):
        mock_connection = MagicMock()
        mock_connect.return_value = mock_connection
        mock_read_sql_query.return_value = pd.DataFrame([{"value": 12, "ratio": 3.5}])
        os.environ["SUPE_ASK_TENANT_ID"] = "42"

        records = query_records("select value, ratio from orders where {{tenant_filter}}")
        scalar = query_scalar("select value from orders where {{tenant_filter}}")

        self.assertEqual(records, [{"value": 12, "ratio": 3.5}])
        self.assertEqual(scalar, 12)


if __name__ == "__main__":
    unittest.main()
