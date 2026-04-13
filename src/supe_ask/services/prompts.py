from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any


SEMANTIC_RESOLUTION_JSON_SCHEMA = {
    "name": "supe_ask_semantic_resolution",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "reasoning": {"type": "string"},
            "canonical_question_number": {"type": ["integer", "null"]},
            "cluster_key": {"type": "string"},
            "intent": {"type": "string"},
            "matched_entities": {"type": "array", "items": {"type": "string"}},
            "matched_metrics": {"type": "array", "items": {"type": "string"}},
            "matched_time_grain": {"type": "string"},
            "filters": {"type": "array", "items": {"type": "string"}},
            "grouping": {"type": "array", "items": {"type": "string"}},
            "outputs": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number"},
            "fallback_used": {"type": "boolean"},
        },
        "required": [
            "reasoning",
            "canonical_question_number",
            "cluster_key",
            "intent",
            "matched_entities",
            "matched_metrics",
            "matched_time_grain",
            "filters",
            "grouping",
            "outputs",
            "confidence",
            "fallback_used",
        ],
    },
}


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
                    "key_highlights": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "title": {"type": "string"},
                                "detail": {"type": "string"},
                                "value": {"type": "string"},
                                "tone": {"type": "string"},
                            },
                            "required": ["title", "detail", "value", "tone"],
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
                "required": ["report_sections", "key_highlights", "suggested_next_questions", "artifacts"],
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


def _grounding_summary(grounding_context: dict[str, Any]) -> str:
    summary = grounding_context.get("summary") or {}
    lines = [
        "Resident semantic catalog summary:",
        f"- clusters: {summary.get('clusterCount', 0)}",
        f"- canonical_questions: {summary.get('canonicalQuestionCount', 0)}",
        f"- variants: {summary.get('variantCount', 0)}",
        f"- entities: {summary.get('entityCount', 0)}",
        f"- metrics: {summary.get('metricCount', 0)}",
    ]
    return "\n".join(lines)


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


def build_semantic_resolution_system_prompt(grounding_context: dict[str, Any]) -> str:
    return f"""
You are Supe Ask Semantic Resolver.

Your job is to resolve the user's business question against a resident FMCG semantic catalog.

Return JSON only matching the provided schema.

Rules:
- Choose the most relevant canonical question family from the provided candidates.
- Prefer the provided semantic candidates over inventing new entities or metrics.
- Keep time semantics explicit. If the question is month-oriented and does not specify otherwise, prefer MTD.
- Resolve business language like billing -> revenue, coverage -> coverage, outstanding -> outstanding, collection -> collection.
- Use only entity and metric keys present in the provided candidates when possible.
- Set fallback_used=true only if the candidates were weak and you had to infer beyond exact matches.
- Confidence must be between 0 and 1.

{_grounding_summary(grounding_context)}
""".strip()


def build_semantic_resolution_user_prompt(question: str, grounding_context: dict[str, Any]) -> str:
    return f"""
Question:
{question}

Semantic candidates:
{json.dumps(_json_safe(grounding_context), ensure_ascii=True, indent=2)}

Resolve the question into a typed semantic grounding.
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
  - from supe_lib.charts import bar_chart, line_chart, pie_chart, waterfall_chart
  - from supe_lib.db import query_df
  - from supe_lib.report import emit_markdown, emit_metric, emit_table, emit_plotly, progress, emit_kpi_card, emit_section, emit_summary, fmt_currency, fmt_percent, fmt_number
  - from supe_lib.display import display
  - from supe_lib.metrics import safe_percent, percent_delta, growth_rate
  - from supe_lib.time import period_bounds
  - from supe_lib.supe import build_scope_filter, build_period_filter, build_kpi_summary
- Do not use file I/O, network I/O, subprocesses, shell commands, package installation, eval, exec, or input.
- Always respect tenant-safe access by querying only through query_df.
- Every SQL statement must contain the literal placeholder {{tenant_filter}}.
- If a table is aliased, call query_df(..., tenant_id_column="alias.tenant_id") so the runtime can expand {{tenant_filter}} safely.
- Prefer the provided questionGrounding, analysisPlan, relevantTables, and joinPaths over inventing structure.
- Treat final_context.queryGuardrails as hard constraints, especially blockedTables and preferredFactTables.
- Use semanticPolicies.datePolicies, semanticPolicies.thresholdPolicies, and semanticPolicies.metricAliases when present before making assumptions.
- Do not answer a business metric question by merely selecting one pre-aggregated row from a snapshot or summary table unless the user explicitly asked for a stored snapshot metric.
- Prefer calculating the requested metric from the most granular relevant fact tables available in final_context. Use summary tables only as a fallback or benchmark.
- Do not query entity_metric_snapshots for business KPI answers. Recalculate metrics from raw fact tables such as sales_orders and sales_order_items.
- Every executive answer must feel like a dashboard, not a single-number lookup.
- Before answering, reason through the missing scope choices: period, business scope, comparison baseline, and the first useful drill-downs.
- If the question is underspecified but still answerable, proceed with sensible defaults instead of blocking. Capture those defaults in artifact_plan.working_assumptions as 1 to 4 concise bullets.
- Only set follow_up_needed=true when the analysis is genuinely blocked by a critical ambiguity or missing dataset.
- For KPI-style questions, generate:
  - 3 to 6 KPI cards using emit_kpi_card or emit_metric
  - at least 1 chart (trend, distribution, ranking, or waterfall) when there are enough rows
  - at least 1 supporting table or ranking cut
  - related analysis around likely follow-up topics such as trend, contributor breakdown, concentration, and exceptions
- Use emit_section to structure the report into visible blocks such as overview, trend, contributor breakdown, and risks/opportunities.
- Prepare a leadership-console answer shape, not a generic chatbot response.
- The artifact_plan.report_sections field must describe the major answer blocks in display order.
- The artifact_plan.key_highlights field must contain concise ranked business callouts with concrete business-facing values or impact strings. Never use implementation placeholders such as "calculated in script", "computed at runtime", "from SQL", or "from query".
- The assistant_summary should begin with a short interpretation of the question and, when assumptions were needed, explicitly state them before the dashboard narrative.
- The artifact_plan.suggested_next_questions field must contain exactly 3 natural follow-up questions that a sales leader is likely to ask next.
- Prefer emit_summary, emit_section, emit_kpi_card, emit_metric, emit_table, and the chart helpers for dashboard outputs.
- Use display(df) or emit_table(...) for tabular outputs.
- Use plotly for charts. fig.show() is supported and will be captured automatically. emit_plotly(...) is also supported.
- Use progress(...) or print("Progress: ...") for concise execution updates.
- Print useful Info:/Result:/Warning:/Error: lines for anything worth surfacing in logs.
- For time filtering, prefer build_period_filter(date_column, period, today=...) instead of calling period_bounds directly.
- If you do call period_bounds, the first positional argument must be the period label, and any current date must be passed as today=...
- Never pass a period label such as "mtd" or "qtd" as the today/date argument.
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
