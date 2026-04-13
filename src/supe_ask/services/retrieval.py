"""Deterministic semantic retrieval for Ask.

This module resolves a question against DB-backed semantic metadata, grounds it
with one LLM semantic-resolution call, then uses the resident graph cache to
assemble the minimum code-generation context.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..config import settings
from ..repository import repository
from .graph_cache import graph_cache_service


EventHandler = Callable[[str, dict[str, Any]], None] | None

DERIVED_SNAPSHOT_TABLES = {"entity_metric_snapshots", "target_progress_snapshots"}
RAW_FACT_TABLES = {"sales_orders", "sales_order_items", "order_payments"}


def _tokenize(text: str) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9_]+", text.lower()) if len(token) > 1]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)



class CatalogUnavailableError(RuntimeError):
    pass


class RetrievalService:
    def plan_and_retrieve(
        self,
        tenant_id: str,
        question: str,
        event_handler: EventHandler = None,
    ) -> dict[str, Any]:
        return self._resolve_from_semantic_catalog(tenant_id, question, event_handler)

    def _emit(self, event_handler: EventHandler, event_type: str, payload: dict[str, Any]) -> None:
        if event_handler:
            event_handler(event_type, payload)

    def _catalog_status(self, tenant_id: str) -> dict[str, Any]:
        latest = repository.get_latest_catalog_refresh(tenant_id)
        table_count = repository.count_catalog_tables(tenant_id)
        semantic_version = repository.get_latest_semantic_pack_version(tenant_id)
        available = bool(latest and latest.get("status") == "completed" and table_count > 0 and semantic_version)
        stale = False
        if latest and latest.get("completed_at"):
            age = _utc_now() - latest["completed_at"]
            stale = age > timedelta(seconds=settings.schema_refresh_interval_seconds)
        return {
            "available": available,
            "stale": stale,
            "tableCount": table_count,
            "latestRefresh": latest,
            "latestSemanticPackVersion": semantic_version,
        }

    def _resolve_from_semantic_catalog(
        self,
        tenant_id: str,
        question: str,
        event_handler: EventHandler = None,
    ) -> dict[str, Any]:
        status = self._catalog_status(tenant_id)
        if not status["available"]:
            raise CatalogUnavailableError("Semantic catalog is not available for this tenant")

        latest_refresh = status["latestRefresh"]
        graph_snapshot = graph_cache_service.load_snapshot(tenant_id, latest_refresh)
        if not graph_snapshot:
            raise CatalogUnavailableError("Schema graph snapshot is not available for this tenant")

        tokens = _tokenize(question)[:12]
        semantic_candidates = self._semantic_candidates(tenant_id, tokens)
        self._emit(
            event_handler,
            "run.retrieval.grounding.started",
            {
                "question": question,
                "tokenCount": len(tokens),
                "clusterCandidates": len(semantic_candidates["clusters"]),
                "questionCandidates": len(semantic_candidates["canonicalQuestions"]),
            },
        )

        grounding = self._enrich_grounding(
            self._classify_question_deterministically(question, semantic_candidates),
            semantic_candidates,
        )
        self._emit(
            event_handler,
            "run.retrieval.grounding.completed",
            {
                "clusterKey": grounding.get("cluster_key"),
                "canonicalQuestionNumber": grounding.get("canonical_question_number"),
                "matchedEntities": grounding.get("matched_entities") or [],
                "matchedMetrics": grounding.get("matched_metrics") or [],
                "confidence": grounding.get("confidence"),
                "fallbackUsed": grounding.get("fallback_used"),
            },
        )

        catalog_terms = self._catalog_terms(question, semantic_candidates, grounding)
        candidate_tables = self._search_catalog(tenant_id, question, catalog_terms, graph_snapshot, grounding)
        seed_tables = [candidate["tableName"] for candidate in candidate_tables[:2]]
        expanded_tables = self._expand_tables(tenant_id, [candidate["tableName"] for candidate in candidate_tables[:4]], candidate_tables)
        graph_neighborhood = self._expand_graph_neighbors(
            tenant_id,
            graph_snapshot,
            seed_tables,
            expanded_tables,
        )
        expanded_tables = self._merge_tables(expanded_tables, graph_neighborhood["expandedTables"])
        selected_columns = self._collect_selected_columns(expanded_tables)
        join_paths = self._build_join_paths(graph_snapshot, [table["tableName"] for table in expanded_tables])
        analysis_plan = self._build_analysis_plan(tenant_id, question, grounding, expanded_tables, join_paths)
        semantic_policies = {
            "datePolicies": semantic_candidates.get("datePolicies") or [],
            "thresholdPolicies": semantic_candidates.get("thresholdPolicies") or [],
            "metricAliases": semantic_candidates.get("metricAliases") or [],
        }
        final_context = self._build_final_context(
            question,
            grounding,
            analysis_plan,
            expanded_tables,
            selected_columns,
            join_paths,
            graph_neighborhood["graphNeighborhood"],
            semantic_policies,
        )

        self._emit(
            event_handler,
            "run.retrieval.completed",
            {
                "strategy": "semantic_catalog_graph",
                "tableCount": len(final_context["relevantTables"]),
                "joinPathCount": len(join_paths),
                "selectedTables": [table["tableName"] for table in expanded_tables],
            },
        )

        return {
            "strategy": "semantic_catalog_graph",
            "catalogStatus": status,
            "semanticCandidates": semantic_candidates,
            "questionGrounding": grounding,
            "analysisPlan": analysis_plan,
            "selectedTables": [table["tableName"] for table in expanded_tables],
            "selectedColumns": selected_columns,
            "joinPaths": join_paths,
            "fallbackUsed": bool(grounding.get("fallback_used")),
            "confidence": grounding.get("confidence"),
            "finalContext": final_context,
        }

    def _detect_time_grain(self, question_lower: str) -> str:
        if re.search(r"\b(this\s+month|month\s+to\s+date|mtd)\b", question_lower):
            return "mtd"
        if re.search(r"\b(this\s+quarter|quarter\s+to\s+date|qtd)\b", question_lower):
            return "qtd"
        if re.search(r"\b(this\s+year|year\s+to\s+date|ytd)\b", question_lower):
            return "ytd"
        if re.search(r"\blast\s+7\s+days?\b", question_lower):
            return "last_7_days"
        if re.search(r"\blast\s+30\s+days?\b", question_lower):
            return "last_30_days"
        if re.search(r"\blast\s+90\s+days?\b", question_lower):
            return "last_90_days"
        m = re.search(r"\blast\s+(\d+)\s+days?\b", question_lower)
        if m:
            return f"last_{m.group(1)}_days"
        if re.search(r"\b(weekly|this\s+week|last\s+week)\b", question_lower):
            return "last_7_days"
        return "mtd"

    def _detect_intent(self, question_lower: str) -> str:
        if re.search(r"\b(trend|over\s+time|by\s+(day|week|month|date)|daily|weekly|monthly|growth|trajectory)\b", question_lower):
            return "trend"
        if re.search(r"\b(top|bottom|rank|ranking|best|worst|highest|lowest|leading|lagging)\b", question_lower):
            return "rank"
        if re.search(r"\b(compare|vs\.?|versus|against|difference|gap|vs\b)\b", question_lower):
            return "compare"
        if re.search(r"\b(breakdown|split|distribution|segment|by\s+\w+|category|categories)\b", question_lower):
            return "analysis"
        return "summary"

    def _classify_question_deterministically(
        self,
        question: str,
        semantic_candidates: dict[str, Any],
    ) -> dict[str, Any]:
        """Deterministic intent classifier — replaces the Gemini grounding LLM call.

        Scores semantic candidates using token overlap and regex pattern matching.
        No network calls, no latency, identical grounding shape to what the LLM returned.
        """
        question_lower = question.lower()
        question_tokens = set(_tokenize(question))

        time_grain = self._detect_time_grain(question_lower)
        intent = self._detect_intent(question_lower)

        # --- Score clusters by token overlap ---
        best_cluster_key = ""
        best_cluster_score = 0.0
        for cluster in semantic_candidates.get("clusters") or []:
            cluster_key = str(cluster.get("cluster_key") or "")
            overlap = len(question_tokens & set(_tokenize(cluster_key)))
            if overlap > best_cluster_score:
                best_cluster_score = float(overlap)
                best_cluster_key = cluster_key

        # --- Score canonical questions and variants by token overlap ---
        best_canonical_number = None
        best_canonical_score = 0.0
        for row in semantic_candidates.get("canonicalQuestions") or []:
            cq_text = str(row.get("canonical_question") or "")
            overlap = len(question_tokens & set(_tokenize(cq_text)))
            if overlap > best_canonical_score:
                best_canonical_score = float(overlap)
                best_canonical_number = row.get("question_number")
                if not best_cluster_key:
                    best_cluster_key = str(row.get("cluster_key") or "")
        for row in semantic_candidates.get("questionVariants") or []:
            variant_text = str(row.get("variant_text") or "")
            overlap = len(question_tokens & set(_tokenize(variant_text)))
            if overlap > best_canonical_score:
                best_canonical_score = float(overlap)
                best_canonical_number = row.get("canonical_question_number")

        # --- Match entities ---
        matched_entities: list[str] = []
        for entity in semantic_candidates.get("entities") or []:
            entity_key = str(entity.get("entity_key") or entity.get("name") or "")
            if question_tokens & set(_tokenize(entity_key)):
                matched_entities.append(entity_key)

        # --- Match metrics (keys + aliases) ---
        matched_metrics: list[str] = []
        seen_metrics: set[str] = set()
        for metric in semantic_candidates.get("metrics") or []:
            metric_key = str(metric.get("metric_key") or metric.get("name") or "")
            if (question_tokens & set(_tokenize(metric_key))) and metric_key not in seen_metrics:
                matched_metrics.append(metric_key)
                seen_metrics.add(metric_key)
        for alias_row in semantic_candidates.get("metricAliases") or []:
            alias = str(alias_row.get("alias") or "")
            canonical = str(alias_row.get("metric_key") or alias_row.get("canonical_metric") or alias)
            if (question_tokens & set(_tokenize(alias))) and canonical not in seen_metrics:
                matched_metrics.append(canonical)
                seen_metrics.add(canonical)

        # --- Grouping hints (by X, per X, breakdown by X) ---
        grouping: list[str] = []
        for pattern in (
            r"\bby\s+(\w+(?:\s+\w+)?)",
            r"\bper\s+(\w+(?:\s+\w+)?)",
            r"\bbreakdown\s+by\s+(\w+(?:\s+\w+)?)",
            r"\bgroup(?:ed)?\s+by\s+(\w+(?:\s+\w+)?)",
        ):
            for m in re.finditer(pattern, question_lower):
                grouping.append(m.group(1).strip())

        # --- Filter hints (for X, in region X) ---
        filters: list[str] = []
        for pattern in (
            r"\bfor\s+([\w\s]+?)(?:\s+in\b|\s+by\b|\s+this\b|\s+last\b|$)",
            r"\bin\s+([\w\s]+?)(?:\s+by\b|\s+for\b|\s+this\b|$)",
        ):
            for m in re.finditer(pattern, question_lower):
                candidate = m.group(1).strip()
                if len(candidate) > 1 and candidate not in {"the", "a", "an", "my"}:
                    filters.append(candidate)

        # --- Outputs by intent ---
        outputs_map = {
            "trend": ["trend_chart", "trend_table"],
            "rank": ["ranking_table", "bar_chart"],
            "compare": ["comparison_table", "bar_chart"],
            "analysis": ["kpi_cards", "breakdown_table", "bar_chart"],
            "summary": ["kpi_cards", "trend_chart", "breakdown_table"],
        }
        outputs = outputs_map.get(intent, ["table", "summary"])

        total_score = best_cluster_score + best_canonical_score + len(matched_metrics) * 0.5 + len(matched_entities) * 0.3
        confidence = round(min(0.95, max(0.4, total_score / 8.0)), 3)
        fallback_used = total_score < 2.0

        return {
            "reasoning": (
                f"Deterministic classification: cluster_score={best_cluster_score:.1f}, "
                f"canonical_score={best_canonical_score:.1f}, metrics={matched_metrics!r}, "
                f"time_grain={time_grain}, intent={intent}"
            ),
            "canonical_question_number": best_canonical_number,
            "cluster_key": best_cluster_key,
            "intent": intent,
            "matched_entities": matched_entities[:6],
            "matched_metrics": matched_metrics[:8],
            "matched_time_grain": time_grain,
            "filters": filters[:4],
            "grouping": grouping[:4],
            "outputs": outputs,
            "confidence": confidence,
            "fallback_used": fallback_used,
        }

    def _enrich_grounding(self, grounding: dict[str, Any], semantic_candidates: dict[str, Any]) -> dict[str, Any]:
        cluster_key = str(grounding.get("cluster_key") or "").strip().lower()
        canonical_number = grounding.get("canonical_question_number")
        cluster_id = None
        canonical_question_id = None
        canonical_question = ""
        for cluster in semantic_candidates.get("clusters") or []:
            if str(cluster.get("cluster_key") or "").strip().lower() == cluster_key:
                cluster_id = cluster.get("id")
                break
        if canonical_number is not None:
            try:
                target_number = int(canonical_number)
            except Exception:
                target_number = None
            if target_number is not None:
                for question in semantic_candidates.get("canonicalQuestions") or []:
                    if int(question.get("question_number") or -1) == target_number:
                        canonical_question_id = question.get("id")
                        canonical_question = str(question.get("canonical_question") or "")
                        if not cluster_key:
                            cluster_key = str(question.get("cluster_key") or "")
                        break
        grounding["cluster_key"] = cluster_key
        grounding["cluster_id"] = cluster_id
        grounding["canonical_question_id"] = canonical_question_id
        grounding["canonical_question"] = canonical_question
        return grounding

    def _semantic_candidates(self, tenant_id: str, terms: list[str]) -> dict[str, Any]:
        clusters = repository.search_question_clusters(tenant_id, terms)
        canonical_questions = repository.search_canonical_questions(tenant_id, terms)
        variants = repository.search_question_variants(tenant_id, terms)
        entities = repository.search_entities(tenant_id, terms)
        metrics = repository.search_metrics(tenant_id, terms)
        metric_aliases = repository.search_metric_aliases(tenant_id, terms)
        date_policies = repository.list_date_policies(tenant_id)
        threshold_policies = repository.list_threshold_policies(tenant_id)
        semantic_pack = repository.get_latest_semantic_pack_version(tenant_id)
        return {
            "summary": {
                "clusterCount": int(semantic_pack.get("cluster_count") or 0) if semantic_pack else 0,
                "canonicalQuestionCount": int(semantic_pack.get("canonical_question_count") or 0) if semantic_pack else 0,
                "variantCount": int(semantic_pack.get("variant_count") or 0) if semantic_pack else 0,
                "entityCount": int(semantic_pack.get("entity_count") or 0) if semantic_pack else 0,
                "metricCount": int(semantic_pack.get("metric_count") or 0) if semantic_pack else 0,
            },
            "clusters": clusters,
            "canonicalQuestions": canonical_questions,
            "questionVariants": variants,
            "entities": entities,
            "metrics": metrics,
            "metricAliases": metric_aliases,
            "datePolicies": date_policies,
            "thresholdPolicies": threshold_policies,
        }

    def _catalog_terms(
        self,
        question: str,
        semantic_candidates: dict[str, Any],
        grounding: dict[str, Any],
    ) -> list[str]:
        terms = set(_tokenize(question))
        for key in grounding.get("matched_entities") or []:
            terms.update(_tokenize(str(key)))
        for key in grounding.get("matched_metrics") or []:
            terms.update(_tokenize(str(key)))
        canonical_number = grounding.get("canonical_question_number")
        if canonical_number is not None:
            for row in semantic_candidates.get("canonicalQuestions") or []:
                if int(row.get("question_number") or -1) == int(canonical_number):
                    terms.update(_tokenize(str(row.get("canonical_question") or "")))
        return list(terms)[:12]

    def _search_catalog(
        self,
        tenant_id: str,
        question: str,
        search_terms: list[str],
        graph_snapshot: dict[str, Any],
        grounding: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        table_rows = repository.search_catalog_tables(tenant_id, search_terms)
        column_rows = repository.search_catalog_columns(tenant_id, search_terms)
        alias_rows = repository.search_catalog_aliases(tenant_id, search_terms)
        relationship_rows = repository.search_catalog_relationships(tenant_id, search_terms)

        candidates: dict[str, dict[str, Any]] = {}
        question_lower = question.lower()

        def ensure_candidate(table_name: str) -> dict[str, Any]:
            candidate = candidates.get(table_name)
            if candidate:
                return candidate
            candidate = {"tableName": table_name, "score": 0.0, "matchedColumns": [], "matchedAliases": [], "reasons": []}
            candidates[table_name] = candidate
            return candidate

        for row in table_rows:
            table_name = str(row["table_name"])
            candidate = ensure_candidate(table_name)
            search_text = str(row.get("search_text") or "")
            hits = sum(1 for term in search_terms if term in search_text)
            candidate["score"] += hits * 4 + 2
            if table_name.replace("_", " ") in question_lower:
                candidate["score"] += 3
            candidate["description"] = row.get("description")
            candidate["tenantColumn"] = row.get("tenant_column")
            candidate["dateColumns"] = list(row.get("date_columns") or [])
            candidate["metricHints"] = list(row.get("metric_hints") or [])
            candidate["dimensionHints"] = list(row.get("dimension_hints") or [])
            candidate["reasons"].append("table metadata match")

        for row in alias_rows:
            table_name = str(row.get("table_name") or row.get("object_name") or "")
            if not table_name:
                continue
            candidate = ensure_candidate(table_name)
            alias = str(row.get("alias") or "")
            weight = int(row.get("weight") or 1)
            hits = sum(1 for term in search_terms if term in alias.lower())
            candidate["score"] += max(1, hits) * (weight + 1)
            candidate["matchedAliases"].append(alias)
            candidate["reasons"].append("alias match")

        alias_index = graph_snapshot.get("aliasIndex") or {}
        for term in search_terms:
            for hit in alias_index.get(str(term).lower(), []):
                table_name = str(hit.get("tableName") or "")
                if not table_name:
                    continue
                candidate = ensure_candidate(table_name)
                candidate["score"] += int(hit.get("weight") or 1) + 0.75
                candidate["matchedAliases"].append(str(hit.get("objectName") or table_name))
                candidate["reasons"].append("graph alias match")

        for row in column_rows:
            table_name = str(row["table_name"])
            candidate = ensure_candidate(table_name)
            column_name = str(row["column_name"])
            search_text = str(row.get("search_text") or "")
            hits = sum(1 for term in search_terms if term in search_text or term in column_name.lower())
            candidate["score"] += hits * 2.5 + 0.5
            candidate["matchedColumns"].append(column_name)
            candidate["reasons"].append("column match")

        for row in relationship_rows:
            for key in ("from_table", "to_table"):
                table_name = str(row.get(key) or "")
                if table_name:
                    candidate = ensure_candidate(table_name)
                    candidate["score"] += 1
                    candidate["reasons"].append("relationship match")

        ranked = []
        for candidate in candidates.values():
            ranked.append(
                {
                    "tableName": candidate["tableName"],
                    "score": round(candidate["score"], 4),
                    "matchedColumns": sorted(set(candidate["matchedColumns"]))[:8],
                    "matchedAliases": sorted(set(candidate["matchedAliases"]))[:8],
                    "reasons": sorted(set(candidate["reasons"]))[:6],
                    "description": candidate.get("description"),
                    "tenantColumn": candidate.get("tenantColumn"),
                    "dateColumns": candidate.get("dateColumns") or [],
                    "metricHints": candidate.get("metricHints") or [],
                    "dimensionHints": candidate.get("dimensionHints") or [],
                }
            )
        ranked = self._prefer_fact_tables(question, grounding or {}, ranked)
        ranked.sort(key=lambda item: (item["score"], item["tableName"]), reverse=True)
        return ranked[:8]

    def _prefer_fact_tables(
        self,
        question: str,
        grounding: dict[str, Any],
        ranked: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        question_lower = question.lower()
        explicit_snapshot_request = any(
            token in question_lower
            for token in (
                "snapshot",
                "snapshots",
                "precalculated",
                "pre-calculated",
                "stored kpi",
                "signal",
                "signals",
                "target progress",
            )
        )
        metric_question = bool(grounding.get("matched_metrics")) or str(grounding.get("intent") or "").lower() in {
            "summary",
            "analysis",
            "compare",
            "trend",
            "rank",
        }
        available_tables = {item["tableName"] for item in ranked}
        has_raw_facts = bool(available_tables & RAW_FACT_TABLES)
        if explicit_snapshot_request or not metric_question or not has_raw_facts:
            return ranked

        adjusted: list[dict[str, Any]] = []
        for item in ranked:
            updated = dict(item)
            reasons = list(updated.get("reasons") or [])
            table_name = str(updated.get("tableName") or "")
            if table_name in DERIVED_SNAPSHOT_TABLES:
                updated["score"] = float(updated.get("score") or 0) - 12
                reasons.append("derived snapshot deprioritized for recalculated metric analysis")
            elif table_name in RAW_FACT_TABLES:
                updated["score"] = float(updated.get("score") or 0) + 6
                reasons.append("raw fact table preferred for recalculated metric analysis")
            updated["reasons"] = sorted(set(reasons))[:8]
            adjusted.append(updated)
        return adjusted

    def _expand_tables(self, tenant_id: str, table_names: list[str], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not table_names:
            return []
        candidate_map = {item["tableName"]: item for item in candidates}
        tables = {row["table_name"]: row for row in repository.list_catalog_tables(tenant_id, table_names)}
        columns_by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in repository.list_catalog_columns(tenant_id, table_names):
            columns_by_table[str(row["table_name"])].append(row)
        relationships_by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in repository.list_catalog_relationships(tenant_id, table_names):
            relationships_by_table[str(row["from_table"])].append(row)
            relationships_by_table[str(row["to_table"])].append(row)

        expanded: list[dict[str, Any]] = []
        for table_name in table_names:
            row = tables.get(table_name)
            if not row:
                continue
            candidate = candidate_map.get(table_name, {})
            matched_columns = set(candidate.get("matchedColumns") or [])
            candidate_columns = []
            for column in columns_by_table.get(table_name, []):
                column_name = str(column["column_name"])
                if (
                    column_name in matched_columns
                    or column.get("semantic_role") in {"date", "metric", "dimension"}
                    or column_name == row.get("tenant_column")
                ):
                    candidate_columns.append(
                        {
                            "tableName": table_name,
                            "columnName": column_name,
                            "dataType": column.get("data_type"),
                            "semanticRole": column.get("semantic_role"),
                            "referencesTable": column.get("references_table"),
                            "referencesColumn": column.get("references_column"),
                        }
                    )
            expanded.append(
                {
                    "tableName": table_name,
                    "description": row.get("description"),
                    "displayName": row.get("display_name"),
                    "tenantColumn": row.get("tenant_column"),
                    "primaryKeyColumns": list(row.get("primary_key_columns") or []),
                    "dateColumns": list(row.get("date_columns") or []),
                    "metricHints": list(row.get("metric_hints") or []),
                    "dimensionHints": list(row.get("dimension_hints") or []),
                    "matchedScore": candidate.get("score", 0),
                    "matchedColumns": list(candidate.get("matchedColumns") or []),
                    "matchedAliases": list(candidate.get("matchedAliases") or []),
                    "candidateColumns": candidate_columns[:12],
                    "relationships": [
                        {
                            "fromTable": relationship.get("from_table"),
                            "fromColumn": relationship.get("from_column"),
                            "toTable": relationship.get("to_table"),
                            "toColumn": relationship.get("to_column"),
                            "source": relationship.get("source"),
                        }
                        for relationship in relationships_by_table.get(table_name, [])[:8]
                    ],
                }
            )
        return expanded

    def _expand_graph_neighbors(
        self,
        tenant_id: str,
        graph_snapshot: dict[str, Any],
        seed_tables: list[str],
        expanded_tables: list[dict[str, Any]],
    ) -> dict[str, Any]:
        tables_map = graph_snapshot.get("tables") or {}
        adjacency = graph_snapshot.get("adjacency") or {}
        expanded_names = {str(item.get("tableName") or "") for item in expanded_tables}
        seeds = [table for table in seed_tables if table in tables_map] or [item for item in expanded_names if item]
        neighbor_candidates: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        seen_edges: set[tuple[str, str, str, str]] = set()

        for seed in seeds[:3]:
            for edge in adjacency.get(seed, [])[:10]:
                edge_key = (
                    str(edge.get("fromTable") or ""),
                    str(edge.get("fromColumn") or ""),
                    str(edge.get("toTable") or ""),
                    str(edge.get("toColumn") or ""),
                )
                if edge_key not in seen_edges:
                    seen_edges.add(edge_key)
                    edges.append(edge)
                neighbor_table = str(edge.get("toTable") or "")
                if not neighbor_table or neighbor_table in expanded_names or neighbor_table in seeds:
                    continue
                table_meta = dict(tables_map.get(neighbor_table) or {})
                if not table_meta:
                    continue
                candidate = neighbor_candidates.setdefault(
                    neighbor_table,
                    {
                        "tableName": neighbor_table,
                        "score": 0.0,
                        "matchedColumns": [],
                        "matchedAliases": [],
                        "reasons": ["graph neighbor"],
                        "description": table_meta.get("description"),
                        "tenantColumn": table_meta.get("tenantColumn"),
                        "dateColumns": list(table_meta.get("dateColumns") or []),
                        "metricHints": list(table_meta.get("metricHints") or []),
                        "dimensionHints": list(table_meta.get("dimensionHints") or []),
                    },
                )
                candidate["score"] += 2.0 if str(edge.get("source") or "database") == "database" else 1.5

        ranked_neighbors = sorted(neighbor_candidates.values(), key=lambda item: (item["score"], item["tableName"]), reverse=True)[:2]
        expanded = self._expand_tables(tenant_id, [item["tableName"] for item in ranked_neighbors], ranked_neighbors)
        return {
            "expandedTables": expanded,
            "graphNeighborhood": {
                "seedTables": seeds[:4],
                "neighborTables": [item["tableName"] for item in ranked_neighbors],
                "edges": edges[:16],
            },
        }

    def _resolve_join_path(self, graph_snapshot: dict[str, Any], from_table: str, to_table: str) -> dict[str, Any] | None:
        if not from_table or not to_table or from_table == to_table:
            return None
        join_path_memo = graph_snapshot.setdefault("joinPathMemo", {})
        memo_key = f"{from_table}->{to_table}"
        if memo_key in join_path_memo:
            return join_path_memo[memo_key]

        graph = graph_snapshot.get("adjacency") or {}
        queue: deque[tuple[str, list[dict[str, Any]]]] = deque([(from_table, [])])
        visited = {from_table}
        while queue:
            table, path = queue.popleft()
            if len(path) >= 3:
                continue
            for edge in graph.get(table, []):
                next_table = str(edge.get("toTable") or "")
                next_path = [*path, edge]
                if next_table == to_table:
                    result = {"fromTable": from_table, "toTable": to_table, "edges": next_path}
                    join_path_memo[memo_key] = result
                    return result
                if next_table and next_table not in visited:
                    visited.add(next_table)
                    queue.append((next_table, next_path))
        return None

    def _build_join_paths(self, graph_snapshot: dict[str, Any], table_names: list[str]) -> list[dict[str, Any]]:
        join_paths: list[dict[str, Any]] = []
        if not table_names:
            return join_paths
        anchor = table_names[0]
        seen: set[tuple[str, str]] = set()
        for table_name in table_names[1:]:
            path = self._resolve_join_path(graph_snapshot, anchor, table_name)
            if not path:
                path = self._resolve_join_path(graph_snapshot, table_name, anchor)
            if not path:
                continue
            key = (str(path["fromTable"]), str(path["toTable"]))
            if key in seen:
                continue
            seen.add(key)
            join_paths.append(path)
        return join_paths

    def _build_analysis_plan(
        self,
        tenant_id: str,
        question: str,
        grounding: dict[str, Any],
        expanded_tables: list[dict[str, Any]],
        join_paths: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "tenantId": str(tenant_id),
            "question": question,
            "canonicalQuestionId": grounding.get("canonical_question_id"),
            "canonicalQuestionNumber": grounding.get("canonical_question_number"),
            "clusterId": grounding.get("cluster_id"),
            "clusterKey": grounding.get("cluster_key"),
            "intent": grounding.get("intent") or "analysis",
            "matchedEntities": grounding.get("matched_entities") or [],
            "matchedMetrics": grounding.get("matched_metrics") or [],
            "matchedTimeGrain": grounding.get("matched_time_grain") or "mtd",
            "filters": grounding.get("filters") or [],
            "grouping": grounding.get("grouping") or [],
            "outputs": grounding.get("outputs") or [],
            "selectedTables": [table["tableName"] for table in expanded_tables],
            "joinPaths": join_paths,
            "fallbackUsed": bool(grounding.get("fallback_used")),
            "confidence": float(grounding.get("confidence") or 0),
        }

    def _build_final_context(
        self,
        question: str,
        grounding: dict[str, Any],
        analysis_plan: dict[str, Any],
        relevant_tables: list[dict[str, Any]],
        relevant_columns: list[dict[str, Any]],
        join_paths: list[dict[str, Any]],
        graph_neighborhood: dict[str, Any],
        semantic_policies: dict[str, Any],
    ) -> dict[str, Any]:
        business_hints = []
        for table in relevant_tables:
            for hint in table.get("metricHints") or []:
                business_hints.append(f"{table['tableName']} metric hint: {hint}")
            for hint in table.get("dimensionHints") or []:
                business_hints.append(f"{table['tableName']} dimension hint: {hint}")
        return {
            "question": question,
            "questionGrounding": grounding,
            "analysisPlan": analysis_plan,
            "relevantTables": relevant_tables[:6],
            "relevantColumns": relevant_columns[:30],
            "joinPaths": join_paths,
            "graphNeighborhood": graph_neighborhood,
            "semanticPolicies": semantic_policies,
            "businessHints": sorted(set(business_hints))[:30],
            "queryGuardrails": {
                "tenantFilterToken": "{{tenant_filter}}",
                "readOnly": True,
                "preferProvidedJoinPaths": True,
                "blockedTables": sorted(DERIVED_SNAPSHOT_TABLES),
                "preferFactTablesForMetrics": True,
                "preferredFactTables": sorted(RAW_FACT_TABLES),
            },
        }

    def _collect_selected_columns(self, expanded_tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[str, str]] = set()
        selected = []
        for table in expanded_tables:
            for column in table.get("candidateColumns") or []:
                key = (str(column["tableName"]), str(column["columnName"]))
                if key in seen:
                    continue
                seen.add(key)
                selected.append(column)
        return selected

    def _merge_tables(self, existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged = {item["tableName"]: item for item in existing}
        for item in incoming:
            merged[item["tableName"]] = item
        return list(merged.values())


retrieval_service = RetrievalService()
