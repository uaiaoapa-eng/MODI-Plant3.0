"""chat 턴 재사용 게이팅(#44) — 생성 직전 검색으로 재사용/근접/신규 결정.

resolved intent 기준으로 저장된 결과물을 찾아 %게이팅(search_lib 의 reuse/review/register)에
따라 분기한다. LLM 추가 호출 없음(검색 1회). 첫 컷은 **코드(빌드 턴)** 만 대상 —
가장 비싼 생성(시뮬레이션 153s)을 잡는 최고 ROI 지점. base 청크는 learning_note payload 라
코드 게이트는 등록된 코드 결과물이 있을 때만 발동(회귀 안전).

가드: coding_type 일치 · intent 종류 일치(same intent) · 후보 없으면 None(콜드스타트 → 정상 생성).
"""
from __future__ import annotations

import os

# resolved intent → 재사용 대상 payload kind. (learning_note/design_doc 재사용은 후속 확장)
_INTENT_KIND = {"implement_request": "code"}

# 재사용 대상 kind → 검색 필터용 청크 intent(chunk_fields._CHUNK_INTENT 와 정렬).
# 게이트 검색을 code 자산으로 한정 — 일반 검색은 노트·설계가 top-k 를 점령해
# code 후보가 5위 안에 못 들어 실전 발동 0% 였던 원인 수정(EDU-27 진단 §13).
_KIND_SEARCH_INTENT = {"code": "implement_request"}
# 킬스위치(기본 ON): 문제 시 REUSE_KIND_SEARCH=0 으로 종전(무필터) 검색 복귀.
_REUSE_KIND_SEARCH = os.getenv("REUSE_KIND_SEARCH", "1").strip().lower() not in ("0", "false", "no", "")

# #27: 프라임에 MODI 하드웨어 모듈(uses)·학습노트 카드(realized_by)를 포함할지(기본 ON).
# 출력/입력 인플레가 문제면 개별로 끈다(무회귀 킬스위치).
_PRIME_INCLUDE_MODI = os.getenv("PRIME_INCLUDE_MODI", "1").strip().lower() not in ("0", "false", "no", "")
_PRIME_INCLUDE_CARDS = os.getenv("PRIME_INCLUDE_CARDS", "1").strip().lower() not in ("0", "false", "no", "")

# EDU-27 vec 성분 승격: combined(=0.4·vec+0.15·lex+0.45·con) 는 con(개념 centroid 신호)이
# "같은 UI 프리미티브 계열"이면 무관 쌍도 부풀려, 진짜 재사용(combined .539/.563)과 오탐
# ("로그인 화면" 요청에 "버튼 LED 제어" 코드, combined .526)을 못 가른다(실트래픽 22건, 2026-07-08).
# 반면 vec(BGE 의미 유사도) 성분은 완벽 분리 — 진짜쌍 vec .604/.628, 가짜쌍 vec .442.
# 그래서 review 판정 후보 중 vec 이 충분히 높으면 reuse 로 승격한다(검색 랭킹·register 판정은 불변).
# 킬스위치(기본 ON): 문제 시 REUSE_VEC_GATE=0 으로 종전(combined 만) 동작 복귀.
_REUSE_VEC_GATE = os.getenv("REUSE_VEC_GATE", "1").strip().lower() not in ("0", "false", "no", "")
TAU_REUSE_VEC = float(os.getenv("TAU_REUSE_VEC", "0.58"))


def gate(user_input: str, intent: str, coding_type: str | None,
         user_id: str | None = None) -> dict | None:
    """반환: {decision, kind, candidate, top1, cand_score, vec, vec_promoted} 또는
    None(재사용 대상 아님/검색 실패).

    decision ∈ {reuse, review, register, none}. reuse/review 만 프라임에 쓴다(register/none 은
    정상 생성이되 near-miss 계측용으로 top1·cand_score 를 관측에 남긴다).
    vec/vec_promoted: EDU-27 — combined 판정을 vec(BGE 의미 유사도) 성분으로 review→reuse
    승격했는지 관측용(Langfuse 코호트). 승격 안 됐거나 후보가 없으면 vec_promoted=False.
    """
    kind = _INTENT_KIND.get(intent)
    if not kind:
        return None
    # 프록시 정합: 배포에선 rag-search(BGE+Redis) 로, 로컬에선 인프로세스 search_lib 로 검색한다.
    # (메인앱은 torch 가 없어 인프로세스가 lexical 이라 재사용이 안 걸리던 문제를 프록시로 해소.)
    # kind 한정 검색(기본): top1 은 "가장 가까운 code 자산" 기준이 되어 near-miss 계측도
    # 콘텐츠 팩토리 수요 신호로 더 정확해진다. 킬스위치 OFF 면 종전 무필터 검색.
    search_intent = _KIND_SEARCH_INTENT.get(kind) if _REUSE_KIND_SEARCH else None
    res = _search(user_input, coding_type, user_id, top=5, intent=search_intent)
    if not res.get("ok"):
        return None
    for r in res.get("results") or []:
        payload = r.get("payload") or {}
        if payload.get("kind") != kind:
            continue
        if kind == "code" and coding_type and coding_type != "any" \
                and r.get("coding_type") not in (coding_type, "any"):
            continue
        decision = r.get("decision")
        vec_raw = r.get("vec")
        vec = None
        if vec_raw is not None:
            try:
                vec = float(vec_raw)
            except (TypeError, ValueError):
                vec = None
        # EDU-27 vec 승격: review 판정만 승격 대상(reuse 는 이미 최고 티어, register 는
        # combined<0.48 로 near 도 미달 — vec 만으로 두 단계 뛰어넘는 건 보수적으로 배제).
        vec_promoted = bool(_REUSE_VEC_GATE and vec is not None
                            and vec >= TAU_REUSE_VEC and decision == "review")
        if vec_promoted:
            decision = "reuse"
        return {"decision": decision, "kind": kind, "candidate": r,
                "top1": res.get("top1_score"), "cand_score": r.get("score"),
                "vec": vec, "vec_promoted": vec_promoted}
    # near-miss 계측(#84 후속): 검색은 돌았지만 top5 에 code 후보가 없음 —
    # 실트래픽이 재사용 근처에 오는지(top1 분포)를 재기 위해 점수는 남긴다.
    # decision="none" 은 프라임/직접서브 어느 분기도 타지 않는다(호출측은 reuse/review 만 사용).
    return {"decision": "none", "kind": kind, "candidate": None,
            "top1": res.get("top1_score"), "cand_score": None,
            "vec": None, "vec_promoted": False}


def prime(payload: dict, decision: str) -> str:
    """재사용 후보 payload → 빌드 시스템 프롬프트에 붙일 프라임 블록.

    reuse(거의 동일): 기존 결과를 기반으로 최소 수정. review(근접): 참고 예시.
    설계 문서 §서빙("Haiku 가 원본을 편집만") 원칙 — 새로 짜지 말고 편집하게 유도.
    """
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, dict) or not files:
        return ""
    body = "\n\n".join(f"### {fn}\n```\n{code}\n```" for fn, code in files.items())
    if decision == "reuse":
        head = ("이전에 만든 **거의 동일한** 결과물이 있어요. 새로 처음부터 짜지 말고, "
                "아래 코드를 기반으로 이번 요청에 맞게 **최소한만 수정**해서 내보내세요(구조·파일 구성 유지):")
    else:  # review
        head = ("아래는 이전에 만든 **비슷한** 결과물이에요. 참고 예시로만 활용하고 "
                "이번 요청에 맞게 만들어 주세요:")
    return f"[재사용 컨텍스트]\n{head}\n\n{body}"


def _art_key(r: dict) -> str:
    """검색 결과물의 안정적 식별키(세션 내 반복 제안 회피용). chunk_id 우선, 없으면 제목+본문 앞부분."""
    cid = r.get("chunk_id")
    if cid is not None:
        return f"id:{cid}"
    return f"t:{(r.get('title') or '')[:40]}|{(r.get('content') or '')[:24]}"


def _search(user_input: str, coding_type: str | None,
            user_id: str | None, top: int, intent: str | None = None) -> dict:
    """검색 응답 전체({ok, results, top1_score}) 반환 — **배포 백엔드와 정합**.

    프록시 모드(RAG_UPSTREAM 설정, 온프렘 배포)면 rag-search 의 /api/search 로 프록시해
    나머지 시스템과 동일한 MySQL+Redis+시맨틱(BGE-m3) 인덱스를 본다. 미설정(로컬/개발)이면
    인프로세스 search_lib(sqlite+lexical). 프록시 실패 시 로컬로 폴백(가용성 우선).
    메인앱(:18080)은 torch 가 없어 인프로세스는 lexical 뿐 → 프록시가 BGE/Redis 재사용의 핵심.
    """
    up = os.getenv("RAG_UPSTREAM", "").strip()
    if up:
        try:
            import httpx

            params = {"q": user_input, "coding_type": coding_type or "any", "top": top}
            if user_id:
                params["user_id"] = user_id
            if intent:
                params["intent"] = intent
            resp = httpx.get(f"{up}/api/search", params=params, timeout=5.0)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok"):
                    return data
        except Exception:
            pass  # 프록시 도달 실패 → 로컬 폴백
    try:
        import search_lib

        return search_lib.search(user_input, coding_type=coding_type, top=top,
                                 user_id=user_id, intent=intent) or {}
    except Exception:
        return {}


def _fetch_artifacts(user_input: str, coding_type: str | None,
                     user_id: str | None, top: int) -> list[dict]:
    """유사 결과물 검색 결과 리스트 — _search(프록시/로컬 정합) 의 results."""
    return _search(user_input, coding_type, user_id, top).get("results") or []


def _fetch_graph(user_input: str, coding_type: str | None) -> dict | None:
    """온톨로지 그래프(개념·선수학습·관련·MODI·카드)를 **배포 백엔드와 정합**하게 가져온다.

    프록시 모드면 rag-search 의 /api/query(derive) 로 프록시 — 메인앱(:18080)은 그래프 DB
    접근이 없어 로컬 graph_conn 이 비므로, artifacts 처럼 프록시로 그래프도 채운다(#27 요구 #2).
    grade=9(게이팅 무효)로 전체 그래프를 받는다. 미설정/실패 시 None → 호출측이 로컬 폴백.
    반환: {primary, prerequisites, related, modi_modules, cards} 또는 None.
    """
    up = os.getenv("RAG_UPSTREAM", "").strip()
    if not up:
        return None
    try:
        import httpx

        params = {"question": user_input, "coding_type": coding_type or "any", "grade": 9}
        resp = httpx.get(f"{up}/api/query", params=params, timeout=5.0)
        if resp.status_code == 200:
            d = resp.json()
            if d.get("ok") and d.get("primary"):
                return {
                    "primary": {"key": d["primary"].get("key"), "label": d["primary"].get("label")},
                    "prerequisites": d.get("prerequisites") or [],
                    "related": d.get("related") or [],
                    "modi_modules": d.get("modi_modules") or [],
                    "cards": d.get("cards") or [],
                }
    except Exception:
        pass
    return None


def ontology_suggest(user_input: str, coding_type: str | None = None,
                     user_id: str | None = None, seen: set | None = None,
                     top: int = 6) -> dict:
    """제안형 온톨로지 프라임(#27) — 사용자 메시지 하나로 개념·선수학습 경로 + 유사 결과물 제안.

    /chat 빌드/설계 직전에 호출. 사용자 추가 요청 없이 메시지 텍스트만으로:
      1) 개념 식별(alias, torch 불필요·오프라인) + 선수학습 경로/관련 개념(온톨로지 그래프)
      2) 유사 과거 결과물(학습노트·코드) 검색(search_lib) — reuse/review 티어만(콜드셀 제외)
      3) '복사'가 아니라 '이번 요청에 맞게 각색' 프레이밍 + seen 중복 회피(매번 동일 제안 방지)

    반환: {ok, primary, prerequisites, related, artifacts, seen_keys, block} 또는 {ok: False}.
      seen: 이번 세션에서 이미 제안한 항목 키 집합 → 같은 카드 반복 노출을 막는다(호출측이 누적).
      seen_keys: 이번에 제안에 포함된 키 목록 → 호출측이 seen 에 합쳐 다음 턴에 회피.
    LLM 추가 호출 없음(검색 1회 + SQLite 그래프 조회). 어떤 예외도 삼켜 chat 을 깨지 않는다.
    """
    if not (user_input or "").strip():
        return {"ok": False}
    try:
        from ontology_lib import (chunks_for, graph_conn, load_seed, match_concepts,
                                   prerequisites, related, uses)
    except Exception:
        return {"ok": False}

    # 1) 개념 식별(alias 부분일치, 오프라인) — 가장 강하게 매칭된 개념을 핵심으로.
    try:
        concepts = load_seed()
        matched = match_concepts(user_input, concepts, top=3)
    except Exception:
        concepts, matched = [], []
    label = {c["key"]: c["label"] for c in concepts}

    primary = None
    prereq: list[dict] = []
    rel: list[dict] = []
    modi: list[str] = []
    cards: list[dict] = []

    # 그래프(선수학습·관련·MODI·카드): 프록시(rag-search) 우선 — 메인앱은 그래프 DB 접근이 없어
    # 로컬 graph_conn 이 비므로, artifacts 처럼 프록시로 채운다(#27 요구 #2). 미설정/실패 시 로컬 폴백.
    g = _fetch_graph(user_input, coding_type)
    if g and g.get("primary"):
        primary = g["primary"]
        prereq = g.get("prerequisites") or []
        rel = g.get("related") or []
        modi = (g.get("modi_modules") or []) if _PRIME_INCLUDE_MODI else []
        cards = (g.get("cards") or []) if _PRIME_INCLUDE_CARDS else []
    elif matched:
        pkey = matched[0][0]
        primary = {"key": pkey, "label": label.get(pkey, pkey)}
        try:
            conn = graph_conn()
            try:
                prereq = prerequisites(conn, pkey) or []
                rel = related(conn, pkey) or []
                # #27: 하드웨어(MODI) 모듈 + 실현 학습노트 카드까지 프라임에 포함(요구 #2).
                if _PRIME_INCLUDE_MODI:
                    modi = uses(conn, pkey) or []
                if _PRIME_INCLUDE_CARDS:
                    cards = chunks_for(conn, pkey, coding_type) or []
            finally:
                conn.close()
        except Exception:
            prereq, rel, modi, cards = [], [], [], []

    # 2) 유사 과거 결과물 검색 → reuse/review 만(register=콜드셀은 재사용 제안 아님).
    #    seen 에 든 항목은 건너뛴다(매번 동일 카드 방지). 순위(score)는 그대로 보존.
    artifacts: list[dict] = []
    seen_keys: list[str] = []
    for r in _fetch_artifacts(user_input, coding_type, user_id, top):
        if r.get("decision") == "register":
            continue
        k = _art_key(r)
        if seen and k in seen:
            continue
        artifacts.append(r)
        seen_keys.append(k)

    # 매칭된 개념도, 재사용할 결과물도 없으면 주입하지 않는다(빈 프라임 = 정상 신규 생성).
    if not primary and not artifacts:
        return {"ok": False}

    block = _suggest_block(user_input, primary, prereq, rel, artifacts, modi, cards)
    return {"ok": True, "primary": primary, "prerequisites": prereq,
            "related": rel, "modi_modules": modi, "cards": cards,
            "artifacts": artifacts, "seen_keys": seen_keys, "block": block}


def _suggest_block(user_input: str, primary: dict | None, prereq: list[dict],
                   rel: list[dict], artifacts: list[dict],
                   modi: list[str] | None = None, cards: list[dict] | None = None) -> str:
    """제안 구조 → 시스템 프롬프트에 붙일 프라임 블록. '각색' 프레이밍(복사 금지)."""
    lines = ["[온톨로지 제안 · 이번 요청에 맞게 각색하세요]"]
    if primary:
        lines.append(f"핵심 개념: {primary['label']}")
    if prereq:
        # level 순 정렬된 선수학습 체인 → 스캐폴딩 경로. 개념 하나만 반복하지 않게 경로로 제시.
        path = " → ".join(p.get("label", "") for p in prereq[:5] if p.get("label"))
        if path:
            lines.append(f"학습 경로(선수학습): {path}")
    if rel:
        names = ", ".join(r.get("label", "") for r in rel[:4] if r.get("label"))
        if names:
            lines.append(f"관련 개념(대안 제시 가능): {names}")
    if modi:
        # #27: 핵심 개념이 실제로 쓰는 MODI 하드웨어 모듈 — 코드가 어떤 모듈을 다뤄야 하는지 힌트.
        # 라벨 중복 제거(순서 보존) — 같은 역할이 여러 엣지로 중복될 수 있다.
        uniq = list(dict.fromkeys(m for m in modi if m))
        mods = ", ".join(uniq[:6])
        if mods:
            lines.append(f"사용 하드웨어(MODI): {mods}")
    if cards:
        # #27: 개념을 실현한 과거 학습노트 카드 — 설명/스캐폴딩 톤 참고(각색 전제).
        names = ", ".join((c.get("title") or "").strip() for c in cards[:4] if c.get("title"))
        if names:
            lines.append(f"참고 학습노트: {names}")

    if artifacts:
        lines.append(
            "\n아래는 이전 대화에서 만든 **비슷한 결과물**이에요. **그대로 복사하지 말고** "
            "이번 요청('" + user_input.strip()[:60] + "')에 맞게 각색·개선해서 제안하세요:")
        for a in artifacts[:4]:
            payload = a.get("payload") or {}
            kind = payload.get("kind") or a.get("chunk_type") or "note"
            title = a.get("title") or payload.get("title") or "(제목 없음)"
            if kind == "code":
                files = list((payload.get("files") or {}).keys())
                lines.append(f"- [코드] {title} — 파일: {', '.join(files[:4]) or '없음'}")
            elif kind == "design_doc":
                lines.append(f"- [설계] {title}")
            else:  # learning_note 등
                what = (payload.get("what") or a.get("content") or "").strip()
                lines.append(f"- [학습노트] {title}" + (f" — {what[:70]}" if what else ""))
    return "\n".join(lines)
