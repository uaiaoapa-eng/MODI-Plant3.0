"""부하 관측 원장 회귀 테스트 — 시간대 규약 · 새 컬럼 · 운영 사건.

2026-08-21 실측으로 확인된 사고를 고정한다:

    운영 서버에 **KST 20:11:51** 에 발생한 턴이 리포트에 **"11시"** 로 표시됐다.
    ts 는 UTC 로 저장되는데(앱이 +09:00 오프셋을 보내고 MySQL 이 세션 타임존으로
    변환) 조회 쪽 DATE_FORMAT(ts,...) 이 변환 없이 라벨을 붙였기 때문이다.

    저장을 UTC 로 두는 건 의도한 규약이다(서버 로케일이 바뀌어도 값이 안 흔들린다).
    깨진 쪽은 **조회**다. 그래서 여기서는 "경계는 UTC 로 옮기고, 라벨은 KST 로
    되돌린다"는 두 규칙을 각각 고정한다.

server 를 임포트하지 않으므로 langfuse 없이도 돈다(원장 계층만 검증).
"""
from __future__ import annotations

import os
import sys

import pytest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import store_mysql as M  # noqa: E402


# ──────────────────────────────────────────────────────────────────────────────
# 시간대 — 저장 UTC / 표시 KST
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kst,utc", [
    ("2026-08-22 09:00:00", "2026-08-22 00:00:00"),   # 수업 시작(오전 9시)
    ("2026-08-22 00:00:00", "2026-08-21 15:00:00"),   # KST 자정 → 전날 UTC 15시
    ("2026-08-22 23:59:59", "2026-08-22 14:59:59"),
])
def test_kst_to_utc(kst, utc):
    assert M.kst_to_utc(kst) == utc


def test_kst_to_utc_is_fail_open_on_garbage():
    """경계 변환이 실패해도 조회가 통째로 죽으면 안 된다 — 원본을 그대로 흘린다."""
    assert M.kst_to_utc("") == ""
    assert M.kst_to_utc("2026-08-22") == "2026-08-22"


def test_morning_class_boundary_is_not_lost():
    """★ 오전 수업의 경계 사고를 고정한다.

    KST 08:30(수업 전 준비)은 UTC 로 **전날 23:30** 이다. 경계를 KST 문자열 그대로
    넘기면 이 턴이 조회 범위 밖으로 새어 '전날'로 잡힌다. 변환 후에는 포함된다.
    """
    lo = M.kst_to_utc("2026-08-22 00:00:00")
    hi = M.kst_to_utc("2026-08-22 23:59:59")
    turn_utc = "2026-08-21 23:30:00"          # = KST 08:30
    assert lo <= turn_utc <= hi


# ──────────────────────────────────────────────────────────────────────────────
# 질의가 실제로 변환을 쓰는가 (문자열 검증 — DB 없이)
# ──────────────────────────────────────────────────────────────────────────────

def _code_lines(name: str = "store_mysql.py") -> list[str]:
    """주석을 걷어낸 코드 줄만. 주석에 적어 둔 설명 문구가 검사에 걸리면 안 된다."""
    src = open(os.path.join(_SCRIPTS, name), encoding="utf-8").read()
    return [ln for ln in src.splitlines() if not ln.strip().startswith("#")]


def test_source_labels_hours_and_days_in_kst():
    """DATE_FORMAT 이 ts 에 바로 붙으면 9시간 어긋난다 — CONVERT_TZ 를 거쳐야 한다."""
    code = "\n".join(_code_lines())
    assert "DATE_FORMAT(ts," not in code, (
        "ts 에 DATE_FORMAT 을 직접 붙였다 — ts 는 UTC 라 라벨이 9시간 어긋난다. "
        "CONVERT_TZ(ts,'+00:00','+09:00') 를 거쳐야 한다")
    assert "DATE_FORMAT(created_at," not in code, (
        "sessions.created_at 도 CURRENT_TIMESTAMP(=UTC)다 — 같은 문제")


def test_source_uses_offsets_not_zone_names():
    """지역명은 MySQL 타임존 테이블이 없으면 **조용히 NULL** 을 준다 — 오프셋을 쓴다."""
    code = "\n".join(_code_lines())
    assert "CONVERT_TZ(ts,'+00:00','+09:00')" in code
    bad = [ln for ln in _code_lines() if "CONVERT_TZ" in ln and "Asia/Seoul" in ln]
    assert not bad, f"CONVERT_TZ 에 지역명을 썼다(타임존 테이블 없으면 NULL): {bad}"


# ──────────────────────────────────────────────────────────────────────────────
# 새 컬럼 적재
# ──────────────────────────────────────────────────────────────────────────────

class _Cur:
    def __init__(self, sink): self._sink = sink
    def __enter__(self): return self
    def __exit__(self, *e): return False
    def execute(self, sql, params=()): self._sink.append((sql, params))


class _Conn:
    def __init__(self): self.calls = []
    def cursor(self): return _Cur(self.calls)


def _insert(**over) -> tuple:
    conn = _Conn()
    row = {"ts": "2026-08-22T09:30:00+09:00", "subject": "u:s1",
           "started_at": "2026-08-22T09:28:30+09:00", "duration_ms": 90000,
           "ttft_ms": 3200, "status": "ok", "error_code": "", "replica": "edu-agent-2",
           "intent": "implement_request", "phase": "implement", "outcome": "code",
           "reuse_top1": 0.58, "direct_served": 0, "docs_restored": 3}
    row.update(over)
    M.insert_usage_turn(conn, row)
    sql, params = conn.calls[0]
    return sql, params


def test_insert_carries_load_columns():
    sql, params = _insert()
    for col in ("started_at", "duration_ms", "ttft_ms", "status", "error_code",
                "replica", "intent", "phase", "outcome",
                "reuse_top1", "direct_served", "docs_restored"):
        assert col in sql, f"{col} 이 INSERT 에서 빠졌다"
    assert 90000 in params and 3200 in params
    assert "edu-agent-2" in params and "implement_request" in params


def test_blank_started_at_becomes_null_not_zero_date():
    """★ 빈 문자열을 넣으면 MySQL 이 '0000-00-00' 으로 받아 동접 곡선이 1970년으로 튄다."""
    _, params = _insert(started_at="")
    assert None in params, "started_at 이 NULL 로 안 들어갔다"


def test_missing_load_fields_default_safely():
    """구버전 앱(관측 필드 미전송)이 보내도 기록 자체는 살아야 한다."""
    conn = _Conn()
    M.insert_usage_turn(conn, {"ts": "2026-08-22T09:30:00+09:00", "subject": "u:s1"})
    _, params = conn.calls[0]
    assert "ok" in params          # status 기본값
    assert None in params          # started_at


def test_oversized_values_are_truncated_not_rejected():
    """길이 초과로 INSERT 가 죽으면 그 턴 기록이 통째로 사라진다 — 잘라서라도 남긴다."""
    _, params = _insert(replica="x" * 100, intent="y" * 100, error_code="z" * 100)
    assert all(len(p) <= 100 for p in params if isinstance(p, str))
    assert "x" * 25 not in params


def test_reuse_top1_is_stored_as_float():
    """임계값 곡선의 원천이라 반올림·형변환이 어긋나면 절감 추정이 틀어진다."""
    _, params = _insert(reuse_top1="0.58")
    assert 0.58 in params


# ──────────────────────────────────────────────────────────────────────────────
# 운영 사건 — 턴이 안 생기는 사건
# ──────────────────────────────────────────────────────────────────────────────

def test_insert_ops_event_writes_all_columns():
    conn = _Conn()
    M.insert_ops_event(conn, {"ts": "2026-08-22T09:31:02+09:00", "kind": "session_busy",
                              "user_id": "stu-1", "session_id": "s1",
                              "replica": "edu-agent-3", "detail": "mode=design"})
    sql, params = conn.calls[0]
    assert sql.strip().startswith("INSERT INTO ops_events")
    assert "session_busy" in params and "edu-agent-3" in params


def test_ops_event_truncates_long_detail():
    conn = _Conn()
    M.insert_ops_event(conn, {"ts": "t", "kind": "error", "detail": "e" * 900})
    _, params = conn.calls[0]
    assert all(len(p) <= 255 for p in params if isinstance(p, str))


# ──────────────────────────────────────────────────────────────────────────────
# 스키마 — 소급 불가 컬럼이 배포에 실제로 들어가는가
# ──────────────────────────────────────────────────────────────────────────────

def test_ensure_columns_covers_every_new_load_column():
    """★ schema.sql 의 CREATE TABLE IF NOT EXISTS 는 **기존 테이블에 컬럼을 안 더한다.**

    운영 DB 에는 usage_turns 가 이미 있으므로, ensure_columns 에 등재되지 않은 컬럼은
    영원히 생기지 않는다. 그리고 이 데이터는 **소급이 안 된다** — 수업 당일 컬럼이
    없으면 그날의 응답시간·동접·실패율은 다시 얻을 수 없다.
    """
    import apply_schema
    listed = {c for t, c, _, _ in apply_schema._ADD_COLUMNS if t == "usage_turns"}
    required = {"started_at", "duration_ms", "ttft_ms", "status", "error_code",
                "replica", "intent", "phase", "outcome",
                "reuse_top1", "direct_served", "docs_restored"}
    assert required <= listed, f"ensure_columns 누락: {sorted(required - listed)}"


def test_schema_declares_ops_events():
    """ops_events 는 새 테이블이라 CREATE TABLE IF NOT EXISTS 로 자동 생성된다."""
    schema = open(os.path.join(os.path.dirname(_SCRIPTS), "deploy", "schema.sql"),
                  encoding="utf-8").read()
    assert "CREATE TABLE IF NOT EXISTS ops_events" in schema


def test_schema_has_no_compound_statements():
    """apply_schema 는 세미콜론으로 쪼개므로 CREATE PROCEDURE 등이 들어가면 깨진다."""
    schema = open(os.path.join(os.path.dirname(_SCRIPTS), "deploy", "schema.sql"),
                  encoding="utf-8").read()
    body = "\n".join(ln for ln in schema.splitlines() if not ln.strip().startswith("--"))
    assert "CREATE PROCEDURE" not in body.upper()
    assert "DELIMITER" not in body.upper()


# ──────────────────────────────────────────────────────────────────────────────
# 원장에 넣는 시각은 오프셋 없는 UTC 여야 한다
# ──────────────────────────────────────────────────────────────────────────────

def test_writeback_sends_offset_free_utc():
    """★ 2026-08-21 실측 사고를 고정한다.

    같은 형식(tz-aware ISO '+09:00')으로 보낸 두 컬럼이 서로 다르게 저장됐다:

        ts         → 2026-08-21 11:50:32   (UTC 로 변환됨)
        started_at → 2026-08-21 20:50:30   (KST 그대로)

    시작이 종료보다 9시간 뒤가 되어 **동접 계산이 통째로 0** 이 됐고, 곡선이 비어도
    예외가 안 나서 '한산했다'로 오독될 뻔했다. MySQL 의 암묵적 오프셋 해석에 기대는
    한 이 어긋남을 막을 수 없으므로, 앱이 UTC 로 확정해 보낸다.
    """
    root = os.path.dirname(_SCRIPTS)
    src = open(os.path.join(root, "server.py"), encoding="utf-8").read()
    code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    assert "datetime.now(_KST).isoformat()" not in code, (
        "원장 시각을 tz-aware ISO 로 보내고 있다 — MySQL 의 암묵 변환이 컬럼마다 "
        "달라져 시작>종료가 된다. _utc_stamp() 로 오프셋 없는 UTC 를 보내라")
    assert "def _utc_stamp()" in code


def test_start_after_end_is_recovered_not_dropped():
    """시간대가 어긋난 행이 있어도 동접이 0 으로 무너지지 않아야 한다.

    duration 폴백은 ts 한 컬럼에서만 파생되므로 시간대 규약과 무관하게 항상 옳다.
    """
    import load_analysis as LA
    from datetime import datetime as _dt
    rows = [{"ts": _dt(2026, 8, 21, 11, 50, 32),        # UTC
             "started_at": _dt(2026, 8, 21, 20, 50, 30),  # KST (어긋남)
             "duration_ms": 2711, "status": "ok"}]
    out = LA.concurrency_timeline(rows)
    assert out["measured"] == 1, "어긋난 행을 버려 동접이 0 이 됐다"
    assert out["skipped"] == 0


def test_skipped_rows_are_counted_not_hidden():
    """계산에서 빠진 행은 반드시 세어 돌려줘야 한다 — 빈 곡선과 '한산함'은 다르다."""
    import load_analysis as LA
    out = LA.concurrency_timeline([{"ts": None, "duration_ms": 0, "status": "ok"}])
    assert out["skipped"] == 1 and out["measured"] == 0


def test_project_list_timestamps_are_converted_to_kst():
    """★ 2026-08-21 실측 회귀: 22:33 에 만든 프로젝트가 목록에 13:33 으로 찍혔다.

    집계(per_day)와 필터 경계는 변환했는데 **목록에 표시되는 created_at 만** 빠져서
    프로젝트 목록만 9시간 뒤처져 보였다. 같은 화면 안에서 어떤 숫자는 맞고 어떤
    숫자는 틀린 상태라 알아채기 어렵다.
    """
    code = "\n".join(_code_lines())
    assert "CONVERT_TZ(created_at,'+00:00','+09:00') AS created_at" in code, (
        "프로젝트 목록의 created_at 이 UTC 그대로 나간다 — 화면에 9시간 뒤처져 표시된다")
    assert "CONVERT_TZ(updated_at,'+00:00','+09:00') AS updated_at" in code


def test_project_list_orders_by_raw_column_not_converted():
    """정렬은 원본 컬럼으로 — 변환은 표시용이고, 컬럼에 함수를 씌우면 인덱스를 못 탄다."""
    code = "\n".join(_code_lines())
    assert "ORDER BY created_at DESC" in code
    assert "ORDER BY CONVERT_TZ" not in code
