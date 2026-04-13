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

    def test_semantic_retrieval_builds_final_context(self):
        service = RetrievalService()
        events: list[tuple[str, dict]] = []
        latest_refresh = {"status": "completed", "completed_at": datetime.now(timezone.utc)}
        semantic_version = {
            "status": "completed",
            "cluster_count": 1,
            "canonical_question_count": 1,
            "variant_count": 1,
            "entity_count": 1,
            "metric_count": 1,
        }
        grounding = {
            "reasoning": "Revenue by salesman maps to the revenue cluster.",
            "canonical_question_number": 1,
            "cluster_key": "revenue_billing_performance",
            "intent": "compare",
            "matched_entities": ["salesman"],
            "matched_metrics": ["revenue"],
            "matched_time_grain": "mtd",
            "filters": [],
            "grouping": ["salesman"],
            "outputs": ["table"],
            "confidence": 0.93,
            "fallback_used": False,
        }

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
        semantic_clusters = [
            {
                "id": "cluster-1",
                "cluster_key": "revenue_billing_performance",
                "title": "Revenue & Billing Performance",
                "description": "Revenue questions",
            }
        ]
        canonical_questions = [
            {
                "id": "question-1",
                "cluster_key": "revenue_billing_performance",
                "question_number": 1,
                "canonical_question": "What is my total secondary revenue MTD?",
                "primary_entity": "Salesman",
            }
        ]
        semantic_entities = [{"entity_key": "salesman", "display_name": "Salesman"}]
        semantic_metrics = [{"metric_key": "revenue", "display_name": "Revenue"}]
        semantic_metric_aliases = [{"metric_key": "revenue", "alias": "billing", "weight": 3}]
        semantic_date_policies = [{"policy_key": "wall_clock_primary_refresh-1", "date_column": "order_sale_date", "time_grains": ["mtd", "monthly"]}]
        semantic_threshold_policies = [{"policy_key": "revenue_alert", "metric_key": "revenue", "threshold_name": "low", "comparator": "<"}]

        with patch("supe_ask.services.retrieval.repository.count_catalog_tables", return_value=2), patch(
            "supe_ask.services.retrieval.repository.get_latest_catalog_refresh", return_value=latest_refresh
        ), patch(
            "supe_ask.services.retrieval.repository.get_latest_semantic_pack_version", return_value=semantic_version
        ), patch(
            "supe_ask.services.retrieval.graph_cache_service.load_snapshot", return_value=graph_snapshot
        ), patch("supe_ask.services.retrieval.llm_service.resolve_question_grounding", return_value=grounding), patch(
            "supe_ask.services.retrieval.repository.search_question_clusters", return_value=semantic_clusters
        ), patch(
            "supe_ask.services.retrieval.repository.search_canonical_questions", return_value=canonical_questions
        ), patch(
            "supe_ask.services.retrieval.repository.search_question_variants", return_value=[]
        ), patch(
            "supe_ask.services.retrieval.repository.search_entities", return_value=semantic_entities
        ), patch(
            "supe_ask.services.retrieval.repository.search_metrics", return_value=semantic_metrics
        ), patch(
            "supe_ask.services.retrieval.repository.search_metric_aliases", return_value=semantic_metric_aliases
        ), patch(
            "supe_ask.services.retrieval.repository.list_date_policies", return_value=semantic_date_policies
        ), patch(
            "supe_ask.services.retrieval.repository.list_threshold_policies", return_value=semantic_threshold_policies
        ), patch(
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
            result = service.plan_and_retrieve("12", "Compare revenue by salesman", lambda event_type, payload: events.append((event_type, payload)))

        self.assertEqual(result["strategy"], "semantic_catalog_graph")
        self.assertEqual([item["tableName"] for item in result["finalContext"]["relevantTables"]], ["sales_orders", "salesmen"])
        self.assertEqual(len(result["joinPaths"]), 1)
        self.assertEqual(result["finalContext"]["graphNeighborhood"]["neighborTables"], [])
        self.assertEqual(result["questionGrounding"]["canonical_question_id"], "question-1")
        self.assertEqual(result["finalContext"]["semanticPolicies"]["datePolicies"], semantic_date_policies)
        self.assertEqual(result["finalContext"]["semanticPolicies"]["thresholdPolicies"], semantic_threshold_policies)
        self.assertEqual(result["finalContext"]["semanticPolicies"]["metricAliases"], semantic_metric_aliases)
        self.assertTrue(any(event_type == "run.retrieval.grounding.completed" for event_type, _ in events))

    def test_prefers_sales_orders_over_entity_metric_snapshots_for_metric_questions(self):
        service = RetrievalService()
        ranked = service._prefer_fact_tables(
            "What is my total secondary revenue MTD?",
            {"intent": "summary", "matched_metrics": ["revenue"]},
            [
                {
                    "tableName": "entity_metric_snapshots",
                    "score": 20.0,
                    "reasons": ["table metadata match"],
                },
                {
                    "tableName": "sales_orders",
                    "score": 10.0,
                    "reasons": ["table metadata match"],
                },
                {
                    "tableName": "sales_order_items",
                    "score": 9.0,
                    "reasons": ["column match"],
                },
            ],
        )

        ranked.sort(key=lambda item: (item["score"], item["tableName"]), reverse=True)
        self.assertEqual(ranked[0]["tableName"], "sales_orders")
        self.assertNotEqual(ranked[0]["tableName"], "entity_metric_snapshots")


if __name__ == "__main__":
    unittest.main()
