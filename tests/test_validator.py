import unittest

from supe_ask.services.validator import CodeValidationError, validate_python_code


class ValidatorTests(unittest.TestCase):
    def test_allows_supported_imports(self):
        validate_python_code(
            """
import pandas as pd
from supe_lib.db import query_df
from supe_lib.report import emit_markdown
"""
        )

    def test_blocks_forbidden_import(self):
        with self.assertRaises(CodeValidationError):
            validate_python_code("import os")

    def test_blocks_forbidden_builtin_call(self):
        with self.assertRaises(CodeValidationError):
            validate_python_code("eval('1 + 1')")

    def test_blocks_entity_metric_snapshot_queries(self):
        with self.assertRaises(CodeValidationError):
            validate_python_code(
                """
from supe_lib.db import query_df

sql = "select * from entity_metric_snapshots where {tenant_filter}"
query_df(sql)
"""
            )

    def test_blocks_unsafe_period_bounds_call_shape(self):
        with self.assertRaises(CodeValidationError):
            validate_python_code(
                """
from supe_lib.time import period_bounds

period_bounds("2026-04-13", "mtd")
"""
            )


if __name__ == "__main__":
    unittest.main()
