from types import SimpleNamespace

from api.services.campaign.writeback.sheets_writeback import (
    build_row_values,
    resolve_disposition,
    resolve_field,
)


def _run(**overrides):
    base = dict(
        id=42,
        state="completed",
        usage_info={"call_duration_seconds": 87.4},
        cost_info={"total_cost_usd": 0.42},
        recording_url="https://cdn/run42.wav",
        transcript_url="https://cdn/run42.txt",
        gathered_context={"call_disposition": "no_answer", "customer_intent": "buy"},
        created_at=SimpleNamespace(isoformat=lambda: "2026-07-21T10:00:00+00:00"),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _workflow(**overrides):
    base = dict(call_disposition_codes={"no_answer": "No Answer (auto)"})
    base.update(overrides)
    return SimpleNamespace(**base)


def test_resolve_field_call_state():
    assert resolve_field("call_state", _run(), _workflow()) == "completed"


def test_resolve_field_duration_nested_path():
    assert (
        resolve_field("usage_info.call_duration_seconds", _run(), _workflow()) == "87.4"
    )


def test_resolve_field_gathered_context_nested_path():
    assert (
        resolve_field("gathered_context.customer_intent", _run(), _workflow()) == "buy"
    )


def test_resolve_field_missing_path_returns_blank():
    assert resolve_field("gathered_context.nonexistent", _run(), _workflow()) == ""
    assert resolve_field("usage_info.missing", _run(), _workflow()) == ""


def test_resolve_field_recording_and_transcript_url():
    run = _run()
    assert resolve_field("recording_url", run, _workflow()) == run.recording_url
    assert resolve_field("transcript_url", run, _workflow()) == run.transcript_url


def test_resolve_field_raw_json_dump():
    assert '"customer_intent": "buy"' in resolve_field("raw_json", _run(), _workflow())


def test_resolve_disposition_matches_code():
    assert resolve_disposition(_run(), _workflow()) == "No Answer (auto)"


def test_resolve_disposition_falls_back_to_raw_string():
    run = _run(gathered_context={"call_disposition": "unmapped_code"})
    assert resolve_disposition(run, _workflow()) == "unmapped_code"


def test_resolve_disposition_blank_when_absent():
    assert resolve_disposition(_run(gathered_context={}), _workflow()) == ""


def test_build_row_values_applies_full_mapping():
    mapping = {
        "F": "call_disposition",
        "G": "usage_info.call_duration_seconds",
        "H": "recording_url",
        "I": "gathered_context.customer_intent",
    }
    row = build_row_values(mapping, _run(), _workflow())
    assert row == {
        "F": "No Answer (auto)",
        "G": "87.4",
        "H": "https://cdn/run42.wav",
        "I": "buy",
    }
