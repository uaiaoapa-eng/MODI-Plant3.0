"""wildlife_guard 순수 로직 테스트 (카메라·모델 없이 실행)."""

import pytest

from wildlife_guard.alerts import format_message
from wildlife_guard.animals import ANIMALS, BY_KEY
from wildlife_guard.events import DetectionGate


def test_catalog_has_30_unique_animals():
    assert len(ANIMALS) == 30
    assert len(BY_KEY) == 30
    for a in ANIMALS:
        assert a.prompts and a.risk in (1, 2, 3) and a.group in ("mammal", "bird")


def test_gate_requires_consecutive_hits():
    gate = DetectionGate(window=5, min_hits=3, cooldown_s=60)
    assert gate.update({"wild_boar"}, 0) == []
    assert gate.update({"wild_boar"}, 1) == []
    assert gate.update({"wild_boar"}, 2) == ["wild_boar"]


def test_gate_ignores_single_frame_false_positive():
    gate = DetectionGate(window=5, min_hits=3, cooldown_s=60)
    fired = [gate.update({"black_bear"} if t == 0 else set(), t) for t in range(5)]
    assert fired == [[]] * 5


def test_gate_cooldown():
    gate = DetectionGate(window=3, min_hits=1, cooldown_s=60)
    assert gate.update({"sparrow"}, 0) == ["sparrow"]
    assert gate.update({"sparrow"}, 30) == []
    assert gate.update({"sparrow"}, 61) == ["sparrow"]


def test_gate_rejects_bad_params():
    with pytest.raises(ValueError):
        DetectionGate(window=2, min_hits=3)


def test_protected_species_message():
    msg = format_message(BY_KEY["black_bear"], 0.9, "cam1")
    assert "보호종" in msg and "긴급" in msg
