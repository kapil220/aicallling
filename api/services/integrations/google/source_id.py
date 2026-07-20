"""Encode/decode a Google Sheets campaign source_id.

CampaignModel.source_id (api/db/models.py) is a single String column shared with
CSV (a bare file key). Sheets needs spreadsheet + tab + optional range, so it's
packed as ``gsheet:{spreadsheet_id}:{sheet_name}:{a1_range}`` — the trailing segment
is empty when no explicit range was chosen (full-tab read).
"""

from dataclasses import dataclass

_PREFIX = "gsheet:"


@dataclass(frozen=True)
class SheetSourceRef:
    spreadsheet_id: str
    sheet_name: str
    a1_range: str | None


def encode_sheet_source_id(
    spreadsheet_id: str, sheet_name: str, a1_range: str | None = None
) -> str:
    return f"{_PREFIX}{spreadsheet_id}:{sheet_name}:{a1_range or ''}"


def decode_sheet_source_id(source_id: str) -> SheetSourceRef:
    if not source_id.startswith(_PREFIX):
        raise ValueError(f"source_id must start with '{_PREFIX}': {source_id!r}")
    body = source_id[len(_PREFIX) :]
    parts = body.split(":", 2)
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ValueError(f"malformed gsheet source_id: {source_id!r}")
    spreadsheet_id, sheet_name = parts[0], parts[1]
    a1_range = parts[2] if len(parts) == 3 and parts[2] else None
    return SheetSourceRef(spreadsheet_id, sheet_name, a1_range)


def sheet_range(ref: SheetSourceRef) -> str:
    if ref.a1_range:
        return f"{ref.sheet_name}!{ref.a1_range}"
    return ref.sheet_name
