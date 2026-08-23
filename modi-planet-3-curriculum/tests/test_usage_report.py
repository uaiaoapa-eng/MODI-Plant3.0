"""사용량·비용 리포트 회귀 테스트 — MySQL/네트워크 미사용(가짜 커서로 집계 검증).

배경:
  `/chat` 은 턴마다 `usage_turns` 에 이중쓰기하고 있었지만(#133) **읽는 경로가 없어**
  쌓인 데이터로 비용을 계산할 방법이 없었다. `store_mysql.usage_report()` 와
  `/api/usage/report`, `scripts/usage_report.py` 를 추가하면서 이 파일이 산식을 고정한다.

핵심은 **비용 등식**이다:
    weighted_tokens = input×1 + output×5 + cache_read×0.1 + cache_creation×1.25
    → 이 비율이 Haiku 4.5 단가(입력 $1 / 출력 $5 / 캐시읽기 $0.1 / 캐시쓰기 $1.25 per MTok)와
      일치하므로  weighted_tokens / 1e6 = USD
이 등식이 깨지면 리포트 금액이 통째로 틀리므로, 실측값으로 교차검증한다.
"""
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

try:
    import store_mysql as M
    import usage_report as R
except Exception as e:  # 의존성 미설치 환경에서는 스킵
    pytest.skip(f"scripts import 불가: {e}", allow_module_level=True)


# ──────────────────────────────────────────────────────────────────────────────
# 비용 등식 — 실측값과 교차검증
# ──────────────────────────────────────────────────────────────────────────────

def test_weighted_to_usd_matches_haiku_pricing():
    """가중치가 Haiku 4.5 단가 비율과 일치해야 weighted/1e6 = USD 가 성립한다."""
    # 입력 단가 $1/MTok 로 정규화했을 때의 배수
    assert M.USD_PER_WEIGHTED_MTOK == 1.0
    # 100만 가중토큰 = $1
    assert round(1_000_000 / 1_000_000 * M.USD_PER_WEIGHTED_MTOK, 4) == 1.0


@pytest.mark.parametrize("weighted,expected_usd", [
    # 2026-08-21 프로덕션 실측 — 이 값들이 리포트 금액의 근거다
    (1_161_909, 1.1619),   # 동접 40 생성턴 테스트 합계(API 모드)
    (29_048, 0.0290),      # 위 테스트의 턴당 평균
    (128_600, 0.1286),     # CLI 모드 풀 생성 턴 중위값
    (12_814, 0.0128),      # 되묻기 턴
    (0, 0.0),
])
def test_measured_values_convert_correctly(weighted, expected_usd):
    assert round(weighted / 1_000_000 * M.USD_PER_WEIGHTED_MTOK, 4) == expected_usd


# ──────────────────────────────────────────────────────────────────────────────
# 기간 경계 — 날짜만 줘도 하루 전체를 덮어야 한다
# ──────────────────────────────────────────────────────────────────────────────

def test_date_only_expands_to_full_day():
    s, e = M._day_bounds("2026-08-22", "2026-08-22")
    assert s == "2026-08-22 00:00:00"
    assert e == "2026-08-22 23:59:59", "23:59:59 이 아니면 저녁 수업이 리포트에서 누락된다"


def test_end_defaults_to_start_day():
    s, e = M._day_bounds("2026-08-22", "")
    assert s.startswith("2026-08-22") and e.startswith("2026-08-22")


def test_empty_defaults_to_today_kst():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")
    s, e = M._day_bounds("", "")
    assert s.startswith(today) and e.startswith(today), "서버 TZ 와 무관하게 KST 기준이어야 한다"


def test_datetime_input_preserved():
    s, e = M._day_bounds("2026-08-22 09:00:00", "2026-08-22 11:00:00")
    assert s == "2026-08-22 09:00:00" and e == "2026-08-22 11:00:00"


# ──────────────────────────────────────────────────────────────────────────────
# 집계 — 가짜 커서로 SQL 결과를 주입해 계산을 검증
# ──────────────────────────────────────────────────────────────────────────────

class _FakeCursor:
    """execute 순서대로 미리 준비한 결과를 돌려준다(usage_report 의 쿼리 순서에 의존)."""

    def __init__(self, results):
        self._results = list(results)
        self._current = None
        self.queries = []

    def execute(self, sql, args=None):
        self.queries.append((sql, args))
        self._current = self._results.pop(0)

    def fetchone(self):
        return self._current

    def fetchall(self):
        return self._current

    def __enter__(self): return self
    def __exit__(self, *a): return False


class _FakeConn:
    def __init__(self, results):
        self._cur = _FakeCursor(results)

    def cursor(self):
        return self._cur


def _report_with(totals, by_kind=None, top_users=None, by_hour=None,
                 by_day=None, projects=None):
    """usage_report 의 질의 순서대로 결과를 주입한다.

    projects 를 주지 않으면 세션 질의에서 결과가 동나 예외가 나고, `_projects_block`
    이 그걸 잡아 ok=False 로 떨어진다 — **비용 리포트는 그대로 나온다**는 게 설계다.
    """
    results = [totals, by_kind or [], top_users or [], by_hour or [], by_day or []]
    if projects is not None:
        results += [{"n": projects.get("created", 0)},
                    projects.get("by_type", []),
                    projects.get("per_day_rows", []),
                    projects.get("items", [])]
    return M.usage_report(_FakeConn(results), start="2026-08-22", end="2026-08-22")


def test_totals_and_cost_computed():
    """실측 규모(40턴 = 1,161,909 가중토큰)로 총액·턴당 단가가 맞는지."""
    rep = _report_with({
        "turns": 40, "users": 40, "sessions": 40,
        "input_tokens": 400, "output_tokens": 200_000,
        "cache_read_tokens": 50_000, "cache_creation_tokens": 100_000,
        "weighted_tokens": 1_161_909,
        "first_ts": "2026-08-22 09:00:00", "last_ts": "2026-08-22 09:02:00",
    })
    t = rep["totals"]
    assert rep["ok"] is True
    assert t["turns"] == 40 and t["users"] == 40
    assert t["usd"] == 1.1619, "총 비용이 실측 가중토큰과 어긋난다"
    assert t["krw"] == round(1.1619 * 1400)
    assert t["usd_per_turn"] == round(1.1619 / 40, 4)
    assert t["turns_per_user"] == 1.0


def test_zero_turns_does_not_crash():
    """기록이 없어도 0으로 떨어져야 한다(0 나눗셈 금지) — 수업 전 조회 시 흔한 케이스."""
    rep = _report_with({
        "turns": 0, "users": 0, "sessions": 0,
        "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
        "cache_creation_tokens": 0, "weighted_tokens": 0,
        "first_ts": None, "last_ts": None,
    })
    t = rep["totals"]
    assert t["turns"] == 0 and t["usd"] == 0.0
    assert t["usd_per_turn"] == 0.0 and t["turns_per_user"] == 0.0


def test_breakdown_rows_get_cost_fields():
    """분해 표(모드별·사용자별·시간별) 각 행에도 비용이 붙어야 리포트가 의미를 갖는다."""
    rep = _report_with(
        {"turns": 2, "users": 2, "sessions": 2, "input_tokens": 0, "output_tokens": 0,
         "cache_read_tokens": 0, "cache_creation_tokens": 0, "weighted_tokens": 300_000,
         "first_ts": None, "last_ts": None},
        by_kind=[{"mode": "quick", "coding_type": "blockly", "turns": 1,
                  "weighted_tokens": 200_000}],
        top_users=[{"user_id": "u1", "turns": 1, "weighted_tokens": 200_000}],
        by_hour=[{"hour": "2026-08-22 09:00", "turns": 1, "users": 1,
                  "weighted_tokens": 100_000}],
    )
    assert rep["by_kind"][0]["usd"] == 0.2
    assert rep["top_users"][0]["usd"] == 0.2
    assert rep["by_hour"][0]["usd"] == 0.1
    assert rep["by_hour"][0]["krw"] == round(0.1 * 1400)


def test_user_filter_adds_where_clause():
    """user_id 를 주면 SQL 에 조건이 실제로 붙어야 한다(전체가 딸려오면 안 됨)."""
    conn = _FakeConn([
        {"turns": 0, "users": 0, "sessions": 0, "input_tokens": 0, "output_tokens": 0,
         "cache_read_tokens": 0, "cache_creation_tokens": 0, "weighted_tokens": 0,
         "first_ts": None, "last_ts": None}, [], [], [], []])
    M.usage_report(conn, start="2026-08-22", user_id="alice")
    sql, args = conn._cur.queries[0]
    assert "user_id = %s" in sql
    assert "alice" in args


# ──────────────────────────────────────────────────────────────────────────────
# 렌더링 — 사람이 읽는 출력이 깨지지 않아야 한다
# ──────────────────────────────────────────────────────────────────────────────

def test_render_zero_turns_gives_guidance():
    """턴이 0이면 원인 안내를 보여줘야 한다(RAG_UPSTREAM 미설정이 흔한 원인)."""
    text = R.render(_report_with({
        "turns": 0, "users": 0, "sessions": 0, "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0, "weighted_tokens": 0,
        "first_ts": None, "last_ts": None}))
    assert "기록된 턴이 없습니다" in text
    assert "RAG_UPSTREAM" in text


def test_render_includes_cost_and_budget():
    rep = _report_with({
        "turns": 40, "users": 40, "sessions": 40, "input_tokens": 400,
        "output_tokens": 200_000, "cache_read_tokens": 50_000,
        "cache_creation_tokens": 100_000, "weighted_tokens": 1_161_909,
        "first_ts": "2026-08-22 09:00:00", "last_ts": "2026-08-22 09:02:00"})
    text = R.render(rep, budget_usd=100)
    assert "$1.16" in text
    assert "예산" in text and "남은 예산" in text
    assert "출력" in text, "비용 지배 요인(출력)이 보여야 한다"


def test_render_failed_report():
    assert "실패" in R.render({"ok": False, "error": "MySQL down"})


# ──────────────────────────────────────────────────────────────────────────────
# counts() — 진단 함수가 진단 대상의 부재로 죽으면 안 된다
# ──────────────────────────────────────────────────────────────────────────────

class _MissingTableCursor:
    """특정 테이블만 1146(no such table)을 내는 가짜 커서."""

    def __init__(self, missing: set):
        self.missing = missing
        self.table = None

    def execute(self, sql, args=None):
        self.table = sql.split("FROM ")[-1].strip()
        if self.table in self.missing:
            raise Exception(f"(1146, \"Table 'edu_agent.{self.table}' doesn't exist\")")

    def fetchone(self):
        return {"n": 7}

    def __enter__(self): return self
    def __exit__(self, *a): return False


class _MissingTableConn:
    def __init__(self, missing):
        self._missing = missing

    def cursor(self):
        return _MissingTableCursor(self._missing)


def test_counts_survives_missing_table():
    """★ 회귀: usage_turns 가 없어도 예외를 던지면 안 된다.

    2026-08-21 사고 — counts() 에 usage_turns 를 추가하자 운영 DB 에 그 테이블이 없어
    배포의 RAG 백필 스텝이 통째로 실패했다:
        pymysql.err.ProgrammingError: (1146, "Table 'edu_agent.usage_turns' doesn't exist")
    없다는 사실 자체가 알아야 할 정보이므로 None 으로 보고하고 계속 진행한다.
    """
    out = M.counts(_MissingTableConn({"usage_turns"}))
    assert out["usage_turns"] is None, "없는 테이블은 None 으로 보고해야 한다"
    assert out["sessions"] == 7, "다른 테이블 집계는 정상 진행돼야 한다"


def test_counts_reports_all_expected_tables():
    """usage_turns 가 목록에 있어야 '사용량이 쌓이고 있나' 를 원격에서 확인할 수 있다."""
    out = M.counts(_MissingTableConn(set()))
    for t in ("sessions", "knowledge_chunks", "ontology_nodes", "ontology_edges",
              "usage_turns"):
        assert t in out


def test_counts_survives_all_tables_missing():
    """DB 가 비어 있어도(스키마 미적용) 죽지 않는다 — 그 상태를 드러내는 게 목적이다."""
    out = M.counts(_MissingTableConn({"sessions", "knowledge_chunks", "ontology_nodes",
                                      "ontology_edges", "usage_turns"}))
    assert all(v is None for v in out.values())


# ──────────────────────────────────────────────────────────────────────────────
# DB 타입 — MySQL SUM() 은 DECIMAL 을 돌려준다
# ──────────────────────────────────────────────────────────────────────────────
#
# 2026-08-21 운영 실패:
#     GET /api/usage/report?start=2026-08-21
#     → {"ok": false,
#        "error": "unsupported operand type(s) for *: 'decimal.Decimal' and 'float'"}
#
# 원인은 리포트 산식이 아니라 **DB 반환 타입**이었다. MySQL 은 BIGINT 를 SUM 해도
# 오버플로 방지를 위해 DECIMAL 로 돌려준다. 총계는 int() 로 감쌌지만 행 단위 집계는
# raw 를 그대로 넘겨서, 표가 한 줄이라도 있으면 리포트 전체가 실패했다.
#
# 위 집계 테스트들이 전부 통과하면서도 이 버그를 놓친 이유가 여기 있다 — 가짜 커서에
# **int 를 넣었기 때문**이다. 실제 드라이버가 주는 타입으로 한 번 더 돌린다.

def _dec(v):
    from decimal import Decimal
    return Decimal(str(v))


def _mysql_shaped_report():
    """pymysql 이 실제로 주는 모양(집계=Decimal, COUNT=int)으로 리포트를 만든다."""
    return _report_with(
        {
            "turns": 40, "users": 12, "sessions": 15,
            "input_tokens": _dec(12_000), "output_tokens": _dec(200_000),
            "cache_read_tokens": _dec(50_000),
            "cache_creation_tokens": _dec(100_000),
            "weighted_tokens": _dec(1_161_909),
            "first_ts": "2026-08-22 09:00:00", "last_ts": "2026-08-22 11:00:00",
        },
        by_kind=[{"mode": "quick", "coding_type": "blockly",
                  "turns": 30, "weighted_tokens": _dec(1_000_000)}],
        top_users=[{"user_id": "s01", "turns": 5, "weighted_tokens": _dec(161_909)}],
        by_hour=[{"hour": "2026-08-22 09:00", "turns": 40, "users": 12,
                  "weighted_tokens": _dec(1_161_909)}],
    )


def test_decimal_aggregates_do_not_break_report():
    """★ 핵심 회귀: Decimal 집계가 들어와도 리포트가 계산돼야 한다."""
    rep = _mysql_shaped_report()          # 고치기 전에는 여기서 TypeError
    assert rep["ok"] is True
    assert rep["totals"]["usd"] == 1.1619
    assert rep["by_kind"][0]["usd"] == 1.0
    assert rep["top_users"][0]["usd"] == 0.1619
    assert rep["by_hour"][0]["usd"] == 1.1619


def test_report_is_json_serializable():
    """★ Decimal 이 남아 있으면 응답 직렬화에서 다시 깨진다 — 값까지 int 로 정규화."""
    import json
    json.dumps(_mysql_shaped_report())    # default= 없이 통과해야 한다


def test_decimal_values_normalized_to_int():
    """표에 남는 토큰 수도 Decimal 이 아니라 int 여야 한다(합산·정렬 안전)."""
    rep = _mysql_shaped_report()
    for row in (*rep["by_kind"], *rep["top_users"], *rep["by_hour"]):
        assert isinstance(row["weighted_tokens"], int)
        assert isinstance(row["turns"], int)
    for k in ("input_tokens", "output_tokens", "cache_read_tokens",
              "cache_creation_tokens", "weighted_tokens", "users", "sessions"):
        assert isinstance(rep["totals"][k], int), f"totals.{k} 가 int 가 아니다"


@pytest.mark.parametrize("raw,expected", [
    (None, 0), ("", 0), (0, 0), (12_814, 12_814),
    ("29048", 29_048),          # 드라이버 설정에 따라 문자열로 오는 경우
])
def test_num_coerces_db_values(raw, expected):
    assert M._num(raw) == expected


def test_num_never_raises_on_garbage():
    """진단용 값 하나 때문에 리포트 전체가 죽으면 안 된다."""
    assert M._num(object()) == 0
    assert M._num(float("nan")) == 0
