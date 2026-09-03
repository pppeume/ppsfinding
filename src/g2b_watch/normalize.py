"""조달청 OpenAPI 응답 아이템을 Notion 적재용 공통 레코드로 변환.

입찰공고(bid) 필드명은 「조달청 공공데이터 개방 OpenAPI 참고자료 v1.2」의
getBidPblancListInfo{Thng,Servc}PPSSrch 응답 명세에서 확인한 값이다.
값이 비어 오는 경우가 있어 의미가 같은 대체 필드까지 후보로 두고 첫 유효값을 취한다.

담당자명·전화번호·이메일(ntceInsttOfclNm 등)은 개인정보라 의도적으로 매핑하지 않는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

KST = timezone(timedelta(hours=9))

# --- 필드 후보 정의 -----------------------------------------------------------

BID_FIELDS: dict[str, tuple[str, ...]] = {
    "notice_no": ("bidNtceNo",),          # 입찰공고번호
    "notice_ord": ("bidNtceOrd",),        # 입찰공고차수 (예: "000")
    "title": ("bidNtceNm",),              # 입찰공고명
    "notice_kind": ("ntceKindNm",),       # 공고종류명 (등록공고/변경공고/취소공고/재공고)
    "demand_org": ("dminsttNm",),         # 수요기관명
    "notice_org": ("ntceInsttNm",),       # 공고기관명
    "notice_dt": ("bidNtceDt", "rgstDt"), # 입찰공고일시 → 없으면 등록일시
    "close_dt": ("bidClseDt",),           # 입찰마감일시
    "opening_dt": ("opengDt",),           # 개찰일시
    "price": ("presmptPrce", "asignBdgtAmt"),      # 추정가격 → 없으면 배정예산금액
    "contract_method": ("cntrctCnclsMthdNm",),     # 계약체결방법명
    "url": ("bidNtceDtlUrl", "bidNtceUrl"),        # 입찰공고상세URL
    "ref_no": ("refNo",),                 # 참조번호
}

# 사전규격은 별도 서비스(「조달청_나라장터 사전규격정보서비스」)이고 명세를 아직 받지 못했다.
# config/sources.yaml 에서 해당 소스를 비활성화해 두었으며, 아래 후보는 미검증이다.
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
