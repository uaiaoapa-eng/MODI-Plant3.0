"""텍스트 덤프에서 코드 복구(salvage).

구현 응답이 generate_code 도구 호출 없이 마크다운 코드펜스로 코드를 쏟았을 때,
펜스를 파일 단위로 역파싱해 도구 호출로 되살린다. LLM 재시도(비용) 전에 시도하는
0원짜리 복구 계층 — CLI 래퍼가 <tool_call> 태그를 텍스트에서 파싱하는 것과 같은
원리를 '태그 없는 덤프'에 적용한 것이다.

보수적으로 판정한다:
- 파일 경로가 명시된 펜스(직전 라인 또는 본문 첫 줄 주석)만 파일로 신뢰
- 경로가 하나도 없으면, 'export default가 있는 단일 실질 펜스'만 App.tsx로 폴백
- App 엔트리가 없으면 미리보기가 어차피 못 뜨므로 복구 실패([]) — 재시도가 낫다
"""

from __future__ import annotations

import re

# 미리보기(Sandpack)가 다루는 코드 파일 확장자
_CODE_EXTS = ("tsx", "ts", "jsx", "js", "css", "html", "json")
_PATH_RE = re.compile(r"([\w@$-][\w@$.-]*(?:/[\w@$.-]+)*\.(?:%s))\b" % "|".join(_CODE_EXTS))
# ```lang ... ``` — 마지막 펜스가 안 닫혔으면(출력 잘림) 문서 끝까지를 본문으로 본다
_FENCE_RE = re.compile(r"```[ \t]*([\w+-]*)[^\n]*\n(.*?)(?:\n?```|\Z)", re.DOTALL)
# 코드 파일일 수 없는 펜스 언어 태그
_NON_CODE_LANGS = {"bash", "sh", "shell", "zsh", "text", "txt", "md", "markdown", "console", "output"}

# 파일 하나로 인정할 최소 본문 길이 — 사용법 예시 같은 짧은 조각을 거른다
_MIN_BODY_CHARS = 80
_MAX_FILES = 15


def _path_near_fence(before_text: str, body: str) -> str:
    """펜스 직전 라인들(예: "**App.tsx**", "### components/Header.tsx") 또는
    본문 첫 줄 주석(예: "// src/App.tsx")에서 파일 경로를 찾는다."""
    tail = [ln for ln in before_text.splitlines() if ln.strip()][-2:]
    for line in reversed(tail):
        m = _PATH_RE.search(line)
        if m:
            return m.group(1)
    first = body.lstrip().splitlines()[0] if body.strip() else ""
    if first.lstrip().startswith(("//", "/*", "<!--", "#")):
        m = _PATH_RE.search(first)
        if m:
            return m.group(1)
    return ""


def extract_code_files(text: str, known_paths=None) -> list[tuple[str, str]]:
    """덤프 텍스트에서 (file_path, code) 목록을 추출. 복구 불가면 빈 리스트.

    같은 경로가 여러 번 나오면 마지막 것(모델이 낸 수정본)이 이긴다.

    known_paths: 이미 생성된 파일 경로들(수정 턴). 주어지면 그중 하나와 겹치는
    펜스가 있어야 '이 앱의 코드 덤프'로 신뢰하고, App 엔트리 요구는 하지 않는다
    (수정 덤프는 일부 파일만 담는 게 정상). 비어 있으면 신규 생성 규칙(App 필수).
    """
    files: dict[str, str] = {}
    unnamed: list[str] = []
    for m in _FENCE_RE.finditer(text):
        lang = (m.group(1) or "").lower()
        body = m.group(2).strip("\n")
        if len(body.strip()) < _MIN_BODY_CHARS:
            continue
        if lang in _NON_CODE_LANGS:
            continue
        path = _path_near_fence(text[: m.start()], body)
        if path:
            files[path] = body
        else:
            unnamed.append(body)
        if len(files) >= _MAX_FILES:
            break

    known = set(known_paths or ())
    # 경로 없는 펜스 폴백: 실질적인 React 컴포넌트로 보이는 '단일' 펜스만 App.tsx로 (신규 생성만)
    if not files and not known and len(unnamed) == 1 and "export default" in unnamed[0]:
        files["App.tsx"] = unnamed[0]

    if known:
        # 수정 턴: 기존 파일과 겹치는 경로가 하나도 없으면 이 앱의 덤프라 확신 못 함
        if not (set(files) & known):
            return []
    elif not any(p.rsplit("/", 1)[-1].startswith("App.") for p in files):
        # 신규 생성: 엔트리(App.*)가 없으면 미리보기가 못 뜬다 — 재시도가 낫다
        return []
    return list(files.items())
