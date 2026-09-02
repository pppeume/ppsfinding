"""네트워크 없이 파이프라인 전체(호출→파싱→정규화→매칭)를 검증한다."""
import json
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from g2b_watch.config import load_keywords, load_sources
from g2b_watch.g2b_client import G2BClient, G2BError
from g2b_watch.matcher import match
from g2b_watch.normalize import to_record

SOURCES = load_sources()
KEYWORDS = load_keywords()


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


class FakeSession:
    """지정한 path 만 정상 응답하고 나머지는 404 를 주는 가짜 세션."""

    def __init__(self, live_path, items, total=None):
        self.live_path = live_path
        self.items = items
        self.total = len(items) if total is None else total
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if not url.endswith(self.live_path):
            return FakeResponse("<OpenAPI_ServiceResponse><cmmMsgHeader>"
                                "<returnAuthMsg>NO_OPENAPI_SERVICE_ERROR</returnAuthMsg>"
                                "<returnReasonCode>12</returnReasonCode>"
                                "</cmmMsgHeader></OpenAPI_ServiceResponse>", 200)
        rows = int(params.get("numOfRows", 100))
        page = int(params.get("pageNo", 1))
        chunk = self.items[(page - 1) * rows: page * rows]
        return FakeResponse(json.dumps({
            "response": {
                "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE."},
                "body": {"items": chunk, "totalCount": self.total,
                         "pageNo": page, "numOfRows": rows},
            }
        }, ensure_ascii=False))


def _source(source_id):
    return next(s for s in SOURCES.sources if s.id == source_id)


# 테스트에서는 호출 간 대기를 없앤다 (ApiSettings 는 frozen 이라 replace 로 새로 만든다)
FAST_API = replace(SOURCES.api, sleep_between_calls_sec=0.0, backoff_sec=0.0)


def test_resolve_skips_dead_variants_and_picks_live_one():
    source = _source("bid_servc")
    live = source.variants[2].path  # 세 번째 후보만 살아있다고 가정
    session = FakeSession(live, [{"bidNtceNo": "1", "bidNtceOrd": "00", "bidNtceNm": "x"}])
    client = G2BClient("KEY", FAST_API, session=session)

    from datetime import datetime
    variant = client.resolve(source, datetime(2026, 9, 1), datetime(2026, 9, 2))
    assert variant.path == live
    # 두 번째 호출은 캐시를 써서 추가 probe 를 하지 않는다
    before = len(session.calls)
    client.resolve(source, datetime(2026, 9, 1), datetime(2026, 9, 2))
    assert len(session.calls) == before


def test_resolve_raises_when_every_variant_fails():
    source = _source("bid_thng")
    session = FakeSession("정상경로없음", [])
    client = G2BClient("KEY", FAST_API, session=session)
    from datetime import datetime
    with pytest.raises(G2BError) as err:
        client.resolve(source, datetime(2026, 9, 1), datetime(2026, 9, 2))
    assert "사용 가능한 오퍼레이션을 찾지 못했습니다" in str(err.value)


def test_fetch_paginates_until_total_count():
    source = _source("bid_servc")
    live = source.variants[0].path
    items = [{"bidNtceNo": str(i), "bidNtceOrd": "00", "bidNtceNm": f"공고{i}"} for i in range(250)]
    session = FakeSession(live, items)
    client = G2BClient("KEY", FAST_API, session=session)

    from datetime import datetime
    got = list(client.fetch(source, datetime(2026, 9, 1), datetime(2026, 9, 2)))
    assert len(got) == 250


def test_end_to_end_filtering():
    """실제 API 를 흉내낸 응답에서 관련 공고만 남는지."""
    source = _source("bid_servc")
    live = source.variants[0].path
    items = [
        {"bidNtceNo": "A1", "bidNtceOrd": "00", "bidNtceNm": "본관 BEMS 구축 용역",
         "dminsttNm": "○○공단", "presmptPrce": "450,000,000",
         "bidNtceDt": "2026-09-01 09:00:00"},
        {"bidNtceNo": "A2", "bidNtceOrd": "00", "bidNtceNm": "구내식당 급식 위탁운영",
         "dminsttNm": "△△청"},
        {"bidNtceNo": "A3", "bidNtceOrd": "00", "bidNtceNm": "AI 기반 통합관제 플랫폼",
         "dminsttNm": "□□시"},
        {"bidNtceNo": "A4", "bidNtceOrd": "00", "bidNtceNm": "홍보용 기념품 제작"},
    ]
    session = FakeSession(live, items)
    client = G2BClient("KEY", FAST_API, session=session)

    from datetime import datetime
    kept = []
    for raw in client.fetch(source, datetime(2026, 9, 1), datetime(2026, 9, 2)):
        rec = to_record(raw, source_id=source.id, kind=source.kind, label=source.label)
        assert rec is not None
        result = match((rec.title, rec.demand_org), KEYWORDS)
        if result.matched:
            rec.axes, rec.score = result.axes, result.score
            kept.append(rec)

    assert [r.key for r in kept] == ["A1-00", "A3-00"]
    assert kept[0].price == 450000000
    assert "BEMS" in kept[0].axes
    assert set(kept[1].axes) >= {"AI", "통합관제"}
