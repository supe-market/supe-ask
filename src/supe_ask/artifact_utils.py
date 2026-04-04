from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from .config import settings


OFFLOAD_CANDIDATE_TYPES = {"plotly", "table", "log"}


def json_byte_size(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=True, default=str).encode("utf-8"))


def should_offload_artifact(artifact_type: str, payload: dict[str, Any]) -> bool:
    if artifact_type not in OFFLOAD_CANDIDATE_TYPES:
        return False
    return json_byte_size(payload) > settings.artifact_s3_threshold_bytes


def build_preview_payload(artifact_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if artifact_type == "table":
        rows = list(payload.get("rows") or [])
        preview = dict(payload)
        preview["rows"] = rows[: settings.max_table_rows]
        preview["rowCount"] = int(payload.get("rowCount") or len(rows))
        return preview
    if artifact_type == "log":
        lines = [str(line) for line in payload.get("lines") or []]
        preview = dict(payload)
        preview["lines"] = lines[:200]
        return preview
    if artifact_type == "plotly":
        preview = deepcopy(payload)
        data = preview.get("data")
        if isinstance(data, list):
            preview["data"] = [_truncate_trace(dict(trace) if isinstance(trace, dict) else trace) for trace in data[:8]]
        return preview
    return dict(payload)


def _truncate_trace(trace: Any) -> Any:
    if not isinstance(trace, dict):
        return trace
    for key, value in list(trace.items()):
        if isinstance(value, list) and len(value) > 100:
            trace[key] = value[:100]
        elif isinstance(value, dict):
            trace[key] = _truncate_trace(value)
    return trace
