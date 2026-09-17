"""참가자격 판정 엔진 (Bid Qualification Engine).

설계 원칙
  1. 응답에 근거가 있는 것만 단정한다. 근거가 약하면 '참여불가'가 아니라 '검토필요'.
     특히 대기업 참여제한은 사업유형·금액·예외 인정 여부에 따라 달라져 Boolean 하나로
     결론 낼 수 없으므로 항상 '검토필요'로만 표시한다.
  2. '참여불가'로 판정된 공고도 버리지 않는다. 경쟁사·발주처·시장 동향 자료가 된다.
  3. 회사 프로필이 비어 있으면 그 항목의 판정을 건너뛴다(설정 미완료를 오판으로 만들지 않는다).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .company import CompanyProfile, Rules
from .normalize import Record


@dataclass(frozen=True)
class Restriction:
    code: str
    label: str
    detail: str
    verdict: str  # "ineligible" | "review"

    def describe(self) -> str:
        return f"{self.code} {self.label}" + (f" — {self.detail}" if self.detail else "")


@dataclass(frozen=True)
class Qualification:
    verdict: str                       # 참여가능 / 검토필요 / 참여불가
    restrictions: tuple[Restriction, ...]

    @property
    def codes(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for r in self.restrictions:
            seen.setdefault(r.code, None)
        return tuple(seen)

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(r.describe() for r in self.restrictions)


def _contains_any(text: str, markers: Iterable[str]) -> str:
    for m in markers:
        if m and m in text:
            return m
    return ""


def _region_matches(allowed: Iterable[str], mine: Iterable[str]) -> bool:
    """지역명은 표기가 흔들린다('경기', '경기도'). 부분일치 양방향으로 본다."""
    for a in allowed:
        a = a.strip()
        if not a or a in {"전국"}:
            return True
        for m in mine:
            m = m.strip()
            if m and (m in a or a in m):
                return True
    return False


def evaluate(rec: Record, company: CompanyProfile, rules: Rules) -> Qualification:
    found: list[Restriction] = []
    haystack = " ".join(filter(None, (rec.title, rec.bid_method, rec.contract_method)))

    # --- 지역제한 ---------------------------------------------------------
    cfg = rules.rule("region")
    if cfg.get("enabled") and company.headquarters_region:
        mine = [company.headquarters_region]
        if rec.branch_bid_allowed or not cfg.get("honor_branch_only_when_permitted", True):
            mine += list(company.branch_regions)

        if rec.allowed_regions:
            if not _region_matches(rec.allowed_regions, mine):
                found.append(
                    Restriction(
                        cfg["code"],
                        rules.restriction_label(cfg["code"]),
                        f"참가가능지역 {', '.join(rec.allowed_regions)} / 본사 {company.headquarters_region}",
                        cfg.get("verdict_on_mismatch", "ineligible"),
                    )
                )
        elif rec.region_limit_basis and not cfg.get("treat_missing_as_open", True):
            found.append(
                Restriction(
                    cfg["code"],
                    rules.restriction_label(cfg["code"]),
                    f"지역제한 공고이나 참가가능지역을 확인하지 못함({rec.region_limit_basis})",
                    "review",
                )
            )

    # --- 면허·업종 제한 ---------------------------------------------------
    cfg = rules.rule("license")
    if cfg.get("enabled") and rec.industry_limited:
        if not company.licenses and cfg.get("skip_when_company_licenses_empty", True):
            pass  # 회사 면허 미설정 — 판정하지 않는다
        elif not rec.required_licenses:
            found.append(
                Restriction(
                    cfg["code"],
                    rules.restriction_label(cfg["code"]),
                    "업종제한 공고이나 요구 면허를 확인하지 못함",
                    cfg.get("verdict_on_unknown", "review"),
                )
            )
        elif not _license_matches(rec.required_licenses, company.licenses):
            found.append(
                Restriction(
                    cfg["code"],
                    rules.restriction_label(cfg["code"]),
                    f"요구 면허 {', '.join(rec.required_licenses[:5])}",
                    cfg.get("verdict_on_mismatch", "ineligible"),
                )
            )

    # --- 대기업 참여제한(정보화사업) --------------------------------------
    cfg = rules.rule("large_enterprise_sw")
    if cfg.get("enabled") and rec.info_business and company.is_large_scale:
        found.append(
            Restriction(
                cfg["code"],
                rules.restriction_label(cfg["code"]),
                "정보화사업 — 대기업 참여제한 적용 여부와 예외 인정 여부 확인 필요",
                cfg.get("verdict", "review"),
            )
        )

    # --- 중소기업자간 경쟁 / 소기업·소상공인 제한 -------------------------
    cfg = rules.rule("smallbiz_only")
    if cfg.get("enabled") and company.is_large_scale:
        codes = cfg.get("codes", {})
        hit = _contains_any(haystack, cfg.get("sme_markers", []))
        if hit:
            code = codes.get("sme", "R03")
            found.append(
                Restriction(code, rules.restriction_label(code), f"공고 문구 '{hit}'", cfg.get("verdict", "ineligible"))
            )
        hit = _contains_any(haystack, cfg.get("small_markers", []))
        if hit:
            code = codes.get("small", "R04")
            found.append(
                Restriction(code, rules.restriction_label(code), f"공고 문구 '{hit}'", cfg.get("verdict", "ineligible"))
            )

    # --- 실적경쟁 ---------------------------------------------------------
    cfg = rules.rule("performance")
    if cfg.get("enabled") and rec.performance_competition:
        found.append(
            Restriction(cfg["code"], rules.restriction_label(cfg["code"]),
                        "실적경쟁 공고 — 유사 실적 요건 확인 필요", cfg.get("verdict", "review"))
        )

    # --- 지명경쟁 ---------------------------------------------------------
    cfg = rules.rule("designated")
    if cfg.get("enabled") and rec.designated_competition:
        found.append(
            Restriction(cfg["code"], rules.restriction_label(cfg["code"]),
                        "지명경쟁 — 지명받지 않으면 참여 불가", cfg.get("verdict", "ineligible"))
        )

    # --- 입찰참가제한 -----------------------------------------------------
    cfg = rules.rule("participation_limited")
    if cfg.get("enabled") and rec.participation_limited:
        found.append(
            Restriction(cfg["code"], rules.restriction_label(cfg["code"]),
                        "입찰참가제한 공고 — 제한 내용 확인 필요", cfg.get("verdict", "review"))
        )

    # --- 지역의무공동도급 -------------------------------------------------
    cfg = rules.rule("joint_contract_region")
    if cfg.get("enabled") and rec.joint_contract_regions and company.headquarters_region:
        if not _region_matches(rec.joint_contract_regions, [company.headquarters_region]):
            found.append(
                Restriction(
                    cfg["code"],
                    rules.restriction_label(cfg["code"]),
                    f"지역의무공동도급 {', '.join(rec.joint_contract_regions)} — 컨소시엄 구성 필요",
                    cfg.get("verdict", "review"),
                )
            )

    if any(r.verdict == "ineligible" for r in found):
        verdict = rules.verdict("ineligible")
    elif found:
        verdict = rules.verdict("review")
    else:
        verdict = rules.verdict("eligible")

    return Qualification(verdict=verdict, restrictions=tuple(found))


def _license_matches(required: Iterable[str], owned: Iterable[str]) -> bool:
    """요구 면허 중 하나라도 보유하면 통과. 표기 흔들림을 감안해 부분일치."""
    owned_norm = [o.replace(" ", "") for o in owned if o]
    for req in required:
        r = req.replace(" ", "")
        if not r:
            continue
        for o in owned_norm:
            if o in r or r in o:
                return True
    return False
