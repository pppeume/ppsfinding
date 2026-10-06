import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from g2b_watch.config import load_keywords
from g2b_watch.matcher import match, normalize_text

CFG = load_keywords()


def m(title, org=None):
    return match((title, org), CFG)


def test_bems_hit():
    r = m("○○청사 BEMS 구축 용역")
    assert r.matched and "BEMS" in r.axes and r.score >= 5


def test_spaced_korean_is_matched():
    assert m("스마트 통합 관제 시스템 구축").matched


def test_iot_case_insensitive_with_boundary():
    assert m("IoT 기반 설비 원격감시").matched
    # 단어 내부에 낀 경우는 잡지 않는다
    assert not m("BIOTECH 장비 구매").matched


def test_ai_abbrev_false_positive_is_excluded():
    # 조류인플루엔자(AI) 는 전역 제외어로 걸러진다
    assert not m("고병원성 조류인플루엔자(AI) 방역물자 구매").matched


def test_fm_radio_false_positive_is_excluded():
    assert not m("FM 라디오 송신기 교체").matched


def test_weak_single_term_below_threshold():
    # '에너지' 단독(weight 1)은 min_score 3 미만이라 탈락
    r = m("에너지 절약 홍보물 제작")
    assert not r.matched and r.score < CFG.min_score


def test_energy_diagnosis_passes():
    assert m("공공건축물 에너지진단 용역").matched


def test_multi_axis_score_sums():
    r = m("AI 기반 통합관제 플랫폼 및 IoT 센서 구축")
    assert r.matched
    assert set(r.axes) >= {"AI", "통합관제", "IoT"}


def test_global_exclude_drops_record():
    r = m("청사 청소용역 통합관제 위탁")
    assert not r.matched and "전역 제외어" in r.reason


def test_normalize_text_strips_separators():
    assert normalize_text("통합 · 관제", None) == "통합관제"
