from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


ASK_RESPONSE_JSON_SCHEMA = {
    "name": "supe_ask_code_response",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string"},
            "assistant_summary": {"type": "string"},
            "python_code": {"type": "string"},
            "artifact_plan": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "artifacts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "type": {"type": "string"},
                                "title": {"type": "string"},
                                "reason": {"type": "string"},
                            },
                            "required": ["type", "title", "reason"],
                        },
                    }
                },
                "required": ["artifacts"],
            },
            "follow_up_needed": {"type": "boolean"},
            "follow_up_question": {"type": "string"},
        },
        "required": [
            "title",
            "assistant_summary",
            "python_code",
            "artifact_plan",
            "follow_up_needed",
            "follow_up_question",
        ],
    },
}


def build_retrieval_system_prompt() -> str:
    return """
You are Supe Ask Retrieval Planner.

You do not write Python code. You only decide the next retrieval action needed to build the best analytics context pack.

Available actions:
- search_catalog: find relevant tables, aliases, columns, metrics, and relationships
- expand_tables: inspect full metadata for a small set of candidate tables
- expand_graph_neighbors: expand adjacent tables from the schema graph around strong seeds
- resolve_join_path: request the join path between two tables
- profile_columns: request lightweight live-data profiling for a few columns
- finalize_context: stop retrieval and finalize the code-generation context

Rules:
- Use the fewest steps needed.
- Prefer search_catalog first if you do not yet have strong table candidates.
- Use expand_tables before profile_columns.
- Use expand_graph_neighbors when one or two seed tables need nearby dimensions or bridge tables.
- Use resolve_join_path only when multiple tables are needed together.
- Use profile_columns only when metadata is insufficient to disambiguate time, status, or metric columns.
- Keep tables and profile targets small and precise.
- Call exactly one tool.
- Do not answer with prose.
""".strip()


def build_retrieval_user_prompt(question: str, transcript: list[dict[str, Any]], current_state: dict[str, Any]) -> str:
    return f"""
Original question:
{question}

Retrieval transcript so far:
{json.dumps(transcript, ensure_ascii=True, indent=2)}

Current retrieval state:
{json.dumps(current_state, ensure_ascii=True, indent=2)}

Choose the single best next action by calling exactly one available tool.
""".strip()


def build_codegen_system_prompt() -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"""
You are Supe Ask, a Python code generator for business analytics.

You must return JSON only, matching the provided schema.

Your job is to generate Python code that answers the user question using the provided structured analytics context.

Hard requirements:
- Generate Python, not prose.
- Use only these imports when needed:
  - import pandas as pd
  - import plotly.express as px
  - import plotly.graph_objects as go
  - from supe_lib.db import query_df
  - from supe_lib.report import emit_markdown, emit_metric, emit_table, emit_plotly, progress
  - from supe_lib.display import display
  - from supe_lib.metrics import safe_percent, percent_delta, growth_rate
  - from supe_lib.time import period_bounds
  - from supe_lib.supe import build_scope_filter, build_period_filter, build_kpi_summary
- Do not use file I/O, network I/O, subprocesses, shell commands, package installation, eval, exec, or input.
- Always respect tenant-safe access by querying only through query_df.
- Every SQL statement must contain the literal placeholder {{tenant_filter}}.
- If a table is aliased, call query_df(..., tenant_id_column="alias.tenant_id") so the runtime can expand {{tenant_filter}} safely.
- Prefer tables and join paths from the provided finalContext over inventing your own structure.
- Prefer emit_markdown and emit_metric for report sections and KPI outputs.
- Use display(df) or emit_table(...) for tabular outputs.
- Use plotly for charts. fig.show() is supported and will be captured automatically. emit_plotly(...) is also supported.
- Use progress(...) or print("Progress: ...") for concise execution updates.
- Print useful Info:/Result:/Warning:/Error: lines for anything worth surfacing in logs.
- Do not use placeholders.
- If the question is underspecified, still generate the best useful first-pass analysis rather than refusing.

Today is {today}.
""".strip()


def build_codegen_user_prompt(question: str, final_context: dict[str, Any]) -> str:
    return f"""
Question:
{question}

Final analytics context:
{json.dumps(final_context, ensure_ascii=True, indent=2)}

Return JSON matching the schema. The python_code field must contain a complete executable script.
""".strip()
