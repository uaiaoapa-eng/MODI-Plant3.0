"""동기 오케스트레이터 — StreamOrchestrator를 래핑하여 로컬 CLI에서 사용."""

from __future__ import annotations

from agent.orchestrator_stream import StreamOrchestrator


class Orchestrator:
    def __init__(self, api_key: str, session_id: str = ""):
        self._stream = StreamOrchestrator(api_key, session_id=session_id)

    @property
    def state(self):
        return self._stream.state

    def chat_stream(self, user_input: str, coding_type: str = None):
        """StreamOrchestrator의 이벤트를 그대로 전달."""
        yield from self._stream.chat_stream(user_input, coding_type=coding_type)

    def get_phase(self) -> str:
        return self._stream.get_phase()

    def get_diagram(self) -> str:
        return self._stream.get_diagram()

    def reset(self):
        self._stream.reset()
