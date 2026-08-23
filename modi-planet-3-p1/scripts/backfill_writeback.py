#!/usr/bin/env python3
"""기존 세션 결과(코드/설계/노트)를 rag-search 검색 창고에 일괄 적재(#57).

배경
    #58 로 새 chat 빌드는 실시간으로 rag-search(/api/writeback)에 등록된다. 이
    스크립트는 그 통로를 **재사용**해, 서비스 초반 콜드스타트를 없애려고 **이미 쌓인
    옛 세션**(projects/<user_id>/*.json)을 한 번 밀어넣는다.

왜 이 방식인가 (torch 재빌드/자산 커밋 대비 장점)
    - 등록·임베딩은 torch 를 가진 rag-search 가 담당 → 이 스크립트엔 torch 불필요.
    - 버전관리된 canonical 자산(chunk_emb.npy 등)을 안 건드림 → gold(커밋 자산 기준)
      회귀 함정 자체가 없음.
    - register_learning_notes/register_result 가 (session_id, title) 로 중복 제거 →
      **멱등**. 여러 번 돌려도 중복 등록 안 됨.

사용
    RAG_UPSTREAM=http://localhost:8100 python scripts/backfill_writeback.py
    python scripts/backfill_writeback.py --url http://localhost:8100 --projects projects
    python scripts/backfill_writeback.py --projects projects --dry-run   # 무엇이 보내질지만 출력
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

import httpx


def _modi_keys(modi_modules):
    """세션 MODI 모듈 → 등록용 key 목록. server._rag_feedback 와 동일 파생."""
    try:
        import chunk_fields
        return chunk_fields.modi_module_keys(modi_modules)
    except Exception:  # chunk_fields 부재 시에도 적재는 진행(하드웨어 연계만 비움).
        return None


def bundle_from_session(data: dict) -> dict | None:
    """세션 JSON → /api/writeback 묶음. 실질 내용(코드/설계/노트)이 없으면 None.

    server._rag_feedback 가 orch.state 에서 만드는 묶음과 같은 필드·goal 우선순위를 쓴다.
    """
    code_map = data.get("generated_code") or None
    design_doc = data.get("design_doc") or None
    notes = data.get("learning_notes") or []
    if not (code_map or design_doc or notes):
        return None
    goal = ((design_doc or {}).get("description")
            or (design_doc or {}).get("project_name")
            or data.get("title") or "")
    return {
        "session_id": data.get("session_id") or "",
        "user_id": data.get("user_id") or None,
        "coding_type": data.get("coding_type"),
        "learning_notes": notes,
        "design_doc": design_doc,
        "code_map": code_map,
        "modi_keys": _modi_keys(data.get("modi_modules")),
        "goal": goal,
    }


def iter_sessions(projects_dir: str):
    """projects/<user_id>/<session>.json 을 순회하며 (경로, dict) 를 yield."""
    for path in sorted(glob.glob(os.path.join(projects_dir, "*", "*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                yield path, json.load(f)
        except Exception as e:
            print(f"[skip] {path}: {e}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description="기존 세션을 rag-search /api/writeback 으로 일괄 적재")
    ap.add_argument("--url", default=os.getenv("RAG_UPSTREAM", "").rstrip("/"),
                    help="rag-search 베이스 URL (기본: 환경변수 RAG_UPSTREAM)")
    ap.add_argument("--projects", default="projects", help="세션 폴더 루트 (기본: projects)")
    ap.add_argument("--dry-run", action="store_true", help="전송 없이 보낼 묶음 요약만 출력")
    ap.add_argument("--timeout", type=float, default=30.0, help="요청 타임아웃(초)")
    args = ap.parse_args()
    if not args.url and not args.dry_run:
        ap.error("--url 또는 환경변수 RAG_UPSTREAM 필요 (예: http://localhost:8100)")

    sessions = notes_total = result_total = errors = skipped = 0

    def _run(post) -> None:
        nonlocal sessions, notes_total, result_total, errors, skipped
        for path, data in iter_sessions(args.projects):
            bundle = bundle_from_session(data)
            if bundle is None:
                skipped += 1
                continue
            sessions += 1
            if args.dry_run:
                print(f"[dry] {path} sid={bundle['session_id']} "
                      f"notes={len(bundle['learning_notes'])} "
                      f"code={bool(bundle['code_map'])} design={bool(bundle['design_doc'])}")
                continue
            try:
                res = post(bundle)
                notes_total += res.get("notes_added", 0)
                result_total += res.get("result_added", 0)
            except Exception as e:
                errors += 1
                print(f"[err] {path}: {e}", file=sys.stderr)

    if args.dry_run:
        _run(lambda b: {})
    else:
        with httpx.Client(timeout=args.timeout) as c:
            def _post(bundle):
                r = c.post(f"{args.url}/api/writeback", json=bundle)
                r.raise_for_status()
                return r.json()
            _run(_post)

    print(f"\n완료: 세션 {sessions} 처리, 내용없음 {skipped} 스킵, "
          f"노트 +{notes_total}, 결과(설계/코드) +{result_total}, 오류 {errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
