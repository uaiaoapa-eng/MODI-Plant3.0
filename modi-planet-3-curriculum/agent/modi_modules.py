"""MODI Blockly XML → 준비물(modules) + 조립(assembly) + 배치(layout) 생성."""
from __future__ import annotations

import re
from typing import Dict, List, Any

# 블록 타입 접두사 → 물리 모듈 키
_PREFIX_TO_MODULE = [
    ("input_button", "button"),
    ("input_dial", "dial"),
    ("input_joystick", "joystick"),
    ("input_environment", "env"),
    ("input_imu", "imu"),
    ("input_tof", "tof"),
    ("output_motorA", "motor_a"),
    ("output_motorB", "motor_b"),
    ("output_led", "led"),
    ("output_speaker", "speaker"),
    ("output_display", "display"),
    ("network_upload", "network"),
]

# 모듈 키 → (역할, 한 줄 이유, 한글 라벨)
_MODULE_META: Dict[str, tuple] = {
    "network": ("필수", "PC와 통신하고 전원을 공급해요", "네트워크"),
    "button": ("입력", "버튼 누름을 감지해요", "버튼"),
    "dial": ("입력", "다이얼 회전값을 입력해요", "다이얼"),
    "joystick": ("입력", "조이스틱 방향을 입력해요", "조이스틱"),
    "env": ("입력", "온도·습도·조도·소리를 감지해요", "환경 센서"),
    "imu": ("입력", "기울기·움직임을 감지해요", "자이로(IMU)"),
    "tof": ("입력", "거리를 감지해요", "거리 센서(ToF)"),
    "motor_a": ("출력", "축을 회전시켜요. 바퀴로 쓰면 오른쪽 바퀴예요", "모터 A"),
    "motor_b": ("출력", "축을 회전시켜요. 바퀴로 쓰면 왼쪽 바퀴예요", "모터 B"),
    "led": ("출력", "색깔 빛을 표시해요", "LED"),
    "speaker": ("출력", "소리·멜로디를 출력해요", "스피커"),
    "display": ("출력", "글자·그림을 표시해요", "디스플레이"),
    # 모터 축에 끼우는 부착물 (모듈 아님)
    "wheel": ("부품", "모터 축에 끼워 굴러가는 바퀴예요", "바퀴"),
    "i_horn": ("부품", "모터 축에 끼워 흔드는 막대(I-혼)예요", "I-혼"),
    # 자동차 부품 (모듈 아님 — 바퀴를 달면 높이가 생겨 필요)
    "basic_brick": ("부품", "모듈을 올려 고정하는 2×4 바닥판이에요", "기본 브릭"),
    "extra_wheel_brick": ("부품", "반대쪽 끝에 달아 균형을 잡는 보조 바퀴예요", "보조 바퀴"),
}

_VALID_MODULES = set(_MODULE_META.keys())

_MOTOR_KEYS = ("motor_a", "motor_b")

# 모터 축에 끼우는 부착물 — grid 셀이 아니라 attachments 맵으로 받는다.
# (다이어그램에는 그리지 않고 준비물·조립 안내에만 표시한다.)
_ATTACH_VALUES = ("wheel", "i_horn")

# 자동차 부품 — grid·다이어그램에 없고, 바퀴 자동차면 준비물·조립에 자동 추가된다.
_CAR_ACCESSORIES = ("basic_brick", "extra_wheel_brick")

# grid에 놓을 수 있는 모듈(부착물·자동차부품 제외)
_GRID_MODULES = _VALID_MODULES - set(_ATTACH_VALUES) - set(_CAR_ACCESSORIES)

# 모터 기본(회전 0°) 축 방향 — 이미지 기준: 모터A 축=왼쪽, 모터B 축=오른쪽.
# 시계방향 회전각 → 축이 가리키는 방향.
_SHAFT = {
    "motor_a": {0: "left", 90: "up", 180: "right", 270: "down"},
    "motor_b": {0: "right", 90: "down", 180: "left", 270: "up"},
}

# 모터 '캡'(윗부분, 자석 없는 면) 방향 — 이미지상 A·B 모두 0°에서 위쪽. 회전과 함께 돈다.
# 축(_SHAFT)과 항상 90° 직각이며, 축·캡 두 면엔 자석이 없어 모듈이 못 붙는다(나머지 두 면만 연결).
_CAP = {0: "up", 90: "right", 180: "down", 270: "left"}


def _norm_rot(value) -> int:
    """회전각을 0/90/180/270 중 하나로 정규화. 잘못된 값은 0."""
    try:
        deg = int(round(float(value) / 90.0)) * 90
    except (TypeError, ValueError):
        return 0
    return deg % 360


def sanitize_rotations(rotations) -> Dict[str, int]:
    """LLM rotations 맵 정규화: 유효 모듈 키 + 0/90/180/270, 0(무회전)은 제거."""
    if not isinstance(rotations, dict):
        return {}
    out: Dict[str, int] = {}
    for key, value in rotations.items():
        if key in _GRID_MODULES:
            deg = _norm_rot(value)
            if deg:
                out[key] = deg
    return out


def sanitize_attachments(attachments) -> Dict[str, str]:
    """LLM attachments 맵 정규화: 모터 키 → 'wheel'/'i_horn' 만 남김."""
    if not isinstance(attachments, dict):
        return {}
    out: Dict[str, str] = {}
    for key, value in attachments.items():
        if key in _MOTOR_KEYS and value in _ATTACH_VALUES:
            out[key] = value
    return out


def _wheel_axle(grid: List[List[str | None]], rotations: Dict[str, int]):
    """두 모터가 '바퀴 축' 구성인지 판정.

    바퀴로 쓸 때만 — 모터B·모터A가 (B 왼쪽, A 오른쪽) 인접하고, 회전으로 두 축이
    바깥을 향할 때 — True. 회전값은 LLM이 정하므로 팔·회전대 등 바퀴가 아닌 용도엔
    해당하지 않는다 (모터가 항상 바퀴는 아님). 반환: (row, col_b, col_a) 또는 None.
    """
    pos: Dict[str, tuple] = {}
    for r, row in enumerate(grid):
        for c, key in enumerate(row):
            if key in _MOTOR_KEYS:
                pos.setdefault(key, (r, c))
    if "motor_a" not in pos or "motor_b" not in pos:
        return None
    (ra, ca), (rb, cb) = pos["motor_a"], pos["motor_b"]
    if ra != rb or cb + 1 != ca:  # 모터B 바로 오른쪽 칸이 모터A여야 함
        return None
    rot_a = _norm_rot(rotations.get("motor_a", 0))
    rot_b = _norm_rot(rotations.get("motor_b", 0))
    if _SHAFT["motor_a"][rot_a] == "right" and _SHAFT["motor_b"][rot_b] == "left":
        return (ra, cb, ca)
    return None


def _is_wheel_car(grid, rotations, attachments) -> bool:
    """두 바퀴 자동차인지: 모터가 바퀴 축 구성이거나 휠 부착물이 있으면 True."""
    return (_wheel_axle(grid, sanitize_rotations(rotations)) is not None
            or "wheel" in sanitize_attachments(attachments).values())


def accessory_parts(grid, rotations=None, attachments=None) -> List[Dict[str, Any]]:
    """다이어그램엔 없지만 준비물 목록에 필요한 부품 항목.

    - 축 부착물(휠·I-혼): LLM attachments에서
    - 두 바퀴 자동차면: 휠 2개 보장 + 보조 바퀴 1 + 기본 브릭 1
      (바퀴를 달면 차체에 높이가 생겨 바닥판·보조바퀴가 필요)
    """
    counts: Dict[str, int] = {}
    for att in sanitize_attachments(attachments).values():
        counts[att] = counts.get(att, 0) + 1
    if _is_wheel_car(grid, rotations, attachments):
        counts["wheel"] = max(counts.get("wheel", 0), 2)
        counts["extra_wheel_brick"] = 1
        counts["basic_brick"] = 1
    parts: List[Dict[str, Any]] = []
    for key, count in counts.items():
        role, reason, _ = _MODULE_META.get(key, ("부품", "", key))
        parts.append({"key": key, "role": role, "reason": reason, "count": count})
    return parts


def _block_to_module(block_type: str) -> str | None:
    for prefix, module in _PREFIX_TO_MODULE:
        if block_type.startswith(prefix):
            return module
    return None


def normalize_module_keys(keys: List[str] | None) -> List[str]:
    """MODI 모듈 키를 network 1개 + 실제 모듈 중복 제거 형태로 정규화."""
    ordered: List[str] = []
    for key in keys or []:
        if key == "network" or key not in _GRID_MODULES:
            continue
        if key not in ordered:
            ordered.append(key)
    return ["network"] + ordered


def has_interaction_module(keys: List[str] | None) -> bool:
    """network 외에 웹/블록 로직과 상호작용할 실제 MODI 모듈이 있는지."""
    return any(key != "network" for key in normalize_module_keys(keys))


def validate_modi_module_contract(keys: List[str] | None, context: str = "MODI") -> List[str]:
    """공통 MODI 준비물 계약: network 정확히 1개 + network 외 최소 1개 모듈."""
    raw = [key for key in (keys or []) if key in _GRID_MODULES]
    errors: List[str] = []
    if raw.count("network") != 1:
        errors.append(f"{context}: network 모듈은 반드시 1개만 포함해야 합니다.")
    if not has_interaction_module(raw):
        errors.append(f"{context}: network 외에 실제 동작하는 MODI 모듈을 최소 1개 포함해야 합니다.")
    return errors


def extract_raw_module_keys_from_xml(xml: str) -> List[str]:
    """Blockly XML에서 등장한 MODI 모듈 키를 그대로 반환한다. 검증용이라 중복을 보존한다."""
    types = re.findall(r"""type=["']([^"']+)["']""", xml)
    keys: List[str] = []
    for t in types:
        module = _block_to_module(t)
        if module:
            keys.append(module)
    return keys


def _extract_module_keys(xml: str) -> List[str]:
    """XML에서 필요한 모듈 키를 network 1개 + 중복 없는 실제 모듈 형태로 반환."""
    keys: List[str] = []
    for module in extract_raw_module_keys_from_xml(xml):
        if module not in keys:
            keys.append(module)
    return normalize_module_keys(keys)


def extract_module_keys_from_xml(xml: str) -> List[str]:
    """Blockly XML → 모듈 키 (build_modi_modules_doc와 짝). 하이브리드의
    extract_modi_module_keys(코드 기반)에 대응하는 블록(XML 기반) 추출기."""
    return _extract_module_keys(xml)


def _build_modules(keys: List[str]) -> List[Dict[str, Any]]:
    modules = []
    for key in keys:
        role, reason, _ = _MODULE_META.get(key, ("부품", "", key))
        modules.append({"key": key, "role": role, "reason": reason, "count": 1})
    return modules


# ── 하이브리드(React+MODI SDK) 코드용 모듈 추출 ──
# SDK 타입 문자열/메서드명 → 모듈 키 (environment→env, motorA/B→motor_a/b)
_SDK_TYPE_TO_KEY = {
    "joystick": "joystick", "button": "button", "dial": "dial",
    "tof": "tof", "imu": "imu", "environment": "env", "env": "env",
    "led": "led", "speaker": "speaker", "display": "display",
    "motor": "motor_a", "motorA": "motor_a", "motorB": "motor_b",
}
# use<Sensor> 훅 이름 → 모듈 키
_HOOK_TO_KEY = {
    "useJoystick": "joystick", "useButton": "button", "useDial": "dial",
    "useTof": "tof", "useImu": "imu", "useEnv": "env", "useEnvironment": "env",
}

_HYBRID_FORBIDDEN_OUTPUT_HOOKS = {
    "useLed", "useSpeaker", "useDisplay", "useMotor", "useMotorA", "useMotorB",
}

# 주석·문자열 리터럴 (구문 검사 전에 공백으로 치환할 대상)
_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
# 홑따옴표 문자열은 중괄호 미포함으로 한정 — JSX 문구 속 아포스트로피(It's ... 's)가
# 문자열 여닫이로 오인되면 사이의 '{useDial(0)}' 같은 실제 코드가 문자열로 삼켜져
# 구문 검사·모듈 추출이 눈이 먼다. (중괄호 든 홑따옴표 문자열은 드물어 보존 쪽이 안전)
_STRING_RE = re.compile(
    r"'(?:\\.|[^'\\{}\n])*'"
    r"|\"(?:\\.|[^\"\\\n])*\""
    r"|`(?:\\.|[^`\\])*`",
    re.DOTALL,
)
_NONCODE_RE = re.compile(
    r"//[^\n]*"
    r"|/\*.*?\*/"
    r"|'(?:\\.|[^'\\{}\n])*'"
    r"|\"(?:\\.|[^\"\\\n])*\""
    r"|`(?:\\.|[^`\\])*`",
    re.DOTALL,
)
# JSX 태그 사이의 표시용 텍스트 (>Play as Red< 등).
# 앞이 '='/공백인 '>'는 제외 — 화살표(=>)·비교(a > b)의 '>'를 태그 닫힘으로 오인하면
# 다음 '<'까지의 '실제 코드'(멀티라인 포함)가 통째로 지워져 훅/인덱스 검사가 눈이 먼다.
_JSX_TEXT_RE = re.compile(r"(?<![=\s])>[^<>{}]+<")


def _strip_noncode_text(code: str) -> str:
    """주석·문자열·JSX 텍스트를 공백으로 치환한 '구문 전용 뷰'를 반환.

    TS 문법·금지 훅 같은 구문 검사는 이 뷰 위에서 돌린다 — "Play as Red" 같은 화면
    문구나 주석 속 설명("useLed 금지")이 코드 구문으로 오탐되면, 실제로 고칠 게 없어
    수정 라운드가 영영 같은 오류에 걸리고 멀쩡한 앱이 미리보기에서 차단되기 때문.
    (치환은 길이를 보존해 오류 위치 감이 유지된다.)
    """
    cleaned = _NONCODE_RE.sub(lambda m: " " * len(m.group(0)), code)
    return _JSX_TEXT_RE.sub(lambda m: ">" + " " * (len(m.group(0)) - 2) + "<", cleaned)


def _strip_comments_and_jsx_text(code: str) -> str:
    """문자열 리터럴은 보존하고 주석·JSX 표시 텍스트만 공백 처리한 뷰.

    `MODI.onValue('dial', ...)`처럼 문자열 인자가 실제 API 계약인 패턴을 찾을 때 쓴다.
    화면 문구나 주석 속 "dial"은 상호작용으로 세면 안 된다.
    """
    cleaned = _COMMENT_RE.sub(lambda m: " " * len(m.group(0)), code)
    return _JSX_TEXT_RE.sub(lambda m: ">" + " " * (len(m.group(0)) - 2) + "<", cleaned)


def _inside_any(pos: int, ranges: List[tuple[int, int]]) -> bool:
    return any(start <= pos < end for start, end in ranges)


def extract_modi_module_keys(code: str) -> List[str]:
    """하이브리드(React+MODI SDK) 코드에서 쓰인 MODI 모듈 키를 추출.

    network 맨 앞, 등장 순서대로 중복 없이 반환. (블록 모드의 _extract_module_keys 대응)
    """
    keys: List[str] = []

    def add(key):
        if key and key not in keys:
            keys.append(key)

    syntax_view = _strip_noncode_text(code)
    value_view = _strip_comments_and_jsx_text(code)
    string_ranges = [(m.start(), m.end()) for m in _STRING_RE.finditer(value_view)]

    # onValue('joystick', ...) — 첫 인자 타입 문자열
    for m in re.finditer(r"""onValue\w*\s*\(\s*['"]([A-Za-z]+)['"]""", value_view):
        if not _inside_any(m.start(), string_ranges):
            add(_SDK_TYPE_TO_KEY.get(m.group(1)))
    # MODI.led(...) / MODI.motorA(...) / MODI.joystick() — MODI. 접두 메서드 (HTML <button> 오탐 방지)
    for t in re.findall(r"\bMODI\s*\.\s*([A-Za-z]+)", syntax_view):
        add(_SDK_TYPE_TO_KEY.get(t))
    # use<Sensor> 훅
    for h in re.findall(r"\b(use[A-Z][A-Za-z]+)\b", syntax_view):
        add(_HOOK_TO_KEY.get(h))
    # 모듈 타입을 변수로 넘기는 경우(예: onValue(exp.sensorType, ...) + {sensorType:'environment'},
    # const SENSOR = 'dial' + onValue(SENSOR, ...))는 위 정규식이 못 잡으므로,
    # '명확한 코드 위치'(속성값·변수 선언 초기값)의 타입 리터럴만 함께 인식한다.
    # 일반 문자열 문구까지 세면 has_interaction_module 게이트가 설명 텍스트만으로 통과된다.
    # (button/display/led 등은 HTML/CSS 문자열과 겹칠 수 있어 제외 — 이들은 MODI.x()·use훅으로 이미 잡힘)
    for sdk_type in ("environment", "tof", "imu", "joystick", "dial"):
        for pattern in (
            r"""\b(?:sensorType|moduleType|modiType|type)\s*:\s*['"]%s['"]""",
            r"""\b(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=\s*['"]%s['"]""",
        ):
            m = re.search(pattern % sdk_type, value_view)
            if m and not _inside_any(m.start(), string_ranges):
                add(_SDK_TYPE_TO_KEY[sdk_type])
                break

    return normalize_module_keys(keys)


def validate_hybrid_modi_code(code: str) -> List[str]:
    """하이브리드 앱 코드가 실제 MODI 모듈과 상호작용하는지 검증."""
    errors: List[str] = []
    keys = extract_modi_module_keys(code)
    if not has_interaction_module(keys):
        errors.append(
            "[App.tsx] hybrid 모드에서는 network 외 MODI 모듈을 최소 1개 이상 "
            "실제 코드에서 읽거나 제어해야 합니다. 예: useTof/useButton/useDial 같은 훅으로 "
            "센서값을 화면에 반영하거나, 화면 버튼에서 MODI.led(1).setColor(...)처럼 모듈을 제어하세요."
        )

    # 구문 검사는 주석·문자열·JSX 텍스트를 제거한 뷰에서 — 화면 문구가 오탐되면
    # 고칠 코드가 없어 수정 라운드가 무한히 같은 오류에 걸린다.
    syntax_view = _strip_noncode_text(code)

    ts_patterns = [
        r"\binterface\s+[A-Z]\w*",
        r"\btype\s+[A-Z]\w*\s*=",
        r"\buse(?:State|Ref|Memo|Callback|Reducer)\s*<",
        r"\b(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*:\s*",
        r"\([A-Za-z_$][\w$]*\s*:\s*[^)]*\)\s*=>",
        r"\bas\s+(?:Array|Record|[A-Z]\w*|const)\b",
    ]
    if any(re.search(pattern, syntax_view) for pattern in ts_patterns):
        errors.append(
            "[App.tsx] hybrid 미리보기는 런타임 Babel로 바로 실행되므로 TypeScript 타입 문법을 "
            "쓰면 하얀 화면이 날 수 있습니다. interface/type/useRef<T>/useState<T>/(e: KeyboardEvent)/"
            "`as Array<...>` 같은 타입 표기를 모두 제거하고 순수 JavaScript+JSX로 작성하세요."
        )

    if re.search(r"\bconst\s*\{[^}]*\buse[A-Z][A-Za-z]*[^}]*\}\s*=\s*MODI\b", syntax_view):
        errors.append(
            "[App.tsx] MODI 센서 훅은 MODI 객체에서 구조분해하지 않습니다. "
            "`const { useDial } = MODI` 대신 전역 훅 `useDial(1)`, `useButton(1)`처럼 직접 호출하세요."
        )

    # 금지 훅은 전부 모아 한 번에 알려준다 — 수정 라운드 예산이 1이라 첫 훅만 알려주면
    # 나머지 훅이 재검증에서 또 걸려 통째로 실패한다.
    for hook in sorted(set(re.findall(r"\b(use[A-Z][A-Za-z]+)\s*\(", syntax_view))):
        if hook in _HYBRID_FORBIDDEN_OUTPUT_HOOKS:
            errors.append(
                f"[App.tsx] 지원하지 않는 훅 `{hook}`이 사용되었습니다. hybrid에서는 "
                "출력 모듈을 훅으로 읽지 말고 MODI.led(1), MODI.speaker(1), MODI.display(1)처럼 제어하세요."
            )

    zero_index_patterns = [
        r"\buse(?:Tof|Button|Dial|Joystick|Env|Environment|Imu)\s*\(\s*0\s*(?:[,)]|\})",
        r"\bMODI\s*\.\s*[A-Za-z]+\s*\(\s*0\s*(?:[,)]|\})",
        # MODI.onValue(type, idx, ...) — index가 두 번째 인자 (문자열이 지워진 뷰라 첫 인자는 [^,()]*)
        r"\bonValue\w*\s*\(\s*[^,()]*,\s*0\s*[,)]",
    ]
    if any(re.search(pattern, syntax_view) for pattern in zero_index_patterns):
        errors.append(
            "[App.tsx] MODI 모듈 index는 1부터 시작합니다. useDial(0), useButton(0), "
            "MODI.led(0), MODI.onValue('dial', 0, ...) 같은 호출의 index를 1로 고치세요."
        )

    return errors


SCRIPT_EXTS = (".tsx", ".ts", ".jsx", ".js")


def validate_hybrid_code_map(code_map: Dict[str, str], enforce_single_file: bool = True) -> List[str]:
    """하이브리드 생성물 전체 계약 검증: App.tsx 단일 파일 + SDK 런타임 제약.

    enforce_single_file=False는 이 계약 도입 전에 여러 파일로 저장된 프로젝트의 수정 턴용 —
    구조 계약(단일 파일·import 금지)을 소급 적용하면 그런 프로젝트의 모든 수정이 검증 실패로
    막히므로, 그 경우 MODI 사용 검증만 전체 코드에 대해 수행한다.
    """
    errors: List[str] = []
    file_paths = sorted(path for path in (code_map or {}) if path.endswith(SCRIPT_EXTS))
    if not enforce_single_file:
        combined = "\n".join((code_map or {}).get(path, "") for path in file_paths) \
            or "\n".join((code_map or {}).values())
        errors.extend(validate_hybrid_modi_code(combined))
        return errors

    if file_paths != ["App.tsx"]:
        # 파일 목록을 에러 문구에 그대로 싣는다 — 수정 프롬프트(_fix_system_prompt)는
        # "에러 문구에 등장하는 파일"의 코드만 싣기 때문에, 여기 안 적힌 여분 파일은
        # 수정 모델이 내용을 못 봐 App.tsx로 병합할 수 없다.
        listing = ", ".join(file_paths) if file_paths else "(스크립트 파일 없음)"
        errors.append(
            "[hybrid] hybrid 모드는 App.tsx 단일 파일만 허용합니다. "
            f"현재 스크립트 파일: {listing}. 다른 파일의 코드를 모두 App.tsx 안에 "
            "인라인해서 generate_code('App.tsx', ...)로 전체 재작성하세요. "
            "App.tsx가 새로 작성되면 나머지 스크립트 파일은 시스템이 제거합니다."
        )

    app_code = (code_map or {}).get("App.tsx") or "\n".join((code_map or {}).values())
    if re.search(r"^\s*import\s", app_code, re.MULTILINE):
        errors.append(
            "[App.tsx] hybrid 런타임은 번들러/모듈 해석 없이 실행되므로 import 문을 사용할 수 없습니다. "
            "React, Chart, MODI는 전역으로 제공됩니다."
        )
    if "export default App" not in app_code:
        errors.append("[App.tsx] 최상위 컴포넌트는 App이고 파일 끝에 `export default App`이 있어야 합니다.")

    errors.extend(validate_hybrid_modi_code(app_code))
    return errors


def build_modi_modules_doc(keys: List[str], grid: List[List[str | None]] | None = None,
                           rotations=None, attachments=None) -> Dict[str, Any]:
    """모듈 키 + (선택)격자/회전/부착물로 준비물·배치 다이어그램·조립 순서 문서 생성.

    모듈 추출만 코드 기반이고, 그 뒤 후처리는 블록 모드(_post_impl_blockly)와 '동일'하다:
    repair_grid로 물리 보정 → grid_to_layout / grid_to_assembly / accessory_parts.
    grid를 주면(LLM이 작품 형태에 맞게 배치) 그 배치로, 없으면 한 줄. 감지 모듈 누락 시 보정.
    """
    keys = normalize_module_keys(keys)
    grid_keys = [k for k in keys if k in _GRID_MODULES]
    clean = sanitize_grid(grid) if grid else []
    # 배치는 '코드가 실제로 쓰는 모듈'만 남긴다 — 에이전트가 코드에 없는 모듈을 놓았으면 제거해
    # 필요목록(코드 추출)과 배치가 어긋나는 것을 막는다. 코드가 쓰는데 빠진 모듈은 아래에서 보충.
    allowed = set(grid_keys)
    clean = [[(k if k in allowed else None) for k in row] for row in clean]
    clean = _dedupe_grid_modules(clean)
    present = {k for row in clean for k in row if k}
    if not present:
        # 실제 배치 없음(하이브리드에서 set_modi_layout 미호출 등) → 한 줄 예시 배치.
        # 물리 보정·부품 추가는 하지 않는다(모터를 임의로 자동차로 보정해 엉뚱한 부품이 생기는 것 방지).
        row = [grid_keys]
        return {
            "modules": _build_modules(keys),
            "layout": grid_to_layout(row),
            "assembly": grid_to_assembly(row),
        }
    # 실제 배치 있음 → 블록 모드와 동일한 후처리 (물리 보정 + 배치/조립/부품). 빠진 모듈은 보정.
    missing = [k for k in grid_keys if k not in present]
    if missing:
        clean = clean + [missing]
    clean = _ensure_network_left_free(clean)        # USB 자리: network 왼쪽 칸 비우기 (물리 규칙)
    clean, rot, att = repair_grid(clean, rotations, attachments)
    return {
        "modules": _build_modules(keys) + accessory_parts(clean, rot, att),
        "layout": grid_to_layout(clean, rot),
        "assembly": grid_to_assembly(clean, rot, att),
    }


def sanitize_grid(grid: List[List[str | None]]) -> List[List[str | None]]:
    """LLM grid를 안전한 2D 문자열 격자로 정규화.
    유효 모듈 키(문자열)만 남기고 나머지는 None. 비정상 구조(행이 리스트 아님, 셀이 dict 등
    해시 불가 값)도 throw 없이 처리 — 여기서 터지면 모디 준비물 전체가 유실되므로 견고해야 한다."""
    if not isinstance(grid, list):
        return []
    clean: List[List[str | None]] = []
    for row in grid:
        if not isinstance(row, list):
            clean.append([])
            continue
        clean.append([key if (isinstance(key, str) and key in _GRID_MODULES) else None for key in row])
    return clean


def _dedupe_grid_modules(grid: List[List[str | None]]) -> List[List[str | None]]:
    """배치도의 network 중복만 제거 — 물리 network 모듈은 정확히 1개다.

    다른 모듈은 같은 종류를 여러 개 조립할 수 있으므로(예: LED 2개) 그대로 둔다.
    전부 중복 제거하면 실제로 2개를 쓰는 작품의 배치도에 구멍이 뚫린다.
    """
    seen_network = False
    out: List[List[str | None]] = []
    for row in grid:
        clean_row: List[str | None] = []
        for key in row:
            if key == "network":
                clean_row.append(None if seen_network else key)
                seen_network = True
            else:
                clean_row.append(key)
        out.append(clean_row)
    return out


# 방향 → (행, 열) 오프셋. 위=행-1, 아래=행+1, 왼쪽=열-1, 오른쪽=열+1.
_DIR_OFFSET = {"up": (-1, 0), "down": (1, 0), "left": (0, -1), "right": (0, 1)}


def _cell(grid, r, c):
    """그리드 범위 안이면 셀 값, 밖이면 None(빈칸 취급)."""
    if 0 <= r < len(grid) and 0 <= c < len(grid[r]):
        return grid[r][c]
    return None


def _occupied_box(grid):
    """점유된 셀들의 bounding box (minr, minc, maxr, maxc). 비면 None."""
    cells = [(r, c) for r, row in enumerate(grid) for c, k in enumerate(row) if k]
    if not cells:
        return None
    rs = [p[0] for p in cells]
    cs = [p[1] for p in cells]
    return (min(rs), min(cs), max(rs), max(cs))


def _pad(grid):
    """모든 변에 빈칸 1줄을 두른 직사각 격자 반환(이동 후보가 격자 밖 면까지 닿게)."""
    width = max((len(row) for row in grid), default=0)
    body = [[None] + list(row) + [None] * (width - len(row) + 1) for row in grid]
    empty = [None] * (width + 2)
    return [list(empty)] + body + [list(empty)]


def _trim(grid):
    """바깥 테두리의 빈 행·열만 제거(렌더는 상대 좌표라 오프셋 무방).
    내부의 빈 행·열은 모터 overhang 여유 공간일 수 있으므로 보존한다."""
    width = max((len(row) for row in grid), default=0)
    rect = [list(row) + [None] * (width - len(row)) for row in grid]  # 직사각형 정규화
    rows = [r for r, row in enumerate(rect) if any(row)]
    cols = [c for c in range(width) if any(row[c] for row in rect)]
    if not rows or not cols:
        return []
    r0, r1, c0, c1 = min(rows), max(rows), min(cols), max(cols)        # 내용이 있는 바깥 경계
    return [row[c0:c1 + 1] for row in rect[r0:r1 + 1]]


def _motor_ok(grid, key, r, c, rot, network_pos):
    """모터가 (r,c,rot)에 놓일 때 물리 제약을 만족하는지. (grid엔 이 모터가 빠져 있어야 함)

    HARD: ① 축·캡(overhang) 방향 칸이 비어야(축 부착물·모터 길이 겹침 방지)
          ② network 왼쪽/왼쪽아래에 모터가 오면 안 됨(USB 겹침)
          ③ 자석면(축·캡 아닌 두 면) 중 최소 한 곳에 이웃이 붙어 있어야(연결 유지)
    """
    shaft, cap = _SHAFT[key][rot], _CAP[rot]
    sdr, sdc = _DIR_OFFSET[shaft]
    cdr, cdc = _DIR_OFFSET[cap]
    # ① overhang 칸이 비어야 — 축·캡(직교 두 칸) + 그 사이 대각 칸(모터 모서리가 파고듦)
    for dr, dc in ((sdr, sdc), (cdr, cdc), (sdr + cdr, sdc + cdc)):
        if _cell(grid, r + dr, c + dc) is not None:
            return False
    if network_pos:                                         # ② network USB
        nr, nc = network_pos
        if (r, c) in ((nr, nc - 1), (nr + 1, nc - 1)):
            return False
    magnet = [d for d in _DIR_OFFSET if d not in (shaft, cap)]
    if not any(_cell(grid, r + _DIR_OFFSET[d][0], c + _DIR_OFFSET[d][1]) for d in magnet):
        return False                                        # ③ 어디에도 안 붙으면 탈락
    return True


def _score_motor(grid, key, r, c, rot, orig_pos, orig_rot):
    """유효 후보의 선호도 점수(높을수록 선호).
    - 제자리·원래 회전 유지 우대(최소 변경으로 충돌만 해소)
    - 세로 overhang을 위로(아랫면이 행 경계에 정렬돼 옆 모듈과 아랫면 위상이 맞음) 선호
    - 축·캡이 작품 바깥 빈공간을 향하면 약간 가산, 이동 거리는 감점
    """
    minr, minc, maxr, maxc = _occupied_box(grid)
    shaft, cap = _SHAFT[key][rot], _CAP[rot]
    score = 0.0
    if (r, c) == orig_pos:
        score += 5.0
    if rot == orig_rot:
        score += 3.0
    # 세로 overhang 방향(축·캡 중 위/아래인 것)이 '위'면 아랫면이 행 경계에 정렬 → 위상 일치
    vert = shaft if shaft in ("up", "down") else cap
    if vert == "up":
        score += 2.0
    for d in (shaft, cap):                                  # 축/캡이 bounding box 밖(=바깥 빈공간)을 향함
        dr, dc = _DIR_OFFSET[d]
        nr, nc = r + dr, c + dc
        if not (minr <= nr <= maxr and minc <= nc <= maxc):
            score += 1.0
    score -= abs(r - orig_pos[0]) + abs(c - orig_pos[1])    # 이동 거리 감점
    return score


def _is_intended_car(grid, rotations, attachments) -> bool:
    """'바퀴 자동차로 의도된' 격자인지: 모터 둘이 다 있고, 이미 바퀴 축 구성이거나
    어느 모터든 wheel 부착이 지정됨. (설사 배치가 어긋나 있어도 '자동차 의도'로 본다.)"""
    pos = {k for row in grid for k in row if k in _MOTOR_KEYS}
    if len(pos) < 2:
        return False
    return _wheel_axle(grid, rotations) is not None or "wheel" in attachments.values()


def _compact(grid):
    """빈 행·열을 모두 제거해 모듈만 촘촘히 모은다(모터를 뺀 본체 정리용)."""
    width = max((len(row) for row in grid), default=0)
    rect = [list(row) + [None] * (width - len(row)) for row in grid]
    rect = [row for row in rect if any(row)]
    if not rect:
        return []
    cols = [c for c in range(width) if any(row[c] for row in rect)]
    return [[row[c] for c in cols] for row in rect]


def _repair_car(grid, rotations, attachments):
    """두 바퀴 자동차를 표준형으로 정규화:
    본체는 빈칸 없이 2열로 위에 쌓고, 맨 아래 행에 [motor_b(왼), motor_a(오)]만 인접 배치,
    두 모터 모두 180°(등을 맞대 축이 좌·우 바깥), 두 모터 모두 wheel 부착.
    → `accessory_parts`가 휠 2개 + 기본 브릭 + 보조 바퀴를 자동으로 채운다."""
    body = _compact([[None if k in _MOTOR_KEYS else k for k in row] for row in grid])
    width = max(max((len(row) for row in body), default=0), 2)  # 최소 2열(모터 두 칸)
    body = [row + [None] * (width - len(row)) for row in body]
    motor_row = ["motor_b", "motor_a"] + [None] * (width - 2)   # 맨 아래: 모터 둘만
    grid = body + [motor_row]
    rotations = dict(rotations)
    rotations["motor_a"] = rotations["motor_b"] = 180
    attachments = dict(attachments)
    attachments["motor_a"] = attachments["motor_b"] = "wheel"
    return grid, rotations, attachments


def repair_grid(grid, rotations=None, attachments=None):
    """LLM 격자의 물리·용도 정합성을 검사·보정해 (grid, rotations, attachments)를 반환.

    1) **바퀴 자동차**(모터 둘 + wheel 의도): `_repair_car`로 표준형 강제
       (둘 다 180°, 맨 아래 행에 motor_b·motor_a만 인접, 둘 다 wheel → 휠·브릭·보조바퀴 부품).
    2) **그 외 모터**(팔·회전 등 바퀴 아님): 물리 충돌(축·길이 overhang, network USB)을
       (위치×4회전) 탐색으로 최소 변경 보정하고, 부착이 없으면 **i_horn 기본 지정**
       (축에 뭔가 끼워야 일을 하므로).
    """
    rotations = dict(sanitize_rotations(rotations))
    attachments = dict(sanitize_attachments(attachments))
    grid = [list(row) for row in grid]

    if _is_intended_car(grid, rotations, attachments):
        return _repair_car(grid, rotations, attachments)

    orig_grid = [list(row) for row in grid]
    orig_rot = dict(rotations)
    grid = _pad(grid)  # 빈 테두리 1칸 — 모터를 인접 모듈의 바깥 면(현재 격자 밖)으로도 옮길 수 있게
    # 모터끼리 위치가 얽혀 한 모터를 옮기면 다른 모터가 다시 어긋날 수 있으므로
    # 더 바뀔 게 없을 때까지(또는 안전 상한까지) 반복해 fixpoint로 수렴시킨다.
    for _ in range(4):
        changed = False
        for key in _MOTOR_KEYS:
            changed |= _repair_one(grid, rotations, key)
        if not changed:
            break

    grid = _trim(grid)
    # 안전망: 풀 수 없는 입력(예: 모터 2개+network 1열)에서 보정이 오히려 겹침/USB 위반을
    # 늘렸다면 원본 배치를 그대로 둔다(절대 악화시키지 않음).
    if _badness(grid, rotations) > _badness(orig_grid, orig_rot) + 1e-6:
        grid, rotations = orig_grid, orig_rot
    # 바퀴가 아닌(팔·회전 등) 모터는 i_horn 기본 부착 — 부착 미지정 시에만
    for key in _MOTOR_KEYS:
        if any(key in row for row in grid) and attachments.get(key) != "wheel":
            attachments.setdefault(key, "i_horn")
    return grid, rotations, attachments


def _badness(grid, rotations) -> float:
    """다이어그램의 '나쁨' 척도 = 가장 큰 푸트프린트 겹침 면적 + network USB 위반 수.
    보정 전후를 비교해, 보정이 오히려 나빠졌으면 되돌리는 안전망에 쓴다.
    (합이 아니라 최댓값 — 가장 눈에 띄는 겹침을 키우지 않는 게 목표.)"""
    layout = grid_to_layout(grid, rotations)
    rects = []
    for it in layout:
        x, y = it["pos"]
        rot = it.get("rotation", 0)
        if it["key"] in _MOTOR_KEYS:
            fw, fh = (1.48, 1.44) if rot in (90, 270) else (1.44, 1.48)
        else:
            fw = fh = 1.0
        rects.append((x, y, x + fw, y + fh))
    area = 0.0
    for i in range(len(rects)):
        ax0, ay0, ax1, ay1 = rects[i]
        for j in range(i + 1, len(rects)):
            bx0, by0, bx1, by1 = rects[j]
            ox = max(0.0, min(ax1, bx1) - max(ax0, bx0))
            oy = max(0.0, min(ay1, by1) - max(ay0, by0))
            area = max(area, ox * oy)
    network_pos = next(((r, c) for r, row in enumerate(grid)
                        for c, k in enumerate(row) if k == "network"), None)
    usb = 0
    if network_pos:
        nr, nc = network_pos
        for r, row in enumerate(grid):
            for c, k in enumerate(row):
                if k in _MOTOR_KEYS and (r, c) in ((nr, nc - 1), (nr + 1, nc - 1)):
                    usb += 1
    return area + usb


def _repair_one(grid, rotations, key) -> bool:
    """모터 한 개를 검사하고 필요하면 위치·회전을 보정. 격자/회전을 제자리 수정하고
    실제로 바꿨으면 True. (이미 정상이거나 해법이 없으면 그대로 두고 False)"""
    pos = next(((r, c) for r, row in enumerate(grid)
                for c, k in enumerate(row) if k == key), None)
    if pos is None:
        return False
    r0, c0 = pos
    rot0 = rotations.get(key, 0)
    base = [list(row) for row in grid]                  # 이 모터를 뺀 그리드(이동 평가용)
    base[r0][c0] = None
    network_pos = next(((r, c) for r, row in enumerate(base)
                        for c, k in enumerate(row) if k == "network"), None)
    if _motor_ok(base, key, r0, c0, rot0, network_pos):
        return False                                    # 이미 정상 — 건드리지 않음

    cand_cells = {(r0, c0)}                             # 제자리 + 점유 모듈 인접 빈칸
    for r, row in enumerate(base):
        for c, k in enumerate(row):
            if not k:
                continue
            for dr, dc in _DIR_OFFSET.values():
                rr, cc = r + dr, c + dc
                if _cell(base, rr, cc) is None and 0 <= rr < len(base) and 0 <= cc < len(base[rr]):
                    cand_cells.add((rr, cc))

    best = None                                         # (score, r, c, rot)
    for (r, c) in cand_cells:
        for rot in (0, 90, 180, 270):
            if not _motor_ok(base, key, r, c, rot, network_pos):
                continue
            placed = [list(row) for row in base]
            placed[r][c] = key
            sc = _score_motor(placed, key, r, c, rot, (r0, c0), rot0)
            if best is None or sc > best[0]:
                best = (sc, r, c, rot)

    if best is None:
        return False                                    # 해법 없음 — 원본 유지(악화 금지)
    _, r, c, rot = best
    moved = (r, c) != (r0, c0)
    if moved:
        grid[r0][c0] = None
        grid[r][c] = key
    rot_changed = rot != rot0
    if rot:
        rotations[key] = rot
    else:
        rotations.pop(key, None)
    return moved or rot_changed


def _ensure_network_left_free(grid: List[List[str | None]]) -> List[List[str | None]]:
    """network의 왼쪽 칸(USB 꽂는 자리)을 비운다 — 물리 규칙. 왼쪽에 모듈이 있으면
    network를 그 행의 왼쪽 끝으로 스왑 이동(사이 모듈은 한 칸씩 오른쪽으로 밀림).
    이미 왼쪽이 비어 있으면(또는 맨 왼쪽 열) 그대로 둔다."""
    g = [list(row) for row in grid]
    for row in g:
        if "network" in row:
            c = row.index("network")
            while c > 0 and row[c - 1] is not None:
                row[c - 1], row[c] = row[c], row[c - 1]
                c -= 1
            break
    return g


def grid_to_layout(grid: List[List[str | None]], rotations=None) -> List[Dict[str, Any]]:
    """LLM grid → 좌표 layout 변환.

    큐브: [col, row] 그대로.
    모터(1.44×1.48): 자석 있는 본체(약 1×1)는 자기 셀에 정렬되고, 축과 캡(둘 다 자석 없음)이
      각각 가로·세로 한 방향씩 셀 밖으로 삐져나온다. 그래서 축·캡 쪽 칸은 비어 있어야 하고(축엔
      바퀴·I-혼, 캡은 모터 윗부분), 나머지 두 면(자석)이 이웃 모듈에 붙는다. 두 모터가 인접하면
      자석면끼리 맞붙어 바퀴 축이 형성된다.
    회전(rotations[key], 0/90/180/270)은 layout 항목 rotation으로 전달하고, 90/270이면
    가로·세로 footprint를 맞바꾼다. 회전 여부는 LLM이 정한다(모터가 항상 바퀴는 아님).
    부착물(휠·I-혼)은 다이어그램에 그리지 않는다(준비물·조립 안내에만 표시).
    """
    rotations = sanitize_rotations(rotations)
    mw, mh = 1.44, 1.48
    layout: List[Dict[str, Any]] = []

    for r, row in enumerate(grid):
        for c, key in enumerate(row):
            if not key:
                continue
            rot = rotations.get(key, 0)
            if key in _MOTOR_KEYS:
                fw, fh = (mh, mw) if rot in (90, 270) else (mw, mh)
                # 축(자유)·캡(자석없음)이 각각 가로/세로 한 방향씩 셀 밖으로 나간다.
                # 자석 본체(1×1)는 셀에 정렬 → 반대 모서리에 footprint를 앵커.
                shaft, cap = _SHAFT[key][rot], _CAP[rot]
                horiz = shaft if shaft in ("left", "right") else cap
                vert = shaft if shaft in ("up", "down") else cap
                mx = (c + 1) - fw if horiz == "left" else float(c)
                my = (r + 1) - fh if vert == "up" else float(r)
                item: Dict[str, Any] = {"key": key, "pos": [mx, my]}
            else:
                item = {"key": key, "pos": [float(c), float(r)]}
                if key == "network":
                    item["usbFace"] = "left"
            if rot:
                item["rotation"] = rot
            layout.append(item)

    return layout


def grid_to_assembly(grid: List[List[str | None]], rotations=None, attachments=None) -> List[str]:
    """격자 배치로부터 조립 순서 생성."""
    rotations = sanitize_rotations(rotations)
    attachments = sanitize_attachments(attachments)
    is_car = _is_wheel_car(grid, rotations, attachments)
    steps: List[str] = []
    if is_car:
        steps.append("기본 브릭(2×4 바닥판)을 준비하고 그 위에 모듈을 올려요.")
    has_motor = False
    for row in grid:
        for key in row:
            if not key:
                continue
            label = _MODULE_META.get(key, (None, None, key))[2]
            if key == "network":
                steps.append(f"{label} 모듈을 놓고 USB 케이블을 PC에 연결하세요.")
            elif key in _MOTOR_KEYS:
                has_motor = True
            else:
                steps.append(f"{label} 모듈을 자석 면에 맞대어 연결하세요.")
    if _wheel_axle(grid, rotations):
        steps.append("모터B(왼쪽 바퀴)와 모터A(오른쪽 바퀴)를 각각 180° 돌려 등(자석면)을 맞댄 뒤, 두 축이 바깥을 향하도록 붙이세요.")
    elif has_motor:
        steps.append("모터를 자석 면에 맞대어 연결하고 축이 바깥을 향하게 하세요.")
    counts: Dict[str, int] = {}
    for att in attachments.values():
        counts[att] = counts.get(att, 0) + 1
    if is_car:
        counts["wheel"] = max(counts.get("wheel", 0), 2)  # 자동차는 바퀴 2개 보장
    for att, count in counts.items():
        label = _MODULE_META.get(att, (None, None, att))[2]
        steps.append(f"모터 축에 {label} {count}개를 끼우세요.")
    if is_car:
        steps.append("반대쪽 끝(맨 위)에 보조 바퀴를 달아 차체 균형을 잡으세요.")
    return steps
