"""CLI 엔트리포인트: probe(엔드포인트 진단) / collect(수집·적재)."""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime, timedelta
from typing import Any

from .company import load_company, load_rules
from .config import REPO_ROOT
from .config import KeywordConfig, Source, load_keywords, load_sources
from .enrich import Enrichment, fetch_enrichment
from .g2b_client import ApiPage, G2BClient, G2BError
from .matcher import match
from .normalize import KST, Record, to_record
from .notion_sink import NotionSink
from .qualification import evaluate
from .scoring import score as score_record

log = logging.getLogger("g2b_watch")


def load_dotenv(path: Path | None = None) -> list[str]:
    """저장소 루트의 .env 를 읽어 환경변수로 넣는다(이미 설정된 값은 덮지 않는다).

    로컬·사내 PC 에서 돌릴 때 매번 export 하지 않아도 되게 하기 위한 것.
    GitHub Actions 에서는 .env 가 없으므로 아무 일도 하지 않는다.
    """
    env_path = path or (REPO_ROOT / ".env")
    if not env_path.exists():
        return []

    loaded: list[str] = []
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def dump_json(path: str | Path, records: list[Record]) -> bool:
    """결과를 JSON 파일로 남긴다. 실패해도 예외를 올리지 않고 False 를 준다.

    `--json-out out/result.json` 처럼 아직 없는 디렉터리를 가리킬 수 있으므로 먼저 만든다.
    이건 진단용 산출물이라, 여기서 넘어진다고 이미 수집·판정을 마친 결과를
    Notion 에 못 올리는 일이 있어서는 안 된다.
    """
    out_path = Path(path)
    payload = [{k: v for k, v in vars(rec).items() if k != "raw"} for rec in records]
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        log.error("JSON 저장 실패(수집 결과는 그대로 적재합니다): %s", exc)
        return False
    log.info("JSON 저장: %s", out_path)
    return True


def _setup_logging(verbose: bool) -> None:
    # CI 로그에서 print 출력이 logging 뒤로 밀리지 않도록 라인 버퍼링으로 바꾼다
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:  # pragma: no cover - 아주 오래된 파이썬
        pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def _window(days: int) -> tuple[datetime, datetime]:
    end = datetime.now(KST)
    return end - timedelta(days=days), end


# --- probe -------------------------------------------------------------------


def _tcp_check(host: str, port: int, timeout: float = 8.0) -> str:
    """TCP 연결만 확인한다. 인증·경로 문제와 네트워크 차단을 구분하기 위한 진단."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "열림"
    except socket.timeout:
        return "타임아웃 (방화벽/미개방 가능성)"
    except OSError as exc:
        return f"실패 ({exc.__class__.__name__}: {exc})"


def _print_connectivity(base: str) -> None:
    host = urlparse(base).hostname or base
    print(f"네트워크 진단 — {host}")
    for port, label in ((80, "http"), (443, "https")):
        print(f"  {label:<5} :{port}  {_tcp_check(host, port)}")
    print()


def cmd_probe(args: argparse.Namespace) -> int:
    cfg = load_sources(args.sources)
    _print_connectivity(cfg.api.base)
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

    # --- 참가자격 판정 + 수주검토 점수 ---------------------------------
    company = load_company(args.company)
    rules = load_rules(args.rules)
    if not company.is_configured:
        log.warning(
            "config/company.yaml 이 아직 자리표시자입니다 — 참가자격 판정을 신뢰하지 마세요 "
            "(회사명/보유면허 미입력)"
        )

    enrichment = Enrichment()
    if records and not args.no_enrich:
        enrichment = fetch_enrichment(client, begin, end)

    for rec in records.values():
        rec.allowed_regions = enrichment.allowed_regions(rec.notice_key)
        rec.required_licenses = enrichment.required_licenses(rec.notice_key)
        q = evaluate(rec, company, rules)
        rec.verdict = q.verdict
        rec.restriction_codes = q.codes
        rec.restriction_reasons = q.reasons
        rec.opportunity_score, rec.score_breakdown = score_record(rec, company, rules)

    ordered = sorted(
        records.values(),
        key=lambda r: (-r.opportunity_score, -r.score, r.notice_dt or ""),
    )
    if args.min_opportunity is not None:
        ordered = [r for r in ordered if r.opportunity_score >= args.min_opportunity]
    if args.limit:
        ordered = ordered[: args.limit]

    by_verdict: dict[str, int] = {}
    for rec in ordered:
        by_verdict[rec.verdict] = by_verdict.get(rec.verdict, 0) + 1

    print(f"\n원시 {stats['fetched']}건 → 파싱 {stats['parsed']}건 → 키워드 매칭 {stats['matched']}건 → 적재대상 {len(ordered)}건")
    print("  판정: " + " / ".join(f"{k} {v}건" for k, v in by_verdict.items()) if by_verdict else "  판정: 없음")
    print(f"  영업 우선검토({rules.review_threshold}점 이상): "
          f"{sum(1 for r in ordered if r.opportunity_score >= rules.review_threshold)}건")
    incomplete = [
        label
        for label, ok in (("참가가능지역", enrichment.region_fetched),
                          ("면허제한", enrichment.license_fetched))
        if not ok
    ]
    if incomplete and records and not args.no_enrich:
        print(f"  ⚠ 보조정보 불완전({', '.join(incomplete)}) — 해당 제한 판정은 신뢰하지 마세요")
    print()
    for rec in ordered:
        price = f"{rec.price:,}원" if rec.price else "-"
        codes = ",".join(rec.restriction_codes) or "-"
        print(
            f"  {rec.opportunity_score:>3}점 [{rec.verdict}] {rec.business_type} | "
            f"{rec.title[:50]} | {rec.demand_org} | {price} | {','.join(rec.axes)} | {codes}"
        )

    if args.json_out:
        dump_json(args.json_out, ordered)

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
    p_collect.add_argument("--limit", type=int, default=None, help="적재 상한(수주검토 점수 높은 순)")
    p_collect.add_argument("--company", default=None, help="company.yaml 경로")
    p_collect.add_argument("--rules", default=None, help="rules.yaml 경로")
    p_collect.add_argument("--min-opportunity", type=int, default=None,
                           help="이 수주검토 점수 미만은 적재하지 않는다")
    p_collect.add_argument("--no-enrich", action="store_true",
                           help="참가가능지역·면허제한 보조 조회를 건너뛴다(호출 수 절약)")
    p_collect.add_argument("--dry-run", action="store_true", help="Notion 에 쓰지 않고 로그만")
    p_collect.add_argument("--no-notion", action="store_true", help="Notion 단계를 아예 건너뛴다")
    p_collect.add_argument("--json-out", default=None, help="결과를 JSON 파일로도 저장")
    p_collect.set_defaults(func=cmd_collect)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    loaded = load_dotenv()
    if loaded:
        log.info(".env 에서 %s 를 읽었습니다", ", ".join(loaded))
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 - CLI 경계에서 한 번만 잡는다
        log.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
