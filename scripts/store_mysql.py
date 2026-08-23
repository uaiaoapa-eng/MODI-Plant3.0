"""온프렘 MySQL 8.0 원천 저장소 (source of truth).

deploy/schema.sql 의 sessions / knowledge_chunks / ontology_* / usage_turns 에 대한
얇은 접근 계층. DATABASE_URL 또는 개별 MYSQL_* 환경변수로 연결. pymysql 사용(순수 파이썬).

원천은 MySQL, 벡터 검색은 vector_redis(Redis Stack). 백필은 backfill_onprem.py.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager


def _conn_params() -> dict:
    url = os.getenv("DATABASE_URL", "")
    if url:  # mysql+pymysql://user:pass@host:port/db
        from urllib.parse import urlparse

        u = urlparse(url)
        return {"host": u.hostname or "localhost", "port": u.port or 3306,
                "user": u.username or "root", "password": u.password or "",
                "database": (u.path or "/edu_agent").lstrip("/") or "edu_agent"}
    return {"host": os.getenv("MYSQL_HOST", "localhost"),
            "port": int(os.getenv("MYSQL_PORT", "3306")),
            "user": os.getenv("MYSQL_USER", "root"),
            "password": os.getenv("MYSQL_PASSWORD", ""),
            "database": os.getenv("MYSQL_DATABASE", "edu_agent")}


@contextmanager
def connect():
    import pymysql

    p = _conn_params()
    conn = pymysql.connect(charset="utf8mb4", autocommit=False,
                           cursorclass=pymysql.cursors.DictCursor, **p)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def open_conn():
    """읽기 경로용 단순 연결(autocommit). 호출자가 close 책임.

    서빙(ontology_lib.graph_conn)에서 컨텍스트매니저 없이 sqlite 연결과
    동일한 수명주기(conn.close())로 다루기 위한 얇은 진입점.
    """
    import pymysql

    return pymysql.connect(charset="utf8mb4", autocommit=True,
                           cursorclass=pymysql.cursors.DictCursor, **_conn_params())


def upsert_session(conn, s: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO sessions(session_id,user_id,title,description,coding_type,app_type,phase,raw)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
               ON DUPLICATE KEY UPDATE user_id=VALUES(user_id),title=VALUES(title),
                 description=VALUES(description),coding_type=VALUES(coding_type),
                 app_type=VALUES(app_type),phase=VALUES(phase),raw=VALUES(raw)""",
            (s["session_id"], s.get("user_id", ""), s.get("title"), s.get("description"),
             s.get("coding_type"), s.get("app_type"), s.get("phase"),
             json.dumps(s.get("raw", {}), ensure_ascii=False)),
        )


def list_sessions(conn, user_id: str) -> list[dict]:
    """유저의 세션 요약 목록(raw 포함) — 대화 리스트를 MySQL 원천에서 제공. 최신순.

    raw 를 함께 반환해 호출측(rag-search)이 파일 저장과 동일한 요약(has_code 등)을 만든다.
    updated_at 은 파일 mtime 과 호환되도록 UNIX epoch(float)로 반환한다.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT session_id,title,description,coding_type,app_type,phase,raw,"
            "UNIX_TIMESTAMP(updated_at) AS updated_at "
            "FROM sessions WHERE user_id=%s ORDER BY updated_at DESC",
            (user_id,),
        )
        return list(cur.fetchall())


def get_session(conn, session_id: str, user_id: str | None = None) -> dict | None:
    """세션 전문(raw) 반환. user_id 지정 시 소유자 일치 조건. 없으면 None."""
    with conn.cursor() as cur:
        if user_id:
            cur.execute("SELECT raw FROM sessions WHERE session_id=%s AND user_id=%s",
                        (session_id, user_id))
        else:
            cur.execute("SELECT raw FROM sessions WHERE session_id=%s", (session_id,))
        row = cur.fetchone()
    if not row:
        return None
    raw = row.get("raw")
    return json.loads(raw) if isinstance(raw, str) else raw


def delete_session(conn, session_id: str, user_id: str) -> int:
    """소유자 세션 삭제. 반환: 삭제된 행 수."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE session_id=%s AND user_id=%s",
                    (session_id, user_id))
        return cur.rowcount


def insert_usage_turn(conn, row: dict) -> None:
    """usage_turns 1행 삽입(#133) — 턴당 1건, append-only 분석 원천(upsert 아님).

    쿼터 카운터(Redis, 48h TTL·집행 전용)와 분리된 영구 저장소. 토큰 전부 0인 턴
    (재사용으로 LLM 미호출 등)도 그대로 적재한다 — 그 자체가 분석 대상(엣지 케이스).
    weighted_tokens 는 호출측(server.py)이 기록 시점 가중치로 계산해 넘긴다.
    """
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO usage_turns(ts,subject,user_id,session_id,mode,coding_type,
                 input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens,
                 weighted_tokens,trace_id,llm_mode,reuse_tier,
                 started_at,duration_ms,ttft_ms,status,error_code,replica,
                 intent,phase,outcome,reuse_top1,direct_served,docs_restored,
                 user_agent,client_ip,mem_mb)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                      %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (row["ts"], row["subject"], row.get("user_id", ""), row.get("session_id", ""),
             row.get("mode", ""), row.get("coding_type", ""),
             row.get("input_tokens", 0), row.get("output_tokens", 0),
             row.get("cache_read_tokens", 0), row.get("cache_creation_tokens", 0),
             row.get("weighted_tokens", 0), row.get("trace_id", ""),
             (row.get("llm_mode") or "")[:20], (row.get("reuse_tier") or "")[:16],
             # started_at 은 없으면 NULL 로 둔다 — 빈 문자열을 넣으면 MySQL 이
             # '0000-00-00' 으로 받아 동접 계산이 1970년으로 튄다.
             (row.get("started_at") or None),
             _num(row.get("duration_ms")), _num(row.get("ttft_ms")),
             (row.get("status") or "ok")[:12], (row.get("error_code") or "")[:32],
             (row.get("replica") or "")[:24],
             (row.get("intent") or "")[:24], (row.get("phase") or "")[:12],
             (row.get("outcome") or "")[:16],
             float(row.get("reuse_top1") or 0.0),
             1 if row.get("direct_served") else 0,
             _num(row.get("docs_restored")),
             (row.get("user_agent") or "")[:255], (row.get("client_ip") or "")[:45],
             _num(row.get("mem_mb"))),
        )


def insert_ops_event(conn, row: dict) -> None:
    """ops_events 1행 삽입 — 턴이 만들어지지 않는 사건(거절·차단·에러·재시작).

    usage_turns 와 짝이다. /chat 이 세션 락을 잡기 **전에** 거절하면 사용량 기록이
    도는 finally 에 도달하지 못해 원장에 흔적이 없다. 40명 동시 수업에서 "몇 명이
    튕겼나"가 바로 그 숫자라, 이 테이블이 없으면 부하 분석의 핵심이 비게 된다.
    """
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO ops_events(ts,kind,code,user_id,session_id,replica,detail)
               VALUES(%s,%s,%s,%s,%s,%s,%s)""",
            (row["ts"], (row.get("kind") or "")[:24], (row.get("code") or "")[:32],
             (row.get("user_id") or "")[:64], (row.get("session_id") or "")[:64],
             (row.get("replica") or "")[:24], (row.get("detail") or "")[:255]),
        )


def _json_or_none(v):
    """dict/list → JSON 문자열, None 은 그대로. 이미 문자열이면 그대로(재직렬화 방지)."""
    if v is None or isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False)


_CHUNK_NATURAL_KEY = ("session_id", "title", "chunk_type", "source")


def insert_chunk(conn, c: dict) -> int:
    """knowledge_chunks 1건 적재 → chunk_id 반환.

    chunk_id 가 주어지면 명시적으로 그 id 로 삽입한다(AUTO_INCREMENT 무시).
    온톨로지 엣지(realized_by 등)는 원천 sqlite 의 chunk_id 를 dst 로 참조하므로,
    백필 시 id 를 보존해야 realized_by → knowledge_chunks 조인이 성립한다.

    #44: intent/domain/difficulty/modi_keys/payload 도 함께 적재(페르소나 필터·의도
    라우팅·하드웨어 연계·클릭 시 동일 렌더의 원천). modi_keys/payload 는 JSON 컬럼.

    #116: 되먹임 등록(source=registered)은 자연키 (session_id, title, chunk_type,
    source) 로 dedup 한다 — 이미 있으면 INSERT 대신 UPDATE 로 내용/임베딩만 갱신해,
    writeback 재실행(파일 스토어 소실로 registry_lib._seen 이 비었을 때 포함)에도 MySQL
    에 중복 registered 행이 쌓이지 않는다. base 는 chunk_id 를 보존해야 realized_by
    조인이 성립하므로 dedup 하지 않는다(백필은 clear_base_chunks 로 멱등 보장).
    """
    cid = c.get("chunk_id")
    source = c.get("source", "base")
    # base 스키마 필드 + #44 부가 필드(삽입/갱신 공용). embedding/payload/modi_keys 는 JSON 직렬화.
    fields = {
        "session_id": c.get("session_id"),
        "chunk_type": c.get("chunk_type", "learning_note"),
        "seq": c.get("seq", 0),
        "coding_type": c.get("coding_type"),
        "concept_key": c.get("concept_key"),
        "intent": c.get("intent"),
        "domain": c.get("domain"),
        "difficulty": c.get("difficulty"),
        "modi_keys": _json_or_none(c.get("modi_keys")),
        "title": c.get("title"),
        "content": c.get("content", ""),
        "payload": _json_or_none(c.get("payload")),
        "embedding": json.dumps(c.get("embedding")) if c.get("embedding") is not None else None,
        "source": source,
        "outcome": c.get("outcome"),
        "reusability_score": c.get("reusability_score", 1.0),
    }
    with conn.cursor() as cur:
        # #116: registered 는 자연키로 기존 행을 찾아 갱신 → writeback 재실행 멱등.
        if source == "registered":
            cur.execute(
                "SELECT chunk_id FROM knowledge_chunks "
                "WHERE session_id=%s AND title=%s AND chunk_type=%s AND source=%s "
                "LIMIT 1",
                tuple(fields[k] for k in _CHUNK_NATURAL_KEY),
            )
            existing = cur.fetchone()
            if existing:
                eid = existing["chunk_id"]
                upd = [k for k in fields if k not in _CHUNK_NATURAL_KEY]
                cur.execute(
                    "UPDATE knowledge_chunks SET "
                    + ",".join(f"{k}=%s" for k in upd)
                    + " WHERE chunk_id=%s",
                    (*(fields[k] for k in upd), eid),
                )
                return eid
        cols = list(fields)
        vals = [fields[k] for k in cols]
        if cid is not None:
            cols.insert(0, "chunk_id")
            vals.insert(0, cid)
        placeholders = ",".join(["%s"] * len(cols))
        cur.execute(
            f"INSERT INTO knowledge_chunks ({','.join(cols)}) VALUES({placeholders})",
            tuple(vals),
        )
        return cid if cid is not None else cur.lastrowid


def get_chunks_by_session(conn, session_id: str, chunk_types: list | None = None) -> list[dict]:
    """세션의 청크(학습노트/설계문서 등) 조회 — EDU-27 직접서브 문서복원 세션 조인 폴백.

    knowledge_chunks.session_id 는 auto_save 실시간 되먹임(register_learning_notes/
    register_result)이 항상 채우므로, 코드 청크의 session_id 로 같은 세션의 학습노트·
    설계문서 청크를 직접 조회할 수 있다(Redis 벡터색인의 chunk_id 불일치 문제를 우회).
    반환: [{chunk_type, title, content, payload}] — payload 는 dict 로 역직렬화.
    """
    with conn.cursor() as cur:
        if chunk_types:
            placeholders = ",".join(["%s"] * len(chunk_types))
            cur.execute(
                f"SELECT chunk_type, title, content, payload FROM knowledge_chunks "
                f"WHERE session_id=%s AND chunk_type IN ({placeholders})",
                (session_id, *chunk_types),
            )
        else:
            cur.execute(
                "SELECT chunk_type, title, content, payload FROM knowledge_chunks "
                "WHERE session_id=%s",
                (session_id,),
            )
        rows = cur.fetchall()
    out = []
    for r in rows:
        payload = r.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                payload = None
        out.append({"chunk_type": r.get("chunk_type"), "title": r.get("title"),
                    "content": r.get("content"), "payload": payload})
    return out


def upsert_node(conn, key: str, node_type: str, label: str, level: int = 0,
                meta=None) -> None:
    """온톨로지 노드 upsert. meta(#44): 개념 alias/axes/difficulty·MODI role 등 부가정보(JSON)."""
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO ontology_nodes(node_type,key_name,label,level,meta)
               VALUES(%s,%s,%s,%s,%s)
               ON DUPLICATE KEY UPDATE label=VALUES(label),level=VALUES(level),
                 meta=VALUES(meta)""",
            (node_type, key, label, level, _json_or_none(meta)),
        )


def upsert_edge(conn, src: str, dst: str, rel: str, weight: float = 1.0) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO ontology_edges(src_key,dst_key,rel,weight) VALUES(%s,%s,%s,%s)
               ON DUPLICATE KEY UPDATE weight=VALUES(weight)""",
            (src, dst, rel, weight),
        )


def clear_base_chunks(conn) -> int:
    """base(백필 원천) 청크 제거 → backfill 재실행 멱등성(중복 방지). 등록물(source=registered)은 보존."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM knowledge_chunks WHERE source='base'")
        return cur.rowcount


def clear_ontology(conn) -> dict:
    """온톨로지 노드/엣지 전량 제거 → 재빌드 멱등성.

    edges/nodes 는 upsert(ON DUPLICATE KEY)만 하므로, 과거 실행에서 dst 가 달랐던
    realized_by 등 스테일 엣지가 누적된다(관측: MySQL 1387 vs 원천 821). 재적재가
    전량을 다시 삽입하므로 먼저 비우는 게 안전하다. FK 없음(deploy/schema.sql).
    """
    removed = {}
    with conn.cursor() as cur:
        cur.execute("DELETE FROM ontology_edges")
        removed["ontology_edges"] = cur.rowcount
        cur.execute("DELETE FROM ontology_nodes")
        removed["ontology_nodes"] = cur.rowcount
    return removed


def counts(conn) -> dict:
    """테이블별 행 수. **없는 테이블은 None 으로 보고하고 넘어간다.**

    ⚠ 예전엔 없는 테이블에서 그대로 터졌다. 그 결과 `usage_turns` 를 이 목록에 추가하자
    (비용 리포트 신뢰 근거) 배포의 RAG 백필 스텝이 통째로 실패했다
    (1146 Table 'edu_agent.usage_turns' doesn't exist, 2026-08-21).

    진단 함수가 진단 대상의 부재 때문에 죽으면 안 된다 — 오히려 "없음" 자체가 알아야 할
    정보다. None 으로 돌려 호출측(배포 로그·/api/registry/stats)이 그대로 드러내게 한다.
    """
    out: dict = {}
    for t in ("sessions", "knowledge_chunks", "ontology_nodes", "ontology_edges",
              "usage_turns"):
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) AS n FROM {t}")
                out[t] = cur.fetchone()["n"]
        except Exception:
            # 1146(no such table) 등 — 스키마 미적용 상태를 그대로 노출한다.
            out[t] = None
    return out


# ──────────────────────────────────────────────────────────────────────────────
# 사용량·비용 리포트 (#133 의 읽기 경로)
# ──────────────────────────────────────────────────────────────────────────────
#
# 비용 환산 근거: usage_turns.weighted_tokens 는 기록 시점 가중치
#   input×1 + output×5 + cache_read×0.1 + cache_creation×1.25
# 로 계산돼 있고, 이 비율은 Haiku 4.5 단가(입력 $1 / 출력 $5 / 캐시읽기 $0.1 /
# 캐시쓰기 $1.25 per MTok)와 정확히 일치한다. 즉 입력 단가로 정규화된 값이라
#   weighted_tokens / 1_000_000 = USD
# 가 성립한다. (모델이 바뀌면 이 등식이 깨지므로 리포트에 model 가정을 명시한다)

USD_PER_WEIGHTED_MTOK = 1.0     # Haiku 4.5 입력 단가 기준
_DEF_KRW = 1400.0


def _num(v) -> int:
    """DB 집계값을 int 로 정규화한다.

    ⚠ MySQL 의 SUM() 은 BIGINT 컬럼을 합쳐도 **DECIMAL** 을 돌려준다(오버플로 방지).
    Decimal 은 float 과 섞어 곱할 수 없어서 `Decimal * 1.0` 이 곧바로 터진다.
    2026-08-21 운영에서 실제로 그랬다:

        {"ok": false,
         "error": "unsupported operand type(s) for *: 'decimal.Decimal' and 'float'"}

    총계는 int() 로 감쌌지만 행 단위 집계(by_kind/top_users/by_hour)는 raw 를 그대로
    넘겨서, **표가 한 줄이라도 있으면 리포트 전체가 실패**했다. 덤으로 Decimal 은
    기본 JSON 인코더도 직렬화하지 못하므로 응답 경로에서 한 번 더 깨진다.
    """
    try:
        return int(v or 0)
    except (TypeError, ValueError, ArithmeticError):
        return 0


def _day_bounds(start: str, end: str) -> tuple[str, str]:
    """비었으면 '오늘 하루'(KST). 날짜만 오면 00:00:00 / 23:59:59 로 확장."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")
    s = (start or today).strip()
    e = (end or start or today).strip()
    if len(s) == 10:
        s += " 00:00:00"
    if len(e) == 10:
        e += " 23:59:59"
    return s, e


# ─────────────────────────────────────────────────────────────────────────────
# 시간대 규약 — 저장은 UTC, 표시는 KST
#
# 2026-08-21 실측으로 확인된 사실: usage_turns.ts / sessions.created_at 은 **UTC 로
# 저장된다.** 앱이 tz-aware ISO(+09:00)를 보내면 MySQL 이 세션 타임존(UTC)으로 변환해
# 넣고, created_at 은 CURRENT_TIMESTAMP(=UTC)다. 그런데 조회 쪽이 이를 KST 로 착각해
# DATE_FORMAT(ts,...) 을 그대로 붙여 왔다 → **모든 시간 라벨이 9시간 어긋났다.**
# (KST 20:11:51 에 발생한 턴이 리포트에 "11시"로 표시됨)
#
# 고치는 방식이 두 갈래인데 목적이 다르다:
#   ① 필터(BETWEEN) — KST 경계를 **파이썬에서 UTC 로 바꿔** 넘긴다.
#      컬럼에 함수를 씌우면 ts 인덱스를 못 타기 때문이다.
#   ② 라벨(시/일 표기, 최초/최종 시각) — SQL 에서 CONVERT_TZ 로 KST 로 되돌린다.
#      이쪽은 인덱스와 무관하고, 표기는 반드시 한국 시간이어야 한다.
#
# CONVERT_TZ 에 지역명('Asia/Seoul') 대신 오프셋('+09:00')을 쓰는 이유: 지역명은 MySQL
# 타임존 테이블이 적재돼 있어야 동작하고, 없으면 조용히 NULL 을 돌려준다. 오프셋은
# 항상 동작한다. 한국은 서머타임이 없어 고정 오프셋으로 충분하다.
KST_TS = "CONVERT_TZ(ts,'+00:00','+09:00')"
KST_CREATED = "CONVERT_TZ(created_at,'+00:00','+09:00')"


def kst_to_utc(ts_kst: str) -> str:
    """'YYYY-MM-DD HH:MM:SS'(KST) → 같은 형식의 UTC 문자열.

    필터 경계 변환 전용. 파싱에 실패하면 원본을 그대로 돌려준다 — 시간대 보정 때문에
    조회가 통째로 죽는 것보다 9시간 어긋난 결과라도 나오는 편이 낫다(fail-open).
    """
    from datetime import datetime, timedelta
    try:
        return (datetime.strptime(ts_kst.strip(), "%Y-%m-%d %H:%M:%S")
                - timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return ts_kst


PAGE_SIZE = 25


def _projects_block(conn, start: str, end: str, user_id: str = "",
                    limit: int = PAGE_SIZE, offset: int = 0, q: str = "") -> dict:
    """기간 내 만들어진 프로젝트(세션) 수와 목록.

    비용 원장(usage_turns)과는 다른 테이블(sessions)이고 시간 컬럼도 다르다
    (ts vs created_at). 그래서 여기서 따로 질의한다.

    ⚠ 이 블록이 실패해도 **비용 리포트는 살아야 한다.** 프로젝트 목록은 부가 정보인데
      그것 때문에 청구 근거가 통째로 안 나오면 본말이 전도된다. 그래서 통째로 감싸고
      실패 시 ok=False 로 표시만 한다(2026-08-21 usage_turns 부재 사고와 같은 교훈).
    """
    # sessions.created_at 도 CURRENT_TIMESTAMP(=UTC)다. usage_turns 와 같은 규약이라
    # 경계는 UTC 로 옮기고 라벨만 KST 로 되돌린다.
    where = "created_at BETWEEN %s AND %s"
    args: list = [kst_to_utc(start), kst_to_utc(end)]
    if user_id:
        where += " AND user_id = %s"
        args.append(user_id)

    # 검색은 **집계가 아니라 목록에만** 건다. "이 기간에 몇 개 만들었나"는 검색어와
    # 무관한 사실이고, 그게 검색 때문에 바뀌면 숫자를 못 믿게 된다.
    #
    # 조건절을 접두사와 함께 만들어 두는 이유: 아래에서 usage_turns 와 조인할 때
    # 두 테이블에 user_id/session_id 가 **모두** 있어 컬럼명이 모호해진다. 만들어진
    # 문자열을 나중에 치환하는 방식은 조건이 하나 늘 때 조용히 깨지므로 쓰지 않는다.
    def _clause(pfx: str = "") -> tuple[str, list]:
        c = f"{pfx}created_at BETWEEN %s AND %s"
        a = [kst_to_utc(start), kst_to_utc(end)]
        if user_id:
            c += f" AND {pfx}user_id = %s"
            a.append(user_id)
        if (q or "").strip():
            like = f"%{q.strip()}%"
            c += (f" AND ({pfx}title LIKE %s OR {pfx}user_id LIKE %s "
                  f"OR {pfx}session_id LIKE %s)")
            a += [like, like, like]
        return c, a

    q = (q or "").strip()
    search, sargs = _clause()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM sessions WHERE {where}", args)
            created = _num((cur.fetchone() or {}).get("n"))

            cur.execute(f"SELECT COUNT(*) AS n FROM sessions WHERE {search}", sargs)
            matched = _num((cur.fetchone() or {}).get("n"))

            cur.execute(f"""
                SELECT COALESCE(NULLIF(coding_type,''),'(미지정)') AS coding_type,
                       COALESCE(NULLIF(app_type,''),'(미지정)')    AS app_type,
                       COUNT(*) AS n
                FROM sessions WHERE {where}
                GROUP BY coding_type, app_type ORDER BY n DESC
            """, args)
            by_type = [{**r, "n": _num(r.get("n"))} for r in (cur.fetchall() or [])]

            cur.execute(f"""
                SELECT DATE_FORMAT(CONVERT_TZ(created_at,'+00:00','+09:00'),'%%Y-%%m-%%d') AS day, COUNT(*) AS n
                FROM sessions WHERE {where}
                GROUP BY day ORDER BY day DESC
            """, args)
            per_day = {r["day"]: _num(r.get("n")) for r in (cur.fetchall() or [])}

            cur.execute(f"""
                SELECT session_id, user_id, title, coding_type, app_type, phase,
                       -- ⚠ 목록에 찍히는 시각도 KST 로 돌려서 내보낸다. 집계(per_day)만
                       --   변환하고 여기를 빠뜨려서 프로젝트 목록만 9시간 뒤처져 보였다
                       --   (2026-08-21: 22:33 에 만든 프로젝트가 13:33 으로 표시).
                       --   정렬은 원본 created_at 으로 한다 — 변환은 표시용이고,
                       --   컬럼에 함수를 씌우면 인덱스를 못 탄다.
                       CONVERT_TZ(created_at,'+00:00','+09:00') AS created_at,
                       CONVERT_TZ(updated_at,'+00:00','+09:00') AS updated_at
                FROM sessions WHERE {search}
                ORDER BY created_at DESC LIMIT %s OFFSET %s
            """, sargs + [int(limit), max(0, int(offset))])
            items = [{**r,
                      "created_at": str(r.get("created_at") or ""),
                      "updated_at": str(r.get("updated_at") or "")}
                     for r in (cur.fetchall() or [])]

            # ── 프로젝트별 토큰·비용 ────────────────────────────────────────
            # 프로젝트 수와 비용을 같은 화면에 나란히 두면서 연결해 주지 않으면
            # "300개 만드는 데 0.018달러"로 읽힌다. 실제로는 두 숫자의 계보가
            # 다르다(sessions 는 백필된 과거 전체, usage_turns 는 관측 시작 이후).
            # session_id 로 이어 붙여 **대조 가능한 수치**로 만든다.
            sids = [r["session_id"] for r in items if r.get("session_id")]
            if sids:
                ph = ",".join(["%s"] * len(sids))
                cur.execute(f"""
                    SELECT session_id, COUNT(*) AS turns,
                           COALESCE(SUM(weighted_tokens),0) AS weighted_tokens,
                           COALESCE(SUM(duration_ms),0)     AS duration_ms
                    FROM usage_turns WHERE session_id IN ({ph})
                    GROUP BY session_id
                """, sids)
                agg = {r["session_id"]: r for r in (cur.fetchall() or [])}
                for it in items:
                    a = agg.get(it.get("session_id")) or {}
                    it["turns"] = _num(a.get("turns"))
                    it["weighted_tokens"] = _num(a.get("weighted_tokens"))
                    it["duration_ms"] = _num(a.get("duration_ms"))
                    # 관측 시작 이전 프로젝트를 '공짜'로 오해하지 않도록 표시한다.
                    it["measured"] = bool(a)

            # ── 검색 결과의 비용 합계 ──────────────────────────────────────
            # 기간 총액은 검색과 무관하게 그대로 둔다(그게 흔들리면 숫자를 못 믿는다).
            # 대신 "이 검색에 해당하는 비용"을 별도 값으로 준다.
            # ⚠ 두 테이블에 user_id/session_id 가 모두 있어 조인 시 모호해진다 →
            #   sessions 조건에 s. 접두사를 붙인 사본을 쓴다.
            qsearch, qargs = _clause("s.")
            # ⚠ 턴 시각으로도 걸러야 총계와 대조가 된다.
            #   세션 생성일만 보면 그 세션의 **모든 턴**이 들어와, 조회 기간 밖의 턴까지
            #   합산된다. 2026-08-21 실측: totals 77턴인데 여기가 80턴으로 나왔다
            #   (시간대 규약이 통일되기 전의 잔여 행 3건이 새어 들어왔다).
            #   같은 화면의 두 숫자가 어긋나면 어느 쪽도 못 믿게 된다.
            cur.execute(f"""
                SELECT COUNT(*) AS turns,
                       COUNT(DISTINCT ut.session_id) AS sessions,
                       COALESCE(SUM(ut.weighted_tokens),0) AS weighted_tokens
                FROM usage_turns ut
                JOIN sessions s ON s.session_id = ut.session_id
                WHERE {qsearch} AND ut.ts BETWEEN %s AND %s
            """, qargs + [kst_to_utc(start), kst_to_utc(end)])
            m = cur.fetchone() or {}
            matched_cost = {"turns": _num(m.get("turns")),
                            "sessions": _num(m.get("sessions")),
                            "weighted_tokens": _num(m.get("weighted_tokens"))}

        return {"ok": True, "created": created, "by_type": by_type,
                "per_day": per_day, "items": items, "listed": len(items),
                "matched": matched, "matched_cost": matched_cost,
                "offset": max(0, int(offset)),
                "page_size": int(limit), "q": q}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "created": 0,
                "by_type": [], "per_day": {}, "items": [], "listed": 0,
                "matched": 0, "offset": 0, "page_size": int(limit), "q": q}


def _billing_rows(conn, where: str, args: list) -> tuple[list, list]:
    """llm_mode / reuse_tier 별 원시 집계 두 벌."""
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COALESCE(NULLIF(llm_mode,''),'(미상)') AS llm_mode,
                   COUNT(*) AS turns,
                   COALESCE(SUM(weighted_tokens),0) AS weighted_tokens
            FROM usage_turns WHERE {where}
            GROUP BY llm_mode ORDER BY weighted_tokens DESC
        """, args)
        by_mode = [{**r, "turns": _num(r.get("turns")),
                    "weighted_tokens": _num(r.get("weighted_tokens"))}
                   for r in (cur.fetchall() or [])]

        cur.execute(f"""
            SELECT COALESCE(NULLIF(reuse_tier,''),'(미상)') AS reuse_tier,
                   COUNT(*) AS turns,
                   COALESCE(SUM(weighted_tokens),0) AS weighted_tokens
            FROM usage_turns WHERE {where}
            GROUP BY reuse_tier ORDER BY turns DESC
        """, args)
        by_tier = [{**r, "turns": _num(r.get("turns")),
                    "weighted_tokens": _num(r.get("weighted_tokens"))}
                   for r in (cur.fetchall() or [])]
    return by_mode, by_tier


def _billing_and_reuse(conn, where: str, args: list) -> dict:
    """실과금 구분(API vs 구독)과 재사용 절감 효과.

    ── 왜 필요한가 ────────────────────────────────────────────────────────────
    가중토큰을 USD 로 환산한 값은 "이 사용량을 API 로 돌렸으면 얼마" 다. 그런데 실제로는
    두 갈래가 섞인다:

      · llm_mode='api'               → 진짜 청구된다
      · 'cli' / 'api_fallback_cli'   → 구독 정액이라 **실청구 0**

    날짜 단위 라벨로는 이걸 못 가른다(모드를 바꾼 날엔 하루에 둘이 섞인다). 그래서
    턴마다 실제 경로를 남기고 여기서 쪼갠다.

    ── 절감은 어떻게 재나 ────────────────────────────────────────────────────
    재사용(direct_serve/near)의 값어치는 "안 쓴 돈" 이라 직접 관측되지 않는다.
    **cold 턴(재사용 없이 새로 생성한 턴)의 평균 단가**를 반사실(counterfactual)로 놓고,
    각 티어가 그보다 얼마나 덜 썼는지로 잰다:

        saved = Σ_tier  turns_tier × (avg_usd_cold − avg_usd_tier)

    cold 턴이 없으면 기준선이 없으므로 절감을 계산하지 않는다(0 이 아니라 '미상').
    추정임을 숨기지 않으려고 baseline 을 함께 돌려준다.
    """
    out: dict = {"ok": True}
    try:
        by_mode, by_tier = _billing_rows(conn, where, args)
    except Exception as e:
        # llm_mode/reuse_tier 컬럼이 아직 없는 배포에서도 **비용 리포트는 살아야 한다**.
        # 부가 분석 때문에 청구 근거를 잃는 건 본말전도다(2026-08-21 교훈).
        return {"ok": False, "error": str(e)[:200],
                "billing": {"by_mode": [], "billed_usd": 0.0,
                            "subscription_usd": 0.0, "unknown_usd": 0.0,
                            "unavailable": True},
                "reuse": {"ok": False, "by_tier": [],
                          "reason": "재사용 티어 컬럼이 아직 없습니다(배포 후 수집 시작)"}}

    def usd(w):
        return round(_num(w) / 1_000_000, 4)

    for r in by_mode:
        r["usd"] = usd(r["weighted_tokens"])
    for r in by_tier:
        r["usd"] = usd(r["weighted_tokens"])
        r["usd_per_turn"] = round(r["usd"] / r["turns"], 6) if r["turns"] else 0.0
    billed = sum(r["usd"] for r in by_mode if r["llm_mode"] == "api")
    free = sum(r["usd"] for r in by_mode if r["llm_mode"] != "api")
    unknown = sum(r["usd"] for r in by_mode if r["llm_mode"] == "(미상)")
    out["billing"] = {
        "by_mode": by_mode,
        "billed_usd": round(billed, 4),
        "subscription_usd": round(free, 4),   # 구독으로 나간 = 실청구 0 (환산치)
        "unknown_usd": round(unknown, 4),     # 컬럼 도입 전 데이터
    }

    cold = next((r for r in by_tier if r["reuse_tier"] == "cold"), None)
    if cold and cold["turns"]:
        base = cold["usd_per_turn"]
        saved = 0.0
        for r in by_tier:
            if r["reuse_tier"] in ("cold", "(미상)"):
                continue
            r["saved_usd"] = round(max(0.0, (base - r["usd_per_turn"]) * r["turns"]), 4)
            saved += r["saved_usd"]
        actual = sum(r["usd"] for r in by_tier)
        would_be = base * sum(r["turns"] for r in by_tier if r["reuse_tier"] != "(미상)")
        out["reuse"] = {
            "ok": True, "by_tier": by_tier,
            "baseline_usd_per_turn": base,
            "saved_usd": round(saved, 4),
            "actual_usd": round(actual, 4),
            "counterfactual_usd": round(would_be, 4),
            "saved_pct": round(saved / would_be * 100, 1) if would_be else 0.0,
        }
    else:
        out["reuse"] = {"ok": False, "by_tier": by_tier,
                        "reason": "기준선이 될 cold 턴이 없어 절감을 계산할 수 없습니다"}
    return out


def _user_patterns(conn, where: str, args: list, *,
                   limit: int = PAGE_SIZE, offset: int = 0) -> dict:
    """사용자가 실제로 어떻게 쓰는지 — 턴·세션·작업 성향.

    비용표는 "얼마"만 답한다. 쿼터를 조정하거나 수업 설계를 고치려면 "어떻게 쓰는가"가
    필요하다: 한 사람이 세션을 몇 개 만들고 그 안에서 몇 턴을 도는지, 설계형인지
    바로 생성형인지, 재사용이 걸리는 쪽인지.
    """
    out: dict = {"ok": True}
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(DISTINCT user_id) AS n FROM usage_turns "
                        f"WHERE {where}", args)
            out["total_users"] = _num((cur.fetchone() or {}).get("n"))
            out["offset"] = max(0, int(offset))
            out["page_size"] = int(limit)

            cur.execute(f"""
                SELECT user_id,
                       COUNT(*)                   AS turns,
                       COUNT(DISTINCT session_id) AS sessions,
                       COALESCE(SUM(weighted_tokens),0) AS weighted_tokens,
                       SUM(mode='design')         AS design_turns,
                       SUM(coding_type='blockly') AS blockly_turns,
                       SUM(reuse_tier='cold')     AS cold_turns,
                       MIN(CONVERT_TZ(ts,'+00:00','+09:00')) AS first_ts, MAX(CONVERT_TZ(ts,'+00:00','+09:00')) AS last_ts
                FROM usage_turns WHERE {where}
                GROUP BY user_id ORDER BY turns DESC LIMIT %s OFFSET %s
            """, args + [int(limit), max(0, int(offset))])
            users = []
            for r in (cur.fetchall() or []):
                turns = _num(r.get("turns"))
                sessions = _num(r.get("sessions")) or 1
                users.append({
                    "user_id": r.get("user_id") or "",
                    "turns": turns, "sessions": _num(r.get("sessions")),
                    "weighted_tokens": _num(r.get("weighted_tokens")),
                    "usd": round(_num(r.get("weighted_tokens")) / 1_000_000, 4),
                    "turns_per_session": round(turns / sessions, 1),
                    "design_ratio": round(_num(r.get("design_turns")) / turns * 100)
                                    if turns else 0,
                    "blockly_ratio": round(_num(r.get("blockly_turns")) / turns * 100)
                                     if turns else 0,
                    "cold_ratio": round(_num(r.get("cold_turns")) / turns * 100)
                                  if turns else 0,
                    "first_ts": str(r.get("first_ts") or ""),
                    "last_ts": str(r.get("last_ts") or ""),
                })
            out["users"] = users

            # 세션당 턴 수 분포 — "작품 하나에 몇 턴 드는가"
            cur.execute(f"""
                SELECT bucket, COUNT(*) AS sessions FROM (
                  SELECT session_id,
                         CASE WHEN COUNT(*)=1 THEN '1'
                              WHEN COUNT(*)<=3 THEN '2-3'
                              WHEN COUNT(*)<=6 THEN '4-6'
                              WHEN COUNT(*)<=10 THEN '7-10'
                              ELSE '11+' END AS bucket
                  FROM usage_turns WHERE {where} GROUP BY session_id
                ) t GROUP BY bucket
            """, args)
            order = ["1", "2-3", "4-6", "7-10", "11+"]
            got = {r["bucket"]: _num(r.get("sessions")) for r in (cur.fetchall() or [])}
            out["session_depth"] = [{"bucket": b, "sessions": got.get(b, 0)} for b in order]

            # 사용자 세그먼트 — 쿼터 상한을 어디에 둘지의 근거
            cur.execute(f"""
                SELECT bucket, COUNT(*) AS users FROM (
                  SELECT user_id,
                         CASE WHEN COUNT(*)<=3  THEN '1-3'
                              WHEN COUNT(*)<=10 THEN '4-10'
                              WHEN COUNT(*)<=30 THEN '11-30'
                              WHEN COUNT(*)<=70 THEN '31-70'
                              ELSE '71+' END AS bucket
                  FROM usage_turns WHERE {where} GROUP BY user_id
                ) t GROUP BY bucket
            """, args)
            order2 = ["1-3", "4-10", "11-30", "31-70", "71+"]
            got2 = {r["bucket"]: _num(r.get("users")) for r in (cur.fetchall() or [])}
            out["user_segments"] = [{"bucket": b, "users": got2.get(b, 0)} for b in order2]
        return out
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "users": [],
                "session_depth": [], "user_segments": [],
                "total_users": 0, "offset": 0, "page_size": int(limit)}


def _load_block(conn, where: str, args: list, s_utc: str, e_utc: str,
                user_id: str = "") -> dict:
    """부하 분석 블록 — 응답시간·동접·실패·유형별 단가·재사용 여력.

    ⚠ 이 블록이 실패해도 **비용 리포트는 살아야 한다.** 부하 지표는 부가 정보인데
      그것 때문에 청구 근거가 통째로 안 나오면 본말이 전도된다(프로젝트 블록과
      동일 원칙 · 2026-08-21 사고의 교훈).

    원장을 메모리로 가져와 파이썬에서 계산한다 — 동접(구간 겹침)과 분위수는 GROUP BY
    로 안 나오고, 수업 하루치가 수천 행이라 이 편이 정확하고 검증하기 쉽다.
    """
    import load_analysis as LA

    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT ts, started_at, duration_ms, ttft_ms, status, error_code,
                       replica, intent, phase, outcome, coding_type, mode,
                       reuse_tier, reuse_top1, direct_served, docs_restored,
                       weighted_tokens, user_id, session_id, user_agent, client_ip, mem_mb
                FROM usage_turns WHERE {where}
                ORDER BY ts LIMIT %s
            """, args + [LA.MAX_ROWS + 1])
            rows = cur.fetchall() or []

        truncated = len(rows) > LA.MAX_ROWS
        if truncated:
            rows = rows[:LA.MAX_ROWS]

        total = len(rows)
        ok_n = sum(1 for r in rows if (r.get("status") or "ok") == "ok")
        err_n = sum(1 for r in rows if (r.get("status") or "") == "error")
        abort_n = sum(1 for r in rows if (r.get("status") or "") == "aborted")

        # 레플리카별 — "3대에 고르게 갔나, 한 대만 느린가"
        by_replica: dict[str, dict] = {}
        for r in rows:
            k = (r.get("replica") or "").strip() or "(미상)"
            b = by_replica.setdefault(k, {"turns": 0, "durs": [], "fail": 0})
            b["turns"] += 1
            if (r.get("duration_ms") or 0) > 0:
                b["durs"].append(int(r["duration_ms"]))
            if (r.get("status") or "ok") != "ok":
                b["fail"] += 1
        replicas = []
        for k, b in sorted(by_replica.items()):
            st = LA.latency_stats(b["durs"])
            replicas.append({"replica": k, "turns": b["turns"], "fail": b["fail"],
                             "p50": st["p50"], "p95": st["p95"], "max": st["max"]})

        # 에러 코드별
        by_error: dict[str, int] = {}
        for r in rows:
            if (r.get("status") or "ok") != "ok":
                key = (r.get("error_code") or "").strip() or (r.get("status") or "?")
                by_error[key] = by_error.get(key, 0) + 1

        return {
            "ok": True,
            "truncated": truncated,
            "turns": total,
            "success": ok_n,
            "errors": err_n,
            "aborted": abort_n,
            "fail_pct": round((total - ok_n) / total * 100, 1) if total else 0.0,
            "duration": LA.latency_stats([r.get("duration_ms") or 0 for r in rows]),
            "ttft": LA.latency_stats([r.get("ttft_ms") or 0 for r in rows]),
            "concurrency": LA.concurrency_timeline(rows),
            "by_concurrency": LA.latency_by_concurrency(rows),
            "by_intent": LA.group_stats(rows, "intent", LA._INTENT_LABEL,
                                        USD_PER_WEIGHTED_MTOK),
            "by_outcome": LA.group_stats(rows, "outcome", LA._OUTCOME_LABEL,
                                         USD_PER_WEIGHTED_MTOK),
            "by_replica": replicas,
            "by_error": [{"code": k, "n": v}
                         for k, v in sorted(by_error.items(), key=lambda x: -x[1])],
            "reuse_curve": LA.reuse_threshold_curve(rows),
            # 가장 느린 턴 — 부하 분석에서 제일 먼저 파는 대상이다. 분포만으로는
            # "누가·무엇을 하다 느렸나"를 알 수 없어 원인 추적이 거기서 끊긴다.
            "slowest": _slowest_turns(rows),
            # 접속 환경 — 기기/브라우저별 분포. 사람과 스크립트를 갈라 준다.
            "clients": LA.client_breakdown(rows),
            # 서버 리소스 — 레플리카가 1g 상한에 다가가는지.
            "resources": LA.resource_usage(rows),
            # 턴 하나하나를 점으로 — 분포·군집·이상치는 요약 통계로는 안 보인다.
            "points": LA.turn_points(rows),
            "ops": _ops_block(conn, s_utc, e_utc, user_id),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "turns": 0,
                "duration": {}, "ttft": {}, "concurrency": {"buckets": [], "peak": 0},
                "by_concurrency": [], "by_intent": [], "by_outcome": [],
                "by_replica": [], "by_error": [], "reuse_curve": [],
                "slowest": [], "clients": {}, "points": {}, "resources": {},
                "ops": {}}


def _slowest_turns(rows: list, limit: int = 20) -> list:
    """느린 순 상위 턴. 시각은 KST 로 바꿔 담는다(원장은 UTC).

    started_at 도 함께 실어 둔다 — 동접이 안 잡힐 때 두 시각을 나란히 보면
    시간대 규약이 어긋났는지 바로 드러난다(2026-08-21 진단에서 필요했던 정보).
    """
    def _kst(v) -> str:
        from datetime import datetime as _dt, timedelta as _td
        if isinstance(v, _dt):
            return (v + _td(hours=9)).strftime("%Y-%m-%d %H:%M:%S")
        return str(v or "")

    top = sorted(rows, key=lambda r: int(r.get("duration_ms") or 0), reverse=True)
    out = []
    for r in top[:limit]:
        if not (r.get("duration_ms") or 0):
            break
        out.append({
            "end": _kst(r.get("ts")), "start": _kst(r.get("started_at")),
            "duration_ms": _num(r.get("duration_ms")), "ttft_ms": _num(r.get("ttft_ms")),
            "status": r.get("status") or "", "user_id": r.get("user_id") or "",
            "session_id": r.get("session_id") or "", "intent": r.get("intent") or "",
            "outcome": r.get("outcome") or "", "replica": r.get("replica") or "",
        })
    return out


def _ops_block(conn, s_utc: str, e_utc: str, user_id: str = "") -> dict:
    """운영 사건 집계 — 거절·차단·에러·재시작.

    ★ session_busy 가 여기에만 있다. /chat 은 세션 락을 잡기 **전에** 거절하므로
      usage_turns 에는 한 줄도 안 남는다. "40명 중 몇 명이 튕겼나"의 유일한 근거다.
    """
    where = "ts BETWEEN %s AND %s"
    args: list = [s_utc, e_utc]
    if user_id:
        where += " AND user_id = %s"
        args.append(user_id)
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT kind, COUNT(*) AS n, COUNT(DISTINCT user_id) AS users
                FROM ops_events WHERE {where}
                GROUP BY kind ORDER BY n DESC
            """, args)
            by_kind = [{"kind": r["kind"], "n": _num(r.get("n")),
                        "users": _num(r.get("users"))} for r in (cur.fetchall() or [])]

            cur.execute(f"""
                SELECT DATE_FORMAT(CONVERT_TZ(ts,'+00:00','+09:00'),'%%Y-%%m-%%d %%H:%%i') AS t,
                       kind, COUNT(*) AS n
                FROM ops_events WHERE {where}
                GROUP BY t, kind ORDER BY t
            """, args)
            timeline = [{"t": r["t"], "kind": r["kind"], "n": _num(r.get("n"))}
                        for r in (cur.fetchall() or [])]

            cur.execute(f"""
                SELECT DATE_FORMAT(CONVERT_TZ(ts,'+00:00','+09:00'),'%%Y-%%m-%%d %%H:%%i:%%s') AS t,
                       kind, code, user_id, session_id, replica, detail
                FROM ops_events WHERE {where}
                ORDER BY ts DESC LIMIT 100
            """, args)
            recent = cur.fetchall() or []
        return {"ok": True, "by_kind": by_kind, "timeline": timeline,
                "recent": recent,
                "total": sum(r["n"] for r in by_kind)}
    except Exception as e:
        # ops_events 가 아직 없는 배포(스키마 미적용)에서도 리포트는 떠야 한다.
        return {"ok": False, "error": str(e)[:200], "by_kind": [], "timeline": [],
                "recent": [], "total": 0}


def usage_report(conn, *, start: str = "", end: str = "", user_id: str = "",
                 limit_users: int = PAGE_SIZE, krw_per_usd: float = _DEF_KRW,
                 project_limit: int = PAGE_SIZE, project_offset: int = 0,
                 project_q: str = "", user_offset: int = 0) -> dict:
    """기간 사용량을 집계해 비용까지 계산한 dict 를 돌려준다."""
    s, e = _day_bounds(start, end)
    # 화면·응답에는 KST 경계를 그대로 보여 주되(사용자가 입력한 값), 질의에는 UTC 로
    # 바꿔 넘긴다. 컬럼에 CONVERT_TZ 를 씌우면 ts 인덱스를 못 타므로 경계 쪽을 옮긴다.
    s_utc, e_utc = kst_to_utc(s), kst_to_utc(e)
    where = "ts BETWEEN %s AND %s"
    args: list = [s_utc, e_utc]
    if user_id:
        where += " AND user_id = %s"
        args.append(user_id)

    def usd(w) -> float:
        return round(_num(w) / 1_000_000 * USD_PER_WEIGHTED_MTOK, 4)

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COUNT(*) AS turns,
                   COUNT(DISTINCT user_id)    AS users,
                   COUNT(DISTINCT session_id) AS sessions,
                   COALESCE(SUM(input_tokens),0)          AS input_tokens,
                   COALESCE(SUM(output_tokens),0)         AS output_tokens,
                   COALESCE(SUM(cache_read_tokens),0)     AS cache_read_tokens,
                   COALESCE(SUM(cache_creation_tokens),0) AS cache_creation_tokens,
                   COALESCE(SUM(weighted_tokens),0)       AS weighted_tokens,
                   MIN(CONVERT_TZ(ts,'+00:00','+09:00')) AS first_ts, MAX(CONVERT_TZ(ts,'+00:00','+09:00')) AS last_ts
            FROM usage_turns WHERE {where}
        """, args)
        tot = cur.fetchone() or {}

        cur.execute(f"""
            SELECT COALESCE(NULLIF(mode,''),'(미지정)') AS mode,
                   COALESCE(NULLIF(coding_type,''),'(미지정)') AS coding_type,
                   COUNT(*) AS turns, COALESCE(SUM(weighted_tokens),0) AS weighted_tokens
            FROM usage_turns WHERE {where}
            GROUP BY mode, coding_type ORDER BY weighted_tokens DESC
        """, args)
        by_kind = cur.fetchall() or []

        cur.execute(f"""
            SELECT user_id, COUNT(*) AS turns,
                   COALESCE(SUM(weighted_tokens),0) AS weighted_tokens
            FROM usage_turns WHERE {where}
            GROUP BY user_id ORDER BY weighted_tokens DESC LIMIT %s
        """, args + [int(limit_users)])
        top_users = cur.fetchall() or []

        cur.execute(f"""
            SELECT DATE_FORMAT(CONVERT_TZ(ts,'+00:00','+09:00'),'%%Y-%%m-%%d %%H:00') AS hour,
                   COUNT(*) AS turns,
                   COUNT(DISTINCT user_id) AS users,
                   COALESCE(SUM(weighted_tokens),0) AS weighted_tokens
            FROM usage_turns WHERE {where}
            GROUP BY hour ORDER BY hour
        """, args)
        by_hour = cur.fetchall() or []

        # 일자별 — 기간 조회 목록 페이지(/reports)의 원천.
        # 날짜마다 따로 질의하면 한 달 조회에 30 왕복이 된다. 한 번에 접는다.
        cur.execute(f"""
            SELECT DATE_FORMAT(CONVERT_TZ(ts,'+00:00','+09:00'),'%%Y-%%m-%%d') AS day,
                   COUNT(*) AS turns,
                   COUNT(DISTINCT user_id)    AS users,
                   COUNT(DISTINCT session_id) AS sessions,
                   COALESCE(SUM(input_tokens),0)          AS input_tokens,
                   COALESCE(SUM(output_tokens),0)         AS output_tokens,
                   COALESCE(SUM(cache_read_tokens),0)     AS cache_read_tokens,
                   COALESCE(SUM(cache_creation_tokens),0) AS cache_creation_tokens,
                   COALESCE(SUM(weighted_tokens),0)       AS weighted_tokens
            FROM usage_turns WHERE {where}
            GROUP BY day ORDER BY day DESC
        """, args)
        by_day = cur.fetchall() or []

    analysis = _billing_and_reuse(conn, where, args)
    patterns = _user_patterns(conn, where, args, limit=limit_users, offset=user_offset)
    # 부하 분석(응답시간·동접·실패·유형별 단가). 실패해도 비용 리포트는 그대로 나온다.
    load = _load_block(conn, where, args, s_utc, e_utc, user_id)
    projects = _projects_block(conn, s, e, user_id, limit=project_limit,
                               offset=project_offset, q=project_q)
    # 일자 행에 그날 만들어진 프로젝트 수를 합류 — 목록 페이지에서 한 표로 보이게.
    for row in by_day:
        row["projects"] = _num(projects["per_day"].get(str(row.get("day"))))

    turns = _num(tot.get("turns"))
    w = _num(tot.get("weighted_tokens"))
    for row in (*by_kind, *top_users, *by_hour, *by_day):
        # Decimal 을 여기서 걷어내야 이후 계산·JSON 직렬화가 모두 안전해진다.
        for k in ("weighted_tokens", "turns", "users", "sessions", "input_tokens",
                  "output_tokens", "cache_read_tokens", "cache_creation_tokens"):
            if k in row:
                row[k] = _num(row[k])
        row["usd"] = usd(row["weighted_tokens"])
        row["krw"] = round(row["usd"] * krw_per_usd)

    return {
        "ok": True,
        "period": {"start": s, "end": e,
                   "first_turn": str(tot.get("first_ts") or ""),
                   "last_turn": str(tot.get("last_ts") or "")},
        "assumptions": {
            "model": "claude-haiku-4-5",
            "usd_per_weighted_mtok": USD_PER_WEIGHTED_MTOK,
            "krw_per_usd": krw_per_usd,
            "note": "weighted_tokens 는 기록 시점 가중치로 계산된 값이며 "
                    "Haiku 4.5 단가 비율과 일치 → weighted/1e6 = USD",
        },
        "totals": {
            "turns": turns,
            "users": _num(tot.get("users")),
            "sessions": _num(tot.get("sessions")),
            "input_tokens": _num(tot.get("input_tokens")),
            "output_tokens": _num(tot.get("output_tokens")),
            "cache_read_tokens": _num(tot.get("cache_read_tokens")),
            "cache_creation_tokens": _num(tot.get("cache_creation_tokens")),
            "weighted_tokens": w,
            "usd": usd(w),
            "krw": round(usd(w) * krw_per_usd),
            "usd_per_turn": round(usd(w) / turns, 4) if turns else 0.0,
            "turns_per_user": round(turns / (_num(tot.get("users")) or 1), 1) if turns else 0.0,
        },
        "by_kind": by_kind,
        "top_users": top_users,
        "by_hour": by_hour,
        "by_day": by_day,
        "projects": projects,
        # 부하 관측 — "얼마나 버텼나". 비용 블록과 목적이 다르다.
        "load": load,
        "billing": analysis.get("billing") or {},
        "reuse": analysis.get("reuse") or {},
        "patterns": patterns,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 일별 리포트 확정본 (usage_reports) — 파일이 아니라 DB 가 원천
# ──────────────────────────────────────────────────────────────────────────────
#
# 시간 규약:
#   day        = KST 영업일 라벨(타임스탬프 아님 — "8월 22일 수업"이라는 회계 날짜)
#   *_at_utc   = 실제 시각. UTC 로 저장하고 화면에서 KST 로 변환한다.
#                서버 로케일·컨테이너 TZ 가 바뀌어도 값이 흔들리지 않게.

_REPORT_SCALARS = ("turns", "users", "sessions", "input_tokens", "output_tokens",
                   "cache_read_tokens", "cache_creation_tokens", "weighted_tokens")


def _utc_now_str() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def save_report(conn, day: str, report: dict, *, llm_mode: str = "",
                insight: dict | None = None) -> dict:
    """그날 리포트를 굳힌다(멱등 — 다시 돌리면 덮어쓴다).

    insight 를 주지 않으면 **기존 분석을 지우지 않는다.** 스냅샷 재생성이 어제 만든
    AI 분석을 조용히 날리면 안 되기 때문이다.
    """
    tot = report.get("totals") or {}
    projects = report.get("projects") or {}
    cols = {
        "day": day,
        "generated_at_utc": _utc_now_str(),
        **{k: _num(tot.get(k)) for k in _REPORT_SCALARS},
        "projects": _num(projects.get("created")),
        "usd": round(float(tot.get("usd") or 0), 4),
        "krw": _num(tot.get("krw")),
        "llm_mode": (llm_mode or "")[:8],
        "payload": json.dumps(report, ensure_ascii=False, default=str),
    }
    names = list(cols)
    placeholders = ",".join(["%s"] * len(names))
    updates = ",".join(f"{n}=VALUES({n})" for n in names if n != "day")

    sql = (f"INSERT INTO usage_reports({','.join(names)}) VALUES({placeholders}) "
           f"ON DUPLICATE KEY UPDATE {updates}")
    with conn.cursor() as cur:
        cur.execute(sql, [cols[n] for n in names])
        if insight and insight.get("ok"):
            cur.execute(
                "UPDATE usage_reports SET insight=%s, insight_model=%s, "
                "insight_at_utc=%s WHERE day=%s",
                (insight.get("text", ""), (insight.get("model") or "")[:64],
                 _utc_now_str(), day))
    return {"ok": True, "day": day, "usd": cols["usd"], "turns": cols["turns"]}


def save_report_insight(conn, day: str, insight: dict) -> dict:
    """AI 분석만 갱신한다(버튼으로 재생성할 때)."""
    if not (insight or {}).get("ok"):
        return {"ok": False, "error": (insight or {}).get("error", "빈 분석")}
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE usage_reports SET insight=%s, insight_model=%s, insight_at_utc=%s "
            "WHERE day=%s",
            (insight.get("text", ""), (insight.get("model") or "")[:64],
             _utc_now_str(), day))
        affected = cur.rowcount
    return {"ok": True, "day": day, "stored": affected > 0}


def get_report(conn, day: str) -> dict | None:
    """굳혀 둔 그날 확정본(payload 포함). 없으면 None."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM usage_reports WHERE day=%s", (day,))
        row = cur.fetchone()
    if not row:
        return None
    return _hydrate_report_row(row, with_payload=True)


def list_reports(conn, *, start: str = "", end: str = "", limit: int = 400) -> list[dict]:
    """기간 내 확정본 목록(스칼라만 — 목록 화면은 payload 가 필요 없다)."""
    where, args = [], []
    if start:
        where.append("day >= %s")
        args.append(start[:10])
    if end:
        where.append("day <= %s")
        args.append(end[:10])
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT day, generated_at_utc, {','.join(_REPORT_SCALARS)}, projects,
                       usd, krw, llm_mode, insight_at_utc,
                       CHAR_LENGTH(COALESCE(insight,'')) AS insight_len
                FROM usage_reports {clause} ORDER BY day DESC LIMIT %s""",
            args + [int(limit)])
        rows = cur.fetchall() or []
    return [_hydrate_report_row(r, with_payload=False) for r in rows]


def _hydrate_report_row(row: dict, *, with_payload: bool) -> dict:
    out = {
        "day": str(row.get("day") or ""),
        "generated_at_utc": str(row.get("generated_at_utc") or ""),
        "projects": _num(row.get("projects")),
        "usd": round(float(row.get("usd") or 0), 4),
        "krw": _num(row.get("krw")),
        "llm_mode": row.get("llm_mode") or "",
        "insight_at_utc": str(row.get("insight_at_utc") or ""),
        **{k: _num(row.get(k)) for k in _REPORT_SCALARS},
    }
    if with_payload:
        raw = row.get("payload")
        try:
            out["payload"] = json.loads(raw) if isinstance(raw, (str, bytes)) else (raw or {})
        except Exception:
            out["payload"] = {}
        out["insight"] = row.get("insight") or ""
        out["insight_model"] = row.get("insight_model") or ""
    else:
        out["has_insight"] = _num(row.get("insight_len")) > 0
    return out
