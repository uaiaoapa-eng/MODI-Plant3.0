"""중단(stop) 실효화 테스트(S7) — 진행 중 CLI 서브프로세스를 실제로 종료.

서브프로세스를 실제로 띄우지 않고, 가짜 proc/stream 으로 kill 디스패치 로직만 검증한다.
"""
import pytest

from agent.claude_client import _CliStream


class FakeProc:
    def __init__(self, alive=True):
        self.killed = False
        self._alive = alive

    def poll(self):
        return None if self._alive else 0  # None=실행 중

    def kill(self):
        self.killed = True
        self._alive = False


def test_clistream_kill_terminates_running_proc():
    s = _CliStream("prompt", "model", False)
    fp = FakeProc(alive=True)
    s._proc = fp
    s.kill()
    assert s._killed is True
    assert fp.killed is True


def test_clistream_kill_noop_when_proc_finished():
    s = _CliStream("prompt", "model", False)
    fp = FakeProc(alive=False)  # 이미 종료됨
    s._proc = fp
    s.kill()
    assert s._killed is True
    assert fp.killed is False  # poll() != None → kill 호출 안 함


def test_clistream_kill_without_proc_is_safe():
    s = _CliStream("prompt", "model", False)
    s.kill()  # _proc 가 None — 예외 없이
    assert s._killed is True


def test_clistream_exit_kills_proc():
    s = _CliStream("prompt", "model", False)
    fp = FakeProc(alive=True)
    s._proc = fp
    s.__exit__(None, None, None)  # with 블록 이탈 시 정리
    assert fp.killed is True


# ── 오케스트레이터 cancel() 디스패치 ──

class FakeStream:
    def __init__(self):
        self.killed = False

    def kill(self):
        self.killed = True


def _make_orch():
    try:
        from agent.orchestrator_stream import StreamOrchestrator
    except Exception as e:  # 의존성 미설치 환경
        pytest.skip(f"StreamOrchestrator import 불가: {e}")
    return StreamOrchestrator(api_key="", session_id="t")


def test_cancel_sets_flag_and_kills_active_stream():
    orch = _make_orch()
    fs = FakeStream()
    orch._active_stream = fs
    orch.cancel()
    assert orch._cancelled is True
    assert fs.killed is True


def test_cancel_without_active_stream_is_safe():
    orch = _make_orch()
    orch._active_stream = None
    orch.cancel()  # 예외 없이
    assert orch._cancelled is True
