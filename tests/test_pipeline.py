"""네트워크 없이 파이프라인 전체(호출→파싱→정규화→매칭)를 검증한다."""
import json
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from g2b_watch.config import DateParams, Source, Variant, load_keywords, load_sources
from g2b_watch.g2b_client import G2BClient, G2BError, normalize_service_key
from g2b_watch.matcher import match
from g2b_watch.normalize import to_record

SOURCES = load_sources()
KEYWORDS = load_keywords()

# 테스트에서는 호출 간 대기를 없앤다 (ApiSettings 는 frozen 이라 replace 로 새로 만든다)
FAST_API = replace(SOURCES.api, sleep_between_calls_sec=0.0, backoff_sec=0.0)

WINDOW = (datetime(2026, 9, 1), datetime(2026, 9, 2))


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


class FakeSession:
    """지정한 path 만 정상 응답하고 나머지는 오류 envelope 를 주는 가짜 세션."""

    def __init__(self, live_path, items, total=None):
        self.live_path = live_path
        self.items = items
        self.total = len(items) if total is None else total
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if not url.endswith(self.live_path):
            return FakeResponse(
                "<OpenAPI_ServiceResponse><cmmMsgHeader>"
                "<returnAuthMsg>NO_OPENAPI_SERVICE_ERROR</returnAuthMsg>"
                "<returnReasonCode>12</returnReasonCode>"
                "</cmmMsgHeader></OpenAPI_ServiceResponse>",
                200,
            )
        rows = int(params.get("numOfRows", 100))
        page = int(params.get("pageNo", 1))
        chunk = self.items[(page - 1) * rows : page * rows]
        return FakeResponse(
            json.dumps(
                {
                    "response": {
                        "header": {"resultCode": "00", "resultMsg": "정상"},
                        "body": {
                            "items": chunk,
                            "totalCount": self.total,
                            "pageNo": page,
                            "numOfRows": rows,
                        },
                    }
                },
                ensure_ascii=False,
            )
        )


def _source(source_id):
    return next(s for s in SOURCES.sources if s.id == source_id)


# --- 인증키 정규화 -------------------------------------------------------------


def test_encoding_key_is_decoded():
    """포털의 Encoding 키를 넣어도 이중 인코딩되지 않도록 디코딩된다."""
    assert normalize_service_key("abc%2Bdef%2Fghi%3D%3D") == "abc+def/ghi=="


def test_decoding_key_passes_through():
    assert normalize_service_key("abc+def/ghi==") == "abc+def/ghi=="
    assert normalize_service_key("  spaced+key==  ") == "spaced+key=="


def test_empty_key_is_rejected():
    with pytest.raises(G2BError):
        G2BClient("", FAST_API)


# --- 요청 조립 (문서 명세와 일치하는지) ----------------------------------------


def test_request_matches_documented_spec():
    source = _source("bid_thng")
    assert source.variants[0].path == "getBidPblancListInfoThngPPSSrch"

    session = FakeSession(source.variants[0].path, [])
    client = G2BClient("KEY%2B1", FAST_API, session=session)
    list(client.fetch(source, *WINDOW, keyword="BEMS"))

    url, params = session.calls[-1]
    assert url == f"{FAST_API.base}/getBidPblancListInfoThngPPSSrch"
    assert params["ServiceKey"] == "KEY+1"      # 대문자 S, 디코딩된 키
    assert params["type"] == "json"
    assert params["inqryDiv"] == "1"            # 1 = 공고게시일시
    assert params["inqryBgnDt"] == "202609010000"   # YYYYMMDDHHMM
    assert params["inqryEndDt"] == "202609020000"
    assert params["bidNtceNm"] == "BEMS"        # 공고명 부분일치
    assert "serviceKey" not in params


def test_servc_source_uses_servc_operation():
    assert _source("bid_servc").variants[0].path == "getBidPblancListInfoServcPPSSrch"


def test_prestd_source_is_disabled_until_spec_arrives():
    """사전규격은 별도 서비스라 명세를 받기 전까지 비활성이어야 한다."""
    assert not _source("prestd").enabled
    assert [s.id for s in SOURCES.enabled_sources()] == ["bid_thng", "bid_servc"]


# --- variant 탐색 --------------------------------------------------------------


def _multi_variant_source():
    """후보가 여러 개인 상황을 재현하기 위한 합성 소스."""
    dp = DateParams(begin="inqryBgnDt", end="inqryEndDt", fmt="%Y%m%d%H%M")
    return Source(
        id="synthetic",
        label="용역",
        kind="bid",
        enabled=True,
        keyword_param="bidNtceNm",
        fixed_params={"inqryDiv": "1"},
        variants=(
            Variant(path="deadOne", date_params=dp),
            Variant(path="deadTwo", date_params=dp),
            Variant(path="getBidPblancListInfoServcPPSSrch", date_params=dp),
        ),
    )


def test_resolve_skips_dead_variants_and_picks_live_one():
    source = _multi_variant_source()
    live = source.variants[2].path
    session = FakeSession(live, [{"bidNtceNo": "1", "bidNtceOrd": "000", "bidNtceNm": "x"}])
    client = G2BClient("KEY", FAST_API, session=session)

    assert client.resolve(source, *WINDOW).path == live
    # 두 번째 호출은 캐시를 써서 추가 probe 를 하지 않는다
    before = len(session.calls)
    client.resolve(source, *WINDOW)
    assert len(session.calls) == before


def test_resolve_raises_when_every_variant_fails():
    source = _source("bid_thng")
    session = FakeSession("정상경로없음", [])
    client = G2BClient("KEY", FAST_API, session=session)
    with pytest.raises(G2BError) as err:
        client.resolve(source, *WINDOW)
    assert "사용 가능한 오퍼레이션을 찾지 못했습니다" in str(err.value)


def test_fetch_paginates_until_total_count():
    source = _source("bid_servc")
    items = [
        {"bidNtceNo": str(i), "bidNtceOrd": "000", "bidNtceNm": f"공고{i}"} for i in range(250)
    ]
    session = FakeSession(source.variants[0].path, items)
    client = G2BClient("KEY", FAST_API, session=session)
    assert len(list(client.fetch(source, *WINDOW))) == 250


# --- end-to-end ----------------------------------------------------------------


def test_end_to_end_filtering_with_documented_payload():
    """문서의 실제 응답 필드명을 그대로 쓴 페이로드에서 관련 공고만 남는지."""
    source = _source("bid_servc")
    items = [
        {
            "bidNtceNo": "R25BK00934017",
            "bidNtceOrd": "000",
            "bidNtceNm": "본관 BEMS 구축 용역",
            "ntceKindNm": "등록공고",
            "dminsttNm": "한국생산기술연구원",
            "ntceInsttNm": "조달청",
            "cntrctCnclsMthdNm": "협상에의한계약",
            "bidNtceDt": "2025-07-01 07:54:07",
            "bidClseDt": "2025-07-14 10:00:00",
            "opengDt": "2025-07-14 11:00:00",
            "presmptPrce": "450000000",
            "refNo": "P81202500407",
            "bidNtceDtlUrl": "https://www.g2b.go.kr/link/PNPE027_01/single/?bidPbancNo=R25BK00934017",
            # 개인정보 필드는 응답에 있어도 레코드로 옮기지 않아야 한다
            "ntceInsttOfclNm": "홍길동",
            "ntceInsttOfclTelNo": "051-000-0000",
            "ntceInsttOfclEmailAdrs": "someone@example.org",
        },
        {"bidNtceNo": "A2", "bidNtceOrd": "000", "bidNtceNm": "구내식당 급식 위탁운영"},
        {"bidNtceNo": "A3", "bidNtceOrd": "000", "bidNtceNm": "AI 기반 통합관제 플랫폼"},
        {"bidNtceNo": "A4", "bidNtceOrd": "000", "bidNtceNm": "홍보용 기념품 제작"},
    ]
    session = FakeSession(source.variants[0].path, items)
    client = G2BClient("KEY", FAST_API, session=session)

    kept = []
    for raw in client.fetch(source, *WINDOW):
        rec = to_record(raw, source_id=source.id, kind=source.kind, label=source.label)
        assert rec is not None
        result = match((rec.title, rec.demand_org), KEYWORDS)
        if result.matched:
            rec.axes, rec.score = result.axes, result.score
            kept.append(rec)

    assert [r.key for r in kept] == ["R25BK00934017-000", "A3-000"]

    bems = kept[0]
    assert bems.price == 450000000
    assert bems.demand_org == "한국생산기술연구원"
    assert bems.contract_method == "협상에의한계약"
    assert bems.close_dt.startswith("2025-07-14T10:00:00")
    assert bems.opening_dt.startswith("2025-07-14T11:00:00")
    assert bems.ref_no == "P81202500407"
    assert "BEMS" in bems.axes

    # 개인정보가 레코드 어느 필드에도 실리지 않았는지
    leaked = {k: v for k, v in vars(bems).items() if k != "raw"}
    assert "홍길동" not in json.dumps(leaked, ensure_ascii=False, default=str)
    assert "someone@example.org" not in json.dumps(leaked, ensure_ascii=False, default=str)

    assert set(kept[1].axes) >= {"AI", "통합관제"}
