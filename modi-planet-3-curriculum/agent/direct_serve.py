"""#84 직접서브(direct-serve) 티어 — 재사용 고신뢰 후보를 LLM 생성 없이 그대로 응답.

EDU-27 실측 결론: 코드 재사용의 진짜 비용 절감은 "LLM으로 편집"이 아니라
**"저장물 직접 서브(생성 LLM=0)"** 에서 나온다. 단, 직접서브는 요청이 저장물에 **새 델타를
안 더할 때만** 만족(실측 만족도 95~100). 조그만 델타(색 하나)라도 명시하면 만족도 20으로
붕괴하므로, 검색 유사도(주제)만으로는 직접서브를 켜면 안 된다 — "파란 하트 좋아요 버튼"은
저장물(빨간 하트)과 유사도가 높아 reuse 로 뜨지만 직접서브하면 실패한다.

해법 = 유사도 高(reuse 티어)일 때만 **값싼 만족도 검증 1회(haiku ~1k 입력토큰)** 를 태워
저장물을 그대로 서브해도 요청을 충족하는지 판정 → accept 면 직접서브, else 생성 경로로.
검증비용(~1k) << 생성 출력(1,200~2,400)이라 accept 시 순이득이다.

LLM 은 만족도 검증 1회만 호출한다(생성 0). 어떤 예외도 삼켜 chat 흐름을 깨지 않는다.
"""
from __future__ import annotations

import json
import os

from langfuse import get_client

from agent.usage import update_generation as _update_generation


def _env_flag(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if raw == "":
        return default
    return raw not in ("0", "false", "no")


# 킬스위치(기본 ON). 오판(델타 놓침) 시 즉시 비활성 — 저장물이 요청을 못 채워도 그대로 나가는 위험.
ENABLED = _env_flag("DIRECT_SERVE", True)

# EDU-27 직접서브 문서복원 킬스위치(기본 ON). OFF 면 현행대로 문서 없이 코드만 서브한다.
DOCS_ENABLED = _env_flag("DIRECT_SERVE_DOCS", True)

# accept 컷오프(보수적). 만족도가 이 값 이상이어야 저장물을 그대로 서브한다.
# 실측: 델타 없음 95~100 / 색·기능 델타 15~20 → 90 컷이면 델타는 걸러지고 동일요청만 통과.
try:
    MIN_SCORE = max(0, min(100, int((os.getenv("DIRECT_SERVE_MIN_SCORE") or "90").strip())))
except (ValueError, TypeError):
    MIN_SCORE = 90

# 검증 입력 토큰 상한(문자수 근사). 저장물 코드가 커도 만족도 판정엔 구조/기능만 보면 되므로
# 파일당·전체를 잘라 ~1k 입력토큰 목표를 지킨다(검증비용 << 생성비용 전제 유지).
_MAX_FILE_CHARS = 1500
_MAX_TOTAL_CHARS = 4000


_SYSTEM = """\
당신은 코딩 생성 서비스의 "재사용 만족도" 판정기입니다.
이미 저장된 결과물(코드)을 **그대로(수정 없이)** 사용자에게 내보냈을 때, 이번 요청을 충분히
충족하는지 0~100 으로 판정합니다.

핵심 원칙:
- 요청이 저장물에 **새로운 델타(추가·변경 요구)** 를 더하면 그대로 서브는 실패입니다.
  델타 예: 색상 지정("파란"), 기능 추가("커지는 애니메이션"), 데이터/문구/동작 변경.
- 요청이 저장물이 이미 하는 일과 **같거나 더 일반적**이면 그대로 서브가 성립합니다.
- 주제만 비슷하고 실제 요구가 다르면(예: 저장물=좋아요버튼, 요청=별점리뷰) 낮게 줍니다.

보수적으로 판정하세요: 조금이라도 새 델타가 명시되면 delta=true, 점수를 크게 낮춥니다.

JSON 객체 하나만 출력:
{"score": 0-100, "delta": true|false, "reason": "간단한 근거"}
"""


def _candidate_text(candidate: dict) -> str:
    """만족도 판정용 저장물 요약 — 제목 + 파일별 (잘린) 내용. 입력 토큰을 상한으로 억제."""
    payload = candidate.get("payload") or {}
    files = payload.get("files") or {}
    title = candidate.get("title") or payload.get("title") or "(제목 없음)"
    parts = [f"[저장물 제목] {title}"]
    total = 0
    for fn, code in files.items():
        if not isinstance(code, str):
            continue
        snippet = code[:_MAX_FILE_CHARS]
        if total + len(snippet) > _MAX_TOTAL_CHARS:
            snippet = snippet[: max(0, _MAX_TOTAL_CHARS - total)]
        parts.append(f"### {fn}\n{snippet}")
        total += len(snippet)
        if total >= _MAX_TOTAL_CHARS:
            parts.append("...(이하 생략)")
            break
    return "\n\n".join(parts)


def check_satisfaction(client, model: str, user_request: str, candidate: dict) -> dict:
    """저장물을 그대로 서브해도 요청을 충족하는지 값싼 haiku 판정 1회.

    반환: {"score": int, "delta": bool, "accept": bool, "reason": str, "ok": bool}.
    실패(파싱·API 예외) 시 ok=False + accept=False → 호출측은 생성 경로로 폴백(안전).
    """
    payload = {
        "user_request": (user_request or "").strip(),
        "stored_artifact": _candidate_text(candidate),
    }
    try:
        with get_client().start_as_current_observation(
                name="재사용 만족도 검증 (direct_serve)", as_type="generation",
                input={"user_request": payload["user_request"]}) as _gen:
            response = client.messages.create(
                model=model,
                max_tokens=256,
                system=_SYSTEM,
                messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            )
            text = _first_text(response)
            data = _parse_json(text)
            score = _clamp_score(data.get("score"))
            delta = bool(data.get("delta"))
            # 델타가 있으면(모델이 명시) accept 하지 않는다 — 점수만으로는 놓칠 수 있어 이중 가드.
            accept = (score >= MIN_SCORE) and not delta
            result = {"score": score, "delta": delta, "accept": accept,
                      "reason": str(data.get("reason") or "")[:200], "ok": True}
            _update_generation(_gen, model, response=response,
                               output={"score": score, "delta": delta, "accept": accept},
                               step="direct_serve_satisfaction")
            return result
    except Exception as exc:
        try:
            get_client().create_event(
                name="재사용 만족도 검증 실패 (direct_serve)",
                metadata={"error": str(exc)[:300]}, level="WARNING")
        except Exception:
            pass
        return {"score": 0, "delta": False, "accept": False, "reason": "", "ok": False}


def _clamp_score(value) -> int:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (ValueError, TypeError):
        return 0


def _first_text(response) -> str:
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "text":
            return getattr(block, "text", "") or ""
        if isinstance(block, dict) and block.get("type") == "text":
            return block.get("text", "") or ""
    return ""


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("응답에서 JSON 객체를 찾지 못했습니다")
    return json.loads(text[start:end + 1])


# --- EDU-27 직접서브 문서복원 (writeback 동봉 + 세션 조인 폴백) --------------------
#
# 직접서브는 재사용 후보의 payload.files 만 state.generated_code_map 에 채우고 생성
# 루프(_agent_loop_stream) 를 통째로 건너뛴다. 그런데 학습노트/설계문서는 그 루프 안에서
# LLM 이 add_learning_note/update_design_doc 도구를 호출해야만 만들어져, 직접서브 턴은
# 미리보기·코드는 정상인데 문서 탭이 비는 공백이 있었다(실유저 트레이스로 확인).
#
# 해법 두 단계(우선순위 순):
#   1) writeback 동봉: 코드를 등록할 때 같은 세션의 학습노트/설계문서를 payload["docs"]
#      에 실어 저장한다(registry_lib.register_result → chunk_fields.docs_payload).
#      직접서브가 이 후보를 서브할 때 재조회 없이 즉시 복원된다.
#   2) 세션 조인 폴백: docs 동봉 이전에 등록된(기존) 자산은 payload.docs 가 없다.
#      후보의 session_id 로 같은 세션의 learning_note/design_doc 청크를 조회해 복원한다
#      (fetch_session_docs). 조회 실패/부재 시 조용히 문서 없음(현행과 동일).


def restore_docs(state, candidate: dict) -> int:
    """직접서브 candidate 로부터 학습노트/설계문서를 state 에 복원.

    반환: 복원된 항목 수(노트 개수 + 설계문서 있으면 1, 관측용 docs_restored).
    DOCS_ENABLED=False 면 즉시 0(현행 동작). 어떤 예외도 삼킨다 — 문서는 부가물이라
    실패해도 직접서브(코드 서브) 자체는 절대 깨면 안 된다.
    """
    if not DOCS_ENABLED:
        return 0
    try:
        payload = (candidate or {}).get("payload") or {}
        docs = payload.get("docs")
        if not docs:
            session_id = (candidate or {}).get("session_id")
            docs = fetch_session_docs(session_id) if session_id else None
        if not docs:
            return 0
        return _apply_docs(state, docs)
    except Exception:
        return 0


def _apply_docs(state, docs: dict) -> int:
    """docs({"learning_notes":[...], "design_doc":{...}|None}) 를 state 필드에 반영.

    state.learning_notes(List[Dict]) 에 append, state.project.design_doc 을 교체한다.
    직접서브는 항상 코드 없는(콜드) 세션에서만 발동하므로(_maybe_direct_serve 가드),
    design_doc 은 보통 비어 있어 그대로 교체해도 기존 내용을 잃지 않는다.
    """
    restored = 0
    notes = docs.get("learning_notes") or []
    if notes:
        state.learning_notes.extend(notes)
        restored += len(notes)
    dd = docs.get("design_doc")
    if dd:
        from agent.models import DesignDoc

        state.project.design_doc = DesignDoc(**dd)
        restored += 1
    return restored


def fetch_session_docs(session_id: str) -> dict | None:
    """세션 조인 폴백 — 후보의 session_id 로 같은 세션의 학습노트/설계문서 청크를 조회.

    반환: {"learning_notes": [...], "design_doc": {...}|None} 또는 None(부재/실패).
    조회 경로:
      - 프록시 모드(RAG_UPSTREAM): rag-search 의 /api/chunks?session_id=&kind=... (신규
        최소 엔드포인트, scripts/rag_demo_app.py). MySQL 우선 조회 + registry_lib 폴백.
      - 로컬/개발 모드: registry_lib 인프로세스 스토어를 직접 조회.
    TODO(EDU-27): mysql_redis 배포에서 Redis 색인 경로로 잡힌 과거(session_id 미기록) 후보는
    이 폴백이 못 미친다 — payload.docs 동봉(1순위)으로 신규 등록분은 이미 커버됨.
    """
    if not session_id:
        return None
    up = os.getenv("RAG_UPSTREAM", "").strip()
    if up:
        try:
            import httpx

            resp = httpx.get(f"{up}/api/chunks", params={
                "session_id": session_id, "kind": "learning_note,design_doc"}, timeout=5.0)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok"):
                    return _chunks_to_docs(data.get("chunks") or [])
        except Exception:
            pass
        return None
    try:
        import registry_lib

        chunks = registry_lib.get_by_session(session_id, ("learning_note", "design_doc"))
        return _chunks_to_docs(chunks)
    except Exception:
        return None


def _chunks_to_docs(chunks: list) -> dict | None:
    """/api/chunks 또는 registry_lib.get_by_session 결과 → {learning_notes, design_doc}."""
    notes: list[dict] = []
    design_doc: dict | None = None
    for c in chunks or []:
        ctype = c.get("chunk_type")
        payload = c.get("payload") or {}
        if ctype == "learning_note":
            notes.append({
                "title": payload.get("title") or c.get("title") or "",
                "what": payload.get("what", ""),
                "why": payload.get("why", ""),
                "where": payload.get("where", ""),
            })
        elif ctype == "design_doc" and design_doc is None:
            design_doc = {k: v for k, v in payload.items() if k != "kind"}
    if not notes and not design_doc:
        return None
    return {"learning_notes": notes, "design_doc": design_doc}
