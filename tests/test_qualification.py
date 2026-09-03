"""참가자격 판정 엔진과 수주검토 점수 검증."""
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from g2b_watch.company import load_company, load_rules
from g2b_watch.normalize import Record
from g2b_watch.qualification import evaluate
from g2b_watch.scoring import score

RULES = load_rules()
BASE_COMPANY = replace(
    load_company(),
    name="테스트회사",
    scale="large_enterprise",
    headquarters_region="경기도",
    branch_regions=("부산광역시",),
    licenses=("시설물유지관리업", "정보통신공사업", "전기공사업"),
    business_areas=("FM용역", "IoT", "BEMS", "에너지", "통합관제", "AI"),
)

ELIGIBLE = RULES.verdict("eligible")
REVIEW = RULES.verdict("review")
INELIGIBLE = RULES.verdict("ineligible")


def rec(**kw) -> Record:
    base = dict(
        key="R1-000",
        title="○○청사 시설관리 용역",
        source_id="bid_servc",
        business_type="용역",
        axes=("FM용역",),
        score=9,
    )
    base.update(kw)
    return Record(**base)


# --- 지역제한 -----------------------------------------------------------------


def test_region_mismatch_is_ineligible():
    r = rec(allowed_regions=("부산광역시",))
    q = evaluate(r, BASE_COMPANY, RULES)
    assert q.verdict == INELIGIBLE
    assert "R01" in q.codes
    assert "부산광역시" in q.reasons[0]


def test_branch_region_counts_only_when_branch_bidding_allowed():
    """지사가 부산에 있어도 지사투찰 불허 공고면 참여불가."""
    blocked = evaluate(rec(allowed_regions=("부산광역시",), branch_bid_allowed=False), BASE_COMPANY, RULES)
    assert blocked.verdict == INELIGIBLE

    allowed = evaluate(rec(allowed_regions=("부산광역시",), branch_bid_allowed=True), BASE_COMPANY, RULES)
    assert allowed.verdict == ELIGIBLE


def test_nationwide_and_partial_name_match_pass():
    assert evaluate(rec(allowed_regions=("전국",)), BASE_COMPANY, RULES).verdict == ELIGIBLE
    assert evaluate(rec(allowed_regions=("경기",)), BASE_COMPANY, RULES).verdict == ELIGIBLE


def test_no_region_info_is_treated_as_open():
    assert evaluate(rec(), BASE_COMPANY, RULES).verdict == ELIGIBLE


# --- 면허 ---------------------------------------------------------------------


def test_missing_license_is_ineligible():
    r = rec(industry_limited=True, required_licenses=("액화석유가스판매사업",))
    q = evaluate(r, BASE_COMPANY, RULES)
    assert q.verdict == INELIGIBLE and "R05" in q.codes


def test_owned_license_passes():
    r = rec(industry_limited=True, required_licenses=("정보통신공사업", "전기공사업"))
    assert evaluate(r, BASE_COMPANY, RULES).verdict == ELIGIBLE


def test_industry_limited_without_license_list_is_review_not_ineligible():
    q = evaluate(rec(industry_limited=True), BASE_COMPANY, RULES)
    assert q.verdict == REVIEW and "R05" in q.codes


def test_license_check_skipped_when_company_licenses_unset():
    """회사 면허를 아직 입력하지 않았으면 오판하지 않고 건너뛴다."""
    unset = replace(BASE_COMPANY, licenses=())
    r = rec(industry_limited=True, required_licenses=("액화석유가스판매사업",))
    assert evaluate(r, unset, RULES).verdict == ELIGIBLE


# --- 대기업 / 중소기업 --------------------------------------------------------


def test_info_business_is_review_never_ineligible():
    """정보화사업 대기업 참여제한은 예외 규정이 있어 단정하지 않는다."""
    q = evaluate(rec(info_business=True), BASE_COMPANY, RULES)
    assert q.verdict == REVIEW and "R02" in q.codes


def test_sme_only_notice_blocks_large_enterprise():
    q = evaluate(rec(title="중소기업자간 경쟁제품 구매"), BASE_COMPANY, RULES)
    assert q.verdict == INELIGIBLE and "R03" in q.codes


def test_sme_only_notice_does_not_block_sme_company():
    sme = replace(BASE_COMPANY, scale="sme")
    assert evaluate(rec(title="중소기업자간 경쟁제품 구매"), sme, RULES).verdict == ELIGIBLE


# --- 기타 제한 ----------------------------------------------------------------


def test_designated_competition_is_ineligible():
    q = evaluate(rec(designated_competition=True), BASE_COMPANY, RULES)
    assert q.verdict == INELIGIBLE and "R10" in q.codes


def test_performance_competition_is_review():
    q = evaluate(rec(performance_competition=True), BASE_COMPANY, RULES)
    assert q.verdict == REVIEW and "R07" in q.codes


def test_joint_contract_region_mismatch_is_review():
    q = evaluate(rec(joint_contract_regions=("서울특별시", "인천광역시")), BASE_COMPANY, RULES)
    assert q.verdict == REVIEW and "R09" in q.codes


def test_multiple_restrictions_ineligible_wins():
    q = evaluate(
        rec(performance_competition=True, designated_competition=True), BASE_COMPANY, RULES
    )
    assert q.verdict == INELIGIBLE
    assert set(q.codes) >= {"R07", "R10"}


# --- 점수 ---------------------------------------------------------------------


def _scored(r: Record) -> tuple[int, dict]:
    q = evaluate(r, BASE_COMPANY, RULES)
    r.verdict, r.restriction_codes = q.verdict, q.codes
    return score(r, BASE_COMPANY, RULES)


def test_score_is_within_bounds_and_sums():
    total, parts = _scored(rec(price=1_000_000_000))
    assert 0 <= total <= 100
    assert total == sum(parts.values())
    assert set(parts) == set(RULES.weights)


def test_ideal_notice_scores_high():
    total, _ = _scored(
        rec(title="스마트빌딩 BEMS 통합관제 구축", axes=("BEMS", "통합관제", "IoT"),
            score=15, price=1_000_000_000)
    )
    assert total >= RULES.review_threshold


def test_ineligible_notice_scores_lower_than_same_eligible_one():
    good, _ = _scored(rec(price=1_000_000_000))
    bad, _ = _scored(rec(price=1_000_000_000, designated_competition=True))
    assert bad < good


def test_tiny_budget_zeroes_budget_component():
    _, parts = _scored(rec(price=1_000_000))
    assert parts["budget"] == 0


def test_missing_price_gets_half_budget_credit():
    _, parts = _scored(rec(price=None))
    assert parts["budget"] == round(RULES.weights["budget"] / 2)


def test_weights_must_total_100():
    assert sum(RULES.weights.values()) == 100
