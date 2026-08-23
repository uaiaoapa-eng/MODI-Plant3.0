"""Redis Stack(HNSW) 벡터 인덱스 — 운영 검색 백엔드.

redis_vector_smoke.py 에서 검증한 HNSW/COSINE + 태그 필터 패턴을 실제 1024차원
(BGE-m3)으로 확장. 원천은 MySQL(store_mysql), 벡터 검색은 여기.

  ensure_index()            인덱스 생성(존재하면 통과)
  upsert(chunk_id, ...)     청크 1건 색인(등록 되먹임 포함)
  search(qvec, ...)         KNN + 태그 필터(coding_type/source/user_id) → 유사도 랭킹

의존성: redis>=5(설치됨). 인덱스는 온프렘 redis/redis-stack-server 에서 동작.
"""
from __future__ import annotations

import array
import os

import numpy as np
import redis
from redis.commands.search.field import NumericField, TagField, TextField, VectorField
from redis.commands.search.index_definition import IndexDefinition, IndexType
from redis.commands.search.query import Query

# ⚠️ RediSearch(FT.CREATE)는 DB 0 에서만 동작 → 반드시 db0. 전용 redis-stack 컨테이너라
# 논리 분리 불필요(세션락 redis 와는 물리적으로 다른 컨테이너).
VECTOR_REDIS_URL = os.getenv("VECTOR_REDIS_URL", "redis://localhost:6379/0")
INDEX = os.getenv("VECTOR_INDEX", "idx:kchunks")
PREFIX = os.getenv("VECTOR_PREFIX", "kchunk:")
# 개념 centroid(개념별 청크 임베딩 평균)를 저장하는 해시 키 프리픽스. 온프렘 Redis 경로의
# centroid+alias 재랭킹(search_lib._redis_search)에서 로드. HNSW 불필요(개념 수 적음 → 앱 dot).
CENTROID_PREFIX = os.getenv("VECTOR_CENTROID_PREFIX", "kcentroid:")
DIM = int(os.getenv("EMBED_DIM", "1024"))

_client: redis.Redis | None = None


def client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(VECTOR_REDIS_URL)
    return _client


def f32(vec) -> bytes:
    """float 시퀀스 → RediSearch float32 바이트."""
    return array.array("f", [float(x) for x in vec]).tobytes()


def ensure_index() -> bool:
    """인덱스 없으면 생성. 반환: 새로 만들었으면 True."""
    r = client()
    try:
        r.ft(INDEX).info()
        return False
    except redis.ResponseError:
        pass
    schema = (
        TagField("coding_type"),
        TagField("source"),          # base | registered
        TagField("concept_key"),
        TagField("user_id"),         # 등록물 소유자(스코핑)
        TagField("domain"),          # #44 커리큘럼 domain 필터
        TagField("difficulty"),      # #44 페르소나 난이도 필터(easy|medium|hard)
        TagField("intent"),          # #44 의도 라우팅
        TextField("title"),
        TextField("content"),
        NumericField("reusability_score"),
        VectorField("embedding", "HNSW",
                    {"TYPE": "FLOAT32", "DIM": DIM, "DISTANCE_METRIC": "COSINE"}),
    )
    r.ft(INDEX).create_index(
        schema, definition=IndexDefinition(prefix=[PREFIX], index_type=IndexType.HASH))
    return True


def reset_index() -> None:
    """인덱스+문서 전부 삭제 후 재생성 → backfill 재실행 멱등성(스테일 키 제거)."""
    r = client()
    try:
        r.ft(INDEX).dropindex(delete_documents=True)
    except redis.ResponseError:
        pass
    ensure_index()


def upsert(chunk_id, embedding, *, title="", content="", coding_type="any",
           source="base", concept_key="", user_id="",
           domain="", difficulty="", intent="", payload=None, session_id="") -> None:
    """청크 1건 색인(HSET). embedding 은 길이 DIM float 시퀀스.

    #44: domain/difficulty/intent 는 태그 필터용, payload 는 클릭 시 동일 렌더용(JSON 문자열).
    session_id(EDU-27 직접서브 문서복원): 색인 스키마엔 없는 저장 전용 필드 — 검색 후
    HGET 파이프라인(payload 와 동일 패턴)으로만 읽는다. base(canonical) 청크는 빈 문자열.
    """
    import json

    mapping = {
        "coding_type": coding_type or "any",
        "source": source or "base",
        "concept_key": concept_key or "",
        "user_id": user_id or "",
        "domain": domain or "",
        "difficulty": difficulty or "",
        "intent": intent or "",
        "title": title or "",
        "content": (content or "")[:2000],
        "embedding": f32(embedding),
        "session_id": session_id or "",
    }
    if payload is not None:
        mapping["payload"] = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    client().hset(f"{PREFIX}{chunk_id}", mapping=mapping)


def _escape_tag(v: str) -> str:
    """RediSearch 태그 값의 특수문자 escape(하이픈·언더스코어 등 uuid 안전).

    주의: 이전 구현의 `ch in "가-힣"` 은 한글 범위가 아니라 문자 '가'/'-'/'힣' 3개를 뜻해
    하이픈이 escape 를 빠져나갔고, uuid user_id 의 '-' 가 부정 연산자로 파싱돼 매 검색
    Syntax error → 로컬 폴백이었다(EDU-27 감사). 한글은 isalnum()=True 라 범위 체크 불필요.
    """
    out = []
    for ch in str(v):
        if not ch.isalnum():
            out.append("\\")
        out.append(ch)
    return "".join(out)


def search(qvec, *, coding_type: str | None = None, user_id: str | None = None,
           difficulty: str | None = None, domain: str | None = None,
           intent: str | None = None, top: int = 8) -> list[dict]:
    """KNN + 태그 필터. 반환: [{chunk_id,title,content,coding_type,concept_key,source,vec,
    domain,difficulty,intent,payload}] 유사도 내림차순.

    user_id 지정 시: base(전역) OR 내 등록물(@source:base | @user_id:me) 로 스코핑.
    difficulty/domain 지정 시: 페르소나 필터(#44).
    intent 지정 시: 의도 필터 — 재사용 게이트가 code 자산(implement_request)만 대상으로
    검색할 때 사용(EDU-27 실전 미발동 수정: 노트·설계가 top-k 를 점령해 code 후보가
    안 잡히던 문제를 색인 단계에서 해소).
    """
    import json

    filt = []
    if coding_type and coding_type != "any":
        filt.append(f"@coding_type:{{{_escape_tag(coding_type)}}}")
    if user_id:
        filt.append(f"(@source:{{base}} | @user_id:{{{_escape_tag(user_id)}}})")
    if difficulty:
        filt.append(f"@difficulty:{{{_escape_tag(difficulty)}}}")
    if domain and domain != "any":
        filt.append(f"@domain:{{{_escape_tag(domain)}}}")
    if intent:
        filt.append(f"@intent:{{{_escape_tag(intent)}}}")
    base = " ".join(filt) if filt else "*"
    # "payload" 는 return_fields 에 넣으면 안 된다: redis-py Document(id, payload=None, **fields)
    # 의 payload kwarg 와 충돌해 매 검색 TypeError → 호출측(search_lib)이 조용히 로컬 폴백해
    # Redis 인덱스가 통째로 무시된다(EDU-27 감사에서 발견). 저장 필드는 그대로 두고
    # 검색 후 HGET 파이프라인(1 왕복)으로만 payload 를 읽는다 — 기존 해시 마이그레이션 불필요.
    q = (Query(f"({base})=>[KNN {top} @embedding $vec AS dist]")
         .sort_by("dist")
         .return_fields("title", "content", "coding_type", "concept_key", "source",
                        "domain", "difficulty", "intent", "dist")
         .paging(0, top)
         .dialect(2))
    res = client().ft(INDEX).search(q, query_params={"vec": f32(qvec)})
    pipe = client().pipeline(transaction=False)
    for d in res.docs:
        # payload/session_id 둘 다 색인 스키마 밖 저장 전용 필드라 HMGET 1왕복으로 함께 읽는다
        # (session_id: EDU-27 직접서브 문서복원 — 세션 조인 폴백 키).
        pipe.hmget(d.id, "payload", "session_id")
    extra_raws = pipe.execute() if res.docs else []
    out = []
    for d, (payload_raw, session_id_raw) in zip(res.docs, extra_raws):
        # RediSearch COSINE distance ∈ [0,2] → 유사도 = 1 - dist
        sim = max(0.0, 1.0 - float(d.dist))
        cid = d.id[len(PREFIX):] if d.id.startswith(PREFIX) else d.id
        try:
            # HGET 은 decode_responses 미설정이라 bytes — json.loads 가 그대로 처리한다.
            payload = json.loads(payload_raw) if payload_raw else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = None
        session_id = None
        if session_id_raw:
            session_id = session_id_raw.decode() if isinstance(session_id_raw, bytes) else session_id_raw
            session_id = session_id or None
        out.append({
            "chunk_id": cid,
            "title": getattr(d, "title", ""),
            "content": getattr(d, "content", ""),
            "coding_type": getattr(d, "coding_type", None),
            "concept_key": getattr(d, "concept_key", "") or None,
            "source": getattr(d, "source", "base"),
            "domain": getattr(d, "domain", "") or None,
            "difficulty": getattr(d, "difficulty", "") or None,
            "intent": getattr(d, "intent", "") or None,
            "payload": payload,
            "session_id": session_id,
            "vec": round(sim, 3),
        })
    return out


def upsert_centroid(concept_key: str, embedding) -> None:
    """개념 centroid 1건 저장(HSET). embedding 은 길이 DIM float 시퀀스(정규화 권장)."""
    client().hset(f"{CENTROID_PREFIX}{concept_key}", mapping={"embedding": f32(embedding)})


def clear_centroids() -> int:
    """모든 centroid 키 제거 → backfill 재실행 멱등성(스테일 개념 제거). 반환: 삭제 건수."""
    r = client()
    keys = list(r.scan_iter(match=f"{CENTROID_PREFIX}*"))
    if keys:
        r.delete(*keys)
    return len(keys)


def load_centroids() -> tuple[list[str], np.ndarray]:
    """모든 개념 centroid 로드 → (concept_keys, (K,DIM) float32 정규화 행렬).

    없으면 ([], (0,DIM)). 정규화는 로드 시 방어적으로 한 번 더 수행(코사인 dot 용)."""
    r = client()
    keys: list[str] = []
    vecs: list[np.ndarray] = []
    for raw_key in r.scan_iter(match=f"{CENTROID_PREFIX}*"):
        key = raw_key.decode() if isinstance(raw_key, (bytes, bytearray)) else raw_key
        raw = r.hget(raw_key, "embedding")
        if not raw:
            continue
        keys.append(key[len(CENTROID_PREFIX):])
        vecs.append(np.frombuffer(raw, dtype=np.float32))
    if not keys:
        return [], np.zeros((0, DIM), dtype=np.float32)
    mat = np.vstack(vecs).astype(np.float32)
    norm = np.linalg.norm(mat, axis=1, keepdims=True)
    mat = np.divide(mat, norm, out=np.zeros_like(mat), where=norm > 0)
    return keys, mat


def count() -> int:
    try:
        return int(client().ft(INDEX).info()["num_docs"])
    except redis.ResponseError:
        return 0
