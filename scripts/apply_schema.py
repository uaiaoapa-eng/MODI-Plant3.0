"""deploy/schema.sql 을 실행 중인 MySQL 에 idempotent 하게 재적용한다 (#149).

배경: `deploy/schema.sql` 은 MySQL 공식 이미지의 `docker-entrypoint-initdb.d` 로
**최초 컨테이너 기동 시에만** 자동 적용된다(deploy/README.md 참조). 이미 떠서 데이터가
쌓인 `mysql_data` 볼륨에 새 테이블(예: `usage_turns`, #133)이 추가된 스키마를 반영하려면
지금까지는 `down -v`(전체 데이터 삭제)가 유일한 경로였다 — 위험하고 되돌릴 수 없다.

이 스크립트는 `deploy/schema.sql` 전체를 세미콜론 기준으로 순차 실행해, initdb.d 가
하는 일을 **실행 중인 서버에 안전하게 재현**한다. 전 문이 `CREATE DATABASE IF NOT EXISTS`
/ `CREATE TABLE IF NOT EXISTS` 뿐이라(설계 §Non-goals: ALTER 마이그레이션 미도입) 몇 번을
재실행해도 기존 테이블·데이터에 영향이 없다(멱등).

사용:
    python scripts/apply_schema.py                     # deploy/schema.sql (기본)
    python scripts/apply_schema.py --file some.sql     # 다른 스키마 파일(테스트용)

연결 파라미터는 store_mysql._conn_params() 와 동일한 env(DATABASE_URL 또는 MYSQL_*)를
따른다. 단, schema.sql 자신이 `CREATE DATABASE IF NOT EXISTS` + `USE`로 시작하므로
(docker-entrypoint-initdb.d 와 동일한 전제) 이 스크립트는 특정 database 를 미리 선택하지
않고 연결한 뒤, 파일에 적힌 순서 그대로 실행한다 — DB 생성 권한이 있는 사용자(예: 운영
스택의 root, `deploy/README.md` 참조)로 실행할 것.

Non-goals(#149): 앱에서 MySQL 직접 접근 금지(이 스크립트는 앱 런타임 경로가 아니라 운영자가
호스트/컨테이너에서 실행하는 배포 유틸리티) · usage writeback fail-open 변경 · ALTER 마이그
레이션 프레임워크 도입 · usage_turns DDL 변경 — 전부 건드리지 않는다.
"""
from __future__ import annotations

import argparse
import os
import sys

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import store_mysql as M  # noqa: E402

_REPO_ROOT = os.path.dirname(_SCRIPTS_DIR)
DEFAULT_SCHEMA_PATH = os.path.join(_REPO_ROOT, "deploy", "schema.sql")


def split_statements(sql_text: str) -> list[str]:
    """`--` 라인 주석을 제거하고 세미콜론 기준으로 개별 DDL 문으로 쪼갠다.

    `deploy/schema.sql` 은 CREATE DATABASE/TABLE 문과 `--` 라인 주석만 포함하고
    문자열 리터럴 내부에 세미콜론이 없으므로 단순 split 으로 충분하다(범용 SQL 파서 불필요
    — 이 스크립트의 유일한 입력은 이 리포의 schema.sql 뿐).
    """
    lines = [
        ln for ln in sql_text.splitlines()
        if ln.strip() and not ln.strip().startswith("--")
    ]
    cleaned = "\n".join(lines)
    return [s.strip() for s in cleaned.split(";") if s.strip()]


def _connect_no_database():
    """database 미지정 연결.

    schema.sql 첫 문이 `CREATE DATABASE IF NOT EXISTS`라, 대상 DB가 아직 없는 완전
    신규 볼륨에서도 동작하려면 연결 시점에 특정 database 를 선택하면 안 된다(선택하려는
    DB가 없으면 연결 자체가 실패한다). 이후 문(`USE edu_agent` 등)이 같은 커넥션 안에서
    DB를 선택한다 — docker-entrypoint-initdb.d 가 하는 것과 동일한 순서.
    """
    import pymysql

    params = dict(M._conn_params())
    params.pop("database", None)
    return pymysql.connect(
        charset="utf8mb4", autocommit=False,
        cursorclass=pymysql.cursors.DictCursor, **params,
    )


# 기존 배포에 나중에 추가된 컬럼 — CREATE TABLE IF NOT EXISTS 는 **이미 있는 테이블을
# 손대지 않으므로**, 신규 설치에만 반영되고 운영 DB 에는 영원히 안 생긴다.
# (2026-08-21 usage_turns 부재 사고와 정확히 같은 함정이다 — 이번엔 컬럼 단위로 반복된다)
#
# 각 항목: (테이블, 컬럼, ALTER 로 붙일 정의, 함께 만들 인덱스 SQL 또는 "")
_ADD_COLUMNS = [
    ("usage_turns", "llm_mode", "VARCHAR(20) NOT NULL DEFAULT ''",
     "ADD INDEX idx_mode_ts (llm_mode, ts)"),
    ("usage_turns", "reuse_tier", "VARCHAR(16) NOT NULL DEFAULT ''",
     "ADD INDEX idx_tier_ts (reuse_tier, ts)"),
    # ── 부하 관측(2026-08-22 40명 동시 수업) ──
    # 이 값들은 **소급이 안 된다.** 수업 당일 컬럼이 없으면 그날의 응답시간·동접·실패율은
    # 영원히 알 수 없다. 그래서 화면보다 컬럼을 먼저 넣는다.
    ("usage_turns", "started_at", "DATETIME NULL", "ADD INDEX idx_started (started_at)"),
    ("usage_turns", "duration_ms", "INT NOT NULL DEFAULT 0", ""),
    ("usage_turns", "ttft_ms", "INT NOT NULL DEFAULT 0", ""),
    ("usage_turns", "status", "VARCHAR(12) NOT NULL DEFAULT 'ok'",
     "ADD INDEX idx_status_ts (status, ts)"),
    ("usage_turns", "error_code", "VARCHAR(32) NOT NULL DEFAULT ''", ""),
    ("usage_turns", "replica", "VARCHAR(24) NOT NULL DEFAULT ''", ""),
    # ── 질문 유형·결과 ──
    ("usage_turns", "intent", "VARCHAR(24) NOT NULL DEFAULT ''",
     "ADD INDEX idx_intent_ts (intent, ts)"),
    ("usage_turns", "phase", "VARCHAR(12) NOT NULL DEFAULT ''", ""),
    ("usage_turns", "outcome", "VARCHAR(16) NOT NULL DEFAULT ''", ""),
    # ── 비용 절감 분석 ──
    ("usage_turns", "reuse_top1", "FLOAT NOT NULL DEFAULT 0", ""),
    ("usage_turns", "direct_served", "TINYINT NOT NULL DEFAULT 0", ""),
    ("usage_turns", "docs_restored", "INT NOT NULL DEFAULT 0", ""),
    # ── 접속 환경 ──
    ("usage_turns", "user_agent", "VARCHAR(255) NOT NULL DEFAULT ''", ""),
    ("usage_turns", "client_ip", "VARCHAR(45) NOT NULL DEFAULT ''", ""),
    ("usage_turns", "mem_mb", "INT NOT NULL DEFAULT 0", ""),
]


def ensure_columns(conn) -> list[str]:
    """빠진 컬럼만 골라 ALTER 한다(멱등). 반환: 실제로 추가한 것들.

    information_schema 를 먼저 보므로 몇 번을 돌려도 안전하다. schema.sql 안에서
    처리하지 않는 이유는 split_statements() 가 세미콜론으로 쪼개서
    CREATE PROCEDURE 같은 복합문을 못 다루기 때문이다.
    """
    added = []
    with conn.cursor() as cur:
        for table, column, definition, index_sql in _ADD_COLUMNS:
            cur.execute(
                """SELECT COUNT(*) AS n FROM information_schema.COLUMNS
                   WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s""",
                (table, column))
            row = cur.fetchone()
            n = (row or {}).get("n", 0) if isinstance(row, dict) else (row or [0])[0]
            if n:
                continue
            tail = f", {index_sql}" if index_sql else ""
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}{tail}")
            added.append(f"{table}.{column}")
    return added


def apply_schema(schema_path: str = DEFAULT_SCHEMA_PATH, conn=None) -> int:
    """schema_path 의 DDL 문을 순차 실행한다. 반환: 실행한 문 개수.

    conn 을 넘기면 그 연결을 그대로 재사용하고 commit/close 는 호출측 책임으로 남긴다
    (테스트에서 fake 커넥션을 주입하기 위함). conn 을 생략하면 새로 연결해 전체를
    하나의 트랜잭션으로 commit 하고 닫는다.
    """
    with open(schema_path, "r", encoding="utf-8") as f:
        sql_text = f.read()
    statements = split_statements(sql_text)

    owns_conn = conn is None
    if owns_conn:
        conn = _connect_no_database()
    try:
        with conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)
        added = ensure_columns(conn)
        if owns_conn:
            conn.commit()
        for col in added:
            print(f"added column {col}")
    except Exception:
        if owns_conn:
            conn.rollback()
        raise
    finally:
        if owns_conn:
            conn.close()
    return len(statements)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--file", default=DEFAULT_SCHEMA_PATH,
        help="재적용할 스키마 파일 경로(기본: deploy/schema.sql)",
    )
    args = parser.parse_args(argv)
    n = apply_schema(args.file)
    print(f"applied {n} statement(s) from {args.file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
