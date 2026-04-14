from __future__ import annotations

import json
from datetime import date, datetime, timezone
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
                    "report_sections": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "title": {"type": "string"},
                                "subtitle": {"type": "string"},
                            },
                            "required": ["title", "subtitle"],
                        },
                    },
                    "working_assumptions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 4,
                    },
                    "suggested_next_questions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 3,
                        "maxItems": 3,
                    },
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
                "required": ["report_sections", "suggested_next_questions", "artifacts"],
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


CORRECTION_JSON_SCHEMA = {
    "name": "supe_ask_correction_response",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "python_code": {"type": "string"},
            "correction_summary": {"type": "string"},
        },
        "required": ["python_code", "correction_summary"],
    },
}


def build_correction_system_prompt() -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"""
You are Supe Ask, a Python code debugger for business analytics.

The code you generated previously failed at runtime. Your only job is to fix the specific error.

Hard rules:
- Return JSON only: python_code (complete fixed script) and correction_summary (one sentence describing what you fixed).
- Fix only what is broken. Do not change the analysis intent, restructure sections, or add new features.
- python_code must be a complete, executable script — not a diff or patch.
- All the same import, SQL, and library rules from the original system prompt apply.
- Column names: only use columns that appear in the relevantTables schema. Never invent names.
- SQL parameters: always use psycopg2 %(name)s placeholders.

Today is {today}.
""".strip()


def build_correction_turn_prompt(error_message: str, execution_output: str) -> str:
    """Build the user turn that feeds an execution failure back into the conversation.

    Mirrors ScalerField's ``generate_error_context`` — the model already has the
    original question, context, and its prior code in the conversation history,
    so this turn only needs to supply what changed: the error and any output
    that was produced before the failure.
    """
    output_section = ""
    if execution_output and execution_output.strip():
        output_section = f"\nExecution output before failure:\n{execution_output.strip()}\n"
    return f"""Code execution failed.{output_section}
Error:
{error_message}

Analyze the error and the output above. Fix only what is broken — do not change the analysis intent, restructure sections, or add new features. If the error is minor, correct only the affected lines. Include Debug: print statements if the root cause is unclear.

Return JSON with python_code (the complete corrected script) and correction_summary (one sentence describing what you fixed).""".strip()


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(nested_value) for key, nested_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


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
  - from concurrent.futures import ThreadPoolExecutor
  - from supe_lib.charts import bar_chart, line_chart, pie_chart, waterfall_chart
  - from supe_lib.db import query_df
  - from supe_lib.report import emit_markdown, emit_metric, emit_table, emit_plotly, progress, emit_kpi_card, emit_section, emit_summary, emit_highlights, fmt_currency, fmt_percent, fmt_number
  - from supe_lib.display import display
  - from supe_lib.metrics import safe_percent, percent_delta, growth_rate
  - from supe_lib.time import period_bounds
  - from supe_lib.supe import build_scope_filter, build_period_filter, build_kpi_summary
- Do not use file I/O, network I/O, subprocesses, shell commands, package installation, eval, exec, or input.
- Always query data through query_df, query_records, or query_scalar — never use raw psycopg2 or sqlalchemy directly.
- The database is PostgreSQL. Write strictly valid PostgreSQL — correct syntax, functions, casts, and interval arithmetic.
- SQL parameters: always use psycopg2 %(name)s placeholders. Never use :name, ?, or f-string interpolation for values.
- Column names: only use columns that appear in the relevantTables schema provided in the context. Never invent or guess column names. If a column you need is not in the schema, note the gap in working_assumptions and work around it.
- ORDER BY: column aliases defined in SELECT are visible in a bare ORDER BY (ORDER BY alias) but not inside expressions. Never write ORDER BY CASE WHEN alias = ... — repeat the full expression or use a positional index (ORDER BY 1).
- Tenant isolation is enforced automatically by the runtime. Write plain SQL without any tenant_id filter — do not add WHERE tenant_id = ... manually.
- If a table is aliased in the query, pass tenant_id_column="alias.tenant_id" to query_df so the runtime can locate the right column:
    df = query_df(sql, params=params, tenant_id_column="so.tenant_id")
- Prefer the provided questionGrounding, analysisPlan, relevantTables, and joinPaths over inventing structure.
- Treat final_context.queryGuardrails as hard constraints, especially blockedTables and preferredFactTables.
- Use semanticPolicies.datePolicies, semanticPolicies.thresholdPolicies, and semanticPolicies.metricAliases when present before making assumptions.
- NEVER query entity_metric_snapshots, target_progress_snapshots, or any table whose name ends in _snapshots or _summary for business KPI answers. These tables contain stale pre-aggregated data and will produce wrong results.
- ALWAYS recalculate business metrics (revenue, billing, collection, outstanding, coverage, achievement) from the raw fact tables: sales_orders, sales_order_items, and order_payments. Aggregating from these tables is required — do not shortcut with snapshot rows.
- The queryGuardrails.blockedTables list in final_context is an ABSOLUTE block — never reference those tables in any SQL statement, even in a subquery or CTE.
- If raw fact tables are not in relevantTables, use what is available but note the limitation in working_assumptions. Never fall back to snapshot tables silently.
- Every executive answer must feel like a dashboard, not a single-number lookup.
- Before answering, reason through the missing scope choices: period, business scope, comparison baseline, and the first useful drill-downs.
- If the question is underspecified but still answerable, proceed with sensible defaults instead of blocking. Capture those defaults in artifact_plan.working_assumptions as 1 to 4 concise bullets.
- Only set follow_up_needed=true when the analysis is genuinely blocked by a critical ambiguity or missing dataset.
- For KPI-style questions, generate:
  - 3 to 6 KPI cards using emit_kpi_card or emit_metric
  - 1 highlights artifact using emit_highlights(...) after the metric values are computed
  - at least 1 chart (trend, distribution, ranking, or waterfall) when there are enough rows
  - at least 1 supporting table or ranking cut
  - related analysis around likely follow-up topics such as trend, contributor breakdown, concentration, and exceptions
- Use emit_section to structure the report into visible blocks such as overview, trend, contributor breakdown, and risks/opportunities.
- Prepare a leadership-console answer shape, not a generic chatbot response.
- The artifact_plan.report_sections field must describe the major answer blocks in display order.
- When highlights are relevant, declare a highlights artifact in artifact_plan.artifacts and generate python_code that emits it with emit_highlights(...).
- Final highlight values must come from executed code via the emitted highlights artifact, never from artifact_plan.
- emit_highlights expects a list of objects with keys title, detail, value, and tone. Do not pass a list of plain strings unless there is no better structure available.
- Preferred chart helper shapes are:
  - line_chart(data_frame=df, x="date_col", y="metric_col", title="Trend", labels={{"date_col": "Date", "metric_col": "Revenue"}})
  - bar_chart(data_frame=df, x="metric_col", y="category_col", orientation="h", title="Ranking", labels={{"metric_col": "Revenue", "category_col": "Distributor"}})
- IMPORTANT: line_chart, bar_chart, pie_chart, and waterfall_chart auto-emit the chart when called. Never call emit_plotly() on their return value — doing so emits the chart twice. Only use emit_plotly() when building a figure manually with plotly.graph_objects.
- The assistant_summary should begin with a short interpretation of the question and, when assumptions were needed, explicitly state them before the dashboard narrative.
- The artifact_plan.suggested_next_questions field must contain exactly 3 natural follow-up questions that a sales leader is likely to ask next.
- Prefer emit_summary, emit_section, emit_kpi_card, emit_metric, emit_table, and the chart helpers for dashboard outputs.
- Use display(df) or emit_table(df, title="...") for tabular outputs. emit_table signature is emit_table(frame, title, max_rows) — it has no column_headers argument. To rename columns for display, use df.rename(columns={...}) before passing to emit_table.
- Use plotly for charts. fig.show() is supported and will be captured automatically. emit_plotly(...) is also supported.
- Use progress(...) or print("Progress: ...") for concise execution updates.
- Print useful Info:/Result:/Warning:/Error: lines for anything worth surfacing in logs.
- For time filtering, prefer build_period_filter(date_column, period, today=...) instead of calling period_bounds directly.
- build_period_filter returns a reusable SQL clause helper. Safe patterns are:
  - period_clause, period_params = build_period_filter(...)
  - params = {{**period_params}}
  - sql = f"... {{period_clause}} ..."
  - df = query_df(sql, params=params, tenant_id_column="alias.tenant_id")
- CRITICAL — build_period_filter always uses the same param names (period_start, period_end) for every period. Never merge two sets of params from different build_period_filter calls into one query — they will silently overwrite each other and produce wrong results. The safe patterns are:
  1. Separate queries via ThreadPoolExecutor (one future per period) — always safe, preferred:
     mtd_clause,  mtd_params  = build_period_filter(col, 'mtd',  today=TODAY)
     pmtd_clause, pmtd_params = build_period_filter(col, 'pmtd', today=TODAY)
     fut_mtd  = pool.submit(query_df, f"SELECT ... WHERE {{mtd_clause}}",  params=mtd_params)
     fut_pmtd = pool.submit(query_df, f"SELECT ... WHERE {{pmtd_clause}}", params=pmtd_params)
  2. Single query with explicit param names to avoid collision:
     mtd_clause,  mtd_params  = build_period_filter(col, 'mtd',  today=TODAY, start_param='mtd_start',  end_param='mtd_end')
     pmtd_clause, pmtd_params = build_period_filter(col, 'pmtd', today=TODAY, start_param='pmtd_start', end_param='pmtd_end')
     sql = f"SELECT 'mtd' ... WHERE {{mtd_clause}} UNION ALL SELECT 'pmtd' ... WHERE {{pmtd_clause}}"
     df  = query_df(sql, params={{**mtd_params, **pmtd_params}})  # keys are now distinct, safe to merge
- When you need 2 or more query_df calls whose inputs do not depend on each other's results, always fetch them in parallel using ThreadPoolExecutor. Submit all futures first, then emit each section as soon as its own future resolves — do not wait for all queries to finish before emitting anything. This is mandatory — serial queries are the single largest source of slow responses. Pattern:
  from concurrent.futures import ThreadPoolExecutor
  with ThreadPoolExecutor() as pool:
      fut_kpi   = pool.submit(query_df, kpi_sql,   params=params_kpi)
      fut_trend = pool.submit(query_df, trend_sql,  params=params_trend)
      fut_dist  = pool.submit(query_df, dist_sql,   params=params_dist, tenant_id_column="d.tenant_id")
      # emit each section as soon as its data is ready — other queries keep running in parallel
      kpi_df   = fut_kpi.result();   emit_kpi_cards(kpi_df)
      trend_df = fut_trend.result(); emit_trend_chart(trend_df)
      dist_df  = fut_dist.result();  emit_dist_chart(dist_df)
- If you do call period_bounds, the first positional argument must be the period label, and any current date must be passed as today=...
- Never pass a period label such as "mtd" or "qtd" as the today/date argument.
- Supported comparison periods include pmtd for previous-month-to-date.
- emit_kpi_card accepts either emit_kpi_card(label, current, previous, unit="currency") or emit_kpi_card(title="Revenue", value=..., delta_value=..., delta_label=...)
- Do not use placeholders.
- If the question is underspecified, still generate the best useful first-pass analysis rather than refusing.

Today is {today}.
""".strip()


def build_codegen_user_prompt(question: str, final_context: dict[str, Any]) -> str:
    return f"""
Question:
{question}

Final analytics context:
{json.dumps(_json_safe(final_context), ensure_ascii=True, indent=2)}

Return JSON matching the schema. The python_code field must contain a complete executable script.
""".strip()
