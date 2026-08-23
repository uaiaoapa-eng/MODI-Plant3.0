"""#68 O1: 출력 토큰 구성 계측 검증.

attribute_output 이 한 호출의 출력 토큰을 생성 종류(전체재작성/부분수정/산문/기타도구)별로
문자 길이 비율에 따라 근사 배분하고, 합이 정확히 output_tokens 로 보존되는지 확인한다.
그리고 _emit_turn_scores 가 수정 턴(진입 시 코드 있음)에 한해 "전체재작성 출력" 코호트를
별도 스코어로 찍는지 검증한다(O2 튜닝의 핵심 지표).
"""
from agent.usage import attribute_output
import agent.orchestrator_stream as OS


class _TU:
    """tool_use 블록 흉내 — name/input 속성만 있으면 attribute_output 이 읽는다."""
    def __init__(self, name, inp):
        self.name = name
        self.input = inp


def test_empty_when_no_output_tokens():
    out = attribute_output(0, "some prose", [_TU("generate_code", {"code": "x" * 100})])
    assert out == {"generate_code": 0, "edit_code": 0, "other_tool": 0, "prose": 0}


def test_prose_only():
    out = attribute_output(500, "안녕하세요 " * 10, [])
    assert out["prose"] == 500
    assert out["generate_code"] == out["edit_code"] == out["other_tool"] == 0


def test_sum_preserved_and_split_by_size():
    # generate_code 코드 900자 vs edit_code diff 100자 → 대략 9:1, 합은 정확히 1000.
    tools = [
        _TU("generate_code", {"code": "a" * 900}),
        _TU("edit_code", {"old_code": "b" * 50, "new_code": "c" * 50}),
    ]
    out = attribute_output(1000, "", tools)
    assert sum(out.values()) == 1000  # 반올림 오차 흡수로 합 보존
    assert out["generate_code"] > out["edit_code"] > 0
    assert out["prose"] == 0


def test_generate_dominates_flags_rewrite_waste():
    # 수정 턴에서 작은 편집이면 좋겠지만 전체재작성이면 generate_code 토큰이 지배 → 낭비 신호.
    out = attribute_output(2000, "고칠게요", [_TU("generate_code", {"code": "z" * 5000})])
    assert out["generate_code"] > out["prose"] > 0


def test_dict_tool_use_shape_supported():
    # CLI 폴백 등에서 tool_use 가 dict 로 올 수도 있다.
    out = attribute_output(300, "", [{"name": "edit_code",
                                      "input": {"old_code": "x" * 30, "new_code": "y" * 30}}])
    assert out["edit_code"] == 300


class _FakeClient:
    def __init__(self):
        self.scores = []

    def score_current_trace(self, name, value, data_type=None, **kw):
        self.scores.append({"name": name, "value": value, "data_type": data_type})


def _emit(monkeypatch, *, had_code, gen, edit, prose):
    fake = _FakeClient()
    monkeypatch.setattr(OS, "get_client", lambda: fake)
    orch = OS.StreamOrchestrator(api_key="")
    orch._reuse_flag = None
    orch._ontology_primed = None
    orch._turn_out_generate = gen
    orch._turn_out_edit = edit
    orch._turn_out_prose = prose
    orch._turn_out_other = 0
    orch._turn_had_code_at_start = had_code
    orch._emit_turn_scores("quick")
    return {s["name"]: s for s in fake.scores}


def test_edit_turn_full_rewrite_cohort_emitted(monkeypatch):
    scores = _emit(monkeypatch, had_code=True, gen=7000, edit=200, prose=100)
    assert scores["출력 전체재작성 토큰 (output_generate_tokens)"]["value"] == 7000
    assert scores["출력 부분수정 토큰 (output_edit_tokens)"]["value"] == 200
    # 수정 턴 전용 코호트가 콜드 빌드와 분리되어 찍힌다.
    assert scores["수정턴 전체재작성 출력 (edit_turn_full_rewrite_tokens)"]["value"] == 7000
    assert scores["수정턴 부분수정 출력 (edit_turn_edit_tokens)"]["value"] == 200


def test_cold_build_has_no_edit_turn_cohort(monkeypatch):
    scores = _emit(monkeypatch, had_code=False, gen=5000, edit=0, prose=100)
    assert "출력 전체재작성 토큰 (output_generate_tokens)" in scores
    # 콜드 빌드는 수정 턴 코호트를 찍지 않는다(정상 첫 생성과 수정 낭비를 섞지 않으려).
    assert "수정턴 전체재작성 출력 (edit_turn_full_rewrite_tokens)" not in scores
