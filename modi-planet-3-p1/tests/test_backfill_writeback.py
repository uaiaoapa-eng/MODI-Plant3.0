"""backfill_writeback 의 세션→묶음 변환 테스트 (#57).

네트워크 없이 순수 변환(bundle_from_session)과 순회(iter_sessions)만 검증한다.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

try:
    import backfill_writeback as bw
except Exception as e:  # httpx 등 미설치 환경 스킵
    pytest.skip(f"backfill_writeback import 불가: {e}", allow_module_level=True)


def test_bundle_from_full_session():
    data = {
        "session_id": "s1", "user_id": "u1", "coding_type": "react",
        "title": "카드앱",
        "design_doc": {"project_name": "카드앱", "description": "카드 100개",
                       "features": [{"name": "카드"}]},
        "generated_code": {"app.js": "console.log(1)"},
        "learning_notes": [{"title": "루프", "body": "for 문"}],
        "modi_modules": None,
    }
    b = bw.bundle_from_session(data)
    assert b["session_id"] == "s1" and b["user_id"] == "u1"
    assert b["coding_type"] == "react"
    assert b["code_map"] == {"app.js": "console.log(1)"}
    assert b["design_doc"]["project_name"] == "카드앱"
    assert b["goal"] == "카드 100개"  # design_doc.description 우선
    assert b["learning_notes"] == [{"title": "루프", "body": "for 문"}]


def test_goal_falls_back_to_project_name_then_title():
    # description 없음 → project_name
    b = bw.bundle_from_session({"session_id": "s", "design_doc": {"project_name": "P", "pages": [{}]}})
    assert b["goal"] == "P"
    # design_doc 없고 코드만 → title 로 폴백
    b2 = bw.bundle_from_session({"session_id": "s", "title": "T", "generated_code": {"a.js": "x"}})
    assert b2["goal"] == "T"
    assert b2["design_doc"] is None


def test_empty_session_returns_none():
    assert bw.bundle_from_session({"session_id": "s"}) is None
    assert bw.bundle_from_session(
        {"session_id": "s", "generated_code": {}, "design_doc": None, "learning_notes": []}) is None


def test_iter_sessions_reads_user_folders(tmp_path):
    (tmp_path / "u1").mkdir()
    (tmp_path / "u1" / "s1.json").write_text(json.dumps({"session_id": "s1"}), encoding="utf-8")
    (tmp_path / "u2").mkdir()
    (tmp_path / "u2" / "s2.json").write_text(json.dumps({"session_id": "s2"}), encoding="utf-8")
    found = {d.get("session_id") for _, d in bw.iter_sessions(str(tmp_path))}
    assert found == {"s1", "s2"}
