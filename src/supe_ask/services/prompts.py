from __future__ import annotations

import json
import re
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
- Do not call: exit(), quit(), open(), eval(), exec(), compile(), input(). These are blocked.
- Do not import or access: os, sys, subprocess, socket, requests, httpx, pathlib, shutil.
- Never use placeholders. python_code must be a complete, executable script — never a partial patch or diff.
- Never save a DataFrame to a file or read data from a file.
- pd.merge does not preserve the index of either DataFrame. Reset or re-derive the index after any merge before using index-based access.
- When using while loops, always include a termination condition to avoid infinite loops.
- Column names: only use columns that appear in the relevantTables schema. Never invent names.
- SQL parameters: always use psycopg2 %(name)s placeholders.
- JOIN keys: prefer the columns listed in each table's joinKeys array for JOIN ON conditions. These are internal IDs (e.g. brand_id, distributor_id) and are the safest choice. Avoid joining on external display attributes (name, code, external_*) unless the user explicitly asks for it or no internal join path exists.
- Any query_df call whose SQL joins two or more tables MUST include tenant_id_column="<fact_alias>.tenant_id" (e.g. tenant_id_column="so.tenant_id"). A bare tenant_id filter on a JOIN query causes psycopg2.errors.AmbiguousColumn.
- SQL column aliases must be valid identifiers — never write AS <number> (e.g. AS 0). Wrong: COUNT(*) AS 0 AS total_orders. Correct: COUNT(*) AS total_orders.

Today is {today}.
""".strip()


def build_correction_turn_prompt(error_message: str, execution_output: str, current_code: str = "") -> str:
    """Build the user turn that feeds an execution failure back into the conversation."""
    output_section = ""
    if execution_output and execution_output.strip():
        output_section = f"\nExecution output before failure:\n{execution_output.strip()}\n"
    code_section = ""
    if current_code and current_code.strip():
        code_section = f"\nCurrent code:\n```python\n{current_code.strip()}\n```\n"
    return f"""Code execution failed.{output_section}{code_section}
Error:
{error_message}

Analyze the error and the output above. Fix only what is broken — do not change the analysis intent, restructure sections, or add new features. If the error is minor, correct only the affected lines. Include Debug: print statements if the root cause is unclear.""".strip()


def apply_search_replace(original: str, diff_text: str) -> str:
    """Apply SEARCH/REPLACE diff blocks produced by Claude to source code.

    Expects blocks in the standard merge-conflict style:
        <<<<<<< SEARCH
        <exact lines to find>
        =======
        <replacement lines>
        >>>>>>> REPLACE
    """
    pattern = re.compile(
        r"<<<<<<< SEARCH\n(.*?)\n=======\n(.*?)\n>>>>>>> REPLACE",
        re.DOTALL,
    )
    result = original
    for match in pattern.finditer(diff_text):
        search_text = match.group(1)
        replace_text = match.group(2)
        if search_text in result:
            result = result.replace(search_text, replace_text, 1)
    return result


def build_correction_system_prompt_claude() -> str:
    """Correction system prompt for Claude tool-use — instructs SEARCH/REPLACE diffs."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"""
You are Supe Ask, a Python code debugger for business analytics.

The code you generated previously failed at runtime. Your only job is to fix the specific error.

Use the generate_correction tool. Return the fix as one or more SEARCH/REPLACE blocks in the
search_replace_diff field — change only the broken lines. Unchanged code must not appear in the diff.

SEARCH/REPLACE block format (each change must be a separate block):
<<<<<<< SEARCH
<exact lines to find in the current code, verbatim>
=======
<replacement lines>
>>>>>>> REPLACE

Hard rules:
- Fix only what is broken. Do not change the analysis intent, restructure sections, or add new features.
- When fixing a wrong SQL column name in a query, audit ALL columns in that same SQL block and fix every other column that is not in the relevantTables schema. Fix them all in a single SEARCH/REPLACE block covering the whole SELECT/FROM/JOIN/WHERE/GROUP BY section — do not leave any guessed column names in the same query.
- All the same import, SQL, and library rules from the original system prompt apply.
- Do not call: exit(), quit(), open(), eval(), exec(), compile(), input(). These are blocked.
- Do not import or access: os, sys, subprocess, socket, requests, httpx, pathlib, shutil.
- Never use placeholders. The search_replace_diff must be complete and applicable.
- Never save a DataFrame to a file or read data from a file.
- pd.merge does not preserve the index of either DataFrame. Reset or re-derive the index after any merge before using index-based access.
- When using while loops, always include a termination condition to avoid infinite loops.
- Column names: only use columns that appear in the relevantTables schema. Never invent names.
- SQL parameters: always use psycopg2 %(name)s placeholders.
- JOIN keys: prefer the columns listed in each table's joinKeys array for JOIN ON conditions.
- Any query_df call whose SQL joins two or more tables MUST include tenant_id_column="<fact_alias>.tenant_id". Schema-only queries (e.g. information_schema) that need no tenant filter must pass tenant_id_column=None.
- SQL column aliases must be valid identifiers — never write AS <number>.

Today is {today}.
""".strip()


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
- The following functions and libraries are pre-imported and available in your execution environment. You do not need to import them, but you may include import statements if you prefer — both paths work:
  - pandas (as pd), plotly.express (as px), plotly.graph_objects (as go), ThreadPoolExecutor
  - query_df, query_records, query_scalar
  - emit_markdown, emit_metric, emit_table, emit_plotly, emit_kpi_card, emit_section, emit_summary, emit_highlights, progress
  - fmt_currency, fmt_percent, fmt_number
  - safe_percent, percent_delta, growth_rate
  - bar_chart, line_chart, pie_chart, waterfall_chart
  - build_scope_filter, build_period_filter, build_kpi_summary
  - period_bounds, display
- Do not import any other modules besides the above. Only these are available.
- Do not call: exit(), quit(), open(), eval(), exec(), compile(), input(), __import__(). These are blocked and will raise a validation error before execution.
- Do not import or access: os, sys, subprocess, socket, requests, httpx, pathlib, shutil — these modules are blocked. Do not use them even via attribute access (e.g. os.path, sys.exit).
- Always query data through query_df, query_records, or query_scalar — never use raw psycopg2 or sqlalchemy directly.
- The database is PostgreSQL. Write strictly valid PostgreSQL — correct syntax, functions, casts, and interval arithmetic.
- SQL parameters: always use psycopg2 %(name)s placeholders. Never use :name, ?, or f-string interpolation for values.
- Column names: only use columns that appear in the relevantTables schema provided in the context. Never invent or guess column names. If a column you need is not in the schema, note the gap in working_assumptions and work around it.
- ORDER BY: column aliases defined in SELECT are visible in a bare ORDER BY (ORDER BY alias) but not inside expressions. Never write ORDER BY CASE WHEN alias = ... — repeat the full expression or use a positional index (ORDER BY 1).
- SQL column aliases must be valid identifiers. Never write `AS <number>` (e.g. AS 0, AS 1) — that is a syntax error. Wrong: `COUNT(*) AS 0 AS total_orders`. Correct: `COUNT(*) AS total_orders`.
- Tenant isolation is enforced automatically by the runtime. Write plain SQL without any tenant_id filter — do not add WHERE tenant_id = ... manually.
- CRITICAL — tenant_id_column is MANDATORY for any query that joins two or more tables. The runtime injects a bare "tenant_id = ..." filter; without an explicit alias it becomes ambiguous and crashes with psycopg2.errors.AmbiguousColumn. Rules:
    1. Single-table query (no JOIN): you may omit tenant_id_column and the default works fine.
    2. Any query with a JOIN: you MUST pass tenant_id_column="<primary_fact_table_alias>.tenant_id":
        df = query_df(sql, params=params, tenant_id_column="so.tenant_id")
    The primary fact table alias is the alias of the main driving table (e.g. "so" for sales_orders, "soi" for sales_order_items, "op" for order_payments).
- Every table in relevantTables has a joinKeys array of internal database IDs (e.g. brand_id, distributor_id). Prefer these for all JOIN ON conditions and foreign-key WHERE filters — they are stable, indexed, and unambiguous. Display attributes (names, codes, external_* fields) are for SELECT output; avoid using them as join conditions unless the user explicitly requests it or no internal path exists. Example: join sales_orders to brands using so.brand_id = b.id (from joinKeys), then SELECT b.name for display.
- When the user refers to a specific entity by a code or identifier (e.g. "employee code GGNLP33", "distributor code D001"), look up the column with semanticRole="identifier" on that table in candidateColumns — that is the correct WHERE filter column. Never substitute another code column. Example: for salesmen, the identifier column is employee_code (not salesman_code which is informational only and not unique).
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
- Use your judgment on what artifacts best answer the question, but keep it analytically rich. Think like a leadership-console dashboard, not a chatbot reply.
- Available artifact types: emit_kpi_card (metric cards), emit_highlights (insight bullets), emit_table (data tables), emit_section (section dividers), emit_summary/emit_markdown (narrative text), bar_chart/line_chart/pie_chart/waterfall_chart (charts), and emit_plotly (custom Plotly figures).
- Prefer a mix of KPI cards, charts, and tables when the data supports it, but do not force artifacts that don't add value. A question that needs one table should get one table — not three filler KPI cards.
- Go beyond the literal question. If the user asks about revenue, also surface the top/bottom contributors, the trend, and any anomalies. Think about what related analysis would keep a sales leader engaged in an analytical rabbit-hole exploration.
- Use emit_section to structure the report into visible blocks — overview, trend, contributor breakdown, risks/opportunities — as appropriate for the question.
- The artifact_plan.report_sections field must describe the major answer blocks in display order.
- emit_highlights expects a list of objects with keys title, detail, value, and tone. Use it for executive takeaways after metrics are computed.
- Final highlight values must come from executed code, never from artifact_plan.
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
- emit_kpi_card accepts ONLY these keyword arguments: label, current, previous, unit, title, benchmark, value, delta_value, delta_label, positive_good, tone. Never pass previous_label or any other argument — it does not exist and will raise TypeError at runtime. Correct forms: emit_kpi_card(label="Revenue", current=val, previous=prev, unit="currency") or emit_kpi_card(title="Revenue", value=val, delta_value="+12%", delta_label="vs last month")
- Never use placeholders in code. Always generate the complete, executable script in one pass — never emit partial patches, diffs, or stubs.
- Never save a DataFrame to a file or read data from a file. All data must come from query_df/query_scalar/query_records calls.
- pd.merge does not preserve the index of either DataFrame. After a merge, reset or re-derive the index before using index-based access (.loc, .iloc, or index arithmetic).
- When using while loops, always include a termination condition to avoid infinite loops.
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
