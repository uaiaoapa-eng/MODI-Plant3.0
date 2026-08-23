# `/chat` SSE 에러 이벤트 계약

> 대상: 프론트엔드 구현체(외부 리포, ai.modiplanet.com 등) 인계용 SSOT 문서.
> 이 문서는 **런타임 코드를 변경하지 않는다** — 현재 서버 동작을 그대로 옮겨 적은 것이다.
> 근거 코드: `agent/errors.py`(카탈로그), `server.py`(SSE 방출 지점), `agent/orchestrator_stream.py`(스트림 내 방출 지점).

## 1. 전달 방식

`POST /chat` 은 **항상 HTTP 200 + `Content-Type: text/event-stream`** 으로 응답한다.
에러는 HTTP 상태 코드가 아니라 **스트림 본문의 이벤트**로 온다.

- 429(쿼터 초과), 409(세션 중복 처리) 같은 HTTP 상태 코드를 기대하지 말 것 — 실제로는 오지 않는다.
- 이 규약은 `/chat` 에만 해당한다. 다른 REST 엔드포인트(`/api/*`)는 표준 HTTP 에러 응답(`error_response`, 아래 §5)을 쓴다.

요청 본문(`ChatRequest`, `server.py:235-243`): `session_id`(기본 `"default"`), `message`, `mode`(`"design"|"quick"`, 기본 `"design"`), `coding_type`(`"react"|"blockly"`, 기본 `"react"`), `runtime_error`(선택). 사용자 식별은 쿼리 파라미터 `user_id` 또는 `X-User-Id` 헤더(`server.py:226-232`).

## 2. SSE 직렬화 형식

이벤트 1건은 아래 형식의 한 줄로 온다(`server.py:149-153` `_sse_chunk`):

```
data: <JSON 한 줄>\n\n
```

JSON은 `ensure_ascii=False`(한글 등 유니코드 원문 그대로)로 직렬화되고, 서로게이트 문자는 `errors="replace"`로 정리된다. 파서는 `\n\n` 로 이벤트 경계를 나눠야 한다.

## 3. `error` 이벤트 스키마

```
data: {"type":"error","code":<str>,"message":<str>,"retryable":<bool>[,"retry_after":<int 초>]}\n\n
```

(`agent/errors.py:102-122` `error_event`)

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `type` | `"error"` | 항상 | 고정값 |
| `code` | string | 항상 | 아래 §4 카탈로그의 code 값 |
| `message` | string | 항상 | 사용자에게 보여줄 한국어 문구. 카탈로그 기본값이거나(대부분) 호출부가 동적으로 override(`llm_quota`의 리셋시각 포함 문구 등) |
| `retryable` | bool | 항상 | 카탈로그 고정값(코드별) |
| `retry_after` | int(초) | 있을 때만 | 현재 `quota_exceeded` 에만 실린다(`server.py:350,353`). 없으면 필드 자체가 없다 — `undefined`로 처리할 것, `0`이나 `null`이 아니다 |

## 4. `code` 카탈로그 (`agent/errors.py:45-99` 그대로 대조)

`/chat` SSE 스트림에서 실제로 방출되는 code:

| code | message (기본값) | retryable | 방출 지점 |
|---|---|---|---|
| `quota_exceeded` | 사용 한도에 도달했어요. 잠시 후 다시 시도해주세요. | `false` | `server.py:352-355` (일일 토큰/턴 상한 초과, `retry_after` 동반) |
| `session_busy` | 이 세션의 이전 요청을 아직 처리 중이에요. 잠시 후 다시 시도해주세요. | `true` | `server.py:366-369` (같은 세션 동시 요청 거절) |
| `blocked` | 요청을 처리할 수 없어요. | `false` | `server.py:330-333` (차단 subject 목록 매칭) |
| `internal` | 처리 중 오류가 발생했어요. 잠시 후 다시 시도해주세요. | `false` | `server.py:384-388` (스트림 처리 중 미분류 예외) |
| `llm_auth` | 지금 코딩 도우미에 연결할 수 없어요. 잠시 후 다시 시도해 주세요. 문제가 계속되면 선생님께 알려주세요. | `false` | `agent/orchestrator_stream.py:1934` (Claude CLI 미로그인/인증 만료) |
| `llm_quota` | Claude 사용 한도에 도달했어요. 잠시 후 다시 시도해주세요. (리셋 시각이 있으면 동적으로 override) | `false` | `agent/orchestrator_stream.py:1944` (Claude CLI 구독 한도) |

카탈로그에는 있으나 **현재 `/chat` SSE 경로에서는 방출되지 않는(예약) code**:

| code | message | retryable | 비고 |
|---|---|---|---|
| `llm_overloaded` | 서버가 잠시 붐비고 있어요. 잠시 후 다시 시도해주세요. | `true` | 카탈로그/단위테스트(`tests/test_errors.py`)에는 있으나 현재 코드에서 이 code로 SSE 방출하는 지점 없음(재시도 로직은 `is_retryable_error` 소진 시 예외를 올려 `internal`로 귀결됨) |
| `auth_required` | 로그인이 필요해요. | `false` | 예약(발생 경로 없음), `agent/errors.py:33` |
| `auth_expired` | 로그인이 만료되었어요. 다시 로그인해주세요. | `false` | 예약(발생 경로 없음), `agent/errors.py:34` |
| `not_found` | 요청한 대상을 찾을 수 없어요. | `false` | `/chat`이 아닌 다른 REST 엔드포인트의 HTTP 에러 응답 전용(§5) |
| `invalid_input` | 입력이 올바르지 않아요. | `false` | 위와 동일, HTTP 전용 |
| `upstream_error` | 연동 서비스에 일시적인 문제가 있어요. 잠시 후 다시 시도해주세요. | `true` | 위와 동일, HTTP 전용(`server.py:1203-1222`) |

프론트는 §4 상단 6개 code를 우선 처리하고, 그 외 미지의 `code` 값이 와도 안전하게(예: 일반 에러 메시지로) 폴백해야 한다 — 카탈로그는 추가될 수 있다.

## 5. `token` 폴백 규약 (`SSE_ERROR_AS_TOKEN`)

두 가지 서로 다른 "token 선행" 동작이 있다. 혼동하지 않도록 구분한다.

**(a) 오늘 이미 코드에 있는 동작** — `llm_auth`/`llm_quota` 는 `agent/orchestrator_stream.py` 안에서 **항상**(킬스위치 없이) 같은 문구의 `{"type":"token","text":<message>}` 1건이 `error` 이벤트보다 먼저 나간다(`orchestrator_stream.py:1929-1934`, `1940-1944`). 즉 순서는 `token` → `error` → (스트림 종료, 아래 §6).

**(b) 킬스위치 `SSE_ERROR_AS_TOKEN` 폴백(설계, 관련 작업: 이슈 #147)** — `quota_exceeded`·`blocked`·`session_busy` 3개 게이트 레벨 차단(§4 표의 `server.py` 방출 3건)은 **현재는 `token` 선행 없이 `error` → `done` 만 나간다.** 프론트가 `type:"token"`만 렌더하고 `type:"error"`를 무시하면 이 3가지 상황에서 화면에 아무것도 안 뜨는 무응답이 생긴다(관련 설계: `docs/design/chat-error-surfacing-and-usage-turns-fix.md`). 이를 보완하기 위해 서버에 killswitch env `SSE_ERROR_AS_TOKEN`(기본 `true`)을 도입해, 켜져 있으면 이 3가지 차단에서도 `error` 앞에 카탈로그 `user_message` 그대로의 `{"type":"token","text":<message>}` 1건을 선행 방출하는 것으로 설계돼 있다:

```
data: {"type":"token","text":<message>}\n\n   # SSE_ERROR_AS_TOKEN=true 일 때만
data: {"type":"error","code":<str>,"message":<str>,"retryable":<bool>[,"retry_after":<int>]}\n\n
data: {"type":"done"}\n\n
```

프론트가 `type:"error"`를 네이티브 렌더하게 되면, 서버 운영자가 `SSE_ERROR_AS_TOKEN=false`로 꺼서 `token` 중복 렌더를 없앨 수 있다(운영 스위치).

**결론**: 프론트는 아래 순서로 처리하면 (a)/(b) 어느 조합이 와도 안전하다.
1. `type==="error"` 를 **네이티브로 렌더**한다(말풍선/토스트 등). 이게 이 계약의 핵심 처리다.
2. `type==="error"`를 이미 렌더했다면, 그 직전/직후에 오는 같은 문구의 `type==="token"`은 **무시**(중복 렌더 방지).
3. `type==="error"`를 아직 렌더할 수 없는(레거시) 프론트라면 `type==="token"`만으로도 문구가 화면에 뜬다 — 단, `session_busy`/`blocked`/`quota_exceeded` 3종은 `SSE_ERROR_AS_TOKEN=true`(기본값)일 때만 이 경로로 동작한다.

## 6. 스트림 종료

에러 발생 시 그 직후 항상 아래 이벤트로 스트림이 끝난다:

```
data: {"type":"done"}\n\n
```

(`server.py:332,354,368,388`) 이후 연결이 닫힌다. `done` 이 오지 않고 연결만 끊기는 경우는 클라이언트 측 취소(`GeneratorExit`)이며 에러가 아니다.

## 7. 프론트 처리 규약 요약

- `type==="error"` → `message` 를 사용자에게 노출(말풍선/토스트).
- `retryable===true` → 재시도 버튼/자동 재시도 노출.
- `retry_after` 필드가 있으면 → 해당 초(정수) 카운트다운 UI 표시, 만료 후 재시도 허용.
- `code` 로 분기해 문구 이상의 처리(예: `llm_auth`→ "선생님 호출" 버튼, `quota_exceeded`→ 리셋 안내)를 붙일 수 있다.
- `type==="token"` 은 §5 규약에 따라 `error`와 중복되면 무시.

## 8. 최소 JS 처리 예시

```javascript
async function streamChat(payload, { onToken, onError, onDone }) {
  const res = await fetch("/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload), // { session_id, message, mode, coding_type, runtime_error }
  });

  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buf = "";
  let errorShown = false; // "error"를 렌더했으면 짝인 token 폴백을 무시하기 위한 플래그

  while (true) {
    const { value, done: streamDone } = await reader.read();
    if (streamDone) break;
    buf += decoder.decode(value, { stream: true });

    let idx;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const rawEvent = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const line = rawEvent.startsWith("data: ") ? rawEvent.slice(6) : rawEvent;
      if (!line) continue;

      const event = JSON.parse(line);
      switch (event.type) {
        case "token":
          if (!errorShown) onToken(event.text);
          break;
        case "error":
          errorShown = true;
          onError({
            code: event.code,
            message: event.message,
            retryable: event.retryable,
            retryAfter: event.retry_after, // 없으면 undefined
          });
          break;
        case "done":
          onDone();
          break;
        default:
          // 알려지지 않은 type은 무시(향후 확장 대비)
          break;
      }
    }
  }
}
```

## 9. 참고: HTTP 전용 에러 응답 (`/chat` 이 아닌 엔드포인트)

`/chat` 이외의 REST 엔드포인트는 SSE가 아니라 표준 JSON 에러 응답을 쓴다(`agent/errors.py:125-142` `error_response`):

```
HTTP <카탈로그 http_status>
{"ok": false, "error": {"code": <str>, "message": <str>[, "detail": <str>]}}
```

`code=internal` 인 경우 `detail` 은 정보 노출 방지를 위해 항상 생략된다. 이 §9는 `/chat` SSE 계약과 무관하며 혼동 방지를 위해 병기한다.
