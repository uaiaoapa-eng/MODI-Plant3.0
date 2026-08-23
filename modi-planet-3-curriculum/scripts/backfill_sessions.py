"""기존 파일 세션(projects/<uid>/*.json) → MySQL sessions 원천 일괄 적재(#27 P3).

파일 저장에서 MySQL 세션 스토어로 전환할 때, 이미 쌓인 대화/프로젝트를 원천에 채운다.
raw 컬럼에 파일 전문을 무손실로 넣어 파일과 동일 데이터가 되게 한다(이중쓰기 전환의 초기 적재).

실행 (두 방식 모두 지원 — 환경에 맞게 자동 분기):
  # ① 프록시(권장·배포): 메인 앱 컨테이너 — projects/ 접근 + RAG_UPSTREAM 로 rag-search(MySQL) 도달
  RAG_UPSTREAM=http://host.docker.internal:8100 PYTHONPATH=scripts python scripts/backfill_sessions.py
  # ② 직결: MySQL 이 직접 보이는 환경(rag-onprem 컨테이너 등)
  DATABASE_URL=mysql+pymysql://edu:edupw@mysql:3306/edu_agent PYTHONPATH=scripts python scripts/backfill_sessions.py
"""
from __future__ import annotations

import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECTS = os.path.join(BASE, "projects")


def _iter_sessions():
    """projects/<uid>/*.json → (session_row dict) 제너레이터. 손상 파일은 건너뛴다."""
    for uid in sorted(os.listdir(PROJECTS)):
        udir = os.path.join(PROJECTS, uid)
        if not os.path.isdir(udir):
            continue
        for fn in sorted(os.listdir(udir)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(udir, fn), encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:  # 손상 파일은 건너뛰고 계속
                print(f"  건너뜀 {uid}/{fn}: {e}")
                continue
            sid = data.get("session_id") or fn[:-5]
            yield {
                "session_id": sid, "user_id": data.get("user_id") or uid,
                "title": data.get("title"), "description": data.get("description"),
                "coding_type": data.get("coding_type"), "app_type": data.get("app_type"),
                "phase": data.get("phase"), "raw": data,
            }


def _via_proxy(upstream: str) -> int:
    """RAG_UPSTREAM 프록시 모드 — 각 세션을 rag-search /api/session/save 로 POST(MySQL 원천 적재)."""
    import httpx

    total = 0
    with httpx.Client(timeout=20.0) as c:
        for row in _iter_sessions():
            try:
                r = c.post(f"{upstream.rstrip('/')}/api/session/save", json=row)
                if r.status_code == 200 and r.json().get("ok"):
                    total += 1
                else:
                    print(f"  실패 {row['session_id']}: {r.status_code} {r.text[:120]}")
            except Exception as e:
                print(f"  실패 {row['session_id']}: {e}")
    return total


def _direct() -> int:
    """직결 모드 — store_mysql 로 MySQL 에 직접 upsert."""
    import store_mysql as M

    total = 0
    with M.connect() as conn:
        for row in _iter_sessions():
            M.upsert_session(conn, row)
            total += 1
    return total


def main() -> int:
    sys.path.insert(0, os.path.join(BASE, "scripts"))
    if not os.path.isdir(PROJECTS):
        print(f"projects 폴더 없음: {PROJECTS}")
        return 0
    upstream = os.getenv("RAG_UPSTREAM", "").strip()
    total = _via_proxy(upstream) if upstream else _direct()
    mode = "프록시" if upstream else "직결"
    print(f"[backfill_sessions] {mode} 모드 적재 {total}건")
    return total


if __name__ == "__main__":
    main()
