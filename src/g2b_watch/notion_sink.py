"""Notion 데이터베이스 적재(중복 방지 포함).

중복 판정은 Notion DB 자체를 원장으로 삼는다(로컬 상태파일 없음).
최근 N일 내 생성된 페이지의 '공고번호' 를 모아 집합을 만들고, 거기에 없는 건만 생성한다.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import requests

from .normalize import Record

log = logging.getLogger(__name__)

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
TEXT_LIMIT = 2000
# Notion API 권장 한도는 평균 초당 3회. 여유를 둬서 간격을 잡는다.
WRITE_INTERVAL_SEC = 0.35


class NotionError(RuntimeError):
    pass


def _text(value: str | None) -> list[dict[str, Any]]:
    if not value:
        return []
    return [{"type": "text", "text": {"content": value[:TEXT_LIMIT]}}]


def _date(value: str | None) -> dict[str, Any] | None:
    return {"start": value} if value else None


class NotionSink:
    def __init__(self, token: str, database_id: str, dry_run: bool = False):
        if not dry_run and not token:
            raise NotionError("NOTION_TOKEN 이 비어 있습니다")
        if not database_id:
            raise NotionError("NOTION_DATABASE_ID 가 비어 있습니다")
        self.database_id = database_id
        self.dry_run = dry_run
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            }
        )

    # --- 저수준 ------------------------------------------------------------

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = self.session.post(f"{NOTION_API}{path}", json=payload, timeout=30)
        if resp.status_code == 429:
            wait = float(resp.headers.get("Retry-After", "2"))
            log.warning("Notion rate limit — %ss 대기 후 재시도", wait)
            time.sleep(wait)
            resp = self.session.post(f"{NOTION_API}{path}", json=payload, timeout=30)
        if resp.status_code >= 400:
            raise NotionError(f"Notion {path} 실패 [{resp.status_code}]: {resp.text[:500]}")
        return resp.json()

    # --- 중복 판정 ----------------------------------------------------------

    def existing_keys(self, lookback_days: int) -> set[str]:
        """최근 lookback_days 안에 적재된 페이지의 공고번호 집합."""
        if self.dry_run:
            return set()

        since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
        payload: dict[str, Any] = {
            "page_size": 100,
            "filter": {"timestamp": "created_time", "created_time": {"on_or_after": since}},
        }

        keys: set[str] = set()
        cursor: str | None = None
        while True:
            if cursor:
                payload["start_cursor"] = cursor
            data = self._post(f"/databases/{self.database_id}/query", payload)
            for page in data.get("results", []):
                prop = page.get("properties", {}).get("공고번호", {})
                for chunk in prop.get("rich_text", []):
                    text = chunk.get("plain_text", "").strip()
                    if text:
                        keys.add(text)
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            time.sleep(WRITE_INTERVAL_SEC)

        log.info("Notion 기존 공고번호 %s건 확인 (최근 %s일)", len(keys), lookback_days)
        return keys

    # --- 생성 ---------------------------------------------------------------

    def _properties(self, rec: Record) -> dict[str, Any]:
        props: dict[str, Any] = {
            "공고명": {"title": _text(rec.title)},
            "공고번호": {"rich_text": _text(rec.key)},
            "업무구분": {"select": {"name": rec.business_type}},
            "검토상태": {"select": {"name": "신규"}},
            "관련도": {"number": rec.score},
            "매칭키워드": {"multi_select": [{"name": a} for a in rec.axes]},
            "수주검토점수": {"number": rec.opportunity_score},
        }
        if rec.verdict:
            props["참여여부"] = {"select": {"name": rec.verdict}}
        if rec.restriction_codes:
            props["제한사유"] = {
                "multi_select": [{"name": c} for c in rec.restriction_codes]
            }
        if rec.restriction_reasons:
            props["판정근거"] = {"rich_text": _text(" / ".join(rec.restriction_reasons))}
        if rec.allowed_regions:
            props["참가가능지역"] = {"rich_text": _text(", ".join(rec.allowed_regions))}
        if rec.required_licenses:
            props["요구면허"] = {"rich_text": _text(", ".join(rec.required_licenses))}
        for name, value in (
            ("공고종류", rec.notice_kind),
            ("수요기관", rec.demand_org),
            ("공고기관", rec.notice_org),
            ("계약방법", rec.contract_method),
            ("참조번호", rec.ref_no),
        ):
            if value:
                props[name] = {"rich_text": _text(value)}

        for name, value in (
            ("공고일시", rec.notice_dt),
            ("입찰마감일시", rec.close_dt),
            ("개찰일시", rec.opening_dt),
        ):
            date = _date(value)
            if date:
                props[name] = {"date": date}

        if rec.price is not None:
            props["추정가격"] = {"number": rec.price}
        if rec.url:
            props["공고링크"] = {"url": rec.url[:TEXT_LIMIT]}
        return props

    def create(self, rec: Record) -> None:
        payload = {"parent": {"database_id": self.database_id}, "properties": self._properties(rec)}
        if self.dry_run:
            log.info("[dry-run] 생성 생략: %s | %s", rec.key, rec.title)
            return
        self._post("/pages", payload)
        time.sleep(WRITE_INTERVAL_SEC)

    def create_missing(self, records: Iterable[Record], lookback_days: int) -> tuple[int, int]:
        """중복을 뺀 신규 건만 생성. (생성수, 건너뛴수) 반환."""
        known = self.existing_keys(lookback_days)
        created = skipped = 0
        for rec in records:
            if rec.key in known:
                skipped += 1
                continue
            self.create(rec)
            known.add(rec.key)
            created += 1
        return created, skipped
