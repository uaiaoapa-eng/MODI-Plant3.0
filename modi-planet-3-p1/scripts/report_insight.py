"""리포트 데이터를 LLM 에 넘겨 "다음 비용이 얼마쯤 나올지" 를 읽어 온다.

왜 산식 예측(report_html.forecast) 위에 이걸 또 두나:
    산식은 "최근 평균 × 30" 밖에 못 한다. 실제로 알고 싶은 건 그게 아니라
    **왜 그렇게 나오는가**다 — 특정 요일에 몰리는지, 캐시를 쓰고 회수 못 하고 있는지,
    한 사용자가 몰아 쓰는지, 생성 턴 비중이 늘고 있는지. 그건 여러 표를 겹쳐 봐야
    나오고, 그게 LLM 이 잘하는 일이다.

비용 통제:
    - 페이지를 열 때마다 부르지 않는다. 밤 크론이 하루 한 번 굳히고, 화면에서는
      버튼을 눌러야 부른다.
    - 원본 표를 통째로 넣지 않는다. 상위 N 행으로 접어 넣는다(아래 _compact).
      40명 한 달치를 통째로 넣으면 분석 한 번이 수업 한 시간보다 비싸질 수 있다.

실패해도 리포트는 살아야 한다 — 분석은 부가 정보다.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Mapping

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

__all__ = ["build_prompt", "generate", "MAX_TOKENS"]

MAX_TOKENS = 1400

SYSTEM = """당신은 교육용 AI 코딩 서비스의 운영 비용을 분석하는 데이터 분석가입니다.
주어진 사용량 집계를 읽고, 운영자가 **다음 달 예산을 잡을 수 있도록** 답하세요.

지켜야 할 것:
- 한국어. 존댓말. 과장 없이.
- 숫자는 주어진 데이터에서만 가져오세요. 없는 값을 만들어내지 마세요.
- 근거 없는 단정 금지. 데이터가 부족하면 "데이터가 N일치뿐이라 신뢰도가 낮습니다"라고 쓰세요.
- 이모지·불필요한 인사말·자기소개 금지.

정확히 이 4개 섹션만, 이 제목 그대로 출력하세요(마크다운 ## 사용):

## 한 줄 요약
한 문장. 이 기간이 어땠는지.

## 다음 비용 예상
다음 30일(수업일 기준) 예상 금액을 **범위**로 제시하고, 그 근거를 2~3문장으로.
학급 수나 수업 시간이 바뀌면 어떻게 달라지는지도 한 문장.

## 눈에 띄는 점
2~4개 항목. 비용 구성(출력/캐시), 사용자 편중, 모드별 단가 차이, 요일 쏠림 등에서
**실제로 데이터에 보이는 것만**. 없으면 "특이사항 없음"이라고 쓰세요.

## 줄이려면
1~3개. 구체적인 행동으로. 효과가 클 것부터.

## 수업 진행
부하 데이터(응답시간·동시접속·실패)가 있을 때만 씁니다. 없으면 이 섹션을 통째로 빼세요.
학생 체감 기준으로 2~4문장: 몇 명까지 감당했는지, 어디서 느려졌는지,
다음 수업에서 인원·진행 방식을 어떻게 조정할지. 판단 기준은 **첫 글자까지 걸린 시간**
입니다 — 총 소요가 길어도 글자가 빨리 뜨면 학생은 기다립니다.
"""


def _top(rows: Any, n: int) -> list:
    return list(rows or [])[:n]


def _compact(report: Mapping[str, Any]) -> dict:
    """프롬프트에 넣을 만큼 접는다 — 원본을 통째로 넣으면 분석이 수업보다 비싸진다."""
    totals = report.get("totals") or {}
    projects = report.get("projects") or {}
    return {
        "기간": {k: report.get("period", {}).get(k) for k in ("start", "end")},
        "합계": {
            "턴": totals.get("turns"), "사용자": totals.get("users"),
            "세션": totals.get("sessions"), "비용USD": totals.get("usd"),
            "턴당USD": totals.get("usd_per_turn"),
            "입력토큰": totals.get("input_tokens"),
            "출력토큰": totals.get("output_tokens"),
            "캐시쓰기": totals.get("cache_creation_tokens"),
            "캐시읽기": totals.get("cache_read_tokens"),
        },
        "일자별": [{"날짜": d.get("day"), "턴": d.get("turns"),
                    "사용자": d.get("users"), "프로젝트": d.get("projects"),
                    "비용USD": d.get("usd")} for d in _top(report.get("by_day"), 31)],
        "모드별": [{"모드": d.get("mode"), "타입": d.get("coding_type"),
                    "턴": d.get("turns"), "비용USD": d.get("usd")}
                   for d in _top(report.get("by_kind"), 8)],
        "사용자상위": [{"턴": d.get("turns"), "비용USD": d.get("usd")}
                       for d in _top(report.get("top_users"), 10)],
        "프로젝트생성수": projects.get("created"),
        # 부하 지표 — 비용만으로는 "수업이 굴러갔나"를 알 수 없다. 같은 프롬프트에
        # 넣어야 "비싼데 느리기까지 했다" 같은 교차 판단이 나온다.
        # 없으면(관측 이전 기간) 키 자체를 빼서 모델이 지어내지 않게 한다.
        **_load_summary(report.get("load") or {}),
        "가격정보": {
            "환산식": "weighted_tokens/1e6 = USD",
            "가중치": "input×1, output×5, cache_read×0.1, cache_creation×1.25",
            "환율": (report.get("assumptions") or {}).get("krw_per_usd"),
        },
    }


def _load_summary(load: Mapping[str, Any]) -> dict:
    """부하 블록을 프롬프트용으로 접는다. 데이터가 없으면 **빈 dict** 를 돌려준다.

    빈 값을 0 으로 채워 넣으면 모델이 "응답시간 0초, 실패 0%" 로 읽고 근거 없이
    "매우 안정적"이라고 쓴다. 없는 건 없는 대로 두는 편이 정확하다.
    """
    if not load or not load.get("ok", True) or not load.get("turns"):
        return {}
    dur = load.get("duration") or {}
    ttft = load.get("ttft") or {}
    conc = load.get("concurrency") or {}
    ops = load.get("ops") or {}
    out = {
        "부하": {
            "턴": load.get("turns"), "성공": load.get("success"),
            "실패": load.get("errors"), "중단": load.get("aborted"),
            "실패율%": load.get("fail_pct"),
            "응답시간ms": {"중앙값": dur.get("p50"), "p95": dur.get("p95"),
                           "최대": dur.get("max"), "평균": dur.get("avg")},
            "첫글자까지ms": {"중앙값": ttft.get("p50"), "p95": ttft.get("p95")},
            "최대동시접속": conc.get("peak"), "피크시각KST": conc.get("peak_at"),
        },
        "동시접속별응답시간": _top(load.get("by_concurrency"), 8),
        "질문유형별": [{"유형": r.get("label"), "턴": r.get("turns"),
                        "턴당USD": r.get("usd_per_turn"), "p95ms": r.get("p95"),
                        "실패율%": r.get("fail_pct")}
                       for r in _top(load.get("by_intent"), 8)],
        "결과물별": [{"결과물": r.get("label"), "턴": r.get("turns"),
                      "턴당USD": r.get("usd_per_turn"), "p95ms": r.get("p95")}
                     for r in _top(load.get("by_outcome"), 8)],
    }
    if ops.get("by_kind"):
        out["거절·사건"] = _top(ops.get("by_kind"), 8)
    if load.get("reuse_curve"):
        # "임계를 내리면 얼마나 더 아낄 수 있나" — 줄이려면 섹션의 근거가 된다.
        out["재사용임계곡선"] = _top(load.get("reuse_curve"), 8)
    if load.get("by_replica"):
        out["서버별"] = _top(load.get("by_replica"), 5)
    return out


def build_prompt(report: Mapping[str, Any], *, mode: str = "") -> str:
    data = json.dumps(_compact(report), ensure_ascii=False, indent=1, default=str)
    note = ""
    if mode == "cli":
        note = ("\n참고: 현재 CLI 구독 모드라 **실제 청구는 0원**입니다. "
                "금액은 API 과금 환산치이므로 '전환 시 예상 비용'으로 해석해 쓰세요.\n")
    return f"다음은 사용량 집계입니다.{note}\n```json\n{data}\n```"


def generate(report: Mapping[str, Any], *, mode: str = "",
             prefer_cli: bool = True) -> dict:
    """LLM 분석을 만든다. 실패해도 예외를 밖으로 던지지 않는다.

    prefer_cli=True 가 기본인 이유(2026-08-21 실측):
        수업의 병목은 요청 수가 아니라 **분당 출력 토큰**이었다. 동시 15건 부하 구간에
        분당 6만 토큰을 쓰면서 첫 글자까지 33초가 걸렸다. 그 상황에서 리포트 분석이
        같은 API 예산을 나눠 쓰면 학생 응답이 그만큼 더 밀린다.

        구독(CLI) 경로는 그 예산과 별개다. 분석은 사람이 버튼을 누를 때 한 번,
        혹은 밤에 한 번 도는 일이라 처리량이 낮아도 상관없다 — 학생 응답과
        경쟁시키지 않는 게 훨씬 중요하다.

    ⚠ 이 함수는 `agent/` 패키지와 Claude 인증이 있는 곳에서만 돈다.
      rag-search 컨테이너에는 둘 다 없다(scripts/ 만 COPY). 그래서 생성은 앱이 하고
      저장만 rag-search 에 맡긴다.
    """
    if not (report or {}).get("ok"):
        return {"ok": False, "error": "리포트 데이터가 없습니다"}
    if not (report.get("by_day") or report.get("totals", {}).get("turns")):
        return {"ok": False, "error": "분석할 사용 기록이 없습니다"}

    try:
        from agent.claude_client import LocalClaudeClient, create_client
        from agent.llm_config import HAIKU

        prompt = build_prompt(report, mode=mode)
        model = os.getenv("ANTHROPIC_MODEL", HAIKU)

        def _ask(client):
            msg = client.messages.create(
                model=model, max_tokens=MAX_TOKENS, system=SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(getattr(b, "text", "") for b in (msg.content or [])).strip()
            if not text:
                raise RuntimeError("빈 응답")
            usage = getattr(msg, "usage", None) or {}
            return {"ok": True, "text": text, "model": getattr(msg, "model", "") or model,
                    "usage": usage if isinstance(usage, dict) else {},
                    "route": "cli" if isinstance(client, LocalClaudeClient) else "api"}

        # 구독(CLI) 우선. 실패하면 **인증 문제일 가능성이 크므로 API 로 내려간다** —
        # 분석은 부가 기능이라 여기서 멈추면 그냥 안 나오고 끝난다. 학생 응답과
        # 예산을 나눠 쓰는 건 피하되, 아예 못 만드는 것보다는 API 로라도 만드는 편이 낫다.
        # (반대 방향 폴백 _ApiWithCliFallback 과 같은 원칙, 방향만 뒤집은 것)
        if not prefer_cli:
            return _ask(create_client(os.getenv("ANTHROPIC_API_KEY", "")))
        try:
            return _ask(LocalClaudeClient())
        except Exception as cli_err:
            key = os.getenv("ANTHROPIC_API_KEY", "").strip()
            if not key:
                return {"ok": False,
                        "error": f"구독(CLI) 인증 실패이고 API 키도 없습니다: {str(cli_err)[:200]}"}
            out = _ask(create_client(key))
            out["fallback_from"] = f"cli: {str(cli_err)[:160]}"
            return out
    except ModuleNotFoundError as e:
        # rag-search 컨테이너처럼 agent/ 가 없는 곳에서 부르면 여기로 온다.
        # 원인을 그대로 노출해야 "왜 안 되지"로 시간을 안 버린다.
        return {"ok": False,
                "error": f"이 프로세스에서는 분석을 만들 수 없습니다 ({e}). "
                         "분석은 agent/ 와 Claude 인증이 있는 앱에서 생성하고 "
                         "여기에는 저장만 합니다."}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}
