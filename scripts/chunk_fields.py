"""청크 부가 필드의 결정적 파생(LLM 無) — build_ontology / registry_lib 공유.

이슈 #44: payload/domain/difficulty/intent/modi_keys 를 빌드(base 청크)와
되먹임 등록(registered 청크) 양쪽에서 **동일 규칙**으로 채워
"검색 클릭 = chat 카드" 동일 전달을 성립시킨다.

모든 규칙은 시드(ontology_seed)·세션에 이미 있는 값에서 파생한다(새 분류 LLM 호출 없음).
build_ontology 의 alias 부분일치 방침과 동일하게 결정적이라 재현·회귀 검증이 쉽다.
"""
from __future__ import annotations

# 산출물 종류(chunk_type) → 의도(intent). "설명→learning_note, 만들어→code/design_doc"
# (router.py 의 라벨 체계와 정렬: explain/implement/design). 기본은 설명.
_CHUNK_INTENT = {
    "learning_note": "explain_concept",
    "design_doc": "design_review",
    "diagram": "design_review",
    "code": "implement_request",
}

# 코딩축(coding_type) → 커리큘럼 domain. 개념별 domain 시드가 없으므로 코딩축을 domain 으로
# 쓴다(하드웨어 연계는 modi_keys 로 별도 표현). 향후 개념별 domain 큐레이션 시 이 함수만 교체.
_DOMAIN_BY_CT = {"blockly": "blockly", "react": "react", "hybrid": "hybrid"}


def derive_intent(chunk_type: str | None) -> str:
    return _CHUNK_INTENT.get((chunk_type or "").strip(), "explain_concept")


def derive_difficulty(level) -> str | None:
    """개념 선수학습 깊이(level) → easy/medium/hard 밴드(페르소나 난이도 필터용).

    GRADE_LEVEL_CAP(ontology_lib) 와 정렬: 낮은 학년일수록 낮은 밴드만 허용.
    concept_key 미배정(level None) 이면 None.
    """
    if level is None:
        return None
    try:
        lv = int(level)
    except (TypeError, ValueError):
        return None
    if lv <= 1:
        return "easy"
    if lv <= 3:
        return "medium"
    return "hard"


def derive_domain(coding_type: str | None) -> str:
    return _DOMAIN_BY_CT.get((coding_type or "").strip().lower(), "general")


def note_payload(note, coding_type: str | None = None,
                 concept_key: str | None = None) -> dict:
    """learning_note(dict{title,what,why,where} 또는 str) → 구조화 렌더 원본(payload).

    검색 결과 클릭 시 chat 이 보여준 학습노트 카드를 동일 구조로 재구성하는 전제.
    """
    if isinstance(note, dict):
        payload = {
            "kind": "learning_note",
            "title": (note.get("title") or "").strip(),
            "what": note.get("what") or "",
            "why": note.get("why") or "",
            "where": note.get("where") or "",
        }
    else:
        s = str(note or "").strip()
        payload = {"kind": "learning_note", "title": s[:80], "what": s,
                   "why": "", "where": ""}
    if coding_type:
        payload["coding_type"] = coding_type
    if concept_key:
        payload["concept_key"] = concept_key
    return payload


def design_doc_payload(doc: dict) -> dict:
    """design_doc(model_dump) → 검색 클릭 시 설계 카드 동일 렌더용 payload."""
    d = dict(doc or {})
    d["kind"] = "design_doc"
    return d


# EDU-27 직접서브 문서복원(#84 후속): writeback 시 code payload 에 세션 문서를 동봉할 때의
# 상한 — 노트 개수/본문 길이를 제한해 임베딩·저장 인플레(검색 청크가 지나치게 커지는 것)를
# 막는다. 값은 모듈 상수로 두어 필요 시 한 곳에서만 조정한다.
MAX_DOCS_NOTES = 6
MAX_NOTE_CHARS = 2000


def docs_payload(learning_notes: list | None = None, design_doc: dict | None = None) -> dict | None:
    """세션의 학습노트/설계문서 → code payload 에 동봉할 완결 구조(직접서브 복원용).

    직접서브(#84)가 재사용 코드 후보를 그대로 서브할 때, 같은 세션의 문서(학습노트·설계
    문서)를 재조회 없이 즉시 복원할 수 있도록 code 청크의 payload 에 실어 나른다.
    learning_notes 는 MAX_DOCS_NOTES 개까지, 각 본문(what/why/where) 은 MAX_NOTE_CHARS 자로
    잘라 저장물이 과도하게 커지는 것을 막는다(검색 임베딩 대상은 title/content 뿐이라
    payload 크기는 점수를 오염시키지 않는다 — files 필드와 동일 취급).
    둘 다 없으면 None(기존 code payload 와 동일하게 동작 — 회귀 없음).
    """
    notes: list[dict] = []
    for n in (learning_notes or [])[:MAX_DOCS_NOTES]:
        if isinstance(n, dict):
            notes.append({
                "title": (n.get("title") or "")[:200],
                "what": (n.get("what") or "")[:MAX_NOTE_CHARS],
                "why": (n.get("why") or "")[:MAX_NOTE_CHARS],
                "where": (n.get("where") or "")[:MAX_NOTE_CHARS],
            })
        else:
            s = str(n or "")[:MAX_NOTE_CHARS]
            notes.append({"title": s[:80], "what": s, "why": "", "where": ""})
    dd = dict(design_doc) if design_doc else None
    if not notes and not dd:
        return None
    return {"learning_notes": notes, "design_doc": dd}


def code_payload(code_map: dict, docs: dict | None = None) -> dict:
    """생성 코드 맵({파일명: 코드}) → 코드 카드 동일 렌더용 payload.

    docs(EDU-27 직접서브 문서복원, docs_payload() 산출물)가 있으면 payload["docs"] 에
    동봉한다 — 직접서브가 검색·조회 없이 즉시 학습노트/설계문서를 복원할 수 있게 한다.
    """
    payload = {"kind": "code", "files": dict(code_map or {})}
    if docs:
        payload["docs"] = docs
    return payload


def modi_module_keys(mm) -> list[str]:
    """세션 modi_modules({modules:[{key,...}]} 또는 [{key,...}]) → 정렬된 고유 module key 리스트."""
    if isinstance(mm, dict):
        mods = mm.get("modules") or []
    elif isinstance(mm, list):
        mods = mm
    else:
        return []
    keys = {m.get("key") for m in mods if isinstance(m, dict) and m.get("key")}
    return sorted(k for k in keys if k)
