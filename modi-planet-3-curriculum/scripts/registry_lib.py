"""등록 스토어 — 질문/결과물을 실시간으로 저장·임베딩해 즉시 검색 가능하게(RAG 되먹임).

검색이 '등록 후보(콜드셀)'로 판정한 질문의 결과물(학습노트)을 여기에 append 하면,
그 즉시 임베딩되어 다음 검색부터 재사용(가져오기) 히트로 잡힌다.

저장(둘 다 append-only, index 정합):
  data/registered.jsonl     [{id,question,title,content,coding_type,concept_key,ts}]
  data/registered_emb.npy   (M,1024) float32  — 행별 임베딩(벡터 없으면 0벡터)

build_ontology.py 가 ontology.db 를 재생성해도 이 스토어는 건드리지 않으므로
등록물이 유실되지 않는다. search_lib 가 기본 인덱스와 합쳐 통합 검색한다.
"""
from __future__ import annotations

import json
import os
import threading

import numpy as np

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REG_JSONL = os.path.join(BASE, "data", "registered.jsonl")
REG_EMB = os.path.join(BASE, "data", "registered_emb.npy")
DIM = 1024

_lock = threading.Lock()
_meta: list[dict] | None = None
_emb: np.ndarray | None = None
_seen: set[tuple[str, str]] | None = None  # (session_id, title) 중복 등록 방지
VERSION = 0  # 등록 시 증가 → search_lib 캐시 무효화 신호

# 품질 게이트(#44): outcome=failed/blocked 또는 reusability < 임계 는 등록 거부(오류 전파 방지).
# 기본 임계 0.0 → 기존 동작 보존(명시 설정 시에만 강화).
QUALITY_MIN = float(os.getenv("REG_QUALITY_MIN", "0.0"))
_BAD_OUTCOMES = {"failed", "blocked"}

# 개념 시드/레벨 캐시(difficulty 파생용) — register_learning_notes 에서 지연 로드.
_concepts: list[dict] | None = None
_levels: dict[str, int] | None = None


def _concept_meta() -> tuple[list[dict], dict[str, int]]:
    global _concepts, _levels
    if _concepts is None:
        try:
            from ontology_lib import concept_levels, load_seed

            _concepts = load_seed()
            _levels = concept_levels(_concepts)
        except Exception:
            _concepts, _levels = [], {}
    return _concepts, _levels


def _note_text(n) -> tuple[str, str]:
    """learning_note(dict{title,what,why,where} 또는 str) → (title, body)."""
    if isinstance(n, dict):
        title = (n.get("title") or "").strip()
        body = " ".join(str(n.get(k, "")) for k in ("what", "why", "where")).strip()
        return title, body
    if isinstance(n, str):
        s = n.strip()
        return s[:80], s
    return "", ""


def _embed(text: str) -> np.ndarray:
    """행 임베딩 1건. torch/transformers 부재 시 0벡터(부분일치로만 검색)."""
    try:
        from embed_bge import embed_one

        return np.asarray(embed_one(text), dtype=np.float32).reshape(DIM)
    except Exception:
        return np.zeros(DIM, dtype=np.float32)


def _load() -> tuple[np.ndarray, list[dict]]:
    """스토어 로드(캐시). 행 수와 임베딩 수가 어긋나면 0벡터로 정합 복구."""
    global _meta, _emb, _seen
    if _meta is not None and _emb is not None:
        return _emb, _meta
    meta: list[dict] = []
    if os.path.exists(REG_JSONL):
        with open(REG_JSONL, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    meta.append(json.loads(line))
    if os.path.exists(REG_EMB) and len(meta):
        arr = np.load(REG_EMB).astype(np.float32)
    else:
        arr = np.zeros((0, DIM), dtype=np.float32)
    # 정합 복구(행 수 기준)
    if arr.shape[0] != len(meta):
        fixed = np.zeros((len(meta), DIM), dtype=np.float32)
        n = min(arr.shape[0], len(meta))
        fixed[:n] = arr[:n]
        arr = fixed
    _meta, _emb = meta, arr
    _seen = {(r.get("session_id") or "", r.get("title") or "") for r in meta if r.get("session_id")}
    return _emb, _meta


def assets() -> tuple[np.ndarray, list[dict]]:
    """(emb (M,1024), meta) — search_lib 가 기본 인덱스와 합침."""
    return _load()


def count() -> int:
    _, m = _load()
    return len(m)


def stats() -> dict:
    """등록 스토어 상태 — "저장되고 있는지"를 코드 밖에서 즉시 검증(#103).

    반환: {"count": int, "last_registered_at": str | None,
           "backend": "local" | "mysql_redis"}
    """
    _, meta = _load()
    last_ts = meta[-1].get("ts") if meta else None
    backend = "mysql_redis" if os.getenv("RAG_BACKEND") == "mysql_redis" else "local"
    return {"count": len(meta), "last_registered_at": last_ts, "backend": backend}


def register(question: str, title: str, content: str,
             coding_type: str | None = None, concept_key: str | None = None,
             user_id: str | None = None, session_id: str | None = None,
             ts: str | None = None, *,
             chunk_type: str = "learning_note",
             intent: str | None = None, domain: str | None = None,
             difficulty: str | None = None, modi_keys: list | None = None,
             payload: dict | None = None, outcome: str | None = None,
             reusability_score: float = 1.0) -> dict:
    """결과물 1건 등록 → 저장 + 임베딩 append → 즉시 검색 가능. 반환: 등록 행.

    user_id(uuid)/session_id 는 provenance — 검색에서 user_id 필터로 '내 것'만
    좁혀볼 수 있다(기본은 전체 통합 재사용).

    #44: intent/domain/difficulty/modi_keys/payload 를 함께 저장·색인해 base 청크와
    동형으로 만든다(검색 결과 클릭 시 chat 카드 동일 렌더). 품질 게이트(outcome/
    reusability)로 오류·저품질 결과물의 검색 노출을 차단한다.
    """
    global _meta, _emb, VERSION
    title = (title or "").strip()
    content = (content or "").strip()
    if not title and not content:
        return {"ok": False, "reason": "title 또는 content 가 필요합니다."}
    # 품질 게이트: 실패/차단 결과물이나 임계 미만 재사용성은 검색에 노출하지 않는다.
    if outcome and outcome.lower() in _BAD_OUTCOMES:
        return {"ok": False, "reason": f"품질 게이트: outcome={outcome}"}
    if reusability_score is not None and reusability_score < QUALITY_MIN:
        return {"ok": False, "reason": f"품질 게이트: reusability {reusability_score} < {QUALITY_MIN}"}

    with _lock:
        emb, meta = _load()
        row = {
            "id": len(meta),
            "question": (question or "").strip(),
            "title": title or content[:60],
            "content": content,
            "coding_type": coding_type or "any",
            "concept_key": concept_key,
            "chunk_type": chunk_type or "learning_note",
            "intent": intent,
            "domain": domain,
            "difficulty": difficulty,
            "modi_keys": modi_keys or [],
            "payload": payload,
            "outcome": outcome,
            "reusability_score": reusability_score,
            "user_id": (user_id or None),
            "session_id": (session_id or None),
            "ts": ts,  # 호출측에서 타임스탬프 주입
        }
        vec = _embed(f"{row['title']}. {content[:120]}").reshape(1, DIM)

        os.makedirs(os.path.dirname(REG_JSONL), exist_ok=True)
        with open(REG_JSONL, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        _emb = np.vstack([emb, vec]) if emb.shape[0] else vec
        np.save(REG_EMB, _emb)
        _meta = meta + [row]
        if _seen is not None and row["session_id"]:
            _seen.add((row["session_id"], row["title"]))
        VERSION += 1
        # 온프렘 백엔드면 Redis 벡터 인덱스 + MySQL 원천에도 즉시 반영(되먹임 폐루프).
        if os.getenv("RAG_BACKEND") == "mysql_redis" and vec.any():
            try:
                import vector_redis as V

                V.ensure_index()
                V.upsert(f"reg:{row['id']}", vec.reshape(DIM),
                         title=row["title"], content=content,
                         coding_type=row["coding_type"], source="registered",
                         concept_key=row["concept_key"] or "", user_id=row["user_id"] or "",
                         domain=row["domain"] or "", difficulty=row["difficulty"] or "",
                         intent=row["intent"] or "", payload=row["payload"],
                         session_id=row["session_id"] or "")
            except Exception as exc:  # Redis 문제로 등록 자체가 실패하면 안 됨
                print(f"[registry] Redis 색인 건너뜀: {exc}", flush=True)
            _mysql_register(row, vec.reshape(DIM))
        return {"ok": True, "id": row["id"], "count": len(_meta),
                "vector": bool(vec.any()), "row": row}


def _mysql_register(row: dict, vec) -> None:
    """등록물을 MySQL knowledge_chunks(source=registered) 원천에도 적재(best-effort).

    session_id FK 를 만족하도록 최소 세션 행을 먼저 upsert 한다. 실패해도 등록 자체는
    성공 처리(Redis/JSONL 로 검색은 이미 가능). backfill 재실행 시 base 만 지우므로 보존.
    """
    if not row.get("session_id"):
        return  # FK(session_id NOT NULL) 없이는 적재 불가 → 건너뜀
    try:
        import store_mysql as M

        with M.connect() as conn:
            M.upsert_session(conn, {
                "session_id": row["session_id"], "user_id": row.get("user_id") or "",
                "coding_type": row.get("coding_type"), "raw": {},
            })
            M.insert_chunk(conn, {
                "session_id": row["session_id"],
                "chunk_type": row.get("chunk_type", "learning_note"),
                "coding_type": row.get("coding_type"), "concept_key": row.get("concept_key"),
                "intent": row.get("intent"), "domain": row.get("domain"),
                "difficulty": row.get("difficulty"), "modi_keys": row.get("modi_keys"),
                "title": row["title"], "content": row.get("content", ""),
                # np.float32 스칼라는 JSON 직렬화 불가 → 네이티브 float 로 캐스팅
                # (기존 list(vec) 는 store_mysql json.dumps 에서 전건 실패했음).
                "payload": row.get("payload"),
                "embedding": vec.tolist() if hasattr(vec, "tolist") else [float(x) for x in vec],
                "source": "registered", "outcome": row.get("outcome"),
                "reusability_score": row.get("reusability_score", 1.0),
            })
    except Exception as exc:  # MySQL 문제로 등록 경로가 깨지면 안 됨
        print(f"[registry] MySQL registered 적재 건너뜀: {exc}", flush=True)


def register_learning_notes(session_id: str, user_id: str | None, coding_type: str | None,
                            notes: list, modi_keys: list | None = None) -> int:
    """세션의 learning_notes 중 아직 등록 안 된 것만 자동 등록(중복 제거). 반환: 추가 건수.

    server.py 세션 저장(auto_save) 훅에서 호출 — 결과물이 나오면 즉시 검색 인덱스에 반영.

    #44: base 청크와 동형이 되도록 부가 필드를 결정적으로 파생해 함께 등록한다
    (concept 매칭 → level → difficulty, coding_type → domain, note → payload).
    modi_keys 는 세션 하드웨어 연계(호출측 state.modi_modules 에서 전달).
    빈/공백 노트는 품질 게이트로 건너뛴다(오류·잡음 전파 방지).
    """
    if not notes:
        return 0
    import chunk_fields

    _load()  # _seen 초기화 보장
    concepts, levels = _concept_meta()
    domain = chunk_fields.derive_domain(coding_type)
    intent = chunk_fields.derive_intent("learning_note")
    added = 0
    for n in notes:
        title, body = _note_text(n)
        # 품질 게이트: 제목 없거나 본문이 사실상 비면 등록 안 함.
        if not title or len((body or "").strip()) < 4:
            continue
        key = (session_id or "", title)
        if _seen is not None and key in _seen:
            continue
        ck = None
        if concepts:
            from ontology_lib import match_concepts
            hit = match_concepts(f"{title} {body}", concepts, top=1)
            ck = hit[0][0] if hit else None
        difficulty = chunk_fields.derive_difficulty(levels.get(ck) if ck else None)
        payload = chunk_fields.note_payload(n, coding_type=coding_type, concept_key=ck)
        r = register(question="", title=title, content=body, coding_type=coding_type,
                     concept_key=ck, user_id=user_id, session_id=session_id,
                     intent=intent, domain=domain, difficulty=difficulty,
                     modi_keys=modi_keys, payload=payload, outcome="success")
        if r.get("ok"):
            added += 1
    return added


def register_result(session_id: str, user_id: str | None, coding_type: str | None, *,
                    design_doc: dict | None = None, code_map: dict | None = None,
                    modi_keys: list | None = None, goal: str | None = None,
                    learning_notes: list | None = None) -> int:
    """세션의 design_doc·생성 코드도 registered 청크로 등록(#44 write-back 확장). 반환: 추가 건수.

    learning_note 와 동형(payload+필드)으로 저장해 검색 클릭 시 chat 카드와 동일 렌더된다.
    세션당 안정적 title 로 _seen 중복 제거 → 매 턴 auto_save 에서 호출돼도 1회만 등록.
    품질 게이트: 실질 내용이 있는 것만(design_doc 은 features/pages, code 는 파일 존재).

    goal(요청 목표: design_doc.description·세션 제목 등)을 question/content 에 실어
    **유사한 미래 요청으로 검색·재사용**되게 한다(코드가 파일명만으로는 안 잡히는 문제 해결).

    learning_notes(EDU-27 직접서브 문서복원): 있으면 code payload 에 동봉(chunk_fields.
    docs_payload, 상한 적용)해 직접서브(#84)가 재조회 없이 학습노트·설계문서를 복원하게 한다.
    실시간(server._rag_feedback)·백필(backfill_writeback.py → /api/writeback) 양쪽 모두
    이 함수를 거치므로 여기 한 곳에만 동봉 로직을 둔다.
    """
    import chunk_fields

    _load()  # _seen 보장
    concepts, levels = _concept_meta()
    added = 0
    domain = chunk_fields.derive_domain(coding_type)
    q = (goal or "").strip()

    def _once(title: str, content: str, chunk_type: str, payload: dict, intent: str) -> None:
        nonlocal added
        if not title or (_seen is not None and (session_id or "", title) in _seen):
            return
        # base·learning_note 청크와 동형으로 개념을 파생해 붙인다. 검색 점수는 개념 centroid
        # 신호(W_CONCEPT=0.45, 최대 가중)에 크게 의존하는데, concept_key 가 없으면 이 항이 0이
        # 되어 코드·설계 청크가 완벽 매칭에도 임계(TAU) 아래로 눌려 재사용이 발동하지 않았다.
        ck = None
        if concepts:
            from ontology_lib import match_concepts
            hit = match_concepts(f"{q} {title} {content}", concepts, top=1)
            ck = hit[0][0] if hit else None
        difficulty = chunk_fields.derive_difficulty(levels.get(ck) if ck else None)
        r = register(question=q, title=title, content=content, chunk_type=chunk_type,
                     coding_type=coding_type, concept_key=ck, user_id=user_id,
                     session_id=session_id, intent=intent, domain=domain,
                     difficulty=difficulty, modi_keys=modi_keys,
                     payload=payload, outcome="success")
        if r.get("ok"):
            added += 1

    if design_doc and (design_doc.get("features") or design_doc.get("pages")):
        title = f"설계: {design_doc.get('project_name') or '프로젝트'}"
        # 검색 임베딩 대상 content 에 설명 + 기능/화면 이름을 포함(검색성↑).
        parts = [design_doc.get("description") or ""]
        parts += [f.get("name", "") for f in (design_doc.get("features") or [])]
        parts += [p.get("name", "") for p in (design_doc.get("pages") or [])]
        content = " ".join(x for x in parts if x).strip() or title
        _once(title, content, "design_doc", chunk_fields.design_doc_payload(design_doc),
              chunk_fields.derive_intent("design_doc"))

    if code_map:
        files = list(code_map.keys())
        title = ("코드: " + (q[:40] if q else ", ".join(files[:2]))
                 + (" 외" if not q and len(files) > 2 else ""))
        # content 는 요청 목표(q) 를 앞세워 유사 요청으로 검색되게 한다(파일명만이면 안 잡힘).
        # EDU-27 §13: 목표+파일명만으로는 노트·설계 대비 검색성이 낮아 code 후보가
        # top-k 에 못 들었다 → 설계 설명·기능명까지 포함해 자연어 질의 매칭을 올린다.
        desc_parts: list[str] = []
        if design_doc:
            desc_parts.append(design_doc.get("description") or "")
            desc_parts += [f.get("name", "") for f in (design_doc.get("features") or [])]
        content = " ".join(x for x in ([q] + desc_parts + files) if x).strip() or title
        docs = chunk_fields.docs_payload(learning_notes, design_doc)
        _once(title, content, "code", chunk_fields.code_payload(code_map, docs=docs),
              chunk_fields.derive_intent("code"))

    return added


def get_by_session(session_id: str, chunk_types: "list[str] | tuple[str, ...] | None" = None) -> list:
    """세션의 등록 청크 조회(EDU-27 직접서브 문서복원 — 세션 조인 폴백, 인프로세스 모드).

    payload.docs 가 없는(신규 동봉 이전에 등록된) 후보를 직접서브할 때, 후보의
    session_id 로 같은 세션의 학습노트·설계문서 청크를 찾아 문서를 복원하는 데 쓴다.
    반환: [{chunk_type, title, content, payload}] — store_mysql.get_chunks_by_session 과 동형.
    """
    if not session_id:
        return []
    _, meta = _load()
    types = set(chunk_types) if chunk_types else None
    out = []
    for r in meta:
        if r.get("session_id") != session_id:
            continue
        ct = r.get("chunk_type") or "learning_note"
        if types and ct not in types:
            continue
        out.append({"chunk_type": ct, "title": r.get("title"),
                    "content": r.get("content"), "payload": r.get("payload")})
    return out


def reset() -> None:
    """테스트/초기화용 — 스토어 비우기."""
    global _meta, _emb, _seen, VERSION
    for p in (REG_JSONL, REG_EMB):
        if os.path.exists(p):
            os.remove(p)
    _meta, _emb, _seen, VERSION = None, None, None, VERSION + 1
