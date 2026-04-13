from __future__ import annotations

import json
import time
from threading import Lock
from typing import Any

from ..config import settings
from ..redis_client import RedisClient
from ..repository import repository


class GraphCacheService:
    def __init__(self) -> None:
        self._local_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = Lock()
        self._redis = RedisClient.from_settings()

    def load_snapshot(self, tenant_id: str, latest_refresh: dict[str, Any] | None) -> dict[str, Any] | None:
        if not latest_refresh or not latest_refresh.get("id"):
            return None
        cache_key = self._cache_key(tenant_id, str(latest_refresh["id"]))
        cached = self._get_local(cache_key)
        if cached:
            return cached

        if self._redis:
            try:
                payload = self._redis.get(cache_key)
            except Exception:
                payload = None
            if payload:
                try:
                    snapshot = json.loads(payload)
                except Exception:
                    snapshot = None
                if snapshot:
                    self._store_local(cache_key, snapshot)
                    return snapshot

        snapshot = self._build_snapshot(tenant_id, str(latest_refresh["id"]))
        if self._redis:
            try:
                self._redis.setex(cache_key, settings.graph_cache_ttl_seconds, json.dumps(snapshot, ensure_ascii=True))
            except Exception:
                pass
        self._store_local(cache_key, snapshot)
        return snapshot

    def _get_local(self, cache_key: str) -> dict[str, Any] | None:
        with self._lock:
            cached = self._local_cache.get(cache_key)
            if not cached:
                return None
            expires_at, payload = cached
            if expires_at < time.monotonic():
                self._local_cache.pop(cache_key, None)
                return None
            return payload

    def _store_local(self, cache_key: str, snapshot: dict[str, Any]) -> None:
        with self._lock:
            self._local_cache[cache_key] = (time.monotonic() + settings.graph_cache_ttl_seconds, snapshot)

    def _build_snapshot(self, tenant_id: str, refresh_id: str) -> dict[str, Any]:
        table_rows = repository.list_catalog_tables(tenant_id)
        column_rows = repository.list_catalog_columns(tenant_id)
        relationship_rows = repository.list_catalog_relationships(tenant_id)
        alias_rows = repository.list_catalog_aliases(tenant_id)

        # Index columns by table
        columns_by_table: dict[str, list[dict[str, Any]]] = {}
        for row in column_rows:
            table_name = str(row["table_name"])
            columns_by_table.setdefault(table_name, []).append({
                "columnName": str(row["column_name"]),
                "dataType": str(row.get("data_type") or ""),
                "semanticRole": row.get("semantic_role"),
                "isPrimaryKey": bool(row.get("is_primary_key")),
                "referencesTable": row.get("references_table"),
                "referencesColumn": row.get("references_column"),
            })

        tables: dict[str, dict[str, Any]] = {}
        adjacency: dict[str, list[dict[str, Any]]] = {}
        alias_index: dict[str, list[dict[str, Any]]] = {}

        for row in table_rows:
            table_name = str(row["table_name"])
            tables[table_name] = {
                "tableName": table_name,
                "displayName": row.get("display_name"),
                "description": row.get("description"),
                "tenantColumn": row.get("tenant_column"),
                "primaryKeyColumns": list(row.get("primary_key_columns") or []),
                "dateColumns": list(row.get("date_columns") or []),
                "metricHints": list(row.get("metric_hints") or []),
                "dimensionHints": list(row.get("dimension_hints") or []),
                "columns": columns_by_table.get(table_name, []),
            }
            adjacency.setdefault(table_name, [])

        for row in relationship_rows:
            from_table = str(row["from_table"])
            to_table = str(row["to_table"])
            forward_edge = {
                "fromTable": from_table,
                "fromColumn": str(row["from_column"]),
                "toTable": to_table,
                "toColumn": str(row["to_column"]),
                "relationshipType": str(row.get("relationship_type") or "foreign_key"),
                "cardinality": str(row.get("cardinality") or "many_to_one"),
                "source": str(row.get("source") or "database"),
            }
            reverse_edge = {
                "fromTable": to_table,
                "fromColumn": str(row["to_column"]),
                "toTable": from_table,
                "toColumn": str(row["from_column"]),
                "relationshipType": str(row.get("relationship_type") or "foreign_key"),
                "cardinality": str(row.get("cardinality") or "many_to_one"),
                "source": str(row.get("source") or "database"),
            }
            adjacency.setdefault(from_table, []).append(forward_edge)
            adjacency.setdefault(to_table, []).append(reverse_edge)

        for row in alias_rows:
            alias = str(row.get("alias") or "").strip().lower()
            table_name = str(row.get("table_name") or "")
            if not alias or not table_name:
                continue
            alias_index.setdefault(alias, []).append(
                {
                    "objectType": str(row.get("object_type") or "table"),
                    "objectName": str(row.get("object_name") or table_name),
                    "tableName": table_name,
                    "columnName": row.get("column_name"),
                    "weight": int(row.get("weight") or 1),
                    "source": str(row.get("source") or "catalog"),
                }
            )

        return {
            "refreshId": refresh_id,
            "tenantId": str(tenant_id),
            "tables": tables,
            "adjacency": adjacency,
            "aliasIndex": alias_index,
            "joinPathMemo": {},
        }

    def _cache_key(self, tenant_id: str, refresh_id: str) -> str:
        return f"ask:graph:v1:tenant:{tenant_id}:refresh:{refresh_id}"


graph_cache_service = GraphCacheService()
