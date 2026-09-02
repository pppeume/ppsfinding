"""CLI 엔트리포인트: probe(엔드포인트 진단) / collect(수집·적재)."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Any

from .config import KeywordConfig, Source, load_keywords, load_sources
from .g2b_client import ApiPage, G2BClient, G2BError
from .matcher import match
from .normalize import KST, Record, to_record
from .notion_sink import NotionSink

log = logging.getLogger("g2b_watch")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def _window(days: int) -> tuple[datetime, datetime]:
    end = datetime.now(KST)
    return end - timedelta(days=days), end


# --- probe -------------------------------------------------------------------


def cmd_probe(args: argparse.Namespace) -> int:
    cfg = load_sources(args.sources)
    client = G2BClient(os.environ.get("G2B_SERVICE_KEY", ""), cfg.api)
    begin, end = _window(args.days)

    print(f"조회 기간: {begin:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M} (KST)\n")
    any_ok = False
    for source in cfg.enabled_sources():
        print(f"[{source.id}] {source.label}")
        for variant, outcome in client.probe(source, begin, end):
            if isinstance(outcome, ApiPage):
                mark = "OK  " if outcome.ok else "FAIL"
                detail = f"code={outcome.result_code} total={outcome.total_count} msg={outcome.result_msg}"
                if outcome.ok:
                    any_ok = True
                    if args.dump and outcome.items:
                        detail += "\n       응답 키: " + ", ".join(sorted(outcome.items[0]))
            else:
                mark, detail = "FAIL", outcome.replace("\n", " ")
            print(f"  {mark}  {variant.label}\n       {detail}")
        print()

    if not any_ok:
        print("정상 응답한 오퍼레이션이 하나도 없습니다. 인증키와 config/sources.yaml 을 확인하세요.")
        return 1
    return 0


# --- collect -----------------------------------------------------------------


def _collect_raw(
    client: G2BClient, source: Source, keywords: KeywordConfig, begin, end, mode: str
) -> list[dict[str, Any]]:
    """소스 하나에서 원시 아이템을 모은다(키워드별 호출 또는 전체 스캔)."""
    collected: list[dict[str, Any]] = []
    if mode == "full":
        collected.extend(client.fetch(source, begin, end))
        log.info("[%s] 전체 스캔 %s건", source.id, len(collected))
        return collected

    for term in keywords.all_search_terms():
        try:
            items = list(client.fetch(source, begin, end, keyword=term))
        except G2BError as exc:
            log.error("[%s] 키워드 '%s' 조회 실패: %s", source.id, term, exc)
            continue
        if items:
            log.info("[%s] '%s' → %s건", source.id, term, len(items))
        collected.extend(items)
    return collected


def cmd_collect(args: argparse.Namespace) -> int:
    sources_cfg = load_sources(args.sources)
    keywords = load_keywords(args.keywords)
    if args.min_score is not None:
        keywords = KeywordConfig(
            min_score=args.min_score,
            min_axes=keywords.min_axes,
            exclude_global=keywords.exclude_global,
            axes=keywords.axes,
        )

    begin, end = _window(args.days)
    log.info("조회 기간 %s ~ %s (KST), mode=%s", begin.strftime("%Y-%m-%d %H:%M"), end.strftime("%Y-%m-%d %H:%M"), args.mode)

    client = G2BClient(os.environ.get("G2B_SERVICE_KEY", ""), sources_cfg.api)

    records: dict[str, Record] = {}
    stats = {"fetched": 0, "parsed": 0, "matched": 0}
    failed_sources: list[str] = []

    for source in sources_cfg.enabled_sources():
        try:
            raw_items = _collect_raw(client, source, keywords, begin, end, args.mode)
        except G2BError as exc:
            log.error("[%s] 수집 실패 — 이 소스는 건너뜁니다\n%s", source.id, exc)
            failed_sources.append(source.id)
            continue

        stats["fetched"] += len(raw_items)
        for item in raw_items:
            rec = to_record(item, source_id=source.id, kind=source.kind, label=source.label)
            if rec is None:
                continue
            stats["parsed"] += 1
            if rec.key in records:
                continue
            result = match((rec.title, rec.demand_org), keywords)
            if not result.matched:
                log.debug("제외 %s | %s | %s", rec.key, rec.title, result.reason)
                continue
            rec.axes = result.axes
            rec.score = result.score
            records[rec.key] = rec
            stats["matched"] += 1

    ordered = sorted(records.values(), key=lambda r: (-r.score, r.notice_dt or ""))
    if args.limit:
        ordered = ordered[: args.limit]

    print(f"\n원시 {stats['fetched']}건 → 파싱 {stats['parsed']}건 → 키워드 매칭 {len(ordered)}건")
    for rec in ordered:
        axes = ",".join(rec.axes)
        price = f"{rec.price:,}원" if rec.price else "-"
        print(f"  [{rec.score:>2}] {rec.business_type} | {rec.title[:60]} | {rec.demand_org} | {price} | {axes}")

    if args.json_out:
        payload = [
            {k: v for k, v in vars(rec).items() if k != "raw"} for rec in ordered
        ]
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        log.info("JSON 저장: %s", args.json_out)

    if args.no_notion:
        log.info("--no-notion 지정 — Notion 적재를 건너뜁니다")
        return 1 if failed_sources else 0

    sink = NotionSink(
        token=os.environ.get("NOTION_TOKEN", ""),
        database_id=os.environ.get("NOTION_DATABASE_ID", ""),
        dry_run=args.dry_run,
    )
    created, skipped = sink.create_missing(ordered, lookback_days=args.dedup_days)
    print(f"Notion 적재: 신규 {created}건 / 중복 건너뜀 {skipped}건")

    if failed_sources:
        log.error("수집에 실패한 소스: %s", ", ".join(failed_sources))
        return 1
    return 0


# --- 파서 --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="g2b_watch", description="나라장터 입찰공고 키워드 모니터링")
    parser.add_argument("--sources", default=None, help="sources.yaml 경로")
    parser.add_argument("--keywords", default=None, help="keywords.yaml 경로")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_probe = sub.add_parser("probe", help="오퍼레이션 후보를 하나씩 호출해 살아있는 조합을 찾는다")
    p_probe.add_argument("--days", type=int, default=1)
    p_probe.add_argument("--dump", action="store_true", help="정상 응답의 필드 키 목록을 출력")
    p_probe.set_defaults(func=cmd_probe)

    p_collect = sub.add_parser("collect", help="수집 → 필터 → Notion 적재")
    p_collect.add_argument("--days", type=int, default=2, help="조회 기간(일). 실행 누락 대비 중첩 권장")
    p_collect.add_argument("--mode", choices=["keyword", "full"], default="keyword",
                           help="keyword: 검색어별 호출 / full: 기간 전체 스캔 후 로컬 필터")
    p_collect.add_argument("--dedup-days", type=int, default=14, help="중복 판정에 볼 Notion 최근 적재 기간")
    p_collect.add_argument("--min-score", type=int, default=None, help="keywords.yaml 의 min_score 를 덮어쓴다")
    p_collect.add_argument("--limit", type=int, default=None, help="적재 상한(관련도 높은 순)")
    p_collect.add_argument("--dry-run", action="store_true", help="Notion 에 쓰지 않고 로그만")
    p_collect.add_argument("--no-notion", action="store_true", help="Notion 단계를 아예 건너뛴다")
    p_collect.add_argument("--json-out", default=None, help="결과를 JSON 파일로도 저장")
    p_collect.set_defaults(func=cmd_collect)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 - CLI 경계에서 한 번만 잡는다
        log.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
