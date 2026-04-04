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


if __name__ == "__main__":
    unittest.main()
