"""Column-mapping resolution for Google Sheets write-back.

Reads from WorkflowRunModel (api/db/models.py: gathered_context, usage_info,
cost_info, recording_url, transcript_url, call_type, state) and
WorkflowModel.call_disposition_codes — no new per-call fields required, per the
phase-5 spec. Pure functions: no DB/network access, fully unit-testable.
"""

import json
from typing import Any


def _get_nested(obj: Any, path: str) -> Any:
    """obj.<a>.<b> where obj may be an attribute-bearing object (model/
    SimpleNamespace) whose leaf may itself be a dict (JSON column)."""
    parts = path.split(".")
    current = obj
    for part in parts:
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(part)
        else:
            current = getattr(current, part, None)
    return current


def resolve_disposition(workflow_run, workflow) -> str:
    gathered_context = workflow_run.gathered_context or {}
    raw = gathered_context.get("call_disposition")
    if not raw:
        return ""
    codes = workflow.call_disposition_codes or {}
    return codes.get(raw, raw)


def resolve_field(field_path: str, workflow_run, workflow) -> str:
    if field_path == "call_state":
        return str(workflow_run.state or "")
    if field_path == "call_disposition":
        return resolve_disposition(workflow_run, workflow)
    if field_path == "recording_url":
        return workflow_run.recording_url or ""
    if field_path == "transcript_url":
        return workflow_run.transcript_url or ""
    if field_path == "call_timestamp":
        created_at = getattr(workflow_run, "created_at", None)
        return created_at.isoformat() if created_at else ""
    if field_path == "raw_json":
        return json.dumps(workflow_run.gathered_context or {})

    value = _get_nested(workflow_run, field_path)
    if value is None:
        return ""
    return str(value)


def build_row_values(
    column_mapping: dict[str, str], workflow_run, workflow
) -> dict[str, str]:
    return {
        column: resolve_field(field_path, workflow_run, workflow)
        for column, field_path in column_mapping.items()
    }
