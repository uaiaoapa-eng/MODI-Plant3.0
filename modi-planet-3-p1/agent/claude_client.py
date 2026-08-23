"""
Claude CLI 래퍼 — anthropic.Anthropic()와 동일한 인터페이스로 Claude CLI를 사용.
환경변수 USE_LOCAL_CLAUDE=true 로 활성화.

걷어낼 때: 이 파일 삭제 + create_client() 호출을 anthropic.Anthropic(api_key=...)로 원복.
"""

from __future__ import annotations

import os
import json
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import Any

from agent.retry import run_with_retry, is_api_auth_error
from agent import observability as obs


def _use_local_cli() -> bool:
    return os.getenv("USE_LOCAL_CLAUDE", "true").strip().lower() in ("true", "1", "yes")


# API 인증 실패 래치 — 한 번 실패하면 프로세스 전체가 CLI 로 내려간다.
# 매 호출마다 죽은 API 를 두드리면 지연·에러만 쌓이기 때문. 쿨다운 후 재시도한다
# (운영자가 키를 고쳐도 재배포 없이 스스로 복귀하도록).
_api_failed_at: float | None = None
_api_fallback_reason: str = ""


def _api_cooldown_seconds() -> float:
    try:
        return float(os.getenv("API_FALLBACK_COOLDOWN_SECONDS", "600"))
    except ValueError:
        return 600.0


def _api_is_latched_down(now: float | None = None) -> bool:
    """직전 API 인증 실패가 쿨다운 안이면 True(=CLI 사용)."""
    if _api_failed_at is None:
        return False
    import time as _t
    return ((now if now is not None else _t.time()) - _api_failed_at) < _api_cooldown_seconds()


def note_api_auth_failure(reason: str) -> None:
    """API 인증 실패를 기록해 이후 호출을 CLI 로 보낸다(폴백 래치)."""
    global _api_failed_at, _api_fallback_reason
    import time as _t
    _api_failed_at = _t.time()
    _api_fallback_reason = (reason or "")[:300]
    print(f"[llm] ⚠ API 인증 실패 → CLI 폴백({_api_cooldown_seconds():.0f}s): {_api_fallback_reason}",
          flush=True)
    try:
        obs.capture_load_constraint("api_auth_fallback", subject=_api_fallback_reason[:120])
    except Exception:
        pass  # 관측 실패가 폴백을 막지 않는다


def reset_api_fallback() -> None:
    """래치 해제(테스트·운영 수동 복구용)."""
    global _api_failed_at, _api_fallback_reason
    _api_failed_at = None
    _api_fallback_reason = ""


def create_client(api_key: str = ""):
    """USE_LOCAL_CLAUDE 환경변수로 CLI/API 분기 + API 실패 시 CLI 폴백.

    - true  → 로그인된 구독 인증으로 claude CLI 사용 (API 키 불필요)
    - false → anthropic API 직접 호출. 단 **키가 없거나 인증이 깨지면 CLI 로 내려간다.**

    폴백을 두는 이유(2026-08-21 실측 사고): USE_LOCAL_CLAUDE=false 로 바꿨는데
    ANTHROPIC_API_KEY 가 비어 있어 모든 /chat 이 INTERNAL 로 죽었다. API 는 키 만료·
    크레딧 소진으로 언제든 끊길 수 있고, 그때 **서비스가 통째로 멈추는 게 가장 위험하다.**
    CLI 구독 경로는 그대로 살아있으므로 자동으로 그쪽으로 넘긴다(품질·기능 동일, 처리량만 낮음).
    """
    if _use_local_cli():
        return LocalClaudeClient()

    # ① 키가 아예 없다 → 네트워크 왕복 없이 즉시 폴백. (SDK 는 여기서
    #    "Could not resolve authentication method" 를 내며 매 요청이 실패한다)
    if not (api_key or os.getenv("ANTHROPIC_API_KEY", "")).strip():
        note_api_auth_failure("ANTHROPIC_API_KEY 미설정")
        return LocalClaudeClient()

    # ② 직전 인증 실패가 쿨다운 안이다 → API 를 다시 두드리지 않고 CLI 사용.
    if _api_is_latched_down():
        return LocalClaudeClient()

    # 계측은 orchestrator에서 Langfuse 수동 계측(@observe)으로 처리한다.
    # (자동 instrumentor는 CLI 경로를 못 잡고 수동 generation과 이중으로 찍혀 제거)
    import anthropic
    return _ApiWithCliFallback(anthropic.Anthropic(api_key=api_key))


class _FallbackMessages:
    """API 를 먼저 쓰되 인증 실패면 같은 호출을 CLI 로 재시도한다.

    anthropic.Anthropic().messages 와 LocalClaudeClient().messages 는 같은 표면
    (create/stream)을 갖도록 맞춰져 있어 인자를 그대로 넘길 수 있다.
    """

    def __init__(self, api_messages):
        self._api = api_messages
        self._cli = None  # 필요할 때만 생성
        # 이번 클라이언트로 나간 호출이 실제로 어느 경로를 탔는지. 설정값(USE_LOCAL_CLAUDE)
        # 이 아니라 **실제 경로**여야 비용이 맞는다 — 폴백이 걸리면 API 모드로 설정돼
        # 있어도 그 턴은 구독(CLI)으로 나가 실청구가 0 이기 때문이다.
        self.last_route = ""

    def _cli_messages(self):
        if self._cli is None:
            self._cli = LocalClaudeClient().messages
        return self._cli

    def create(self, **kw):
        try:
            out = self._api.create(**kw)
            self.last_route = "api"
            return out
        except Exception as e:
            if not is_api_auth_error(str(e)):
                raise
            note_api_auth_failure(str(e))
            self.last_route = "api_fallback_cli"
            return self._cli_messages().create(**kw)

    def stream(self, **kw):
        """스트림은 **진입 시점**(HTTP 요청 발행)에 인증이 검증된다.

        SDK 의 stream() 은 매니저를 돌려주고 실제 요청은 __enter__ 에서 나가므로,
        여기서 잡으려면 한 번 열어 봐야 한다. 열기에 성공하면 그대로 넘기고,
        인증 실패면 CLI 스트림으로 교체한다. 토큰이 나가기 전이라 중복 출력이 없다.
        """
        try:
            mgr = self._api.stream(**kw)
            entered = mgr.__enter__()
            self.last_route = "api"
            return _EnteredStream(mgr, entered)
        except Exception as e:
            if not is_api_auth_error(str(e)):
                raise
            note_api_auth_failure(str(e))
            self.last_route = "api_fallback_cli"
            return self._cli_messages().stream(**kw)


class _EnteredStream:
    """이미 __enter__ 된 SDK 스트림을 `with` 로 다시 쓸 수 있게 감싼다."""

    def __init__(self, mgr, entered):
        self._mgr = mgr
        self._entered = entered

    def __enter__(self):
        return self._entered

    def __exit__(self, *exc):
        return self._mgr.__exit__(*exc)

    def __iter__(self):
        return iter(self._entered)

    def __getattr__(self, name):
        return getattr(self._entered, name)


class _ApiWithCliFallback:
    """anthropic.Anthropic() 드롭인 — .messages 만 폴백 래퍼로 교체."""

    def __init__(self, api_client):
        self._api_client = api_client
        self.messages = _FallbackMessages(api_client.messages)

    @property
    def api_key(self):
        return getattr(self._api_client, "api_key", None)

    def __getattr__(self, name):
        return getattr(self._api_client, name)


# ──────────────────────────────────────────────
# Response 객체 (Anthropic SDK 호환)
# ──────────────────────────────────────────────

@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict
    type: str = "tool_use"


@dataclass
class Message:
    content: list
    stop_reason: str
    model: str = ""
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    cost_usd: float | None = None  # CLI 경로의 total_cost_usd (SDK 경로는 None)


# ──────────────────────────────────────────────
# Streaming 이벤트 객체 (Anthropic SDK 호환)
# ──────────────────────────────────────────────

@dataclass
class _TextDelta:
    text: str
    type: str = "text_delta"


@dataclass
class _InputJsonDelta:
    partial_json: str
    type: str = "input_json_delta"


@dataclass
class _ContentBlockStart:
    content_block: Any
    type: str = "content_block_start"


@dataclass
class _ContentBlockDelta:
    delta: Any
    type: str = "content_block_delta"


# ──────────────────────────────────────────────
# 프롬프트 조립 & 파싱
# ──────────────────────────────────────────────

_TOOL_HEADER = """\
You have access to the following tools.
To call a tool, you MUST wrap each call in <tool_call> tags exactly like this:

<tool_call>
{"name": "tool_name", "input": {"param1": "value1"}}
</tool_call>

Rules:
- You may include multiple <tool_call> blocks.
- You may mix regular text with tool calls.
- Each <tool_call> block must contain valid JSON with "name" and "input" keys.
- Do NOT nest or escape the JSON.

Available tools:
"""


def _format_tools(tools: list, tool_choice: dict | None = None) -> str:
    if not tools:
        return ""
    parts = [_TOOL_HEADER]
    for t in tools:
        parts.append(f"\n- **{t['name']}**")
        if t.get("description"):
            parts.append(f"  {t['description']}")
        schema = t.get("input_schema", {})
        if schema.get("properties"):
            parts.append(f"  Parameters: {json.dumps(schema, ensure_ascii=False)}")

    if tool_choice:
        tc_type = tool_choice.get("type")
        if tc_type == "any":
            parts.append(
                "\nIMPORTANT: You MUST call at least one tool. Do NOT respond with text only."
            )
        elif tc_type == "tool":
            name = tool_choice["name"]
            parts.append(f"\nIMPORTANT: You MUST call the `{name}` tool.")

    return "\n".join(parts)


def _tool_choice_reminder(tool_choice: dict) -> str:
    """도구 강제 시 프롬프트 말단에 붙는 리마인더. 응답 직전 위치라 소형 모델도 잘 따른다."""
    if tool_choice.get("type") == "tool":
        target = f"the `{tool_choice['name']}` tool"
    else:
        target = "at least one tool"
    # "한 문장 허용"은 구현 시스템 프롬프트의 "코드 생성 전 '~를 만들게요.' 한 문장 안내"와
    # 정합용 — "Begin with <tool_call>"처럼 상충 지시를 주면 소형 모델이 더 흔들린다.
    return (
        "[SYSTEM REMINDER]\n"
        f"Your response MUST call {target} using <tool_call> tags with valid JSON.\n"
        "You may write at most ONE short sentence of plain text before the first <tool_call>.\n"
        "NEVER output code as plain text or markdown code fences - all code must be "
        "delivered inside a tool call."
    )


def _format_messages(messages: list) -> str:
    """메시지 히스토리를 Claude가 이해하기 쉬운 텍스트로 변환"""
    parts: list[str] = []
    for msg in messages:
        role = msg["role"].upper()
        content = msg["content"]

        if isinstance(content, str):
            parts.append(f"[{role}]\n{content}")
            continue

        if not isinstance(content, list):
            parts.append(f"[{role}]\n{content}")
            continue

        for block in content:
            if not isinstance(block, dict):
                # SDK 객체가 남아 있을 수 있음
                if getattr(block, "type", None) == "text":
                    parts.append(f"[{role}]\n{block.text}")
                elif getattr(block, "type", None) == "tool_use":
                    tc = json.dumps(
                        {"name": block.name, "input": block.input}, ensure_ascii=False
                    )
                    parts.append(f"[{role}]\n<tool_call>\n{tc}\n</tool_call>")
                continue

            btype = block.get("type")
            if btype == "text" and block.get("text"):
                parts.append(f"[{role}]\n{block['text']}")
            elif btype == "tool_use":
                tc = json.dumps(
                    {"name": block["name"], "input": block["input"]},
                    ensure_ascii=False,
                )
                parts.append(f"[{role}]\n<tool_call>\n{tc}\n</tool_call>")
            elif btype == "tool_result":
                tid = block.get("tool_use_id", "")
                parts.append(f"[TOOL_RESULT ({tid})]\n{block.get('content', '')}")

    return "\n\n".join(parts)


# tool-call 태그 관용 매칭: 모델(특히 Haiku)이 <tool_call> 대신 <call>/<tool>/<toolcall>/
# <tool-call>, 속성·공백, open/close 불일치(<call>…</tool_call>)로 흘려도 잡아낸다.
# 네이티브 학습 포맷 계열도 포함: <function_calls>·</tool_function_calls>·복수형(s) —
# 실사고에서 이 변종 태그가 채팅에 그대로 새고 안의 도구 호출이 실행되지 않았음.
# \b + (스트리밍 쪽) '{'/'[' 가드로 <toolbar>·<caller>·<function> 같은 일반어 태그 오삭제는 막는다.
_TC_NAME = r"(?:tool[ _\-]?)?function[ _\-]?calls?|tool(?:[ _\-]?calls?)?|calls?"
_TC_OPEN_RE = re.compile(rf"<\s*(?:{_TC_NAME})\b[^>]*>", re.IGNORECASE)
_TC_CLOSE_RE = re.compile(rf"<\s*/\s*(?:{_TC_NAME})\s*>", re.IGNORECASE)
# 열림 태그가 겹쳐 오는 래퍼 형태(<function_calls>\n<tool_call>{…}</tool_call>\n</tool_function_calls>)를
# 위해 open+·close+ 를 허용 — 래퍼 포장지까지 한 번에 소비한다.
_TOOL_CALL_RE = re.compile(
    rf"(?:{_TC_OPEN_RE.pattern})(?:\s*(?:{_TC_OPEN_RE.pattern}))*\s*(?P<body>.*?)"
    rf"\s*(?:{_TC_CLOSE_RE.pattern})(?:\s*(?:{_TC_CLOSE_RE.pattern}))*",
    re.IGNORECASE | re.DOTALL,
)
# 열림 없이 떠도는 닫는 태그 중 '확실히 툴 포맷 잔해'만 제거 대상 — 복합 이름
# (tool_call·function_calls 계열)만. 단일어(</tool>·</call>)는 평문 <tool>설명</tool>
# 보존 계약과 충돌할 수 있어 제외한다. (스트리밍 필터·_parse_response 공용 — 두 파서 정합)
_TC_STRAY_CLOSE_RE = re.compile(
    r"<\s*/\s*(?:(?:tool[ _\-]?)?function[ _\-]?calls?|tool[ _\-]calls?|toolcalls?)\s*>",
    re.IGNORECASE,
)
# '[' 본문 가드: 배열 배칭 변종(<function_calls>[{…},{…}]</…>)은 tool-call로 소비하되,
# '['로 시작하는 '평문'(<tool>[예시] 버튼을 …</tool> 같은 대괄호 주석)은 보존해야 한다.
# '[' 뒤 첫 비공백이 '{'(객체 배열)일 때만 tool-call 시도로 간주한다.
# (스트리밍 필터·_parse_response 공용 기준 — 두 파서 정합. 한쪽만 고치지 말 것)
_TC_ARRAY_BODY_RE = re.compile(r"\[\s*\{")


def _coerce_tool_calls(raw: str) -> list[dict]:
    """tool-call 본문 JSON을 관용적으로 파싱해 호출 dict 목록으로 — 근접 실패(near-miss)를 복구한다.

    실사고(2026-07-02, 당근마켓 수정 턴): Haiku가 인자를 "input" 래퍼 없이 최상위에
    펼치면서 닫는 괄호는 중첩 포맷 습관대로 두 개(`…"설명"}}`)로 닫음 → strict
    json.loads가 'Extra data'로 실패, 블록 10개 전부 조용히 폐기 → 도구 호출 0 →
    수정 계약 미이행. raw_decode로 첫 JSON 값만 취하고 꼬리 잔해는 무시한다.

    본문은 단일 객체({…}) 또는 객체 배열([{…}, {…}]) — 실사고(2026-07-03, 갤러그 수정 턴):
    Haiku가 "모든 호출을 한 응답에 배칭" 지시를 따르며 여러 호출을 배열 하나로 묶었는데,
    '{' 전용 가드가 배열을 평문 취급 → 블록 전체가 채팅에 그대로 새고 실행 0.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        try:
            data, _ = json.JSONDecoder().raw_decode(raw)
        except (json.JSONDecodeError, ValueError):
            return []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _tool_input_of(data: dict) -> dict:
    """파싱된 tool-call JSON에서 input dict를 추출.

    정상 포맷은 {"name", "input": {…}}이지만, Haiku가 input 래퍼를 빼먹고 인자를
    최상위에 펼치는 변형(위 실사고와 동일 건)이 잦다 — name 외 최상위 키를 입력으로
    승격하면 스키마 파라미터명과 그대로 일치해 실행 가능해진다.
    """
    tool_input = data.get("input")
    if isinstance(tool_input, dict) and tool_input:
        return tool_input
    flat = {k: v for k, v in data.items() if k not in ("name", "input")}
    return flat if flat else (tool_input if isinstance(tool_input, dict) else {})


def _parse_response(text: str, has_tools: bool) -> tuple[str, list[ToolUseBlock]]:
    """응답에서 tool-call 블록을 추출하고 나머지(가시) 텍스트와 분리.

    본문이 '{'(단일 호출) 또는 '[{'(객체 배열 배칭)로 시작하는 블록(=tool-call 시도)만
    텍스트에서 제거하고, 그중 JSON으로 복구 가능하고 name이 있는 것만 실행 대상(tool_uses)으로
    추출한다. 스트리밍 필터의 '{'/'[{' 가드와 동일 기준이라 버블/히스토리가 일치한다.
    <tool>설명</tool>·<tool>[예시] …</tool> 같은 평문(본문이 JSON 아님)은 그대로 보존.
    """
    if not has_tools:
        return text.strip(), []

    tool_uses: list[ToolUseBlock] = []
    out: list[str] = []
    last = 0
    for match in _TOOL_CALL_RE.finditer(text):
        raw = match.group("body").strip()
        if raw and not (raw.startswith("{") or _TC_ARRAY_BODY_RE.match(raw)):
            continue  # 본문이 JSON 아님 → tool-call 아님 → 본문 유지(평문 <tool>설명</tool>·[예시] 등)
        # 본문이 '{'/'[{'로 시작 = tool-call 시도, 빈 본문 = 래퍼 잔해(<function_calls></tool_function_calls>)
        # → 둘 다 가시 텍스트에서 제거(스트리밍 필터의 '{'/'[{' 가드와 일치)
        out.append(text[last:match.start()])
        last = match.end()
        if not raw:
            continue  # 빈 래퍼 — 숨기기만 하고 실행할 건 없음
        for data in _coerce_tool_calls(raw):
            name = data.get("name")
            if not name or not isinstance(name, str):
                continue  # 복구 불가(깨진 JSON/이름 없음) → 숨기되 실행은 안 함
            tool_uses.append(
                ToolUseBlock(
                    id=f"toolu_local_{uuid.uuid4().hex[:12]}",
                    name=name,
                    input=_tool_input_of(data),
                )
            )
    out.append(text[last:])
    # 짝 없이 떠도는 툴 포맷 닫는 태그 잔해 제거 (스트리밍 필터의 stray-drop과 정합)
    clean = _TC_STRAY_CLOSE_RE.sub("", "".join(out)).strip()
    return clean, tool_uses


# ──────────────────────────────────────────────
# CLI 호출
# ──────────────────────────────────────────────

def _cli_env() -> dict:
    """CLI 서브프로세스용 환경.

    CLI 모드는 로그인된 구독 인증으로 동작해야 한다. ANTHROPIC_API_KEY가 환경에
    남아 있으면 CLI가 API 키 과금으로 붙으므로 서브프로세스 환경에서만 제거한다.
    서버 프로세스 자체가 Claude Code 세션 안에서 기동되면 CLAUDECODE가 상속되고,
    매 턴 새로 띄우는 claude CLI가 이를 "중첩 세션"으로 오인해 즉시 실패한다
    (code 1: cannot be launched inside another Claude Code session) — 그래서
    서브프로세스 환경에서만 제거해 매 턴 독립 실행되는 CLI 호출이 막히지 않게 한다.
    """
    return {
        k: v for k, v in os.environ.items()
        if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDECODE")
    }


def _cli_persona_flags(system: str) -> list[str]:
    """CLI가 Claude Code가 아니라 우리 시스템 프롬프트대로 동작하게 만드는 플래그.

    - --system-prompt: Claude Code 기본 정체성을 우리 프롬프트로 '완전 교체'
      (기존처럼 [SYSTEM] 텍스트로 user 메시지에 끼워 넣으면 무시당함)
    - --tools "": Read/Edit/Bash 등 내장 도구 비활성화 (도구는 텍스트로 파싱하므로 불필요)
    - --setting-sources "": 프로젝트/유저 CLAUDE.md·settings 로드 안 함
    - --exclude-dynamic-system-prompt-sections: cwd/env/git/memory 주입 제거
    """
    flags: list[str] = []
    if system:
        flags += ["--system-prompt", system]
    flags += [
        "--tools", "",
        "--setting-sources", "",
        "--exclude-dynamic-system-prompt-sections",
    ]
    return flags


def _cli_lightweight_flags() -> list[str]:
    """매 호출마다 필요 없는 Claude Code 부가기능을 끄는 경량 플래그.

    이 서버는 대화 히스토리와 도구 실행을 자체 관리하므로 CLI 세션 저장, 슬래시 커맨드,
    다음 프롬프트 제안이 필요 없다. effort는 기본 미부착(CLI 기본값 사용) — 소형 모델의
    포맷 준수·코드 품질과 직결되는 튜닝 지점이라 품질 검증 없이 낮추지 않는다.
    CLAUDE_CLI_EFFORT=low 처럼 명시했을 때만 붙는다.
    """
    flags = [
        "--no-session-persistence",
        "--disable-slash-commands",
        "--prompt-suggestions", "false",
    ]
    effort = os.getenv("CLAUDE_CLI_EFFORT", "").strip().lower()
    if effort and effort not in ("default", "none", "off", "false", "0"):
        flags += ["--effort", effort]
    if os.getenv("CLAUDE_CLI_SAFE_MODE", "").strip().lower() in ("true", "1", "yes"):
        flags.append("--safe-mode")
    return flags


# CLI 서브프로세스를 레포 밖에서 실행 → edu-agent 파일/문맥을 끌어오지 않음
_CLI_CWD = tempfile.gettempdir()


def _model_flags(model: str) -> list[str]:
    """요청별 모델을 CLI에 명시. model이 오면 --model로 그 모델 강제(요청 기반 선택),
    비면 ANTHROPIC_MODEL(.env) 기본값으로 돈다. 둘 다 없으면 CLI 구독 기본 모델."""
    return ["--model", model] if model else []


def _call_cli(prompt: str, system: str = "", model: str = "", timeout: int = 180) -> tuple[str, dict | None, float | None]:
    """claude CLI를 호출. (응답텍스트, usage, total_cost_usd) 반환. 프롬프트는 stdin으로 전달.

    model이 오면 --model로 강제(요청별 선택), 비면 ANTHROPIC_MODEL(.env) 기본값(_cli_env가 물려줌).
    --output-format json 결과의 usage/total_cost_usd 를 꺼내 호출자가 Langfuse generation 에 부착한다.

    일시적 실패(429/529/timeout/네트워크)는 지수 백오프로 재시도한다(agent.retry).
    인증 실패·바이너리 없음 같은 영구 실패는 즉시 올린다.
    """
    def _once() -> tuple[str, dict | None, float | None]:
        # 퍼포먼스 계측: 비스트리밍 CLI 호출(가드레일 분류·quick 모드 등) 1회를 span 으로.
        with obs.llm_span("ai.run.claude_cli", f"model={model or 'default'}") as span:
            # subprocess.TimeoutExpired 메시지("timed out ...")는 재시도 대상으로 분류된다.
            result = subprocess.run(
                [
                    "claude", "-p", "--output-format", "json",
                    *_model_flags(model), *_cli_persona_flags(system), *_cli_lightweight_flags(),
                ],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=_cli_env(),
                cwd=_CLI_CWD,
            )

            if result.returncode != 0:
                raise RuntimeError(
                    f"Claude CLI 실패 (code {result.returncode}): "
                    f"{(result.stderr or result.stdout)[:500]}"
                )

            # --output-format json → {"result": "...", "is_error": bool, "usage": {...}, "total_cost_usd": ...}
            data = json.loads(result.stdout)
            if data.get("is_error"):
                raise RuntimeError(f"Claude CLI 오류: {data.get('result', result.stdout[:500])}")
            obs.tag_llm_usage(span, data.get("usage"), data.get("total_cost_usd"))
            return data["result"], data.get("usage"), data.get("total_cost_usd")

    def _on_retry(attempt: int, delay: float, exc: Exception) -> None:
        print(f"[claude-cli] 일시적 실패, {delay:.0f}s 후 재시도({attempt + 1}/2): {exc}", flush=True)
        # 재시도는 부하/일시장애의 1차 신호 — breadcrumb 로 남겨 이후 이벤트 맥락에 붙인다.
        obs.add_breadcrumb(
            f"claude-cli 재시도 {attempt + 1}: {str(exc)[:160]}",
            category="llm", level="warning", delay_s=delay,
        )

    try:
        return run_with_retry(_once, max_attempts=3, on_retry=_on_retry)
    except Exception as e:
        # 재시도 소진 후 최종 실패: 레이트리밋성이면 부하 제약 이벤트로 분류.
        obs.note_llm_failure(e, path="cli_call")
        raise


class _ToolCallFilter:
    """스트리밍 텍스트에서 tool-call 구간을 실시간으로 걸러낸다.

    - 태그 관용 매칭(_TC_OPEN_RE/_TC_CLOSE_RE): <tool_call>·<call>·<tool>·<toolcall>·
      <tool-call>, 속성/공백, open/close 불일치(<call>…</tool_call>)까지.
    - '{'/'[' 가드: 여는 태그 뒤가 (공백 무시) '{'(단일 호출) 또는 '['(호출 배열)로
      시작할 때만 tool-call 로 간주 → 평문 속 <tool>/<call> 같은 일반어 태그를 오삭제하지 않는다.
    - 태그가 청크 경계에 쪼개질 수 있으므로, 미완결 '<...' 꼬리는 보류했다가 다음 청크와 합쳐 판단.
    tool_call 진입 시 entered_tool_call 플래그를 세워 호출자가 감지할 수 있다.
    """

    _NAME_RE = re.compile(r'"name"\s*:\s*"([^"]+)"')

    def __init__(self):
        self._buf = ""
        self._inside = False
        self.entered_tool_call = False  # feed()마다 리셋, 진입 시 True
        self._tool_inner = ""           # tool-call 내부(JSON) 텍스트 축적

    def feed(self, chunk: str) -> str:
        self._buf += chunk
        self.entered_tool_call = False
        out: list[str] = []
        while self._buf:
            cont = self._scan_close() if self._inside else self._scan_open(out)
            if not cont:
                break
        return "".join(out)

    def _scan_open(self, out: list[str]) -> bool:
        """tool-call 밖: 다음 여는 태그를 찾는다. 진입/소비하면 True(계속), 보류면 False(중단)."""
        lt = self._buf.find("<")
        if lt == -1:
            out.append(self._buf)
            self._buf = ""
            return False
        if lt > 0:
            out.append(self._buf[:lt])
            self._buf = self._buf[lt:]
        # 떠도는 '닫는' 태그(</tool_function_calls> 등 래퍼 잔해)는 조용히 버린다 —
        # 복합 이름만 대상이라 평문 <tool>…</tool> 보존과 충돌하지 않는다.
        mc = _TC_STRAY_CLOSE_RE.match(self._buf)
        if mc:
            self._buf = self._buf[mc.end():]
            return True
        m = _TC_OPEN_RE.match(self._buf)
        if m:
            after = self._buf[m.end():]
            stripped = after.lstrip()
            if stripped == "":
                return False  # 태그 뒤 내용 아직 없음 → '{'/'[{' 여부 판단 위해 보류
            body_is_call = stripped[0] == "{"
            if stripped[0] == "[":
                rest = stripped[1:].lstrip()
                if rest == "":
                    return False  # '[' 뒤 내용 아직 없음 → 객체 배열('[{')인지 평문('[예시]')인지 보류
                # '[' 뒤 첫 비공백이 '{'일 때만 배열 배칭 tool-call — 그 외([예시]…)는 평문 보존
                # (_parse_response의 _TC_ARRAY_BODY_RE 가드와 동일 기준. 한쪽만 고치지 말 것)
                body_is_call = rest[0] == "{"
            if body_is_call:
                self._buf = after
                self._inside = True
                self.entered_tool_call = True
                self._tool_inner = ""
                return True
            if stripped[0] == "<":
                # 래퍼 포장지 가능성: <function_calls> 뒤에 곧장 다른 tool 태그가 오면
                # 바깥 태그는 버리고 안쪽 태그를 다음 반복에서 처리한다.
                if _TC_OPEN_RE.match(stripped) or _TC_CLOSE_RE.match(stripped):
                    self._buf = after
                    return True
                if ">" not in stripped:
                    return False  # 안쪽 태그 미완결 — 더 받아 다시 판단(보류)
            # '{'도 tool 태그도 안 이어짐 → tool-call 아님(평문 속 <tool> 등) → 태그를 가시 텍스트로 통과
            out.append(self._buf[:m.end()])
            self._buf = after
            return True
        # 여기 '<'는 (완결된) tool 여는 태그가 아님
        gt = self._buf.find(">")
        if gt == -1:
            return False  # 미완결 태그 — 더 받아 다시 판단(보류)
        out.append(self._buf[:gt + 1])  # <div>·<br/> 등 일반 태그 → 그대로 통과
        self._buf = self._buf[gt + 1:]
        return True

    def _scan_close(self) -> bool:
        """tool-call 안: 닫는 태그까지 본문을 흡수(미가시). 닫으면 True(계속), 보류면 False."""
        m = _TC_CLOSE_RE.search(self._buf)
        if m:
            self._tool_inner += self._buf[:m.start()]
            self._buf = self._buf[m.end():]
            self._inside = False
            return True
        lt = self._buf.rfind("<")
        if lt != -1 and ">" not in self._buf[lt:]:
            # 미완결 '<...' 꼬리 = 부분 닫는 태그일 수 있음 → 보류 (본문의 <br/> 등은 이미 닫혀 통과)
            self._tool_inner += self._buf[:lt]
            self._buf = self._buf[lt:]
        else:
            self._tool_inner += self._buf
            self._buf = ""
        return False

    def peek_tool_name(self) -> str:
        """축적된 tool-call 내부에서 tool 이름을 추출. 아직 없으면 빈 문자열."""
        m = self._NAME_RE.search(self._tool_inner)
        return m.group(1) if m else ""

    def flush(self) -> str:
        """스트림 종료 시 남은 가시 텍스트 반환. (tool-call 안에서 끊겼으면 누수 없이 폐기.)"""
        rest = "" if self._inside else self._buf
        self._buf = ""
        return rest


class _CliStream:
    """claude CLI(stream-json)를 실시간으로 읽어 SDK 호환 이벤트를 yield한다."""

    def __init__(self, prompt: str, model: str, has_tools: bool, system: str = "", timeout: int = 600):
        self._prompt = prompt
        self._model = model
        self._has_tools = has_tools
        self._system = system
        self._timeout = timeout
        self._final: Message | None = None
        self._usage: dict | None = None      # CLI result 이벤트의 usage
        self._cost_usd: float | None = None  # CLI result 이벤트의 total_cost_usd
        self._proc: subprocess.Popen | None = None  # 진행 중 claude 프로세스(취소 시 kill 대상)
        self._killed = False  # 취소로 kill 됐는지 — True 면 returncode 비정상을 에러로 보지 않음
        self._span_cm = None   # 퍼포먼스 span 컨텍스트(비활성 시 no-op)
        self._span = None

    def __enter__(self):
        # 퍼포먼스 계측: 스트리밍 LLM 호출 전체 구간을 span 으로 감싼다.
        self._span_cm = obs.llm_span("ai.run.claude_cli.stream", f"model={self._model or 'default'}")
        self._span = self._span_cm.__enter__()
        return self

    def __exit__(self, *args):
        # with 블록 이탈(정상/취소/예외) 시 살아 있는 프로세스를 정리하고 span 을 닫는다.
        self.kill()
        if self._span_cm is not None:
            # 스트림에서 받은 토큰/비용을 span·트랜잭션에 부착.
            obs.tag_llm_usage(self._span, self._usage, self._cost_usd)
            try:
                self._span_cm.__exit__(*args)
            except Exception:
                pass
            self._span_cm = None

    def kill(self):
        """진행 중인 claude 서브프로세스를 종료(S7).

        다른 스레드(/chat/stop)에서 호출돼도 안전. proc.kill() 은 stdout 을 EOF 로 만들어
        __iter__ 의 읽기 블록을 깨운다. 이미 끝났으면 무해.
        """
        self._killed = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    def __iter__(self):
        proc = subprocess.Popen(
            ["claude", "-p", "--output-format", "stream-json",
             "--include-partial-messages", "--verbose",
             *_model_flags(self._model), *_cli_persona_flags(self._system), *_cli_lightweight_flags()],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_cli_env(),
            cwd=_CLI_CWD,
        )
        assert proc.stdin and proc.stdout
        self._proc = proc  # 취소(cancel/kill) 대상으로 등록
        proc.stdin.write(self._prompt)
        proc.stdin.close()

        raw_text = ""
        text_started = False
        tool_started = False
        tool_name_emitted = False
        filt = _ToolCallFilter()

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            otype = obj.get("type")
            if otype == "stream_event":
                ev = obj.get("event", {})
                ev_type = ev.get("type")

                # text 블록 시작 (CLI의 native 이벤트)
                if ev_type == "content_block_start":
                    cb = ev.get("content_block", {})
                    if cb.get("type") == "text" and not text_started:
                        text_started = True
                        yield _ContentBlockStart(content_block=TextBlock(text=""))

                elif ev_type == "content_block_delta":
                    delta = ev.get("delta", {})
                    if delta.get("type") != "text_delta":
                        continue
                    raw_text += delta.get("text", "")
                    visible = filt.feed(delta.get("text", ""))

                    # <tool_call> 진입 → name 파싱되면 tool_use 이벤트 생성
                    if filt.entered_tool_call:
                        tool_name_emitted = False
                    if filt._inside and not tool_name_emitted:
                        name = filt.peek_tool_name()
                        if name:
                            tool_started = True
                            tool_name_emitted = True
                            yield _ContentBlockStart(
                                content_block=ToolUseBlock(
                                    id=f"cli_{name}", name=name, input={}
                                )
                            )

                    if visible:
                        if not text_started:
                            text_started = True
                            yield _ContentBlockStart(content_block=TextBlock(text=""))
                        yield _ContentBlockDelta(delta=_TextDelta(text=visible))

            elif otype == "result":
                if obj.get("is_error"):
                    proc.kill()
                    raise RuntimeError(
                        f"Claude CLI 오류: {obj.get('result', '')[:500]}"
                    )
                # 성공 result 이벤트: 토큰/비용 계측용으로 보관 (스트림 끝에 한 번 옴)
                self._usage = obj.get("usage")
                self._cost_usd = obj.get("total_cost_usd")

        proc.wait(timeout=self._timeout)
        if self._killed:
            return  # 취소로 종료됨 — 에러가 아니라 정상 중단(상위에서 cancelled 처리)
        if proc.returncode not in (0, None):
            err = (proc.stderr.read() if proc.stderr else "")[:500]
            raise RuntimeError(f"Claude CLI 실패 (code {proc.returncode}): {err}")

        tail = filt.flush()
        if tail:
            if not text_started:
                yield _ContentBlockStart(content_block=TextBlock(text=""))
            yield _ContentBlockDelta(delta=_TextDelta(text=tail))

        # 폴백: 스트리밍에서 tool 이벤트가 안 온 경우 텍스트에서 파싱
        if not tool_started:
            clean_text, tool_uses = _parse_response(raw_text, self._has_tools)
            for tu in tool_uses:
                yield _ContentBlockStart(
                    content_block=ToolUseBlock(id=tu.id, name=tu.name, input={})
                )
                yield _ContentBlockDelta(
                    delta=_InputJsonDelta(partial_json=json.dumps(tu.input, ensure_ascii=False))
                )
        else:
            clean_text, tool_uses = _parse_response(raw_text, self._has_tools)

        content: list = []
        if clean_text:
            content.append(TextBlock(text=clean_text))
        content.extend(tool_uses)
        self._final = Message(
            content=content,
            stop_reason="tool_use" if tool_uses else "end_turn",
            model=self._model,
            usage=self._usage or {"input_tokens": 0, "output_tokens": 0},
            cost_usd=self._cost_usd,
        )

    def get_final_message(self) -> Message:
        if self._final is None:
            raise RuntimeError("스트림이 끝나기 전에 get_final_message가 호출되었습니다")
        return self._final


# ──────────────────────────────────────────────
# 메인 클라이언트
# ──────────────────────────────────────────────

class _LocalMessages:
    """anthropic.Anthropic().messages 호환"""

    def _build_prompt(
        self,
        messages: list,
        tools: list | None = None,
        tool_choice: dict | None = None,
    ) -> str:
        # system은 stdin이 아니라 --system-prompt 플래그로 전달한다(_cli_persona_flags).
        # [SYSTEM] 텍스트로 끼워 넣으면 CLI가 user 메시지로 취급해 무시한다.
        parts: list[str] = []
        if tools:
            parts.append(_format_tools(tools, tool_choice))
        parts.append(_format_messages(messages))
        # 도구 강제는 프롬프트 '맨 끝'(응답 직전)에도 리마인드 — 상단 tools 헤더의 지시는
        # 긴 히스토리에 묻혀 소형 모델(haiku)이 놓치기 쉽다. 말단 배치가 형식 준수에 결정적.
        if tools and tool_choice:
            parts.append(_tool_choice_reminder(tool_choice))
        # 대화를 평문으로 넘기면 모델이 마지막 [USER] 문장을 '이어서' 써버린다.
        # [ASSISTANT] 큐를 붙여 새 어시스턴트 턴이 시작됨을 명시(턴 경계).
        parts.append("[ASSISTANT]\n")
        return "\n\n".join(parts)

    def create(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        messages: list,
        tools: list | None = None,
        tool_choice: dict | None = None,
    ) -> Message:
        prompt = self._build_prompt(messages, tools, tool_choice)
        raw, usage, cost_usd = _call_cli(prompt, system=system, model=model)
        clean_text, tool_uses = _parse_response(raw, bool(tools))

        content: list = []
        if clean_text:
            content.append(TextBlock(text=clean_text))
        content.extend(tool_uses)

        return Message(
            content=content,
            stop_reason="tool_use" if tool_uses else "end_turn",
            model=model,
            usage=usage or {"input_tokens": 0, "output_tokens": 0},
            cost_usd=cost_usd,
        )

    def stream(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        messages: list,
        tools: list | None = None,
        tool_choice: dict | None = None,
    ) -> _CliStream:
        # tool_choice는 _build_prompt→_format_tools에서 "반드시 도구를 호출하라"는
        # 지시문으로 프롬프트에 주입된다(CLI엔 네이티브 tool_choice가 없음 → 소프트 강제).
        prompt = self._build_prompt(messages, tools, tool_choice)
        return _CliStream(prompt, model, has_tools=bool(tools), system=system)


class LocalClaudeClient:
    """anthropic.Anthropic() 드롭인 대체"""

    def __init__(self):
        self.messages = _LocalMessages()


def call_route(client) -> str:
    """이 클라이언트로 나간 **마지막 호출이 실제로 탄 경로**.

    설정값(USE_LOCAL_CLAUDE)이 아니라 실제 경로를 봐야 비용이 맞는다. 두 가지가 어긋난다:

      ① 폴백 — API 모드인데 인증이 실패하면 그 턴은 구독(CLI)으로 나간다.
         설정만 보고 'api' 로 기록하면 실청구 0 인 턴을 과금 턴으로 세게 된다.
      ② 전환 — 운영 중 모드를 바꾼 날은 하루 안에 두 경로가 섞인다
         (2026-08-21 실제로 그랬다). 날짜 단위 라벨로는 구분이 안 된다.

    반환: "cli" | "api" | "api_fallback_cli" | "" (판정 불가)
    """
    if client is None:
        return ""
    if isinstance(client, LocalClaudeClient):
        return "cli"
    msgs = getattr(client, "messages", None)
    route = getattr(msgs, "last_route", "")
    if route:
        return route
    # 아직 호출 전이면 설정상 예정 경로로 답한다(기록 시점엔 항상 호출 후다).
    return "cli" if _use_local_cli() else "api"
