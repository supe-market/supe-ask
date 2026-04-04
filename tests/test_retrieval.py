import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from supe_ask.services.retrieval import RetrievalService


class RetrievalServiceTests(unittest.TestCase):
    def test_resolve_join_path_finds_shortest_path(self):
        service = RetrievalService()
        graph_snapshot = {
            "adjacency": {
                "sales_order_items": [
                    {"fromTable": "sales_order_items", "fromColumn": "sales_order_id", "toTable": "sales_orders", "toColumn": "id", "source": "database"}
                ],
                "sales_orders": [
                    {"fromTable": "sales_orders", "fromColumn": "id", "toTable": "sales_order_items", "toColumn": "sales_order_id", "source": "database"},
                    {"fromTable": "sales_orders", "fromColumn": "salesman_id", "toTable": "salesmen", "toColumn": "id", "source": "database"},
                ],
                "salesmen": [
                    {"fromTable": "salesmen", "fromColumn": "id", "toTable": "sales_orders", "toColumn": "salesman_id", "source": "database"},
                    {"fromTable": "salesmen", "fromColumn": "distributor_id", "toTable": "distributors", "toColumn": "id", "source": "database"},
                ],
                "distributors": [
                    {"fromTable": "distributors", "fromColumn": "id", "toTable": "salesmen", "toColumn": "distributor_id", "source": "database"}
                ],
            },
            "joinPathMemo": {},
        }

        path = service._resolve_join_path(graph_snapshot, "sales_order_items", "distributors")

        self.assertIsNotNone(path)
        self.assertEqual(path["fromTable"], "sales_order_items")
        self.assertEqual(path["toTable"], "distributors")
        self.assertEqual(len(path["edges"]), 3)

    def test_multihop_retrieval_builds_final_context(self):
        service = RetrievalService()
        events: list[tuple[str, dict]] = []
        latest_refresh = {"status": "completed", "completed_at": datetime.now(timezone.utc)}
        actions = [
            {
                "action": "search_catalog",
                "reason": "Find tables",
                "search_terms": ["revenue", "salesman"],
                "tables": [],
                "from_table": "",
                "to_table": "",
                "profile_targets": [],
            },
            {
                "action": "expand_tables",
                "reason": "Inspect tables",
                "search_terms": [],
                "tables": ["sales_orders"],
                "from_table": "",
                "to_table": "",
                "profile_targets": [],
            },
            {
                "action": "expand_graph_neighbors",
                "reason": "Expand neighbors",
                "search_terms": [],
                "tables": ["sales_orders"],
                "from_table": "",
                "to_table": "",
                "profile_targets": [],
            },
            {
                "action": "resolve_join_path",
                "reason": "Resolve joins",
                "search_terms": [],
                "tables": [],
                "from_table": "sales_orders",
                "to_table": "salesmen",
                "profile_targets": [],
            },
            {
                "action": "finalize_context",
                "reason": "Enough context",
                "search_terms": [],
                "tables": ["sales_orders", "salesmen"],
                "from_table": "",
                "to_table": "",
                "profile_targets": [],
            },
        ]

        search_tables = [
            {
                "table_name": "sales_orders",
                "description": "Orders",
                "tenant_column": "tenant_id",
                "date_columns": ["order_sale_date"],
                "metric_hints": ["revenue"],
                "dimension_hints": ["salesman"],
                "search_text": "sales orders revenue salesman",
            },
            {
                "table_name": "salesmen",
                "description": "Salesman master",
                "tenant_column": "tenant_id",
                "date_columns": [],
                "metric_hints": [],
                "dimension_hints": ["salesman"],
                "search_text": "salesmen master salesman",
            },
        ]
        search_columns = [
            {
                "table_name": "sales_orders",
                "column_name": "net_amount",
                "data_type": "numeric",
                "semantic_role": "metric",
                "search_text": "sales_orders net_amount revenue",
            },
            {
                "table_name": "sales_orders",
                "column_name": "salesman_id",
                "data_type": "bigint",
                "semantic_role": "identifier",
                "search_text": "sales_orders salesman_id salesman",
            },
            {
                "table_name": "salesmen",
                "column_name": "salesman_name",
                "data_type": "text",
                "semantic_role": "dimension",
                "search_text": "salesmen salesman_name salesman",
            },
        ]
        search_relationships = [
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
        graph_snapshot = {
            "refreshId": "refresh-1",
            "tenantId": "12",
            "tables": {
                "sales_orders": {
                    "tableName": "sales_orders",
                    "description": "Orders",
                    "tenantColumn": "tenant_id",
                    "dateColumns": ["order_sale_date"],
                    "metricHints": ["revenue"],
                    "dimensionHints": ["salesman"],
                },
                "salesmen": {
                    "tableName": "salesmen",
                    "description": "Salesman master",
                    "tenantColumn": "tenant_id",
                    "dateColumns": [],
                    "metricHints": [],
                    "dimensionHints": ["salesman"],
                },
            },
            "adjacency": {
                "sales_orders": [
                    {
                        "fromTable": "sales_orders",
                        "fromColumn": "salesman_id",
                        "toTable": "salesmen",
                        "toColumn": "id",
                        "relationshipType": "foreign_key",
                        "cardinality": "many_to_one",
                        "source": "database",
                    }
                ],
                "salesmen": [
                    {
                        "fromTable": "salesmen",
                        "fromColumn": "id",
                        "toTable": "sales_orders",
                        "toColumn": "salesman_id",
                        "relationshipType": "foreign_key",
                        "cardinality": "one_to_many",
                        "source": "database",
                    }
                ],
            },
            "aliasIndex": {"revenue": [{"tableName": "sales_orders", "objectName": "sales_orders", "weight": 4}]},
            "joinPathMemo": {},
        }
        list_tables = [
            {
                "table_name": "sales_orders",
                "display_name": "Sales Orders",
                "description": "Orders",
                "tenant_column": "tenant_id",
                "primary_key_columns": ["id"],
                "date_columns": ["order_sale_date"],
                "metric_hints": ["revenue"],
                "dimension_hints": ["salesman"],
            },
            {
                "table_name": "salesmen",
                "display_name": "Salesmen",
                "description": "Salesman master",
                "tenant_column": "tenant_id",
                "primary_key_columns": ["id"],
                "date_columns": [],
                "metric_hints": [],
                "dimension_hints": ["salesman"],
            },
        ]
        list_columns = [
            {
                "table_name": "sales_orders",
                "column_name": "net_amount",
                "data_type": "numeric",
                "semantic_role": "metric",
                "references_table": None,
                "references_column": None,
            },
            {
                "table_name": "sales_orders",
                "column_name": "salesman_id",
                "data_type": "bigint",
                "semantic_role": "identifier",
                "references_table": "salesmen",
                "references_column": "id",
            },
            {
                "table_name": "sales_orders",
                "column_name": "order_sale_date",
                "data_type": "date",
                "semantic_role": "date",
                "references_table": None,
                "references_column": None,
            },
            {
                "table_name": "salesmen",
                "column_name": "salesman_name",
                "data_type": "text",
                "semantic_role": "dimension",
                "references_table": None,
                "references_column": None,
            },
        ]

        with patch("supe_ask.services.retrieval.repository.count_catalog_tables", return_value=2), patch(
            "supe_ask.services.retrieval.repository.get_latest_catalog_refresh", return_value=latest_refresh
        ), patch(
            "supe_ask.services.retrieval.graph_cache_service.load_snapshot", return_value=graph_snapshot
        ), patch("supe_ask.services.retrieval.llm_service.plan_next_retrieval_action", side_effect=actions), patch(
            "supe_ask.services.retrieval.repository.search_catalog_tables", return_value=search_tables
        ), patch(
            "supe_ask.services.retrieval.repository.search_catalog_columns", return_value=search_columns
        ), patch(
            "supe_ask.services.retrieval.repository.search_catalog_aliases", return_value=[]
        ), patch(
            "supe_ask.services.retrieval.repository.search_catalog_relationships", return_value=search_relationships
        ), patch(
            "supe_ask.services.retrieval.repository.list_catalog_tables", return_value=list_tables
        ), patch(
            "supe_ask.services.retrieval.repository.list_catalog_columns", return_value=list_columns
        ), patch(
            "supe_ask.services.retrieval.repository.list_catalog_relationships", return_value=search_relationships
        ):
            result = service._plan_and_retrieve_from_catalog("12", "Compare revenue by salesman", lambda event_type, payload: events.append((event_type, payload)))

        self.assertEqual(result["strategy"], "catalog_graph_multihop")
        self.assertEqual([item["tableName"] for item in result["finalContext"]["relevantTables"]], ["sales_orders", "salesmen"])
        self.assertEqual(len(result["joinPaths"]), 1)
        self.assertEqual(result["finalContext"]["graphNeighborhood"]["neighborTables"], ["salesmen"])
        self.assertTrue(any(event_type == "run.retrieval.action" for event_type, _ in events))
        self.assertTrue(any(step["action"] == "finalize_context" for step in result["steps"]))


if __name__ == "__main__":
    unittest.main()
