"""회사 프로필(company.yaml)과 판정·배점 규칙(rules.yaml) 로딩."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .config import REPO_ROOT

DEFAULT_COMPANY = REPO_ROOT / "config" / "company.yaml"
DEFAULT_RULES = REPO_ROOT / "config" / "rules.yaml"

# 기업규모 중 '대기업 참여제한'·'중소기업자간 경쟁'에 걸리는 쪽
LARGE_SCALES = {"large_enterprise", "middle_standing"}


@dataclass(frozen=True)
class CompanyProfile:
    name: str
    scale: str
    is_software_business: bool
    headquarters_region: str
    branch_regions: tuple[str, ...]
    licenses: tuple[str, ...]
    business_areas: tuple[str, ...]
    min_krw: int
    sweet_spot_min_krw: int
    sweet_spot_max_krw: int

    @property
    def is_large_scale(self) -> bool:
        return self.scale in LARGE_SCALES

    @property
    def is_configured(self) -> bool:
        """자리표시자 그대로인지 판별. 판정 신뢰도를 사용자에게 알릴 때 쓴다."""
        return bool(self.licenses) and not self.name.startswith("(")


@dataclass(frozen=True)
class Rules:
    restriction_codes: dict[str, str]
    verdicts: dict[str, str]
    _rules: dict[str, dict[str, Any]]
    weights: dict[str, int]
    business_fit_full_score: int
    review_threshold: int

    def rule(self, name: str) -> dict[str, Any]:
        return self._rules.get(name, {})

    def restriction_label(self, code: str) -> str:
        return self.restriction_codes.get(code, code)

    def verdict(self, key: str) -> str:
        return self.verdicts.get(key, key)


def load_company(path: str | os.PathLike[str] | None = None) -> CompanyProfile:
    doc = yaml.safe_load(Path(path or DEFAULT_COMPANY).read_text(encoding="utf-8")) or {}
    c = doc.get("company", {}) or {}
    b = doc.get("budget", {}) or {}
    return CompanyProfile(
        name=str(c.get("name", "")),
        scale=str(c.get("scale", "sme")),
        is_software_business=bool(c.get("is_software_business", False)),
        headquarters_region=str(c.get("headquarters_region", "")),
        branch_regions=tuple(c.get("branch_regions") or ()),
        licenses=tuple(c.get("licenses") or ()),
        business_areas=tuple(c.get("business_areas") or ()),
        min_krw=int(b.get("min_krw", 0)),
        sweet_spot_min_krw=int(b.get("sweet_spot_min_krw", 0)),
        sweet_spot_max_krw=int(b.get("sweet_spot_max_krw", 0)),
    )


def load_rules(path: str | os.PathLike[str] | None = None) -> Rules:
    doc = yaml.safe_load(Path(path or DEFAULT_RULES).read_text(encoding="utf-8")) or {}
    scoring = doc.get("scoring", {}) or {}
    weights = {k: int(v) for k, v in (scoring.get("weights") or {}).items()}
    total = sum(weights.values())
    if weights and total != 100:
        raise ValueError(f"scoring.weights 합계가 100 이 아닙니다: {total}")
    return Rules(
        restriction_codes={str(k): str(v) for k, v in (doc.get("restriction_codes") or {}).items()},
        verdicts={str(k): str(v) for k, v in (doc.get("verdicts") or {}).items()},
        _rules=doc.get("rules") or {},
        weights=weights,
        business_fit_full_score=int(scoring.get("business_fit_full_score", 15)),
        review_threshold=int(scoring.get("review_threshold", 70)),
    )
