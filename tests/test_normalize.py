import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from g2b_watch.normalize import build_key, parse_amount, parse_datetime, to_record


def test_parse_datetime_formats():
    assert parse_datetime("2026-09-01 10:30:00").startswith("2026-09-01T10:30:00+09:00")
    assert parse_datetime("202609011030").startswith("2026-09-01T10:30:00+09:00")
    assert parse_datetime("20260901").startswith("2026-09-01T00:00:00+09:00")


def test_parse_datetime_rejects_garbage():
    assert parse_datetime("") is None
    assert parse_datetime("미정") is None
    assert parse_datetime("20261301") is None  # 13월


def test_parse_amount():
    assert parse_amount("1,234,000") == 1234000
    assert parse_amount("1234000.00") == 1234000
    assert parse_amount("0") is None
    assert parse_amount("") is None
    assert parse_amount("비공개") is None


def test_build_key():
    assert build_key("bid", "20260901234", "00", "bid_thng") == "20260901234-00"
    assert build_key("bid", "20260901234", "", "bid_thng") == "20260901234"
    assert build_key("prestd", "20260901111", "", "prestd_servc") == "PS-20260901111"
    assert build_key("bid", "", "00", "bid_thng") == ""


def test_to_record_bid():
    item = {
        "bidNtceNo": "20260901234",
        "bidNtceOrd": "00",
        "bidNtceNm": "청사 BEMS 구축",
        "dminsttNm": "○○공단",
        "ntceInsttNm": "조달청",
        "bidNtceDt": "2026-09-01 09:00:00",
        "bidClseDt": "2026-09-15 10:00:00",
        "presmptPrce": "450,000,000",
        "cntrctCnclsMthdNm": "협상에의한계약",
        "bidNtceDtlUrl": "https://www.g2b.go.kr/example",
    }
    rec = to_record(item, source_id="bid_servc", kind="bid", label="용역")
    assert rec is not None
    assert rec.key == "20260901234-00"
    assert rec.price == 450000000
    assert rec.demand_org == "○○공단"
    assert rec.close_dt.startswith("2026-09-15T10:00:00")


def test_to_record_missing_key_returns_none():
    assert to_record({"bidNtceNm": "제목만 있음"}, source_id="x", kind="bid", label="용역") is None
    assert to_record({"bidNtceNo": "123"}, source_id="x", kind="bid", label="용역") is None


def test_to_record_prestd_uses_alternate_fields():
    item = {
        "bfSpecRgstNo": "20260900777",
        "prdctClsfcNoNm": "통합관제시스템",
        "orderInsttNm": "△△시",
        "rcptDt": "20260901",
        "opninRgstClseDt": "20260908",
        "asignBdgtAmt": "120,000,000",
    }
    rec = to_record(item, source_id="prestd_thng", kind="prestd", label="사전규격")
    assert rec is not None
    assert rec.key == "PS-20260900777"
    assert rec.title == "통합관제시스템"
    assert rec.price == 120000000
