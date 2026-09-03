"""보조 오퍼레이션으로 참가자격 판정에 필요한 정보를 채운다.

입찰공고 목록(PPSSrch) 응답에는 "지역제한이 걸려 있다"는 신호(rgnLmtBidLocplcJdgmBssNm)는
있어도 **어느 지역인지**는 없다. 면허 제한도 여부(indstrytyLmtYn)만 있고 어떤 면허인지는 없다.
그래서 다음 두 오퍼레이션을 별도로 호출한다.

  - getBidPblancListInfoPrtcptPsblRgn  참가가능지역정보조회 → prtcptPsblRgnNm
  - getBidPblancListInfoLicenseLimit   면허제한정보조회     → lcnsLmtNm, permsnIndstrytyList

두 오퍼레이션 모두 inqryDiv=1 로 **등록일시 범위 일괄 조회**가 되므로,
공고 건별로 호출하지 않고 기간 단위로 한 번에 받아 (공고번호, 차수) 로 조인한다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from .config import ApiSettings, DateParams, Source, Variant
from .g2b_client import G2BClient, G2BError

log = logging.getLogger(__name__)

_DATES = DateParams(begin="inqryBgnDt", end="inqryEndDt", fmt="%Y%m%d%H%M")

# 두 보조 오퍼레이션 모두 inqryDiv 1:등록일시 / 2:입찰공고번호 를 받는다. 1을 쓴다.
REGION_SOURCE = Source(
    id="aux_region",
    label="참가가능지역",
    kind="aux",
    enabled=True,
    keyword_param="",
    fixed_params={"inqryDiv": "1"},
    variants=(Variant(path="getBidPblancListInfoPrtcptPsblRgn", date_params=_DATES),),
)

LICENSE_SOURCE = Source(
    id="aux_license",
    label="면허제한",
    kind="aux",
    enabled=True,
    keyword_param="",
    fixed_params={"inqryDiv": "1"},
    variants=(Variant(path="getBidPblancListInfoLicenseLimit", date_params=_DATES),),
)

NoticeKey = tuple[str, str]


@dataclass
class Enrichment:
    """(공고번호, 차수) → 참가가능지역 / 요구면허 조회표."""

    regions: dict[NoticeKey, list[str]] = field(default_factory=dict)
    licenses: dict[NoticeKey, list[str]] = field(default_factory=dict)
    region_fetched: bool = False
    license_fetched: bool = False

    def allowed_regions(self, key: NoticeKey) -> tuple[str, ...]:
        return tuple(self.regions.get(key, ()))

    def required_licenses(self, key: NoticeKey) -> tuple[str, ...]:
        return tuple(self.licenses.get(key, ()))


def _key(item: dict[str, Any]) -> NoticeKey:
    return (
        str(item.get("bidNtceNo", "")).strip(),
        str(item.get("bidNtceOrd", "")).strip(),
    )


def _collect(client: G2BClient, source: Source, begin: datetime, end: datetime) -> list[dict[str, Any]]:
    return list(client.fetch(source, begin, end))


def _add(bucket: dict[NoticeKey, list[str]], key: NoticeKey, value: str) -> None:
    if not key[0] or not value:
        return
    seen = bucket.setdefault(key, [])
    if value not in seen:
        seen.append(value)


def _license_names(item: dict[str, Any]) -> Iterable[str]:
    """면허제한명과 허용업종목록을 모두 요구면허 후보로 본다.

    lcnsLmtNm 표기는 '액화석유가스판매사업/4617' 처럼 '명칭/코드' 형태다.
    permsnIndstrytyList 는 '[액화석유가스충전사업/4615]' 같은 목록 문자열로 온다.
    """
    raw_name = str(item.get("lcnsLmtNm", "") or "").strip()
    if raw_name:
        yield raw_name.split("/")[0].strip()

    raw_list = item.get("permsnIndstrytyList")
    if isinstance(raw_list, str):
        chunks = raw_list.replace("[", "").replace("]", "").split(",")
    elif isinstance(raw_list, (list, tuple)):
        chunks = [str(c) for c in raw_list]
    else:
        chunks = []
    for chunk in chunks:
        name = chunk.split("/")[0].strip()
        if name:
            yield name


def fetch_enrichment(
    client: G2BClient,
    begin: datetime,
    end: datetime,
    *,
    want_regions: bool = True,
    want_licenses: bool = True,
) -> Enrichment:
    """기간 내 참가가능지역·면허제한 정보를 한 번에 받아 조회표로 만든다.

    보조 조회가 실패해도 수집 자체는 계속되어야 하므로 예외를 삼키고 로그만 남긴다.
    받지 못한 경우 Enrichment.*_fetched 가 False 로 남아, 판정 단계에서
    '확인 불가'와 '제한 없음'을 구분할 수 있다.
    """
    result = Enrichment()

    if want_regions:
        try:
            for item in _collect(client, REGION_SOURCE, begin, end):
                _add(result.regions, _key(item), str(item.get("prtcptPsblRgnNm", "") or "").strip())
            result.region_fetched = True
            log.info("참가가능지역 %s건 공고분 확보", len(result.regions))
        except G2BError as exc:
            log.warning("참가가능지역 조회 실패 — 지역 판정을 보류합니다: %s", exc)

    if want_licenses:
        try:
            for item in _collect(client, LICENSE_SOURCE, begin, end):
                key = _key(item)
                for name in _license_names(item):
                    _add(result.licenses, key, name)
            result.license_fetched = True
            log.info("면허제한 %s건 공고분 확보", len(result.licenses))
        except G2BError as exc:
            log.warning("면허제한 조회 실패 — 면허 판정을 보류합니다: %s", exc)

    return result
