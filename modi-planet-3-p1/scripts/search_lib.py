"""하이브리드 검색 + %게이팅(등록 vs 가져오기) 엔진.

벡터(BGE-m3 코사인) + 부분일치(어휘 overlap)를 결합해 학습노트 청크를 랭킹하고,
유사도 임계값으로 각 질문을 세 갈래로 분류한다.

  top1 점수 ≥ τ_reuse            → reuse    (가져오기: 기존 노트 재사용, 캐시 히트)
  τ_near ≤ top1 < τ_reuse        → review   (근접: 검수 후 재사용)
  top1 < τ_near                  → register (콜드셀: 재사용 불가 → 콘텐츠 팩토리 신규 생성)

임계값은 환경변수(TAU_REUSE / TAU_NEAR)로 조정하며, 질문 묶음의 재사용/근접/등록
비율(%)이 "일부는 등록, 일부는 가져오기"의 목표 히트율이다.

벡터 레이어(torch+transformers)가 없으면 자동으로 부분일치 단독으로 폴백해
경량 서버에서도 설치·동작한다(vector_enabled=False).
"""

from __future__ import annotations

import json
import os
import re

import numpy as np

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EMB_PATH = os.path.join(BASE, "data", "chunk_emb.npy")
META_PATH = os.path.join(BASE, "data", "chunk_meta.json")

# 결정 임계값 (0~1 결합 점수 기준). 목표 히트율은 이 값으로 캘리브레이션.
TAU_REUSE = float(os.getenv("TAU_REUSE", "0.62"))
TAU_NEAR = float(os.getenv("TAU_NEAR", "0.48"))
# 하이브리드 가중치 (벡터 : 어휘 : 개념centroid). 합=1 권장.
# centroid(개념 임베딩 평균)는 패러프레이즈에 강함(gold 6/8 > 청크 top1) → 비중 크게.
W_VEC = float(os.getenv("W_VEC", "0.4"))
W_LEX = float(os.getenv("W_LEX", "0.15"))
W_CONCEPT = float(os.getenv("W_CONCEPT", "0.45"))
# 개념 centroid 에 더할 별칭(alias) 매칭 보너스 가중치. 시드 alias가 질문에 나타나면
# 그 개념 점수를 올려 centroid 의미매칭이 놓치는 판별어(장애물/깜깜 등)를 잡는다.
W_CLEX = float(os.getenv("W_CLEX", "0.2"))

# 검색 백엔드: local(SQLite+npy, 기본) | mysql_redis(온프렘 MySQL 원천 + Redis Stack 벡터)
RAG_BACKEND = os.getenv("RAG_BACKEND", "local")
# Redis 경로 재랭킹 후보 배수: KNN 으로 top*FANOUT 을 받아 centroid+alias 로 재정렬.
# 순수 KNN top1 이 놓치는 판별 개념을 후보 안에서 되살리려면 넉넉해야 함(로컬은 전수 재랭킹).
REDIS_FANOUT = int(os.getenv("REDIS_FANOUT", "10"))

_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]+")
_STOP = {"하고", "싶어요", "싶은데", "하는", "하게", "되게", "그리고", "에서", "으로", "해줘",
         "이거", "저거", "만들", "만들고", "하려면", "하면", "위해", "해서"}

# 지연 로드 상태
_emb: np.ndarray | None = None
_meta: list[dict] | None = None
_vector_error: str | None = None

# 통합 인덱스(기본 + 등록 스토어) 캐시
_combo_emb: np.ndarray | None = None
_combo_meta: list[dict] | None = None
_combo_ver: int = -1

# 개념 centroid 캐시 (concept_key → 청크 임베딩 평균, 정규화)
_cent_keys: list[str] | None = None
_cent_mat: np.ndarray | None = None
_cent_ver: int = -1
_labels: dict[str, str] | None = None

# Redis 백엔드 centroid 캐시 (backfill 로 적재된 개념 centroid, 프로세스 내 1회 로드).
# backfill 재실행 시 앱 재기동 전까지 캐시가 남는다(운영은 backfill 후 롤링 재배포).
_redis_cent_keys: list[str] | None = None
_redis_cent_mat: np.ndarray | None = None


def _label_of(key: str | None) -> str | None:
    global _labels
    if not key:
        return None
    if _labels is None:
        try:
            from ontology_lib import load_seed

            _labels = {c["key"]: c["label"] for c in load_seed()}
        except Exception:
            _labels = {}
    return _labels.get(key, key)


_aliases: dict[str, list[str]] | None = None


def _concept_aliases() -> dict[str, list[str]]:
    global _aliases
    if _aliases is None:
        try:
            from ontology_lib import load_seed

            _aliases = {c["key"]: [a.lower() for a in c.get("aliases", [])]
                        for c in load_seed()}
        except Exception:
            _aliases = {}
    return _aliases


def _alias_bonus(query_lower: str, key: str) -> float:
    """질문에 개념 alias 가 몇 개 나타나는지 → [0,1] 보너스(최대 3개까지 반영)."""
    hits = sum(1 for a in _concept_aliases().get(key, []) if a and a in query_lower)
    return min(hits, 3) / 3.0


def _centroids(emb: np.ndarray, meta: list[dict]) -> tuple[list[str], np.ndarray]:
    """개념별 청크 임베딩 평균(정규화) → centroid 행렬. 통합 VERSION 기준 캐시."""
    global _cent_keys, _cent_mat, _cent_ver
    if _cent_keys is not None and _cent_ver == _combo_ver:
        return _cent_keys, _cent_mat
    idx_by_key: dict[str, list[int]] = {}
    for j, r in enumerate(meta):
        ck = r.get("concept_key")
        # 개념 centroid 는 개념을 '설명'하는 학습노트로만 구성 — code/design 재사용 청크가
        # 섞이면 centroid 가 목표/파일명 쪽으로 드리프트해 개념 매칭(gold)이 틀어진다(#3 회귀 수정).
        if ck and r.get("chunk_type", "learning_note") == "learning_note":
            idx_by_key.setdefault(ck, []).append(j)
    keys = list(idx_by_key)
    if not keys:
        _cent_keys, _cent_mat, _cent_ver = [], np.zeros((0, emb.shape[1]), np.float32), _combo_ver
        return _cent_keys, _cent_mat
    cent = np.vstack([emb[idx_by_key[k]].mean(0) for k in keys]).astype(np.float32)
    norm = np.linalg.norm(cent, axis=1, keepdims=True)
    cent = np.divide(cent, norm, out=np.zeros_like(cent), where=norm > 0)
    _cent_keys, _cent_mat, _cent_ver = keys, cent, _combo_ver
    return keys, cent


def _load_meta_from_db() -> list[dict]:
    """임베딩 메타 캐시가 없을 때(경량 배포) DB에서 청크 메타 직접 로드 → 부분일치 검색용.

    RAG_BACKEND=mysql_redis 면 MySQL 원천(knowledge_chunks), 아니면 sqlite(chunks).
    graph_conn/_rows 로 백엔드를 통일한다.
    """
    from ontology_lib import _rows, graph_conn

    try:
        conn = graph_conn()
    except Exception:
        return []
    table = "knowledge_chunks" if RAG_BACKEND == "mysql_redis" else "chunks"
    try:
        rows = _rows(
            conn,
            f"SELECT chunk_id,title,content,concept_key,coding_type,"
            f"intent,domain,difficulty,modi_keys,payload FROM {table}"
            " WHERE concept_key IS NOT NULL",
            (),
        )
    except Exception:  # 온톨로지 DB 미빌드 등 → 등록 스토어만으로 동작
        return []
    finally:
        conn.close()
    # JSON 컬럼(modi_keys/payload)은 문자열로 저장됨 → 객체로 복원(#44)
    for r in rows:
        for k in ("modi_keys", "payload"):
            if isinstance(r.get(k), str) and r[k]:
                try:
                    r[k] = json.loads(r[k])
                except json.JSONDecodeError:
                    pass
    return rows


def _load_assets() -> tuple[np.ndarray | None, list[dict]]:
    """청크 임베딩 + 메타 로드. 임베딩 부재 시 (None, meta), 메타 캐시 부재 시 DB 폴백."""
    global _emb, _meta
    if _meta is not None:
        return _emb, _meta
    meta_from_json = os.path.exists(META_PATH)
    if meta_from_json:
        with open(META_PATH, encoding="utf-8") as f:
            _meta = json.load(f)
    else:
        _meta = _load_meta_from_db()  # 경량 배포: 임베딩 없이 DB 메타만
    # 임베딩(npy)은 json 메타와 index 정합이 보장될 때만 사용(정합 깨짐 방지)
    if meta_from_json and os.path.exists(EMB_PATH):
        arr = np.load(EMB_PATH).astype(np.float32)
        # NaN/inf 방어 + 재정규화(0벡터는 그대로 0 유지)
        arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
        norm = np.linalg.norm(arr, axis=1, keepdims=True)
        _emb = np.divide(arr, norm, out=np.zeros_like(arr), where=norm > 0)
    else:
        _emb = None
    return _emb, _meta


def _combined() -> tuple[np.ndarray | None, list[dict]]:
    """기본 인덱스 + 등록 스토어를 합친 통합 인덱스(등록 VERSION 기준 캐시).

    각 행에 source('base'|'registered') 태그를 붙인다. 경량(벡터 off) 배포에선
    통합 임베딩을 None 으로 두고 부분일치만 사용한다.
    """
    global _combo_emb, _combo_meta, _combo_ver
    import registry_lib as registry

    base_emb, base_meta = _load_assets()
    ver = registry.VERSION
    if _combo_meta is not None and _combo_ver == ver:
        return _combo_emb, _combo_meta

    reg_emb, reg_meta = registry.assets()
    meta = [{**r, "source": "base"} for r in base_meta] + \
           [{**r, "source": "registered"} for r in reg_meta]
    if base_emb is not None:
        emb = np.vstack([base_emb, reg_emb]) if len(reg_meta) else base_emb
    else:
        emb = None  # 경량 배포: 부분일치만
    _combo_emb, _combo_meta, _combo_ver = emb, meta, ver
    return emb, meta


def _embed_query(text: str) -> np.ndarray | None:
    """질문 1건 임베딩. torch/transformers 부재 시 None + 사유 기록."""
    global _vector_error
    try:
        from embed_bge import embed_one  # noqa: WPS433 (지연 임포트: 무거운 의존)

        return np.asarray(embed_one(text), dtype=np.float32).reshape(1, -1)
    except Exception as exc:  # torch 미설치 등
        _vector_error = f"{type(exc).__name__}: {exc}"[:160]
        return None


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text or "") if t not in _STOP and len(t) > 1}


def _lexical_scores(query: str, meta: list[dict]) -> np.ndarray:
    """질문 토큰이 청크(제목+본문)에 얼마나 겹치는지 [0,1]."""
    qt = _tokens(query)
    if not qt:
        return np.zeros(len(meta), dtype=np.float32)
    out = np.empty(len(meta), dtype=np.float32)
    for i, r in enumerate(meta):
        ct = _tokens(f"{r.get('title', '')} {r.get('content', '')}")
        out[i] = len(qt & ct) / len(qt)
    return out


def vector_enabled() -> bool:
    emb, _ = _load_assets()
    return emb is not None


def _decision(score: float) -> str:
    if score >= TAU_REUSE:
        return "reuse"
    if score >= TAU_NEAR:
        return "review"
    return "register"


def _redis_centroids() -> tuple[list[str], np.ndarray | None]:
    """backfill 로 Redis 에 적재된 개념 centroid 로드(프로세스 캐시). 없으면 ([], None)."""
    global _redis_cent_keys, _redis_cent_mat
    if _redis_cent_keys is None:
        import vector_redis as V

        _redis_cent_keys, _redis_cent_mat = V.load_centroids()
    return _redis_cent_keys, _redis_cent_mat


def _redis_search(query: str, coding_type: str | None, top: int,
                  user_id: str | None, difficulty: str | None = None,
                  domain: str | None = None, intent: str | None = None) -> dict | None:
    """Redis Stack(HNSW) 백엔드 검색. 실패 시 None → 호출측이 로컬로 폴백.

    KNN 후보(top*FANOUT) 를 받아 로컬과 동일한 하이브리드 점수로 재랭킹한다:
      combined = W_VEC*vec + W_LEX*lex + W_CONCEPT*(csim(centroid) + W_CLEX*alias_bonus)
    centroid 는 backfill 시 Redis 에 적재(vector_redis.load_centroids). 이로써 온프렘
    검색도 로컬(gold 8/8)과 같은 개념 centroid + alias 재랭킹을 탄다(#38)."""
    try:
        import vector_redis as V
        from embed_bge import embed_one

        qv = np.asarray(embed_one(query), dtype=np.float32)
        cand = V.search(qv, coding_type=coding_type, user_id=user_id,
                        difficulty=difficulty, domain=domain, intent=intent,
                        top=max(top, top * REDIS_FANOUT))
        ckeys, cent = _redis_centroids()
    except Exception as exc:
        global _vector_error
        _vector_error = f"redis:{type(exc).__name__}: {exc}"[:160]
        return None

    # 질문 정규화(코사인 dot 용)
    qn = float(np.linalg.norm(qv))
    qvn = qv / qn if qn > 0 else qv

    # 개념 점수: csim(centroid) + alias 보너스 → primary_concept(전 개념 argmax) + 후보 매핑
    primary_concept: dict | None = None
    cmap: dict[str, float] = {}
    if cent is not None and len(ckeys):
        csim = np.clip(np.nan_to_num((cent @ qvn).ravel(), nan=0.0), 0.0, 1.0)
        ql = (query or "").lower()
        cscore = np.array([csim[i] + W_CLEX * _alias_bonus(ql, k)
                           for i, k in enumerate(ckeys)], dtype=np.float32)
        cmap = {k: float(cscore[i]) for i, k in enumerate(ckeys)}
        pi = int(np.argmax(cscore))
        primary_concept = {"key": ckeys[pi], "label": _label_of(ckeys[pi]),
                           "conf": round(float(csim[pi]), 3)}

    # 후보 재랭킹(로컬 combined 와 동형): 어휘는 후보 제목/본문에서 계산
    lex = _lexical_scores(query, cand)
    for i, h in enumerate(cand):
        v = float(h.get("vec", 0.0))
        cv = cmap.get(h.get("concept_key"), 0.0)
        combined = W_VEC * v + W_LEX * float(lex[i]) + W_CONCEPT * cv
        h["vec"] = round(v, 3)
        h["lex"] = round(float(lex[i]), 3)
        h["con"] = round(cv, 3)  # 개념 centroid 신호
        h["score"] = round(combined, 3)
        h["decision"] = _decision(combined)
    cand.sort(key=lambda x: x["score"], reverse=True)
    # 코퍼스 중복 제거(같은 노트가 여러 세션에 복제) — 최고 점수 1건만.
    seen: set = set()
    deduped: list[dict] = []
    for h in cand:
        dkey = (h.get("title", ""), (h.get("content") or "")[:80])
        if dkey in seen:
            continue
        seen.add(dkey)
        deduped.append(h)
    hits = deduped[:top]
    top1 = hits[0]["score"] if hits else 0.0
    return {
        "ok": True, "query": query,
        "vector": {"enabled": True, "backend": "redis", "error": None},
        "primary_concept": primary_concept,
        "decision": _decision(top1), "top1_score": round(top1, 3),
        "results": hits, "thresholds": {"reuse": TAU_REUSE, "near": TAU_NEAR},
    }


def _visible(r: dict, user_id: str | None) -> bool:
    """user_id 필터: base(전역 지식)는 항상, 등록물은 내 것/무주인만."""
    if not user_id:
        return True
    if r.get("source") != "registered":
        return True  # base = 전역 재사용 지식
    return r.get("user_id") in (None, user_id)


def search(query: str, coding_type: str | None = None, top: int = 8,
           user_id: str | None = None, difficulty: str | None = None,
           domain: str | None = None, intent: str | None = None) -> dict:
    """하이브리드 검색 → 결정 태그가 붙은 청크 랭킹.

    user_id 지정 시: base(전역) + 내가 등록한 것만 노출(다른 사용자 미검수 등록물 제외).
    difficulty/domain 지정 시: 페르소나 필터(#44) — 순위(스코어)는 불변, 노출만 좁힌다.
    반환: {ok, query, vector, decision, top1_score, results:[{...,source,score,decision,
           domain,difficulty,intent,modi_keys,payload,session_id}], thresholds}
    """
    if not (query or "").strip():
        return {"ok": False, "reason": "질문이 비어 있어요.", "query": query}
    if RAG_BACKEND == "mysql_redis":
        redis_res = _redis_search(query, coding_type, top, user_id, difficulty, domain, intent)
        if redis_res is not None:
            return redis_res
        # Redis 경로 실패 시 로컬로 폴백(가용성 우선)
    emb, meta = _combined()

    lex = _lexical_scores(query, meta)
    qv = _embed_query(query) if emb is not None else None
    primary_concept: dict | None = None
    concept_vec = np.zeros(len(meta), dtype=np.float32)
    if qv is not None:
        qn = np.linalg.norm(qv)
        if qn > 0:
            qv = qv / qn
        with np.errstate(all="ignore"):  # macOS Accelerate float32 matmul 허위 경고 억제
            vec = np.clip(np.nan_to_num((emb @ qv.T).ravel(), nan=0.0), 0.0, 1.0)
            # 개념 centroid 신호(패러프레이즈에 강함)
            ckeys, cent = _centroids(emb, meta)
            if len(ckeys):
                csim = np.clip(np.nan_to_num((cent @ qv.T).ravel(), nan=0.0), 0.0, 1.0)
                # centroid(의미) + alias 매칭 보너스(판별어) → 개념 점수
                ql = (query or "").lower()
                cscore = np.array([csim[i] + W_CLEX * _alias_bonus(ql, k)
                                   for i, k in enumerate(ckeys)], dtype=np.float32)
                cmap = {k: float(cscore[i]) for i, k in enumerate(ckeys)}
                pi = int(np.argmax(cscore))
                primary_concept = {"key": ckeys[pi], "label": _label_of(ckeys[pi]),
                                   "conf": round(float(csim[pi]), 3)}
                concept_vec = np.array([cmap.get(r.get("concept_key"), 0.0) for r in meta],
                                       dtype=np.float32)
        combined = W_VEC * vec + W_LEX * lex + W_CONCEPT * concept_vec
        vector_on = True
    else:
        vec = np.zeros(len(meta), dtype=np.float32)
        combined = lex
        vector_on = False

    order = np.argsort(-combined)
    results: list[dict] = []
    seen: set = set()  # 코퍼스 중복(같은 노트가 여러 세션에 복제) → 같은 카드 반복 노출 제거
    for j in order:
        r = meta[int(j)]
        if coding_type and coding_type != "any" and r.get("coding_type") != coding_type:
            continue
        if difficulty and r.get("difficulty") != difficulty:
            continue
        if domain and domain != "any" and r.get("domain") != domain:
            continue
        if intent and r.get("intent") != intent:
            continue
        if not _visible(r, user_id):
            continue
        dkey = (r.get("title", ""), (r.get("content") or "")[:80])
        if dkey in seen:  # 최고 점수 1건만 남기고 중복 스킵(순위 보존)
            continue
        seen.add(dkey)
        results.append({
            "chunk_id": r.get("chunk_id"),
            "title": r.get("title", ""),
            "content": r.get("content", ""),
            "coding_type": r.get("coding_type"),
            "concept_key": r.get("concept_key"),
            "source": r.get("source", "base"),  # base | registered
            # #44 부가 필드: 페르소나·의도·하드웨어·클릭 시 동일 렌더(payload)
            "domain": r.get("domain"),
            "difficulty": r.get("difficulty"),
            "intent": r.get("intent"),
            "modi_keys": r.get("modi_keys"),
            "payload": r.get("payload"),
            # EDU-27 직접서브 문서복원: 같은 세션의 학습노트/설계문서 조인 폴백용 키.
            # base(canonical) 청크는 None — 등록물(registered)만 세션에 귀속.
            "session_id": r.get("session_id"),
            "score": round(float(combined[j]), 3),
            "vec": round(float(vec[j]), 3),
            "lex": round(float(lex[j]), 3),
            "con": round(float(concept_vec[j]), 3),  # 개념 centroid 신호
            "decision": _decision(float(combined[j])),
        })
        if len(results) >= top:
            break

    top1 = results[0]["score"] if results else 0.0
    return {
        "ok": True,
        "query": query,
        "vector": {"enabled": vector_on, "error": None if vector_on else _vector_error},
        "primary_concept": primary_concept,  # centroid 최근접 개념(패러프레이즈 강건)
        "decision": _decision(top1),
        "top1_score": round(top1, 3),
        "results": results,
        "thresholds": {"reuse": TAU_REUSE, "near": TAU_NEAR},
    }


def coverage(queries: list[str], coding_type: str | None = None,
             intent: str | None = None) -> dict:
    """질문 묶음의 재사용/근접/등록 비율(%) — '일부 등록·일부 가져오기' 시각화.

    intent="implement_request" 지정 시 게이트 동형(code 자산 한정) 커버리지 —
    점수 기준 지표가 노트·설계 덕에 낙관 보고되던 착시(EDU-27 진단)를 분리 측정.
    """
    buckets = {"reuse": 0, "review": 0, "register": 0}
    rows: list[dict] = []
    for q in queries:
        s = search(q, coding_type=coding_type, top=1, intent=intent)
        d = s.get("decision", "register") if s.get("ok") else "register"
        buckets[d] += 1
        rows.append({"query": q, "decision": d, "top1_score": s.get("top1_score", 0.0)})
    n = max(len(queries), 1)
    pct = {k: round(v * 100 / n, 1) for k, v in buckets.items()}
    return {
        "ok": True,
        "total": len(queries),
        "counts": buckets,
        "percent": pct,
        "reuse_rate": pct["reuse"] + pct["review"],  # 검수 포함 재사용 가능 비율
        "register_rate": pct["register"],
        "rows": rows,
        "vector_enabled": vector_enabled(),
        "thresholds": {"reuse": TAU_REUSE, "near": TAU_NEAR},
    }
