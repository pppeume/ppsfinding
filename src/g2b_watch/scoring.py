"""수주검토 점수(Opportunity Score) 산출 — 100점 만점.

배점(rules.yaml scoring.weights)
    business_fit  30  키워드 관련도 = 사업 적합성
    area_match    20  매칭된 축이 회사 사업영역과 겹치는 정도
    budget        15  사업규모 적합성
    region        10  지역 적합성
    tech_fit      10  기술 축(IoT/BEMS/통합관제/AI) 해당 여부
    qualification 15  참가자격 판정 결과
"""
from __future__ import annotations

from .company import CompanyProfile, Rules
from .normalize import Record

TECH_AXES = {"IoT", "BEMS", "통합관제", "AI"}


def _clamp(value: float, hi: int) -> int:
    return max(0, min(hi, round(value)))


def score(rec: Record, company: CompanyProfile, rules: Rules) -> tuple[int, dict[str, int]]:
    w = rules.weights
    out: dict[str, int] = {}

    # 사업 적합성 — 키워드 관련도를 만점 기준으로 정규화
    full = max(1, rules.business_fit_full_score)
    out["business_fit"] = _clamp(rec.score / full * w.get("business_fit", 0), w.get("business_fit", 0))

    # 사업영역 적합성 — 매칭 축 중 회사 사업영역에 속하는 비율
    area_max = w.get("area_match", 0)
    if rec.axes and company.business_areas:
        overlap = sum(1 for a in rec.axes if a in company.business_areas)
        out["area_match"] = _clamp(overlap / len(rec.axes) * area_max, area_max)
    else:
        out["area_match"] = 0

    # 사업규모 — 추정가격이 없으면 판단 불가라 절반만 준다
    budget_max = w.get("budget", 0)
    if rec.price is None:
        out["budget"] = _clamp(budget_max / 2, budget_max)
    elif rec.price < company.min_krw:
        out["budget"] = 0
    elif company.sweet_spot_min_krw <= rec.price <= company.sweet_spot_max_krw:
        out["budget"] = budget_max
    else:
        out["budget"] = _clamp(budget_max * 0.6, budget_max)

    # 지역 — 참가가능지역이 없으면 제한 없는 공고로 보고 만점
    region_max = w.get("region", 0)
    if not rec.allowed_regions:
        out["region"] = region_max
    else:
        mine = [company.headquarters_region, *company.branch_regions]
        hit = any(
            a.strip() == "전국" or any(m and (m in a or a in m) for m in mine if m)
            for a in rec.allowed_regions
        )
        out["region"] = region_max if hit else 0

    # 기술 적합성
    tech_max = w.get("tech_fit", 0)
    out["tech_fit"] = tech_max if set(rec.axes) & TECH_AXES else 0

    # 참가자격
    q_max = w.get("qualification", 0)
    label = rec.verdict
    if label == rules.verdict("eligible"):
        out["qualification"] = q_max
    elif label == rules.verdict("review"):
        out["qualification"] = _clamp(q_max / 2, q_max)
    else:
        out["qualification"] = 0

    return sum(out.values()), out
