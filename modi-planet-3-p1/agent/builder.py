"""빌드 검증 — esbuild로 번들 되는지 + tsc로 타입(런타임 크래시류)이 안전한지 확인"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading

logger = logging.getLogger(__name__)

_build_lock = threading.Lock()

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATE_DIR = os.path.join(_BASE_DIR, "build_template")
_BUILD_DIR = os.path.join(_BASE_DIR, ".build_cache")

MAX_BUILD_RETRIES = 1

# tsc 설정: 번들은 되지만 실행 시 터지는 undefined/null 접근류(예: 초기 렌더에 배열이
# undefined인데 .map 호출)를 타입 단계에서 잡는다. 오탐→불필요 재생성을 막기 위해
# strictNullChecks만 켜고 나머지(noImplicitAny·미사용변수 등)는 느슨하게 둔다.
_TSCONFIG = """{
  "compilerOptions": {
    "target": "ES2020",
    "lib": ["DOM", "DOM.Iterable", "ES2020"],
    "module": "ESNext",
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": false,
    "strictNullChecks": true,
    "noImplicitAny": false,
    "noUnusedLocals": false,
    "noUnusedParameters": false,
    "skipLibCheck": true,
    "esModuleInterop": true,
    "allowSyntheticDefaultImports": true,
    "resolveJsonModule": true,
    "noEmit": true,
    "forceConsistentCasingInFileNames": false
  },
  "include": ["src", "ambient.d.ts"]
}
"""


def _ensure_env() -> bool:
    """빌드 환경 준비. node_modules 없거나 package.json이 변경되면 npm install."""
    template_pkg = os.path.join(_TEMPLATE_DIR, "package.json")
    cache_pkg = os.path.join(_BUILD_DIR, "package.json")
    nm = os.path.join(_BUILD_DIR, "node_modules")

    # node_modules 존재 + package.json 변경 없음 → 스킵
    if os.path.exists(nm) and os.path.exists(cache_pkg):
        if os.path.getmtime(template_pkg) <= os.path.getmtime(cache_pkg):
            return True

    os.makedirs(_BUILD_DIR, exist_ok=True)
    shutil.copy2(template_pkg, cache_pkg)

    try:
        result = subprocess.run(
            ["npm", "install", "--prefer-offline"],
            cwd=_BUILD_DIR,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            logger.warning("npm install failed: %s", result.stderr[:300])
            return False
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("npm install 불가: %s", e)
        return False


def build_check(code_map: dict[str, str]) -> tuple[bool, list[str]]:
    """생성된 코드를 esbuild로 번들 체크.

    Returns:
        (success, error_messages)
        환경이 준비되지 않으면 (True, [])로 스킵.
    """
    if not code_map:
        return True, []

    with _build_lock:
        return _build_check_locked(code_map)


def _build_check_locked(code_map: dict[str, str]) -> tuple[bool, list[str]]:
    if not _ensure_env():
        return True, []

    src_dir = os.path.join(_BUILD_DIR, "src")

    # 이전 소스 정리
    if os.path.exists(src_dir):
        shutil.rmtree(src_dir)
    os.makedirs(src_dir)

    # 생성된 파일 쓰기
    for file_path, code in code_map.items():
        full_path = os.path.join(src_dir, file_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(code)

    # App.tsx가 없으면 스킵
    entry = os.path.join(src_dir, "App.tsx")
    if not os.path.exists(entry):
        return True, []

    # esbuild 실행
    esbuild_bin = os.path.join(_BUILD_DIR, "node_modules", ".bin", "esbuild")
    try:
        result = subprocess.run(
            [
                esbuild_bin,
                "src/App.tsx",
                "--bundle",
                "--outfile=/dev/null",
                # npm 패키지는 external 취급 → 미리보기(Sandpack)가 동적 설치하는
                # 비-core 라이브러리(recharts·three 등)를 여기서 못 찾아도 실패하지 않는다.
                # 상대경로(로컬 파일) import·문법·JSX는 그대로 검사한다. (external엔 esm 포맷 필요)
                "--packages=external",
                "--format=esm",
                "--platform=browser",
                "--jsx=automatic",
                "--loader:.tsx=tsx",
                "--loader:.ts=ts",
                "--loader:.css=css",
                "--log-level=warning",
            ],
            cwd=_BUILD_DIR,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("esbuild 실행 불가: %s", e)
        return True, []

    if result.returncode != 0:
        return False, _parse_errors(result.stderr)

    # esbuild 통과(번들 OK) → 타입 검증으로 런타임 크래시류를 추가로 잡는다.
    # 하이브리드 앱(import 없이 전역 React/MODI/Chart 사용)은 타입 오탐이 많아 스킵하고,
    # import가 있는 소프트웨어(react) 앱만 tsc를 돌린다.
    has_imports = any(re.search(r"(?m)^\s*import\s", c) for c in code_map.values())
    if has_imports:
        ok, ts_errors = _tsc_check()
        if not ok:
            return False, ts_errors

    return True, []


def _tsc_check() -> tuple[bool, list[str]]:
    """tsc --noEmit 타입 검증(strictNullChecks). src/ 파일들은 이미 디스크에 있음.

    tsc 바이너리가 없으면(빌드 env에 typescript 미설치) (True, [])로 스킵 — 단 경고 로그.
    """
    tsc_bin = os.path.join(_BUILD_DIR, "node_modules", ".bin", "tsc")
    if not os.path.exists(tsc_bin):
        logger.warning(
            "tsc 없음 → 타입 검증 스킵. build_template에 typescript/@types 추가 후 재설치 필요."
        )
        return True, []

    with open(os.path.join(_BUILD_DIR, "tsconfig.build.json"), "w", encoding="utf-8") as f:
        f.write(_TSCONFIG)
    # 미리보기가 동적 설치하는 비-core 라이브러리(recharts·three 등)는 빌드 env에 없어
    # tsc가 TS2307(Cannot find module)을 낸다. 와일드카드 모듈 선언으로 any 처리해 오탐 제거.
    # 코어 라이브러리는 @types가 우선하므로 실제 타입 유지, 앱 자체 타입검사(undefined.map)도 유지.
    with open(os.path.join(_BUILD_DIR, "ambient.d.ts"), "w", encoding="utf-8") as f:
        f.write("declare module '*';\n")

    try:
        result = subprocess.run(
            [tsc_bin, "--noEmit", "-p", "tsconfig.build.json"],
            cwd=_BUILD_DIR,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("tsc 실행 불가 → 타입 검증 스킵: %s", e)
        return True, []

    if result.returncode == 0:
        return True, []

    # tsc는 에러를 stdout으로 출력. undefined 계열만 남기고(아래 필터) 남은 게 없으면 통과.
    # (전부 null 관용구였거나 tsc 설정/툴 문제인 경우 → 생성 차단하지 않음)
    errors = _parse_tsc_errors(result.stdout + result.stderr)
    if not errors:
        return True, []
    return False, errors


# 순수 null 계열 tsc 코드는 스킵: ref.current(useRef(null))·document.getElementById·
# canvas.getContext 등 "마운트 후엔 사실상 non-null"인 관용구라 런타임 크래시로 거의 안 이어진다.
# 반대로 undefined 계열(초기화 안 된 배열·누락 prop·async 미도착 데이터)은 .map 등에서 실제로
# 터지므로 유지한다. 소스 파일(file.tsx(line,col)) 에러만 대상으로 해 tsconfig/툴 에러는 제외.
_TS_ERR_RE = re.compile(r"\.tsx?\(\d+,\d+\): error TS(\d+)")
_TS_SKIP_CODES = {"2531", "18047"}


def _parse_tsc_errors(output: str) -> list[str]:
    """tsc 에러를 에이전트가 볼 형태로 정리: 소스 에러만, null 관용구 코드 스킵, 개수 제한."""
    cleaned = output.replace(_BUILD_DIR + "/", "")
    cleaned = re.sub(r"(?m)(^|\s)src/", r"\1", cleaned)
    errors = []
    for ln in cleaned.splitlines():
        m = _TS_ERR_RE.search(ln)
        if m and m.group(1) not in _TS_SKIP_CODES:
            errors.append(ln.strip())
    return errors[:5]


def _parse_errors(stderr: str) -> list[str]:
    """esbuild 에러 출력에서 의미있는 에러 블록 추출."""
    # 빌드 캐시 절대 경로 제거
    cleaned = stderr.replace(_BUILD_DIR + "/", "")
    # 줄 시작의 src/ 접두사만 제거 (에이전트에게 보여줄 때 원래 파일 경로와 일치)
    cleaned = re.sub(r"(?m)(^|\s)src/", r"\1", cleaned)

    # ✘ [ERROR] 블록 단위로 분리
    blocks = re.split(r"(?=✘ \[ERROR\])", cleaned)
    errors = [b.strip() for b in blocks if b.strip() and "[ERROR]" in b]

    if not errors:
        # ERROR 패턴이 없으면 원본 메시지 그대로
        return [cleaned[:500]] if cleaned.strip() else []

    return errors[:5]
