"""조달청 OpenAPI 응답 아이템을 Notion 적재용 공통 레코드로 변환.

응답 필드명은 서비스/오퍼레이션 버전마다 조금씩 다르고, 이 저장소를 만든 시점에
공공데이터포털 문서 원문을 직접 열어 확인하지 못했다. 그래서 필드는 단일 키가 아니라
"후보 키 목록"으로 정의하고, 존재하는 첫 번째 값을 취한다.
probe 명령의 --dump 옵션으로 실제 응답 키를 확인한 뒤 이 목록을 정리하면 된다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

KST = timezone(timedelta(hours=9))

# --- 필드 후보 정의 -----------------------------------------------------------

BID_FIELDS: dict[str, tuple[str, ...]] = {
    "notice_no": ("bidNtceNo",),
    "notice_ord": ("bidNtceOrd",),
    "title": ("bidNtceNm",),
    "notice_kind": ("ntceKindNm",),
    "demand_org": ("dminsttNm", "dminsttOfclNm"),
    "notice_org": ("ntceInsttNm",),
    "notice_dt": ("bidNtceDt", "rgstDt"),
    "close_dt": ("bidClseDt", "bidBeginDt"),
    "opening_dt": ("opengDt",),
    "price": ("presmptPrce", "asignBdgtAmt", "bdgtAmt"),
    "contract_method": ("cntrctCnclsMthdNm", "bidMethdNm"),
    "url": ("bidNtceDtlUrl", "bidNtceUrl"),
    "ref_no": ("refNo", "ntceSpecDocUrl1"),
}

PRESTD_FIELDS: dict[str, tuple[str, ...]] = {
    "notice_no": ("bfSpecRgstNo", "prdctClsfcNo"),
    "notice_ord": (),
    "title": ("prdctClsfcNoNm", "bfSpecRgstNoNm", "prdctNm", "bidNtceNm"),
    "notice_kind": ("bsnsDivNm", "prcrmntDivNm"),
    "demand_org": ("rlDminsttNm", "dminsttNm", "rcptInsttNm"),
    "notice_org": ("orderInsttNm", "ntceInsttNm", "rgstInsttNm"),
    "notice_dt": ("rcptDt", "rgstDt", "bfSpecRgstDt"),
    "close_dt": ("opninRgstClseDt", "opninRgstDt"),
    "opening_dt": (),
    "price": ("asignBdgtAmt", "bdgtAmt", "presmptPrce"),
    "contract_method": ("cntrctMthdNm",),
    "url": ("specDocFileUrl1", "bfSpecDtlUrl"),
    "ref_no": ("bfSpecRgstNo",),
}

FIELD_MAP = {"bid": BID_FIELDS, "prestd": PRESTD_FIELDS}


@dataclass
class Record:
    """Notion DB 한 행에 대응하는 정규화 레코드."""

    key: str  # 공고번호 (중복 적재 방지 고유키)
    title: str
    source_id: str
    business_type: str  # "물품" | "용역" | "사전규격"
    notice_kind: str = ""
    demand_org: str = ""
    notice_org: str = ""
    notice_dt: str | None = None  # ISO-8601 (+09:00)
    close_dt: str | None = None
    opening_dt: str | None = None
    price: int | None = None
    contract_method: str = ""
    url: str | None = None
    ref_no: str = ""
    axes: tuple[str, ...] = ()
    score: int = 0
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


def _first(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() not in {"none", "null", "-"}:
            return text
    return ""


_DATE_DIGITS = re.compile(r"\d+")


def parse_datetime(value: str) -> str | None:
    """'2026-09-01 10:00:00', '202609011000', '20260901' 등을 ISO-8601(KST)로.

    파싱 불가면 None 을 돌려주고 호출 측에서 해당 속성을 비운다(임의 값 추정 금지).
    """
    if not value:
        return None
    digits = "".join(_DATE_DIGITS.findall(value))
    layouts = {14: "%Y%m%d%H%M%S", 12: "%Y%m%d%H%M", 10: "%Y%m%d%H", 8: "%Y%m%d"}
    layout = layouts.get(len(digits))
    if layout is None:
        return None
    try:
        parsed = datetime.strptime(digits, layout)
    except ValueError:
        return None
    return parsed.replace(tzinfo=KST).isoformat()


def parse_amount(value: str) -> int | None:
    """'1,234,000' / '1234000.00' → 1234000. 0 이하이거나 파싱 불가면 None."""
    if not value:
        return None
    cleaned = re.sub(r"[^\d.\-]", "", value)
    if not cleaned or cleaned in {"-", ".", "-."}:
        return None
    try:
        amount = int(float(cleaned))
    except ValueError:
        return None
    return amount if amount > 0 else None


def build_key(kind: str, notice_no: str, notice_ord: str, source_id: str) -> str:
    """중복 판정 키. 입찰공고는 공고번호-차수, 사전규격은 등록번호."""
    if not notice_no:
        return ""
    if kind == "bid" and notice_ord:
        return f"{notice_no}-{notice_ord}"
    if kind == "prestd":
        return f"PS-{notice_no}"
    return notice_no


def to_record(item: dict[str, Any], *, source_id: str, kind: str, label: str) -> Record | None:
    """API 아이템 1건을 Record 로. 고유키나 제목이 없으면 None(적재 불가)."""
    fields = FIELD_MAP.get(kind, BID_FIELDS)
    notice_no = _first(item, fields["notice_no"])
    notice_ord = _first(item, fields["notice_ord"])
    title = _first(item, fields["title"])
    key = build_key(kind, notice_no, notice_ord, source_id)
    if not key or not title:
        return None

    return Record(
        key=key,
        title=title,
        source_id=source_id,
        business_type=label,
        notice_kind=_first(item, fields["notice_kind"]),
        demand_org=_first(item, fields["demand_org"]),
        notice_org=_first(item, fields["notice_org"]),
        notice_dt=parse_datetime(_first(item, fields["notice_dt"])),
        close_dt=parse_datetime(_first(item, fields["close_dt"])),
        opening_dt=parse_datetime(_first(item, fields["opening_dt"])),
        price=parse_amount(_first(item, fields["price"])),
        contract_method=_first(item, fields["contract_method"]),
        url=_first(item, fields["url"]) or None,
        ref_no=_first(item, fields["ref_no"]),
        raw=item,
    )
