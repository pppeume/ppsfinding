"""공공데이터포털(조달청) OpenAPI 클라이언트.

이 클라이언트가 방어적으로 작성된 이유:
  1) 오퍼레이션 경로가 서비스 버전에 따라 다를 수 있어, source 별 variants 를
     순서대로 시도하고 최초로 정상 응답한 것을 그 실행 동안 재사용한다.
  2) data.go.kr 은 type=json 을 요청해도 인증/쿼터 오류는 XML 로 돌려주는 경우가 있어
     JSON/XML 을 모두 파싱한다.
"""
from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator

import requests

from .config import ApiSettings, Source, Variant

log = logging.getLogger(__name__)

SUCCESS_CODES = {"00", "0"}


class G2BError(RuntimeError):
    """복구 불가능한 API 오류(인증 실패, 쿼터 초과 등)."""


@dataclass
class ApiPage:
    result_code: str
    result_msg: str
    items: list[dict[str, Any]]
    total_count: int

    @property
    def ok(self) -> bool:
        return self.result_code in SUCCESS_CODES


def _xml_to_page(text: str) -> ApiPage:
    root = ET.fromstring(text)

    # 인증/쿼터 오류 envelope: <OpenAPI_ServiceResponse><cmmMsgHeader>...
    header = root.find(".//cmmMsgHeader")
    if header is not None:
        code = (header.findtext("returnReasonCode") or "").strip()
        msg = (header.findtext("returnAuthMsg") or header.findtext("errMsg") or "").strip()
        return ApiPage(code or "UNKNOWN", msg, [], 0)

    code = (root.findtext(".//header/resultCode") or "").strip()
    msg = (root.findtext(".//header/resultMsg") or "").strip()
    items = [
        {child.tag: (child.text or "").strip() for child in item}
        for item in root.findall(".//body/items/item")
    ]
    total = (root.findtext(".//body/totalCount") or "0").strip()
    return ApiPage(code or "UNKNOWN", msg, items, int(total or 0))


def _json_to_page(payload: dict[str, Any]) -> ApiPage:
    response = payload.get("response", payload)
    header = response.get("header", {}) or {}
    body = response.get("body", {}) or {}

    raw_items = body.get("items", [])
    if isinstance(raw_items, dict):
        raw_items = raw_items.get("item", [])
    if isinstance(raw_items, dict):
        raw_items = [raw_items]
    if not isinstance(raw_items, list):
        raw_items = []

    return ApiPage(
        result_code=str(header.get("resultCode", "")).strip() or "UNKNOWN",
        result_msg=str(header.get("resultMsg", "")).strip(),
        items=[i for i in raw_items if isinstance(i, dict)],
        total_count=int(body.get("totalCount") or 0),
    )


def parse_response(text: str) -> ApiPage:
    """응답 본문을 JSON 이든 XML 이든 ApiPage 로 변환."""
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        import json

        return _json_to_page(json.loads(text))
    if stripped.startswith("<"):
        return _xml_to_page(text)
    raise G2BError(f"해석할 수 없는 응답 형식: {text[:200]!r}")


class G2BClient:
    def __init__(self, service_key: str, api: ApiSettings, session: requests.Session | None = None):
        if not service_key:
            raise G2BError("G2B_SERVICE_KEY 가 비어 있습니다 (data.go.kr 일반 인증키 '디코딩' 값)")
        self.service_key = service_key
        self.api = api
        self.session = session or requests.Session()
        self._resolved: dict[str, Variant] = {}

    # --- 저수준 호출 ---------------------------------------------------------

    def _call(self, variant: Variant, params: dict[str, str]) -> ApiPage:
        url = f"{self.api.base}/{variant.path}"
        query = {
            "serviceKey": self.service_key,
            "type": "json",
            **params,
        }

        last_error: Exception | None = None
        for attempt in range(1, self.api.retries + 1):
            try:
                resp = self.session.get(url, params=query, timeout=self.api.timeout_sec)
                if resp.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {resp.status_code}")
                if resp.status_code == 404:
                    return ApiPage("HTTP_404", "존재하지 않는 오퍼레이션 경로", [], 0)
                resp.raise_for_status()
                return parse_response(resp.text)
            except (requests.RequestException, ValueError, ET.ParseError) as exc:
                last_error = exc
                if attempt < self.api.retries:
                    delay = self.api.backoff_sec * (2 ** (attempt - 1))
                    log.warning("호출 실패(%s/%s) %s — %ss 후 재시도", attempt, self.api.retries, exc, delay)
                    time.sleep(delay)
            finally:
                time.sleep(self.api.sleep_between_calls_sec)

        raise G2BError(f"{variant.path} 호출 실패: {last_error}")

    def _window_params(self, variant: Variant, begin: datetime, end: datetime) -> dict[str, str]:
        dp = variant.date_params
        return {dp.begin: begin.strftime(dp.fmt), dp.end: end.strftime(dp.fmt)}

    # --- variant 탐색 --------------------------------------------------------

    def probe(self, source: Source, begin: datetime, end: datetime) -> list[tuple[Variant, ApiPage | str]]:
        """source 의 모든 variant 를 1건씩 호출해 어떤 조합이 살아있는지 진단."""
        results: list[tuple[Variant, ApiPage | str]] = []
        for variant in source.variants:
            params = {
                "pageNo": "1",
                "numOfRows": "1",
                **source.fixed_params,
                **self._window_params(variant, begin, end),
            }
            try:
                results.append((variant, self._call(variant, params)))
            except G2BError as exc:
                results.append((variant, str(exc)))
        return results

    def resolve(self, source: Source, begin: datetime, end: datetime) -> Variant:
        """정상 응답하는 variant 를 찾아 캐시. 전부 실패하면 G2BError."""
        cached = self._resolved.get(source.id)
        if cached:
            return cached

        failures: list[str] = []
        for variant, outcome in self.probe(source, begin, end):
            if isinstance(outcome, ApiPage) and outcome.ok:
                log.info("[%s] variant 확정: %s", source.id, variant.label)
                self._resolved[source.id] = variant
                return variant
            detail = outcome if isinstance(outcome, str) else f"{outcome.result_code} {outcome.result_msg}"
            failures.append(f"  - {variant.label} → {detail}")

        raise G2BError(
            f"[{source.id}] 사용 가능한 오퍼레이션을 찾지 못했습니다.\n"
            + "\n".join(failures)
            + "\nconfig/sources.yaml 의 variants 를 실제 문서에 맞게 수정하세요."
        )

    # --- 수집 ---------------------------------------------------------------

    def fetch(
        self,
        source: Source,
        begin: datetime,
        end: datetime,
        keyword: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """한 소스에서 기간(+선택 키워드) 조건으로 전체 페이지를 순회한다."""
        variant = self.resolve(source, begin, end)
        base_params = {
            "numOfRows": str(self.api.num_of_rows),
            **source.fixed_params,
            **self._window_params(variant, begin, end),
        }
        if keyword:
            base_params[source.keyword_param] = keyword

        seen = 0
        for page_no in range(1, self.api.max_pages + 1):
            page = self._call(variant, {**base_params, "pageNo": str(page_no)})
            if not page.ok:
                raise G2BError(f"[{source.id}] {page.result_code} {page.result_msg}")
            if not page.items:
                return
            yield from page.items
            seen += len(page.items)
            if seen >= page.total_count or len(page.items) < self.api.num_of_rows:
                return

        log.warning(
            "[%s] max_pages(%s) 도달 — 결과가 잘렸을 수 있습니다 (keyword=%s)",
            source.id,
            self.api.max_pages,
            keyword,
        )
