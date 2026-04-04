import unittest
from unittest.mock import patch

from supe_ask.services.graph_cache import GraphCacheService


class GraphCacheServiceTests(unittest.TestCase):
    def test_builds_and_reuses_local_snapshot_for_refresh(self):
        service = GraphCacheService()
        latest_refresh = {"id": "refresh-1"}
        table_rows = [
            {
                "table_name": "sales_orders",
                "display_name": "Sales Orders",
                "description": "Orders",
                "tenant_column": "tenant_id",
                "primary_key_columns": ["id"],
                "date_columns": ["order_sale_date"],
                "metric_hints": ["revenue"],
                "dimension_hints": ["salesman"],
            }
        ]
        relationship_rows = [
            {
                "from_table": "sales_orders",
                "from_column": "salesman_id",
                "to_table": "salesmen",
                "to_column": "id",
                "relationship_type": "foreign_key",
                "cardinality": "many_to_one",
                "source": "database",
            }
        ]
        alias_rows = [
            {
                "object_type": "table",
                "object_name": "sales_orders",
                "table_name": "sales_orders",
                "column_name": None,
                "alias": "revenue",
                "weight": 4,
                "source": "manifest",
            }
        ]

        with patch("supe_ask.services.graph_cache.repository.list_catalog_tables", return_value=table_rows) as list_tables, patch(
            "supe_ask.services.graph_cache.repository.list_catalog_relationships", return_value=relationship_rows
        ) as list_relationships, patch(
            "supe_ask.services.graph_cache.repository.list_catalog_aliases", return_value=alias_rows
        ) as list_aliases:
            first = service.load_snapshot("12", latest_refresh)
            second = service.load_snapshot("12", latest_refresh)

        self.assertIsNotNone(first)
        self.assertEqual(first, second)
        self.assertEqual(first["refreshId"], "refresh-1")
        self.assertIn("sales_orders", first["tables"])
        self.assertIn("sales_orders", first["adjacency"])
        self.assertIn("revenue", first["aliasIndex"])
        list_tables.assert_called_once()
        list_relationships.assert_called_once()
        list_aliases.assert_called_once()


if __name__ == "__main__":
    unittest.main()
