import pytest

from api.services.integrations.google.source_id import (
    SheetSourceRef,
    decode_sheet_source_id,
    encode_sheet_source_id,
    sheet_range,
)


def test_encode_with_range():
    assert (
        encode_sheet_source_id("1AbC", "Leads", "A1:F200")
        == "gsheet:1AbC:Leads:A1:F200"
    )


def test_encode_without_range():
    assert encode_sheet_source_id("1AbC", "Leads") == "gsheet:1AbC:Leads:"


def test_decode_round_trip_with_range():
    assert decode_sheet_source_id("gsheet:1AbC:Leads:A1:F200") == SheetSourceRef(
        "1AbC", "Leads", "A1:F200"
    )


def test_decode_round_trip_without_range():
    assert decode_sheet_source_id("gsheet:1AbC:Leads:") == SheetSourceRef(
        "1AbC", "Leads", None
    )


def test_decode_rejects_non_gsheet_prefix():
    with pytest.raises(ValueError, match="gsheet:"):
        decode_sheet_source_id("csv/some/file.csv")


def test_decode_rejects_missing_parts():
    with pytest.raises(ValueError):
        decode_sheet_source_id("gsheet:only_id")


def test_sheet_range_full_tab_when_no_range():
    assert sheet_range(SheetSourceRef("1AbC", "Leads", None)) == "Leads"


def test_sheet_range_with_explicit_range():
    assert sheet_range(SheetSourceRef("1AbC", "Leads", "A1:F200")) == "Leads!A1:F200"
