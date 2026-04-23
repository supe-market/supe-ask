"""Database access layer for Ask persistence.

This repository hides the underlying SQL for threads, runs, execution state,
catalog metadata, and artifacts so the higher-level services can focus on flow.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from .db import db


def _json_dumps(value: Any) -> str:
    """Serialize arbitrary values for jsonb columns."""
    return json.dumps(value, ensure_ascii=True, default=str)


def _normalize_run_record(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Ensure run rows always expose a JSON object for stream_state."""
    if not row:
        return row
    normalized = dict(row)
    stream_state = normalized.get("stream_state")
    normalized["stream_state"] = dict(stream_state) if isinstance(stream_state, dict) else {}
    return normalized


def _build_search_filter(fields: list[str], terms: list[str]) -> tuple[str, list[str]]:
    """Build a simple case-insensitive LIKE filter across multiple columns."""
    normalized_terms = [term.strip().lower() for term in terms if term.strip()]
    if not normalized_terms:
        return "true", []

    clauses: list[str] = []
    params: list[str] = []
    for term in normalized_terms:
        pattern = f"%{term}%"
        clauses.append("(" + " or ".join([f"lower({field}) like %s" for field in fields]) + ")")
        params.extend([pattern] * len(fields))
    return " or ".join(clauses), params


class AskRepository:
    """Encapsulate Postgres reads and writes for the Ask service."""

    def list_threads(self, tenant_id: str) -> list[dict[str, Any]]:
        """List visible Ask threads for one tenant."""
        return db.fetch_all(
            """
            select
              t.id,
              t.title,
              t.created_at,
              t.updated_at,
              (
                select r.status
                from ask_runs r
                where r.thread_id = t.id
                order by r.created_at desc
                limit 1
              ) as latest_run_status,
              (
                select r.question
                from ask_runs r
                where r.thread_id = t.id
                order by r.created_at desc
                limit 1
              ) as latest_question
            from ask_threads t
            where t.tenant_id = %s and t.archived = false
            order by t.updated_at desc
            """,
            [tenant_id],
        )

    def create_thread(self, tenant_id: str, created_by: str, title: str | None = None) -> dict[str, Any]:
        """Create a new thread."""
        return db.execute_returning(
            """
            insert into ask_threads (id, tenant_id, title, created_by)
            values (%s, %s, %s, %s)
            returning *
            """,
            [str(uuid.uuid4()), tenant_id, title or "New ask thread", created_by],
        ) or {}

    def get_thread(self, tenant_id: str, thread_id: str) -> dict[str, Any] | None:
        return db.fetch_one(
            "select * from ask_threads where tenant_id = %s and id = %s and archived = false limit 1",
            [tenant_id, thread_id],
        )

    def archive_thread(self, tenant_id: str, thread_id: str) -> dict[str, Any] | None:
        """Soft-delete a thread so it disappears from the Ask UI."""
        return db.execute_returning(
            """
            update ask_threads
            set archived = true, updated_at = now()
            where tenant_id = %s and id = %s and archived = false
            returning id
            """,
            [tenant_id, thread_id],
        )

    def update_thread(self, thread_id: str, title: str | None = None) -> None:
        if title:
            db.execute(
                """
                update ask_threads
                set title = %s, updated_at = now()
                where id = %s
                """,
                [title, thread_id],
            )
            return
        db.execute("update ask_threads set updated_at = now() where id = %s", [thread_id])

    def list_messages(self, tenant_id: str, thread_id: str) -> list[dict[str, Any]]:
        return db.fetch_all(
            """
            select id, thread_id, role, content, run_id, created_at
            from ask_messages
            where tenant_id = %s and thread_id = %s
            order by created_at asc
            """,
            [tenant_id, thread_id],
        )

    def create_message(self, tenant_id: str, thread_id: str, role: str, content: str, run_id: str | None = None) -> dict[str, Any]:
        return db.execute_returning(
            """
            insert into ask_messages (id, tenant_id, thread_id, role, content, run_id)
            values (%s, %s, %s, %s, %s, %s)
            returning *
            """,
            [str(uuid.uuid4()), tenant_id, thread_id, role, content, run_id],
        ) or {}

    def create_run(self, tenant_id: str, thread_id: str, message_id: str, question: str) -> dict[str, Any]:
        """Create the run record that will track one Ask question."""
        return db.execute_returning(
            """
            insert into ask_runs (id, tenant_id, thread_id, message_id, question, status, started_at)
            values (%s, %s, %s, %s, %s, 'queued', now())
            returning *
            """,
            [str(uuid.uuid4()), tenant_id, thread_id, message_id, question],
        ) or {}

    def get_run(self, tenant_id: str, run_id: str) -> dict[str, Any] | None:
        return _normalize_run_record(
            db.fetch_one(
                "select * from ask_runs where tenant_id = %s and id = %s limit 1",
                [tenant_id, run_id],
            )
        )

    def list_runs(self, tenant_id: str, thread_id: str) -> list[dict[str, Any]]:
        return [
            _normalize_run_record(row) or {}
            for row in db.fetch_all(
                """
                select *
                from ask_runs
                where tenant_id = %s and thread_id = %s
                order by created_at asc
                """,
                [tenant_id, thread_id],
            )
        ]

    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        title: str | None = None,
        assistant_summary: str | None = None,
        python_code: str | None = None,
        retrieval_context: dict[str, Any] | None = None,
        artifact_plan: dict[str, Any] | None = None,
        stream_state: dict[str, Any] | None = None,
        error_message: str | None = None,
        completed: bool = False,
    ) -> None:
        """Apply partial updates to a run record."""
        updates: list[str] = ["updated_at = now()"]
        params: list[Any] = []
        if status is not None:
            updates.append("status = %s")
            params.append(status)
        if title is not None:
            updates.append("title = %s")
            params.append(title)
        if assistant_summary is not None:
            updates.append("assistant_summary = %s")
            params.append(assistant_summary)
        if python_code is not None:
            updates.append("python_code = %s")
            params.append(python_code)
        if retrieval_context is not None:
            updates.append("retrieval_context = %s::jsonb")
            params.append(_json_dumps(retrieval_context))
        if artifact_plan is not None:
            updates.append("artifact_plan = %s::jsonb")
            params.append(_json_dumps(artifact_plan))
        if stream_state is not None:
            updates.append("stream_state = %s::jsonb")
            params.append(_json_dumps(stream_state))
        if error_message is not None:
            updates.append("error_message = %s")
            params.append(error_message)
        if completed:
            updates.append("completed_at = now()")
        params.append(run_id)
        db.execute(f"update ask_runs set {', '.join(updates)} where id = %s", params)

    def upsert_run_execution(
        self,
        tenant_id: str,
        run_id: str,
        backend: str,
        status: str,
        *,
        task_arn: str | None = None,
        callback_token_hash: str | None = None,
        input_object_key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or update the execution-plane record for a run."""
        return db.execute_returning(
            """
            insert into ask_run_executions (
              run_id, tenant_id, backend, status, task_arn, callback_token_hash, input_object_key, metadata
            )
            values (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            on conflict (run_id) do update
            set tenant_id = excluded.tenant_id,
                backend = excluded.backend,
                status = excluded.status,
                task_arn = coalesce(excluded.task_arn, ask_run_executions.task_arn),
                callback_token_hash = coalesce(excluded.callback_token_hash, ask_run_executions.callback_token_hash),
                input_object_key = coalesce(excluded.input_object_key, ask_run_executions.input_object_key),
                metadata = coalesce(ask_run_executions.metadata, '{}'::jsonb) || excluded.metadata,
                updated_at = now()
            returning *
            """,
            [run_id, tenant_id, backend, status, task_arn, callback_token_hash, input_object_key, _json_dumps(metadata or {})],
        ) or {}

    def get_run_execution(self, run_id: str) -> dict[str, Any] | None:
        return db.fetch_one(
            """
            select *
            from ask_run_executions
            where run_id = %s
            limit 1
            """,
            [run_id],
        )

    def update_run_execution(
        self,
        run_id: str,
        *,
        status: str | None = None,
        task_arn: str | None = None,
        input_object_key: str | None = None,
        last_callback_sequence: int | None = None,
        touch_heartbeat: bool = False,
        cancel_requested: bool = False,
        runner_started: bool = False,
        runner_completed: bool = False,
        stop_reason: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Apply partial updates to the execution-plane record for a run."""
        updates: list[str] = ["updated_at = now()"]
        params: list[Any] = []
        if status is not None:
            updates.append("status = %s")
            params.append(status)
        if task_arn is not None:
            updates.append("task_arn = %s")
            params.append(task_arn)
        if input_object_key is not None:
            updates.append("input_object_key = %s")
            params.append(input_object_key)
        if last_callback_sequence is not None:
            updates.append("last_callback_sequence = %s")
            params.append(last_callback_sequence)
        if touch_heartbeat:
            updates.append("last_heartbeat_at = now()")
        if cancel_requested:
            updates.append("cancel_requested_at = now()")
        if runner_started:
            updates.append("runner_started_at = coalesce(runner_started_at, now())")
        if runner_completed:
            updates.append("runner_completed_at = now()")
        if stop_reason is not None:
            updates.append("stop_reason = %s")
            params.append(stop_reason)
        if metadata is not None:
            updates.append("metadata = coalesce(metadata, '{}'::jsonb) || %s::jsonb")
            params.append(_json_dumps(metadata))
        params.append(run_id)
        db.execute(f"update ask_run_executions set {', '.join(updates)} where run_id = %s", params)

    def list_stale_run_executions(self, stale_seconds: int) -> list[dict[str, Any]]:
        """Find active executions that have stopped sending heartbeats."""
        return db.fetch_all(
            """
            select *
            from ask_run_executions
            where backend = 'ecs'
              and status in ('launching', 'running')
              and cancel_requested_at is null
              and now() - coalesce(last_heartbeat_at, runner_started_at, created_at) > (%s * interval '1 second')
            order by updated_at asc
            """,
            [stale_seconds],
        )

    def list_stale_queued_run_executions(self, stale_seconds: int) -> list[dict[str, Any]]:
        """Find queued codebox jobs that have not been picked up by any worker."""
        return db.fetch_all(
            """
            select *
            from ask_run_executions
            where backend = 'ecs'
              and status = 'queued'
              and cancel_requested_at is null
              and coalesce(metadata->>'dispatchMode', '') = 'codebox'
              and now() - created_at > (%s * interval '1 second')
            order by updated_at asc
            """,
            [stale_seconds],
        )

    def next_artifact_ordinal(self, tenant_id: str, run_id: str) -> int:
        row = db.fetch_one(
            """
            select coalesce(max(ordinal), 0) + 1 as next_ordinal
            from ask_run_artifacts
            where tenant_id = %s and run_id = %s
            """,
            [tenant_id, run_id],
        )
        return int((row or {}).get("next_ordinal") or 1)

    def create_artifact(
        self,
        tenant_id: str,
        run_id: str,
        artifact_type: str,
        title: str,
        payload: dict[str, Any],
        ordinal: int,
        *,
        storage_backend: str = "database",
        object_key: str | None = None,
        content_type: str | None = None,
        byte_size: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Insert one persisted artifact row."""
        return db.execute_returning(
            """
            insert into ask_run_artifacts (
              id, tenant_id, run_id, artifact_type, title, payload, storage_backend,
              object_key, content_type, byte_size, metadata, ordinal
            )
            values (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s::jsonb, %s)
            returning *
            """,
            [
                str(uuid.uuid4()),
                tenant_id,
                run_id,
                artifact_type,
                title,
                _json_dumps(payload),
                storage_backend,
                object_key,
                content_type,
                byte_size,
                _json_dumps(metadata or {}),
                ordinal,
            ],
        ) or {}

    def delete_run_artifacts(self, run_id: str) -> None:
        db.execute("DELETE FROM ask_run_artifacts WHERE run_id = %s", [run_id])

    def list_artifacts(self, tenant_id: str, run_id: str) -> list[dict[str, Any]]:
        return db.fetch_all(
            """
            select id, run_id, artifact_type, title, payload, storage_backend, object_key, content_type, byte_size, metadata, ordinal, created_at
            from ask_run_artifacts
            where tenant_id = %s and run_id = %s
            order by ordinal asc, created_at asc
            """,
            [tenant_id, run_id],
        )

    def count_catalog_tables(self, tenant_id: str) -> int:
        row = db.fetch_one(
            """
            select count(*)::int as total
            from ask_catalog_tables
            where tenant_id = %s
            """,
            [tenant_id],
        )
        return int((row or {}).get("total") or 0)

    def get_latest_catalog_refresh(self, tenant_id: str) -> dict[str, Any] | None:
        return db.fetch_one(
            """
            select id, tenant_id, status, strategy, refreshed_tables, refreshed_columns,
                   refreshed_relationships, refreshed_aliases, error_message,
                   started_at, completed_at, created_at, updated_at
            from ask_catalog_refreshes
            where tenant_id = %s
            order by created_at desc
            limit 1
            """,
            [tenant_id],
        )

    def get_latest_semantic_pack_version(self, tenant_id: str) -> dict[str, Any] | None:
        return db.fetch_one(
            """
            select id, tenant_id, semantic_pack_id, refresh_id, source_path, status,
                   cluster_count, canonical_question_count, variant_count, entity_count, metric_count,
                   created_at, updated_at
            from ask_semantic_pack_versions
            where tenant_id = %s
            order by created_at desc
            limit 1
            """,
            [tenant_id],
        )

    def search_catalog_tables(self, tenant_id: str, terms: list[str], limit: int = 20) -> list[dict[str, Any]]:
        where_sql, params = _build_search_filter(
            ["table_name", "coalesce(display_name, '')", "coalesce(description, '')", "search_text"],
            terms,
        )
        return db.fetch_all(
            f"""
            select *
            from ask_catalog_tables
            where tenant_id = %s
              and ({where_sql})
            order by updated_at desc, table_name asc
            limit %s
            """,
            [tenant_id, *params, limit],
        )

    def search_catalog_columns(self, tenant_id: str, terms: list[str], limit: int = 50) -> list[dict[str, Any]]:
        where_sql, params = _build_search_filter(
            ["table_name", "column_name", "coalesce(description, '')", "coalesce(semantic_role, '')", "search_text"],
            terms,
        )
        return db.fetch_all(
            f"""
            select *
            from ask_catalog_columns
            where tenant_id = %s
              and ({where_sql})
            order by table_name asc, ordinal_position asc
            limit %s
            """,
            [tenant_id, *params, limit],
        )

    def search_catalog_aliases(self, tenant_id: str, terms: list[str], limit: int = 50) -> list[dict[str, Any]]:
        where_sql, params = _build_search_filter(["alias", "object_name", "coalesce(table_name, '')", "search_text"], terms)
        return db.fetch_all(
            f"""
            select *
            from ask_catalog_aliases
            where tenant_id = %s
              and ({where_sql})
            order by weight desc, alias asc
            limit %s
            """,
            [tenant_id, *params, limit],
        )

    def list_catalog_aliases(self, tenant_id: str) -> list[dict[str, Any]]:
        return db.fetch_all(
            """
            select *
            from ask_catalog_aliases
            where tenant_id = %s
            order by weight desc, alias asc
            """,
            [tenant_id],
        )

    def search_catalog_relationships(self, tenant_id: str, terms: list[str], limit: int = 30) -> list[dict[str, Any]]:
        where_sql, params = _build_search_filter(
            ["from_table", "from_column", "to_table", "to_column", "search_text"],
            terms,
        )
        return db.fetch_all(
            f"""
            select *
            from ask_catalog_relationships
            where tenant_id = %s
              and ({where_sql})
            order by from_table asc, to_table asc
            limit %s
            """,
            [tenant_id, *params, limit],
        )

    def list_catalog_tables(self, tenant_id: str, table_names: list[str] | None = None) -> list[dict[str, Any]]:
        if table_names:
            return db.fetch_all(
                """
                select *
                from ask_catalog_tables
                where tenant_id = %s and table_name = any(%s)
                order by table_name asc
                """,
                [tenant_id, table_names],
            )
        return db.fetch_all(
            """
            select *
            from ask_catalog_tables
            where tenant_id = %s
            order by table_name asc
            """,
            [tenant_id],
        )

    def list_catalog_columns(self, tenant_id: str, table_names: list[str] | None = None) -> list[dict[str, Any]]:
        if table_names:
            return db.fetch_all(
                """
                select *
                from ask_catalog_columns
                where tenant_id = %s and table_name = any(%s)
                order by table_name asc, ordinal_position asc
                """,
                [tenant_id, table_names],
            )
        return db.fetch_all(
            """
            select *
            from ask_catalog_columns
            where tenant_id = %s
            order by table_name asc, ordinal_position asc
            """,
            [tenant_id],
        )

    def list_catalog_relationships(self, tenant_id: str, table_names: list[str] | None = None) -> list[dict[str, Any]]:
        if table_names:
            return db.fetch_all(
                """
                select *
                from ask_catalog_relationships
                where tenant_id = %s
                  and (from_table = any(%s) or to_table = any(%s))
                order by from_table asc, to_table asc
                """,
                [tenant_id, table_names, table_names],
            )
        return db.fetch_all(
            """
            select *
            from ask_catalog_relationships
            where tenant_id = %s
            order by from_table asc, to_table asc
            """,
            [tenant_id],
        )

    def search_question_clusters(self, tenant_id: str, terms: list[str], limit: int = 12) -> list[dict[str, Any]]:
        where_sql, params = _build_search_filter(["cluster_key", "title", "description", "search_text"], terms)
        return db.fetch_all(
            f"""
            select *
            from ask_question_clusters
            where tenant_id = %s
              and ({where_sql})
            order by cluster_number asc, title asc
            limit %s
            """,
            [tenant_id, *params, limit],
        )

    def search_canonical_questions(self, tenant_id: str, terms: list[str], limit: int = 20) -> list[dict[str, Any]]:
        where_sql, params = _build_search_filter(["canonical_question", "primary_entity", "search_text"], terms)
        return db.fetch_all(
            f"""
            select *
            from ask_canonical_questions
            where tenant_id = %s
              and ({where_sql})
            order by question_number asc
            limit %s
            """,
            [tenant_id, *params, limit],
        )

    def search_question_variants(self, tenant_id: str, terms: list[str], limit: int = 20) -> list[dict[str, Any]]:
        where_sql, params = _build_search_filter(["variant_text", "search_text"], terms)
        return db.fetch_all(
            f"""
            select *
            from ask_question_variants
            where tenant_id = %s
              and ({where_sql})
            order by canonical_question_number asc, ordinal_position asc
            limit %s
            """,
            [tenant_id, *params, limit],
        )

    def search_entities(self, tenant_id: str, terms: list[str], limit: int = 20) -> list[dict[str, Any]]:
        where_sql, params = _build_search_filter(["entity_key", "display_name", "search_text"], terms)
        return db.fetch_all(
            f"""
            select *
            from ask_entities
            where tenant_id = %s
              and ({where_sql})
            order by display_name asc
            limit %s
            """,
            [tenant_id, *params, limit],
        )

    def search_metrics(self, tenant_id: str, terms: list[str], limit: int = 20) -> list[dict[str, Any]]:
        where_sql, params = _build_search_filter(["metric_key", "display_name", "search_text"], terms)
        return db.fetch_all(
            f"""
            select *
            from ask_metrics
            where tenant_id = %s
              and ({where_sql})
            order by display_name asc
            limit %s
            """,
            [tenant_id, *params, limit],
        )

    def search_metric_aliases(self, tenant_id: str, terms: list[str], limit: int = 20) -> list[dict[str, Any]]:
        where_sql, params = _build_search_filter(["metric_key", "alias", "search_text"], terms)
        return db.fetch_all(
            f"""
            select *
            from ask_metric_aliases
            where tenant_id = %s
              and ({where_sql})
            order by weight desc, alias asc
            limit %s
            """,
            [tenant_id, *params, limit],
        )

    def list_join_policies(self, tenant_id: str) -> list[dict[str, Any]]:
        return db.fetch_all(
            """
            select *
            from ask_join_policies
            where tenant_id = %s
            order by from_table asc, to_table asc
            """,
            [tenant_id],
        )

    def list_date_policies(self, tenant_id: str) -> list[dict[str, Any]]:
        return db.fetch_all(
            """
            select *
            from ask_date_policies
            where tenant_id = %s
            order by policy_key asc
            """,
            [tenant_id],
        )

    def list_threshold_policies(self, tenant_id: str) -> list[dict[str, Any]]:
        return db.fetch_all(
            """
            select *
            from ask_threshold_policies
            where tenant_id = %s
            order by policy_key asc
            """,
            [tenant_id],
        )

repository = AskRepository()
