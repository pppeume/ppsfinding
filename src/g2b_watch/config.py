"""YAML 설정 로딩 및 소스/키워드 스펙 정규화."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCES = REPO_ROOT / "config" / "sources.yaml"
DEFAULT_KEYWORDS = REPO_ROOT / "config" / "keywords.yaml"


@dataclass(frozen=True)
class DateParams:
    begin: str
    end: str
    fmt: str


@dataclass(frozen=True)
class Variant:
    """한 소스에 대한 엔드포인트 후보. path 와 날짜 파라미터 조합이 함께 바뀔 수 있다."""

    path: str
    date_params: DateParams

    @property
    def label(self) -> str:
        return f"{self.path} [{self.date_params.begin}/{self.date_params.fmt}]"


@dataclass(frozen=True)
class Source:
    id: str
    label: str
    kind: str  # "bid" | "prestd"
    enabled: bool
    keyword_param: str
    fixed_params: dict[str, str]
    variants: tuple[Variant, ...]


@dataclass(frozen=True)
class ApiSettings:
    base: str
    service_key_param: str
    scheme_fallback: bool
    timeout_sec: int
    retries: int
    backoff_sec: float
    num_of_rows: int
    max_pages: int
    sleep_between_calls_sec: float


@dataclass(frozen=True)
class SourcesConfig:
    api: ApiSettings
    sources: tuple[Source, ...]

    def enabled_sources(self) -> tuple[Source, ...]:
        return tuple(s for s in self.sources if s.enabled)


@dataclass(frozen=True)
class Term:
    pattern: str
    weight: int
    regex: str | None = None
    ignore_case: bool | None = None


@dataclass(frozen=True)
class Axis:
    id: str
    search_terms: tuple[str, ...]
    terms: tuple[Term, ...]
    exclude: tuple[str, ...]


@dataclass(frozen=True)
class Combo:
    """단어 하나로는 안 잡히는 사업을 의미 조합으로 잡는 규칙.

    all_of 의 모든 그룹에서 최소 1개씩 걸려야 성립한다.
    예) (기계|전기|소방|승강기) AND (유지관리|운영관리|위탁) → FM용역
    """

    id: str
    axis: str
    weight: int
    all_of: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class KeywordConfig:
    min_score: int
    min_axes: int
    exclude_global: tuple[str, ...]
    axes: tuple[Axis, ...]
    combos: tuple[Combo, ...] = ()

    def all_search_terms(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for axis in self.axes:
            for term in axis.search_terms:
                seen.setdefault(term, None)
        return tuple(seen)


def _date_params(raw: dict[str, Any] | None, fallback: DateParams) -> DateParams:
    if not raw:
        return fallback
    return DateParams(
        begin=raw.get("begin", fallback.begin),
        end=raw.get("end", fallback.end),
        fmt=raw.get("fmt", fallback.fmt),
    )


def load_sources(path: str | os.PathLike[str] | None = None) -> SourcesConfig:
    doc = yaml.safe_load(Path(path or DEFAULT_SOURCES).read_text(encoding="utf-8"))

    api_raw = doc.get("api", {})
    api = ApiSettings(
        base=api_raw.get("base", "http://apis.data.go.kr/1230000/ad/BidPublicInfoService").rstrip("/"),
        service_key_param=api_raw.get("service_key_param", "ServiceKey"),
        scheme_fallback=bool(api_raw.get("scheme_fallback", True)),
        timeout_sec=int(api_raw.get("timeout_sec", 30)),
        retries=int(api_raw.get("retries", 3)),
        backoff_sec=float(api_raw.get("backoff_sec", 2.0)),
        num_of_rows=int(api_raw.get("num_of_rows", 100)),
        max_pages=int(api_raw.get("max_pages", 20)),
        sleep_between_calls_sec=float(api_raw.get("sleep_between_calls_sec", 0.3)),
    )

    defaults = doc.get("defaults", {})
    default_dates = _date_params(
        defaults.get("date_params"),
        DateParams(begin="inqryBgnDt", end="inqryEndDt", fmt="%Y%m%d%H%M"),
    )
    default_keyword_param = defaults.get("keyword_param", "bidNtceNm")

    sources: list[Source] = []
    for raw in doc.get("sources", []):
        variants = tuple(
            Variant(
                path=v["path"].strip("/"),
                date_params=_date_params(v.get("date_params"), default_dates),
            )
            for v in raw.get("variants", [])
        )
        if not variants:
            raise ValueError(f"source '{raw.get('id')}' 에 variants 가 없습니다")
        sources.append(
            Source(
                id=raw["id"],
                label=raw["label"],
                kind=raw.get("kind", "bid"),
                enabled=bool(raw.get("enabled", True)),
                keyword_param=raw.get("keyword_param", default_keyword_param),
                fixed_params={k: str(v) for k, v in (raw.get("fixed_params") or {}).items()},
                variants=variants,
            )
        )
    return SourcesConfig(api=api, sources=tuple(sources))


def load_keywords(path: str | os.PathLike[str] | None = None) -> KeywordConfig:
    doc = yaml.safe_load(Path(path or DEFAULT_KEYWORDS).read_text(encoding="utf-8"))

    axes: list[Axis] = []
    for raw in doc.get("axes", []):
        terms = tuple(
            Term(
                pattern=t["pattern"],
                weight=int(t.get("weight", 1)),
                regex=t.get("regex"),
                ignore_case=t.get("ignore_case"),
            )
            for t in raw.get("terms", [])
        )
        axes.append(
            Axis(
                id=raw["id"],
                search_terms=tuple(raw.get("search_terms", [])),
                terms=terms,
                exclude=tuple(raw.get("exclude", [])),
            )
        )

    axis_ids = {a.id for a in axes}
    combos: list[Combo] = []
    for raw in doc.get("combos", []):
        if raw["axis"] not in axis_ids:
            raise ValueError(f"combo '{raw['id']}' 가 존재하지 않는 축 '{raw['axis']}' 을 가리킵니다")
        combos.append(
            Combo(
                id=raw["id"],
                axis=raw["axis"],
                weight=int(raw.get("weight", 1)),
                all_of=tuple(tuple(g) for g in raw.get("all_of", [])),
            )
        )

    return KeywordConfig(
        min_score=int(doc.get("min_score", 3)),
        min_axes=int(doc.get("min_axes", 1)),
        exclude_global=tuple(doc.get("exclude_global", [])),
        axes=tuple(axes),
        combos=tuple(combos),
    )
