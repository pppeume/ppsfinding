"""키워드 사전 기반 2차 필터 및 관련도 점수 계산."""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from .config import Axis, KeywordConfig, Term


@dataclass(frozen=True)
class MatchResult:
    matched: bool
    score: int
    axes: tuple[str, ...]
    hits: tuple[str, ...]
    reason: str = ""


def _compile(term: Term) -> re.Pattern[str]:
    """term 을 정규식으로 컴파일.

    regex 를 직접 준 경우에는 대소문자를 구분한다(AI/FM/EMS 같은 약어의 오탐을 줄이려고
    경계 조건을 직접 쓴 경우이므로). 평문 pattern 은 기본적으로 대소문자를 무시한다.
    ignore_case 를 명시하면 그 값이 우선한다.
    """
    if term.regex:
        ignore_case = False if term.ignore_case is None else term.ignore_case
        source = term.regex
    else:
        ignore_case = True if term.ignore_case is None else term.ignore_case
        source = re.escape(term.pattern)
    return re.compile(source, re.IGNORECASE if ignore_case else 0)


@lru_cache(maxsize=1024)
def _compiled_axis_terms(axis: Axis) -> tuple[tuple[Term, re.Pattern[str]], ...]:
    return tuple((t, _compile(t)) for t in axis.terms)


def normalize_text(*parts: str | None) -> str:
    """공백/구분기호를 제거해 '통합 관제', '통합-관제' 같은 표기 차이를 흡수한다."""
    joined = " ".join(p for p in parts if p)
    return re.sub(r"[\s·・/\\|,()\[\]{}<>\"'`~_]+", "", joined)


def match(text_parts: tuple[str | None, ...], cfg: KeywordConfig) -> MatchResult:
    """공고명 등 텍스트를 키워드 사전에 대조해 매칭 축과 점수를 계산한다."""
    raw = " ".join(p for p in text_parts if p)
    if not raw.strip():
        return MatchResult(False, 0, (), (), "빈 텍스트")

    # 두 형태 모두에 대조한다. 원문(raw)은 영문 약어의 단어 경계를 살리기 위해,
    # 압축본(squeezed)은 '통합 관제'처럼 띄어쓰기가 낀 한글 표기를 잡기 위해.
    squeezed = normalize_text(*text_parts)

    for bad in cfg.exclude_global:
        if bad in raw or bad in squeezed:
            return MatchResult(False, 0, (), (), f"전역 제외어: {bad}")

    total = 0
    matched_axes: list[str] = []
    hits: list[str] = []

    for axis in cfg.axes:
        if any(bad in raw or bad in squeezed for bad in axis.exclude):
            continue

        axis_score = 0
        for term, pattern in _compiled_axis_terms(axis):
            if pattern.search(raw) or pattern.search(squeezed):
                axis_score += term.weight
                hits.append(f"{axis.id}:{term.pattern}(+{term.weight})")

        if axis_score > 0:
            matched_axes.append(axis.id)
            total += axis_score

    if len(matched_axes) < cfg.min_axes:
        return MatchResult(False, total, tuple(matched_axes), tuple(hits), "매칭 축 부족")
    if total < cfg.min_score:
        return MatchResult(
            False, total, tuple(matched_axes), tuple(hits), f"점수 미달({total}<{cfg.min_score})"
        )

    return MatchResult(True, total, tuple(matched_axes), tuple(hits))
