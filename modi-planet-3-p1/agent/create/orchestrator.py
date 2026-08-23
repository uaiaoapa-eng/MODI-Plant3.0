"""Thin Create-mode adapter around the legacy ``/chat`` contract.

The adapter deliberately contains no generation logic.  It fixes the public
Create product to the existing guided-design flow and translates a v3 Create
turn into the payload already understood by ``server.chat``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar
from uuid import uuid4


SUPPORTED_CODING_TYPES = frozenset({"react", "blockly", "hybrid"})


def _validated_coding_type(value: str) -> str:
    if value not in SUPPORTED_CODING_TYPES:
        allowed = ", ".join(sorted(SUPPORTED_CODING_TYPES))
        raise ValueError(f"coding_type must be one of: {allowed}")
    return value


@dataclass(frozen=True, slots=True)
class CreateOrchestratorAdapter:
    """Session metadata and legacy request translation for Create mode."""

    session_id: str
    coding_type: str
    mode: ClassVar[str] = "design"

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("session_id must not be empty")
        object.__setattr__(self, "coding_type", _validated_coding_type(self.coding_type))

    @classmethod
    def start(cls, coding_type: str) -> "CreateOrchestratorAdapter":
        return cls(session_id=uuid4().hex, coding_type=coding_type)

    def legacy_chat_payload(self, message: str, runtime_error: str = "") -> dict[str, str]:
        """Return the existing ``ChatRequest`` fields with guided mode forced."""
        return {
            "session_id": self.session_id,
            "message": message,
            "mode": self.mode,
            "coding_type": self.coding_type,
            "runtime_error": runtime_error,
        }
