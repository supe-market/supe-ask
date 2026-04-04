"""Graph-aware retrieval for Ask.

This module builds the structured context pack that is handed to code
generation. Retrieval is catalog-first and graph-aware; the older schema-chunk
fallback path has been removed.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from supe_lib.db import query_records, query_scalar

from ..config import settings
from ..repository import repository
from .graph_cache import graph_cache_service
from .llm import llm_service


EventHandler = Callable[[str, dict[str, Any]], None] | None

IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
SENSITIVE_COLUMN_PATTERN = re.compile(r"(email|phone|mobile|address|name|dob|birth|ssn|gst)", re.IGNORECASE)
BLOCKED_PREVIEW_TYPES = ("json", "bytea", "text")


def _tokenize(text: str) -> list[str]:
    """Tokenize free-form text for lexical matching."""
    return [token for token in re.findall(r"[a-z0-9_]+", text.lower()) if len(token) > 1]


def _utc_now() -> datetime:
    """Return the current UTC timestamp."""
    return datetime.now(timezone.utc)


def _safe_identifier(identifier: str) -> bool:
    """Validate a SQL identifier before quoting it into profiling queries."""
    return bool(IDENTIFIER_PATTERN.match(identifier))


def _quote_ident(identifier: str) -> str:
    """Quote a validated SQL identifier."""
    if not _safe_identifier(identifier):
        raise ValueError(f"Invalid identifier: {identifier}")
    return f'"{identifier}"'


class CatalogUnavailableError(RuntimeError):
    """Raised when the structured schema catalog is missing for a tenant."""

    pass


class RetrievalService:
    """Build the context pack that is later handed to code generation."""

    def plan_and_retrieve(
        self,
        tenant_id: str,
        question: str,
        event_handler: EventHandler = None,
    ) -> dict[str, Any]:
        """Run the configured retrieval strategy and return a finalized context."""
        return self._plan_and_retrieve_from_catalog(tenant_id, question, event_handler)

    def _emit(self, event_handler: EventHandler, event_type: str, payload: dict[str, Any]) -> None:
        """Emit retrieval progress back to the orchestrator if a handler exists."""
        if event_handler:
            event_handler(event_type, payload)

    def _catalog_status(self, tenant_id: str) -> dict[str, Any]:
        """Summarize whether the structured schema catalog is usable for a tenant."""
        latest = repository.get_latest_catalog_refresh(tenant_id)
        table_count = repository.count_catalog_tables(tenant_id)
        available = bool(latest and latest.get("status") == "completed" and table_count > 0)
        stale = False
        if latest and latest.get("completed_at"):
            age = _utc_now() - latest["completed_at"]
            stale = age > timedelta(seconds=settings.schema_refresh_interval_seconds)
        return {
            "available": available,
            "stale": stale,
            "tableCount": table_count,
            "latestRefresh": latest,
        }

    def _plan_and_retrieve_from_catalog(
        self,
        tenant_id: str,
        question: str,
        event_handler: EventHandler = None,
    ) -> dict[str, Any]:
        """Run the graph-aware multihop retrieval loop over the schema catalog."""
        status = self._catalog_status(tenant_id)
        if not status["available"]:
            raise CatalogUnavailableError("Schema catalog is not available for this tenant")
        graph_snapshot = graph_cache_service.load_snapshot(tenant_id, status["latestRefresh"])
        if not graph_snapshot:
            raise CatalogUnavailableError("Schema graph snapshot is not available for this tenant")

        transcript: list[dict[str, Any]] = []
        state: dict[str, Any] = {
            "catalogStatus": status,
            "graphSnapshot": graph_snapshot,
            "candidateTables": [],
            "expandedTables": [],
            "graphNeighborhood": {"seedTables": [], "neighborTables": [], "edges": []},
            "selectedColumns": [],
            "joinPaths": [],
            "profiles": [],
            "finalContext": None,
            "enableProfileStep": settings.enable_profile_step,
            "limits": {
                "maxProfileTables": settings.max_profile_tables,
                "maxProfileColumns": settings.max_profile_columns,
                "maxProfilePreviewRows": settings.max_profile_preview_rows,
            },
        }

        for iteration in range(settings.retrieval_max_iterations):
            # Each hop asks the planner for the next deterministic retrieval action,
            # then executes that action locally against the catalog or live DB.
            self._emit(
                event_handler,
                "run.retrieval.iteration.started",
                {"iteration": iteration + 1, "candidateCount": len(state["candidateTables"])},
            )
            planner_state = self._planner_state(question, state, transcript, iteration)
            planned_action = llm_service.plan_next_retrieval_action(question, transcript, planner_state)
            action = self._sanitize_action(planned_action, question, state)
            self._emit(
                event_handler,
                "run.retrieval.action",
                {
                    "iteration": iteration + 1,
                    "action": action["action"],
                    "reason": action["reason"],
                    "tables": action["tables"],
                    "searchTerms": action["search_terms"],
                },
            )
            result = self._execute_action(tenant_id, question, action, state)
            transcript.append(
                {
                    "iteration": iteration + 1,
                    "action": action["action"],
                    "reason": action["reason"],
                    "summary": result["summary"],
                }
            )
            if action["action"] == "profile_columns":
                self._emit(
                    event_handler,
                    "run.retrieval.profile.completed",
                    {
                        "iteration": iteration + 1,
                        "profiles": result.get("profiles") or [],
                    },
                )
            self._emit(
                event_handler,
                "run.retrieval.iteration.completed",
                {
                    "iteration": iteration + 1,
                    "action": action["action"],
                    "summary": result["summary"],
                },
            )
            if action["action"] == "finalize_context":
                break

        if not state["finalContext"]:
            state["finalContext"] = self._build_final_context(question, state)
            transcript.append(
                {
                    "iteration": len(transcript) + 1,
                    "action": "finalize_context",
                    "reason": "Reached retrieval iteration limit",
                    "summary": {
                        "tableCount": len(state["finalContext"]["relevantTables"]),
                        "joinPathCount": len(state["finalContext"]["joinPaths"]),
                    },
                }
            )

        return {
            "strategy": "catalog_graph_multihop",
            "catalogStatus": status,
            "steps": transcript,
            "selectedTables": [table["tableName"] for table in state["expandedTables"]],
            "selectedColumns": state["selectedColumns"],
            "joinPaths": state["joinPaths"],
            "profiles": state["profiles"],
            "finalContext": state["finalContext"],
        }

    def _planner_state(
        self,
        question: str,
        state: dict[str, Any],
        transcript: list[dict[str, Any]],
        iteration: int,
    ) -> dict[str, Any]:
        return {
            "question": question,
            "iteration": iteration + 1,
            "catalogStatus": state["catalogStatus"],
            "candidateTables": state["candidateTables"][:8],
            "expandedTables": [
                {
                    "tableName": table["tableName"],
                    "description": table.get("description"),
                    "tenantColumn": table.get("tenantColumn"),
                    "dateColumns": table.get("dateColumns") or [],
                    "candidateColumns": table.get("candidateColumns") or [],
                }
                for table in state["expandedTables"][:4]
            ],
            "graphNeighborhood": state["graphNeighborhood"],
            "joinPaths": state["joinPaths"][:4],
            "profiles": state["profiles"][:2],
            "transcriptSummary": transcript[-3:],
            "enableProfileStep": state["enableProfileStep"],
            "limits": state["limits"],
        }

    def _sanitize_action(self, planned_action: dict[str, Any], question: str, state: dict[str, Any]) -> dict[str, Any]:
        action = str(planned_action.get("action") or "search_catalog")
        if action not in {
            "search_catalog",
            "expand_tables",
            "expand_graph_neighbors",
            "resolve_join_path",
            "profile_columns",
            "finalize_context",
        }:
            action = "search_catalog"

        search_terms = [
            str(term).strip().lower()
            for term in planned_action.get("search_terms") or []
            if str(term).strip() and re.fullmatch(r"[a-zA-Z0-9_\-\s]+", str(term).strip())
        ]
        if action == "search_catalog" and not search_terms:
            search_terms = _tokenize(question)[:8]

        tables = [table for table in planned_action.get("tables") or [] if _safe_identifier(str(table))]
        from_table = str(planned_action.get("from_table") or "")
        to_table = str(planned_action.get("to_table") or "")
        if from_table and not _safe_identifier(from_table):
            from_table = ""
        if to_table and not _safe_identifier(to_table):
            to_table = ""

        sanitized_targets = []
        for target in planned_action.get("profile_targets") or []:
            table = str(target.get("table") or "")
            if not _safe_identifier(table):
                continue
            columns = [column for column in target.get("columns") or [] if _safe_identifier(str(column))]
            if not columns:
                continue
            sanitized_targets.append({"table": table, "columns": columns[: settings.max_profile_columns]})

        if action == "expand_tables" and not tables:
            tables = [item["tableName"] for item in state["candidateTables"][:2]]
        if action == "expand_graph_neighbors" and not tables:
            tables = [item["tableName"] for item in state["expandedTables"][:2] if item.get("tableName")]
            if not tables:
                tables = [item["tableName"] for item in state["candidateTables"][:2]]
        if action == "resolve_join_path" and (not from_table or not to_table):
            expanded_tables = [table["tableName"] for table in state["expandedTables"] if table.get("tableName")]
            if len(expanded_tables) >= 2:
                from_table, to_table = expanded_tables[0], expanded_tables[1]
        if action == "profile_columns" and (not settings.enable_profile_step or not sanitized_targets):
            action = "finalize_context"
        return {
            "action": action,
            "reason": str(planned_action.get("reason") or "Retrieval step"),
            "search_terms": search_terms[:8],
            "tables": tables[:4],
            "from_table": from_table,
            "to_table": to_table,
            "profile_targets": sanitized_targets[: settings.max_profile_tables],
        }

    def _execute_action(
        self,
        tenant_id: str,
        question: str,
        action: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        action_name = action["action"]
        if action_name == "search_catalog":
            candidates = self._search_catalog(tenant_id, question, action["search_terms"], state["graphSnapshot"])
            state["candidateTables"] = candidates
            return {"summary": {"candidateTables": candidates[:5], "candidateCount": len(candidates)}}
        if action_name == "expand_tables":
            expanded = self._expand_tables(tenant_id, action["tables"], state["candidateTables"])
            state["expandedTables"] = self._merge_tables(state["expandedTables"], expanded)
            state["selectedColumns"] = self._collect_selected_columns(state["expandedTables"])
            return {
                "summary": {
                    "expandedTables": [item["tableName"] for item in expanded],
                    "columnCount": len(state["selectedColumns"]),
                }
            }
        if action_name == "expand_graph_neighbors":
            neighbor_result = self._expand_graph_neighbors(
                tenant_id,
                state["graphSnapshot"],
                action["tables"],
                state["expandedTables"],
            )
            state["expandedTables"] = self._merge_tables(state["expandedTables"], neighbor_result["expandedTables"])
            state["graphNeighborhood"] = self._merge_graph_neighborhood(state["graphNeighborhood"], neighbor_result["graphNeighborhood"])
            state["selectedColumns"] = self._collect_selected_columns(state["expandedTables"])
            return {
                "summary": {
                    "neighborTables": [item["tableName"] for item in neighbor_result["expandedTables"]],
                    "edgeCount": len((neighbor_result["graphNeighborhood"] or {}).get("edges") or []),
                }
            }
        if action_name == "resolve_join_path":
            join_path = self._resolve_join_path(state["graphSnapshot"], action["from_table"], action["to_table"])
            if join_path:
                state["joinPaths"] = self._merge_join_paths(state["joinPaths"], [join_path])
            return {"summary": {"joinPath": join_path}}
        if action_name == "profile_columns":
            profiles = self._profile_columns(tenant_id, action["profile_targets"])
            state["profiles"] = self._merge_profiles(state["profiles"], profiles)
            return {"summary": {"profileCount": len(profiles)}, "profiles": profiles}

        final_context = self._build_final_context(question, state)
        state["finalContext"] = final_context
        return {
            "summary": {
                "tableCount": len(final_context["relevantTables"]),
                "columnCount": len(final_context["relevantColumns"]),
                "joinPathCount": len(final_context["joinPaths"]),
            }
        }

    def _search_catalog(
        self,
        tenant_id: str,
        question: str,
        search_terms: list[str],
        graph_snapshot: dict[str, Any],
    ) -> list[dict[str, Any]]:
        terms = search_terms or _tokenize(question)[:8]
        table_rows = repository.search_catalog_tables(tenant_id, terms)
        column_rows = repository.search_catalog_columns(tenant_id, terms)
        alias_rows = repository.search_catalog_aliases(tenant_id, terms)
        relationship_rows = repository.search_catalog_relationships(tenant_id, terms)

        candidates: dict[str, dict[str, Any]] = {}
        question_lower = question.lower()

        def ensure_candidate(table_name: str) -> dict[str, Any]:
            candidate = candidates.get(table_name)
            if candidate:
                return candidate
            candidate = {
                "tableName": table_name,
                "score": 0.0,
                "matchedColumns": [],
                "matchedAliases": [],
                "reasons": [],
            }
            candidates[table_name] = candidate
            return candidate

        for row in table_rows:
            table_name = str(row["table_name"])
            candidate = ensure_candidate(table_name)
            search_text = str(row.get("search_text") or "")
            hits = sum(1 for term in terms if term in search_text)
            candidate["score"] += hits * 4 + 2
            if table_name.replace("_", " ") in question_lower:
                candidate["score"] += 3
            candidate["description"] = row.get("description")
            candidate["tenantColumn"] = row.get("tenant_column")
            candidate["dateColumns"] = list((row.get("date_columns") or []))
            candidate["metricHints"] = list((row.get("metric_hints") or []))
            candidate["dimensionHints"] = list((row.get("dimension_hints") or []))
            candidate["reasons"].append("table metadata match")

        for row in alias_rows:
            table_name = str(row.get("table_name") or row.get("object_name") or "")
            if not table_name:
                continue
            candidate = ensure_candidate(table_name)
            alias = str(row.get("alias") or "")
            weight = int(row.get("weight") or 1)
            hits = sum(1 for term in terms if term in alias.lower())
            candidate["score"] += max(1, hits) * (weight + 1)
            if alias:
                candidate["matchedAliases"].append(alias)
            candidate["reasons"].append("alias match")

        alias_index = graph_snapshot.get("aliasIndex") or {}
        for term in terms:
            for hit in alias_index.get(str(term).lower(), []):
                table_name = str(hit.get("tableName") or "")
                if not table_name:
                    continue
                candidate = ensure_candidate(table_name)
                alias = str(hit.get("objectName") or table_name)
                weight = int(hit.get("weight") or 1)
                candidate["score"] += weight + 0.75
                candidate["matchedAliases"].append(alias)
                candidate["reasons"].append("graph alias match")

        for row in column_rows:
            table_name = str(row["table_name"])
            candidate = ensure_candidate(table_name)
            column_name = str(row["column_name"])
            search_text = str(row.get("search_text") or "")
            hits = sum(1 for term in terms if term in search_text or term in column_name.lower())
            candidate["score"] += hits * 2.5 + 0.5
            candidate["matchedColumns"].append(column_name)
            candidate["reasons"].append("column match")

        for row in relationship_rows:
            for key in ("from_table", "to_table"):
                table_name = str(row.get(key) or "")
                if not table_name:
                    continue
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
        ranked.sort(key=lambda item: (item["score"], item["tableName"]), reverse=True)
        return ranked[:8]

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

        for seed in seeds[:4]:
            for edge in adjacency.get(seed, [])[:12]:
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
                candidate["score"] += 2.0 if str(edge.get("source") or "database") == "database" else 2.5

        ranked_neighbors = sorted(
            neighbor_candidates.values(),
            key=lambda item: (item["score"], item["tableName"]),
            reverse=True,
        )[:3]
        expanded = self._expand_tables(
            tenant_id,
            [item["tableName"] for item in ranked_neighbors],
            ranked_neighbors,
        )
        return {
            "expandedTables": expanded,
            "graphNeighborhood": {
                "seedTables": seeds[:4],
                "neighborTables": [item["tableName"] for item in ranked_neighbors],
                "edges": edges[:16],
            },
        }

    def _expand_tables(
        self,
        tenant_id: str,
        table_names: list[str],
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
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

        expanded = []
        for table_name in table_names:
            row = tables.get(table_name)
            if not row:
                continue
            candidate = candidate_map.get(table_name, {})
            columns = columns_by_table.get(table_name, [])
            matched_columns = set(candidate.get("matchedColumns") or [])
            candidate_columns = []
            for column in columns:
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

    def _resolve_join_path(self, graph_snapshot: dict[str, Any], from_table: str, to_table: str) -> dict[str, Any] | None:
        if not from_table or not to_table:
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
                    result = {
                        "fromTable": from_table,
                        "toTable": to_table,
                        "edges": next_path,
                    }
                    join_path_memo[memo_key] = result
                    return result
                if next_table in visited:
                    continue
                visited.add(next_table)
                queue.append((next_table, next_path))
        return None

    @contextmanager
    def _query_env(self, tenant_id: str):
        desired_values = {
            "SUPE_ASK_TENANT_ID": str(tenant_id),
            "SUPE_ASK_DB_HOST": settings.db_host,
            "SUPE_ASK_DB_PORT": str(settings.db_port),
            "SUPE_ASK_DB_NAME": settings.db_name,
            "SUPE_ASK_DB_USER": settings.db_user,
            "SUPE_ASK_DB_PASSWORD": settings.db_password,
            "SUPE_ASK_DB_SSL": "true" if settings.db_ssl else "false",
        }
        previous = {key: os.environ.get(key) for key in desired_values}
        try:
            for key, value in desired_values.items():
                os.environ[key] = value
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def _profile_columns(self, tenant_id: str, targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        profiles = []
        with self._query_env(tenant_id):
            for target in targets[: settings.max_profile_tables]:
                table_name = str(target["table"])
                columns = [str(column) for column in target.get("columns") or [] if _safe_identifier(str(column))]
                if not columns:
                    continue
                table_rows = repository.list_catalog_tables(tenant_id, [table_name])
                table_row = table_rows[0] if table_rows else {}
                tenant_column = str(table_row.get("tenant_column") or "")
                if not tenant_column or not _safe_identifier(tenant_column):
                    continue
                table_columns = {row["column_name"]: row for row in repository.list_catalog_columns(tenant_id, [table_name])}
                row_count = query_scalar(
                    f"select count(*) as row_count from {_quote_ident(table_name)} where {{tenant_filter}}",
                    tenant_id_column=tenant_column,
                    default=0,
                )
                profile = {
                    "tableName": table_name,
                    "rowCount": int(row_count or 0),
                    "columnProfiles": [],
                    "previewRows": [],
                }
                safe_preview_columns = []

                for column_name in columns[: settings.max_profile_columns]:
                    metadata = table_columns.get(column_name) or {}
                    data_type = str(metadata.get("data_type") or "")
                    semantic_role = str(metadata.get("semantic_role") or "")
                    null_count = query_scalar(
                        (
                            f"select sum(case when {_quote_ident(column_name)} is null then 1 else 0 end) as null_count "
                            f"from {_quote_ident(table_name)} where {{tenant_filter}}"
                        ),
                        tenant_id_column=tenant_column,
                        default=0,
                    )
                    distinct_count = query_scalar(
                        (
                            f"select count(distinct {_quote_ident(column_name)}) as distinct_count "
                            f"from {_quote_ident(table_name)} where {{tenant_filter}}"
                        ),
                        tenant_id_column=tenant_column,
                        default=0,
                    )
                    column_profile: dict[str, Any] = {
                        "columnName": column_name,
                        "dataType": data_type,
                        "semanticRole": semantic_role,
                        "nullCount": int(null_count or 0),
                        "distinctCount": int(distinct_count or 0),
                    }
                    if semantic_role == "date" or "date" in data_type.lower() or "timestamp" in data_type.lower():
                        min_max = query_records(
                            (
                                f"select min({_quote_ident(column_name)})::text as min_value, "
                                f"max({_quote_ident(column_name)})::text as max_value "
                                f"from {_quote_ident(table_name)} where {{tenant_filter}}"
                            ),
                            tenant_id_column=tenant_column,
                        )
                        if min_max:
                            column_profile["minValue"] = min_max[0].get("min_value")
                            column_profile["maxValue"] = min_max[0].get("max_value")

                    preview_safe = not SENSITIVE_COLUMN_PATTERN.search(column_name) and not any(
                        blocked in data_type.lower() for blocked in BLOCKED_PREVIEW_TYPES
                    )
                    if preview_safe:
                        safe_preview_columns.append(column_name)
                        if semantic_role in {"dimension", "date"}:
                            top_values = query_records(
                                (
                                    f"select {_quote_ident(column_name)} as value, count(*) as row_count "
                                f"from {_quote_ident(table_name)} "
                                f"where {{tenant_filter}} and {_quote_ident(column_name)} is not null "
                                f"group by {_quote_ident(column_name)} "
                                f"order by count(*) desc, {_quote_ident(column_name)}::text asc "
                                f"limit 5"
                            ),
                                tenant_id_column=tenant_column,
                            )
                            column_profile["topValues"] = top_values
                    profile["columnProfiles"].append(column_profile)

                if safe_preview_columns:
                    preview_select = ", ".join(_quote_ident(column) for column in safe_preview_columns[: settings.max_profile_columns])
                    preview_rows = query_records(
                        (
                            f"select {preview_select} from {_quote_ident(table_name)} "
                            f"where {{tenant_filter}} "
                            f"limit {int(settings.max_profile_preview_rows)}"
                        ),
                        tenant_id_column=tenant_column,
                    )
                    profile["previewRows"] = preview_rows
                profiles.append(profile)
        return profiles

    def _build_final_context(self, question: str, state: dict[str, Any]) -> dict[str, Any]:
        relevant_tables = state["expandedTables"] or state["candidateTables"][:4]
        relevant_columns = state["selectedColumns"][:30]
        business_hints = []
        for table in relevant_tables:
            for hint in table.get("metricHints") or []:
                business_hints.append(f"{table['tableName']} metric hint: {hint}")
            for hint in table.get("dimensionHints") or []:
                business_hints.append(f"{table['tableName']} dimension hint: {hint}")

        profile_summaries = []
        for profile in state["profiles"]:
            profile_summaries.append(
                {
                    "tableName": profile["tableName"],
                    "rowCount": profile["rowCount"],
                    "columnProfiles": profile["columnProfiles"],
                    "previewRows": profile["previewRows"],
                }
            )

        return {
            "question": question,
            "relevantTables": relevant_tables[:6],
            "relevantColumns": relevant_columns,
            "joinPaths": state["joinPaths"],
            "graphNeighborhood": state["graphNeighborhood"],
            "businessHints": sorted(set(business_hints))[:30],
            "profileSummaries": profile_summaries,
            "queryGuardrails": {
                "tenantFilterToken": "{{tenant_filter}}",
                "readOnly": True,
                "maxPreviewRows": settings.max_profile_preview_rows,
                "preferProvidedJoinPaths": True,
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

    def _merge_join_paths(self, existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged = {(item["fromTable"], item["toTable"]): item for item in existing}
        for item in incoming:
            merged[(item["fromTable"], item["toTable"])] = item
        return list(merged.values())

    def _merge_profiles(self, existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged = {item["tableName"]: item for item in existing}
        for item in incoming:
            merged[item["tableName"]] = item
        return list(merged.values())

    def _merge_graph_neighborhood(self, existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        merged_seed_tables = list(dict.fromkeys([*(existing.get("seedTables") or []), *(incoming.get("seedTables") or [])]))
        merged_neighbor_tables = list(
            dict.fromkeys([*(existing.get("neighborTables") or []), *(incoming.get("neighborTables") or [])])
        )
        merged_edges = []
        seen_edges: set[tuple[str, str, str, str]] = set()
        for edge in [*(existing.get("edges") or []), *(incoming.get("edges") or [])]:
            edge_key = (
                str(edge.get("fromTable") or ""),
                str(edge.get("fromColumn") or ""),
                str(edge.get("toTable") or ""),
                str(edge.get("toColumn") or ""),
            )
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            merged_edges.append(edge)
        return {
            "seedTables": merged_seed_tables[:8],
            "neighborTables": merged_neighbor_tables[:8],
            "edges": merged_edges[:24],
        }

retrieval_service = RetrievalService()
