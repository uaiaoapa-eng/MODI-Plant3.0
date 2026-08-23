"""#70: 후처리(설계·노트·주석) 게이팅 + 출력 상한/캐시 + 수정 턴 스코어 검증.

- P1: 이미 있는 산출물(설계문서·학습노트·주석)은 _run_post_agents 가 재생성하지 않는다.
- P2: 후처리 서브콜은 MAX_OUTPUT_TOKENS_POST 상한을 쓰고, 캐시 분기를 타며,
      상한에 걸려 잘리면(_turn_post_truncated) 스코어로 노출된다.
- P3: 수정 턴 스코어(modify_no_change/modify_clarified)가 실제 실패 때만 찍히고,
      실변경(코드 바뀜) 턴엔 안 찍힌다 — O2 유도가 실변경까지 억제하지 않는지 회귀 감시.
"""
import agent.orchestrator_stream as OS
from agent.orchestrator_stream import MAX_OUTPUT_TOKENS_POST
from agent.prompts import MODIFY_EDIT_DIRECTIVE


class _FakeObs:
    """generation/span 겸용 — 컨텍스트 매니저이자 update 가능한 핸들."""
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def update(self, **kw):
        pass


class _FakeClient:
    def __init__(self):
        self.scores = []

    def start_as_current_observation(self, **kw):
        return _FakeObs()

    def score_current_trace(self, name, value, data_type=None, **kw):
        self.scores.append({"name": name, "value": value, "data_type": data_type})

    def create_event(self, **kw):
        pass

    def update_current_generation(self, **kw):
        pass

    def update_current_span(self, **kw):
        pass


def _orch(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(OS, "get_client", lambda: fake)
    orch = OS.StreamOrchestrator(api_key="")
    orch._step_count = 0
    orch._turn_steps = []
    return orch, fake


# ── P1: 후처리 게이팅 ──────────────────────────────────────────────

def _record_post_jobs(orch, monkeypatch):
    """3개 후처리 서브에이전트 호출을 이름으로 기록하도록 대체."""
    called = []
    monkeypatch.setattr(orch, "_extract_design_doc", lambda: called.append("design"))
    monkeypatch.setattr(orch, "_generate_learning_notes",
                        lambda *a, **k: called.append("notes"))
    monkeypatch.setattr(orch, "_generate_annotations", lambda: called.append("annotations"))
    return called


def test_post_agents_runs_all_when_absent(monkeypatch):
    orch, _ = _orch(monkeypatch)
    called = _record_post_jobs(orch, monkeypatch)
    # 설계·노트·주석 모두 비어 있음(첫 구현) → 셋 다 실행.
    list(orch._run_post_agents(run_build=False, run_post=True))
    assert set(called) == {"design", "notes", "annotations"}


def test_post_agents_skips_existing_notes_and_annotations(monkeypatch):
    orch, _ = _orch(monkeypatch)
    called = _record_post_jobs(orch, monkeypatch)
    # 이미 산출물이 있는 턴(재진입/재실행) → 재생성 스킵(중복 누적·출력 낭비 방지).
    orch.state.learning_notes = [{"title": "t", "what": "w", "why": "y", "where": "z"}]
    orch.state.code_annotations = [{"file": "App.tsx", "line": 1, "title": "x", "explanation": "e"}]
    from agent.models import Feature
    orch.state.project.design_doc.features = [Feature(name="f")]
    list(orch._run_post_agents(run_build=False, run_post=True))
    assert called == []


def test_post_agents_skips_all_when_run_post_false(monkeypatch):
    orch, _ = _orch(monkeypatch)
    called = _record_post_jobs(orch, monkeypatch)
    # 수정 턴(run_post=False)은 후처리를 아예 돌리지 않는다(빌드만).
    list(orch._run_post_agents(run_build=False, run_post=False))
    assert called == []


# ── P2: 후처리 출력 상한 · 캐시 · truncation ────────────────────────

class _Resp:
    def __init__(self, stop_reason="end_turn"):
        self.content = []          # tool_use 없음 → handle_tool_call 미호출
        self.stop_reason = stop_reason
        self.usage = None


class _FakeMessages:
    def __init__(self, stop_reason="end_turn"):
        self.kwargs = None
        self._stop = stop_reason

    def create(self, **kw):
        self.kwargs = kw
        return _Resp(self._stop)


def test_call_tools_uses_post_output_cap(monkeypatch):
    orch, _ = _orch(monkeypatch)
    monkeypatch.setattr(OS, "_use_local_cli", lambda: True)
    msgs = _FakeMessages()
    orch.client.messages = msgs
    orch._call_tools(["add_learning_note"], "sys", "prompt", {"type": "any"})
    assert msgs.kwargs["max_tokens"] == MAX_OUTPUT_TOKENS_POST


def test_call_tools_cli_mode_passes_plain_system(monkeypatch):
    orch, _ = _orch(monkeypatch)
    monkeypatch.setattr(OS, "_use_local_cli", lambda: True)
    msgs = _FakeMessages()
    orch.client.messages = msgs
    orch._call_tools(["add_learning_note"], "sys", "prompt", {"type": "any"})
    # CLI 모드: system 은 평문 문자열(캐시 블록 없음), tools 는 원본 리스트.
    assert isinstance(msgs.kwargs["system"], str)
    assert isinstance(msgs.kwargs["tools"], list)


def test_call_tools_api_mode_attaches_cache_control(monkeypatch):
    orch, _ = _orch(monkeypatch)
    monkeypatch.setattr(OS, "_use_local_cli", lambda: False)
    msgs = _FakeMessages()
    orch.client.messages = msgs
    orch._call_tools(["add_learning_note"], "sys", "prompt", {"type": "any"})
    # API 모드: system 은 cache_control 붙은 text 블록 리스트.
    sys_arg = msgs.kwargs["system"]
    assert isinstance(sys_arg, list) and sys_arg[0].get("cache_control")
    # tools 마지막 항목에 cache_control 부착.
    assert msgs.kwargs["tools"][-1].get("cache_control")


def test_call_tools_sets_post_truncated_flag(monkeypatch):
    orch, _ = _orch(monkeypatch)
    monkeypatch.setattr(OS, "_use_local_cli", lambda: True)
    orch._turn_post_truncated = False
    orch.client.messages = _FakeMessages(stop_reason="max_tokens")
    orch._call_tools(["add_learning_note"], "sys", "prompt", {"type": "any"})
    assert orch._turn_post_truncated is True


def test_call_tools_no_truncation_when_end_turn(monkeypatch):
    orch, _ = _orch(monkeypatch)
    monkeypatch.setattr(OS, "_use_local_cli", lambda: True)
    orch._turn_post_truncated = False
    orch.client.messages = _FakeMessages(stop_reason="end_turn")
    orch._call_tools(["add_learning_note"], "sys", "prompt", {"type": "any"})
    assert orch._turn_post_truncated is False


def test_post_truncated_emits_score(monkeypatch):
    orch, fake = _orch(monkeypatch)
    orch._reuse_flag = None
    orch._ontology_primed = None
    orch._turn_post_truncated = True
    orch._emit_turn_scores("quick")
    names = {s["name"] for s in fake.scores}
    assert "후처리 출력 잘림 (post_output_truncated)" in names


# ── P3: 수정 턴 스코어 — 실변경까지 억제하지 않는지 회귀 감시 ────────

def _emit_modify(monkeypatch, *, modify_failed=False, modify_clarified=False):
    orch, fake = _orch(monkeypatch)
    orch._reuse_flag = None
    orch._ontology_primed = None
    orch._turn_modify_failed = modify_failed
    orch._turn_modify_clarified = modify_clarified
    orch._emit_turn_scores("quick")
    return {s["name"] for s in fake.scores}


def test_modify_no_change_scored_only_on_failure(monkeypatch):
    names = _emit_modify(monkeypatch, modify_failed=True)
    assert "수정 미적용 (modify_no_change)" in names


def test_successful_edit_turn_has_no_failure_scores(monkeypatch):
    # 코드가 실제로 바뀐 수정 턴: 두 실패 플래그 모두 False → 실패 스코어 없음.
    names = _emit_modify(monkeypatch)
    assert "수정 미적용 (modify_no_change)" not in names
    assert "수정 대신 질문 (modify_clarified)" not in names


def test_modify_directive_is_edit_biased_not_silencing():
    # O2 유도 문구는 'edit_code 로 바꾸라'는 지시지 '수정하지 말라'가 아니어야 한다.
    assert "edit_code" in MODIFY_EDIT_DIRECTIVE
    assert "바꾸는" in MODIFY_EDIT_DIRECTIVE


# ── 후속(속도): 학습 노트 병렬 샤드 + 중복 제거 ──────────────────────

def test_notes_single_shard_calls_once(monkeypatch):
    orch, _ = _orch(monkeypatch)
    calls = []
    monkeypatch.setattr(orch, "_notes_call",
                        lambda ctx, cnt, lens="": calls.append((cnt, lens)))
    orch._generate_learning_notes("some code context", "5~8", shards=1)
    # 단일 샤드: 렌즈 없이 1회(종전 동작 보존).
    assert calls == [("5~8", "")]


def test_notes_sharded_fans_out_with_distinct_lenses(monkeypatch):
    orch, _ = _orch(monkeypatch)
    calls = []
    monkeypatch.setattr(orch, "_notes_call",
                        lambda ctx, cnt, lens="": calls.append((cnt, lens)))
    orch._generate_learning_notes("some code context", "5~8", shards=2)
    # 2 샤드: 총 8개를 4/4 로 분배, 서로 다른 렌즈.
    assert len(calls) == 2
    assert {c for c, _ in calls} == {"4"}
    lenses = {lens for _, lens in calls}
    assert len(lenses) == 2 and "" not in lenses


def test_notes_shards_dedup_by_title(monkeypatch):
    orch, _ = _orch(monkeypatch)

    def fake_call(ctx, cnt, lens=""):
        # 각 샤드가 같은 제목("겹침") 하나 + 고유 제목 하나를 추가 → 샤드 간 중복 유발.
        orch.state.learning_notes.append({"title": "겹침", "what": "", "why": "", "where": ""})
        orch.state.learning_notes.append({"title": f"고유-{lens[:4]}", "what": "", "why": "", "where": ""})

    monkeypatch.setattr(orch, "_notes_call", fake_call)
    orch._generate_learning_notes("some code context", "5~8", shards=2)
    titles = [n["title"] for n in orch.state.learning_notes]
    # "겹침" 은 1개만 남고, 고유 노트 2개는 보존 → 총 3개.
    assert titles.count("겹침") == 1
    assert len(orch.state.learning_notes) == 3
