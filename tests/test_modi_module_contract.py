from agent.modi_modules import (
    build_modi_modules_doc,
    extract_modi_module_keys,
    extract_raw_module_keys_from_xml,
    validate_hybrid_code_map,
    validate_hybrid_modi_code,
    validate_modi_module_contract,
)
from agent.tools import validate_blockly_xml


def test_build_modi_modules_doc_dedupes_network_but_keeps_duplicate_modules():
    doc = build_modi_modules_doc(
        ["network", "led", "network", "led"],
        [["network", "network", "led"], ["led", "network", None]],
    )

    module_keys = [module["key"] for module in doc["modules"]]
    layout_keys = [item["key"] for item in doc["layout"]]

    assert module_keys.count("network") == 1
    assert module_keys.count("led") == 1
    # 물리 network는 1개뿐이지만, LED 같은 모듈은 2개를 실제로 조립할 수 있다 —
    # 배치도에서 지우면 코드가 2개를 제어하는 작품의 조립 안내에 구멍이 생긴다.
    assert layout_keys.count("network") == 1
    assert layout_keys.count("led") == 2


def test_modi_module_contract_requires_network_and_one_real_module():
    assert validate_modi_module_contract(["network", "led"]) == []
    assert validate_modi_module_contract(["network"])  # 상호작용 모듈 없음
    assert validate_modi_module_contract(["led"])      # network 없음
    assert validate_modi_module_contract(["network", "network", "led"])  # network 중복


def test_blockly_rejects_network_blocks_other_than_upload():
    xml = """
<xml>
  <block type="network_upload" deletable="false">
    <next>
      <block type="controls_whileInfinite">
        <statement name="DO">
          <block type="network_dial_value"></block>
        </statement>
      </block>
    </next>
  </block>
</xml>
"""

    errors = validate_blockly_xml(xml)

    assert extract_raw_module_keys_from_xml(xml).count("network") == 1
    assert any("network 계열 블록은 사용하지 않습니다" in error for error in errors)


def test_hybrid_extracts_real_modi_modules_and_normalizes_network():
    code = """
const { useEffect } = React;
function App() {
  const distance = useTof(1);
  useEffect(() => {
    MODI.led(1).setColor(255, 0, 0);
    MODI.motor(1).setSpeed(40);
    MODI.env(1).temperature;
  }, []);
  return <div>{distance}</div>;
}
export default App;
"""

    keys = extract_modi_module_keys(code)

    assert keys.count("network") == 1
    assert keys[0] == "network"
    assert {"tof", "led", "motor_a", "env"}.issubset(set(keys))


def test_hybrid_requires_module_beyond_network():
    code = """
const { useMemo } = React;
function App() {
  const status = useModi();
  return <div>{status.connected ? 'connected' : 'offline'}</div>;
}
export default App;
"""

    errors = validate_hybrid_modi_code(code)

    assert errors
    assert "network 외 MODI 모듈" in errors[0]


def test_hybrid_accepts_real_module_interaction():
    code = """
const { useEffect } = React;
function App() {
  const button = useButton(1);
  useEffect(() => {
    if (button.clicked) MODI.speaker(1).playTone(880, 60);
  }, [button.clicked]);
  return <button>play</button>;
}
export default App;
"""

    assert validate_hybrid_modi_code(code) == []


def test_hybrid_accepts_on_value_reference_pattern():
    code = """
const { useEffect, useRef } = React;
function App() {
  const dialRef = useRef(0);
  useEffect(() => {
    const offDial = MODI.onValue('dial', 1, 'angle', (value) => {
      dialRef.current = value;
    }, { pollMs: 50 });
    return () => offDial && offDial();
  }, []);
  return <div>{dialRef.current}</div>;
}
export default App;
"""

    assert validate_hybrid_modi_code(code) == []


def test_hybrid_code_map_requires_single_app_file_without_imports():
    code_map = {
        "App.tsx": """
import React from 'react';
function App() {
  const distance = useTof(1);
  return <div>{distance}</div>;
}
export default App;
""",
        "components/Gauge.tsx": "export default function Gauge() { return null; }",
    }

    errors = validate_hybrid_code_map(code_map)

    assert any("App.tsx 단일 파일" in error for error in errors)
    assert any("import 문" in error for error in errors)


def test_hybrid_code_map_accepts_single_runtime_app_file():
    code_map = {
        "App.tsx": """
const { useEffect } = React;
function App() {
  const button = useButton(1);
  useEffect(() => {
    if (button.clicked) MODI.led(1).setColor(0, 180, 90);
  }, [button.clicked]);
  return <button>{button.clicked ? 'on' : 'off'}</button>;
}
export default App;
"""
    }

    assert validate_hybrid_code_map(code_map) == []


def test_hybrid_rejects_runtime_babel_incompatible_typescript():
    code = """
const { useRef } = React;
interface Player { x: number; y: number; }
function App() {
  const playerRef = useRef<Player>({ x: 0, y: 0 });
  const move = (e: KeyboardEvent) => { playerRef.current.x = e.clientX; };
  const items = [] as Array<{ x: number }>;
  const distance = useTof(1);
  return <div>{distance}</div>;
}
export default App;
"""

    errors = validate_hybrid_modi_code(code)

    assert any("TypeScript 타입 문법" in error for error in errors)


def test_hybrid_rejects_bad_modi_hook_usage_and_zero_index():
    code = """
const { useState } = React;
const { useDial, useButton, useLed } = MODI;
function App() {
  const dial = useDial(0);
  const led = useLed(1);
  const button = useButton(1);
  const [on, setOn] = useState(false);
  return <button onClick={() => { MODI.led(0).setColor(255, 0, 0); setOn(!on); }}>{dial.turn}{button.clicked}{led}</button>;
}
export default App;
"""

    errors = validate_hybrid_modi_code(code)

    assert any("구조분해하지 않습니다" in error for error in errors)
    assert any("지원하지 않는 훅 `useLed`" in error for error in errors)
    assert any("index는 1부터" in error for error in errors)


def test_hybrid_allows_effect_layer_dom_mutation():
    code = """
const { useRef } = React;
function App() {
  const distance = useTof(1);
  const containerRef = useRef(null);
  const addItem = () => {
    const el = document.createElement('div');
    el.textContent = String(distance);
    containerRef.current.appendChild(el);
    el.remove();
  };
  return <div ref={containerRef} onClick={addItem}>{distance}</div>;
}
export default App;
"""

    assert validate_hybrid_modi_code(code) == []


def test_hybrid_ts_check_ignores_ui_text_and_comments():
    # "Play as Red"(JSX 텍스트), "same as React"(주석)는 TS `as` 캐스트가 아니다 —
    # 오탐되면 고칠 코드가 없어 수정 라운드가 소진되고 멀쩡한 앱이 차단된다.
    code = """
const { useState } = React;
function App() {
  // same as React docs
  const distance = useTof(1);
  const [team, setTeam] = useState('red');
  const label = 'Type check: interface Player is a TS thing';
  return <button onClick={() => setTeam('red')}>Play as Red</button>;
}
export default App;
"""

    assert validate_hybrid_modi_code(code) == []


def test_hybrid_rejects_zero_index_in_on_value_second_arg():
    code = """
const { useRef } = React;
function App() {
  const dialRef = useRef(0);
  React.useEffect(() => {
    const off = MODI.onValue('dial', 0, 'angle', (v) => { dialRef.current = v; }, { pollMs: 50 });
    return () => off && off();
  }, []);
  return <div>{dialRef.current}</div>;
}
export default App;
"""

    errors = validate_hybrid_modi_code(code)

    assert any("index는 1부터" in error for error in errors)


def test_hybrid_reports_all_forbidden_output_hooks():
    # 수정 라운드 예산이 1이라 금지 훅은 첫 개만이 아니라 전부 한 번에 알려줘야 한다.
    code = """
function App() {
  const dial = useDial(1);
  const led = useLed(1);
  const spk = useSpeaker(1);
  return <div>{dial.turn}{led}{spk}</div>;
}
export default App;
"""

    errors = validate_hybrid_modi_code(code)

    assert any("`useLed`" in error for error in errors)
    assert any("`useSpeaker`" in error for error in errors)


def test_hybrid_interaction_gate_ignores_display_only_strings():
    # 화면 문구/속성값의 센서 이름만으로 상호작용 게이트가 통과되면 안 된다.
    code = """
function App() {
  return <div id="dial"><span>Dial: --</span></div>;
}
export default App;
"""

    errors = validate_hybrid_modi_code(code)

    assert any("network 외 MODI 모듈" in error for error in errors)


def test_hybrid_interaction_gate_ignores_comments_and_string_examples():
    code = """
function App() {
  // 예시: MODI.led(1).setColor(255, 0, 0), useDial(1)
  const help = "MODI.onValue('dial', 1, 'angle', cb) and MODI.led(1)";
  return <div>useButton(1) 또는 MODI.speaker(1)을 써보세요</div>;
}
export default App;
"""

    errors = validate_hybrid_modi_code(code)

    assert any("network 외 MODI 모듈" in error for error in errors)


def test_hybrid_interaction_gate_accepts_sensor_type_indirection():
    # 모듈 타입을 변수로 넘기는 패턴({sensorType:'environment'})은 값 자리 리터럴로 인식된다.
    code = """
const SENSOR = { sensorType: 'environment' };
function App() {
  const [temp, setTemp] = React.useState(0);
  React.useEffect(() => {
    const off = MODI.onValue(SENSOR.sensorType, 1, 'temperature', (v) => setTemp(v));
    return () => off && off();
  }, []);
  return <div>{temp}</div>;
}
export default App;
"""

    assert validate_hybrid_modi_code(code) == []


def test_hybrid_legacy_multifile_map_skips_structure_contract():
    # 단일 파일 계약 도입 전에 저장된 여러 파일 프로젝트의 수정 턴 —
    # 구조 계약(단일 파일·import 금지)을 소급하면 모든 수정이 검증 실패로 막힌다.
    code_map = {
        "App.tsx": """
import Gauge from './components/Gauge';
function App() {
  const distance = useTof(1);
  return <Gauge value={distance} />;
}
export default App;
""",
        "components/Gauge.tsx": "export default function Gauge({ value }) { return <div>{value}</div>; }",
    }

    assert validate_hybrid_code_map(code_map, enforce_single_file=False) == []
    assert validate_hybrid_code_map(code_map)  # 기본(신규 생성)은 여전히 단일 파일 강제


def test_hybrid_allows_transform_only_ref_updates():
    code = """
const { useRef } = React;
function App() {
  const distance = useTof(1);
  const playerRef = useRef(null);
  const move = () => {
    if (playerRef.current) {
      playerRef.current.style.transform = 'translate3d(10px,20px,0)';
      playerRef.current.style.opacity = '0.8';
    }
  };
  return <div ref={playerRef} onClick={move}>{distance}</div>;
}
export default App;
"""

    assert validate_hybrid_modi_code(code) == []


def test_blockly_requires_module_beyond_network():
    xml = """
<xml>
  <block type="network_upload" deletable="false">
    <next>
      <block type="controls_whileInfinite"></block>
    </next>
  </block>
</xml>
"""

    errors = validate_blockly_xml(xml)

    assert any("network 외에 실제 동작하는 MODI 모듈" in error for error in errors)


def test_blockly_accepts_network_plus_real_module_contract():
    xml = """
<xml>
  <block type="network_upload" deletable="false">
    <next>
      <block type="controls_whileInfinite">
        <statement name="DO">
          <block type="output_led_color">
            <field name="INDEX">0</field>
            <value name="COLOUR">
              <shadow type="colour_hsv_sliders">
                <field name="COLOUR">#ff0000</field>
              </shadow>
            </value>
          </block>
        </statement>
      </block>
    </next>
  </block>
</xml>
"""

    errors = validate_blockly_xml(xml)

    assert not any("network 외에 실제 동작하는 MODI 모듈" in error for error in errors)


def test_hybrid_interaction_gate_accepts_const_sensor_assignment():
    # 타입 문자열을 변수 선언으로 빼는 패턴(const SENSOR = 'dial' + onValue(SENSOR, ...))도
    # 상호작용으로 인식해야 한다 — 못 잡으면 멀쩡히 돌던 앱의 모든 수정 턴이 거부된다.
    code = """
const SENSOR = 'dial';
function App() {
  const [angle, setAngle] = React.useState(0);
  React.useEffect(() => {
    const off = MODI.onValue(SENSOR, 1, 'degree', (v) => setAngle(v));
    return () => off && off();
  }, []);
  return <div>{angle}</div>;
}
export default App;
"""
    assert validate_hybrid_modi_code(code) == []


def test_hybrid_apostrophe_prose_does_not_hide_code():
    # JSX 문구 속 아포스트로피(It's … 's)가 문자열 여닫이로 오인되면 사이의
    # {useDial(0)} 실코드가 문자열로 삼켜져 zero-index 검사가 눈이 멀었다.
    code = """
function App() {
  return <p>It's {useDial(0) > 3 ? 'hot' : 'cold'}'s turn</p>;
}
export default App;
"""
    errors = validate_hybrid_modi_code(code)
    assert any("index는 1부터" in e for e in errors)


def test_hybrid_arrow_multiline_code_not_blanked():
    # '=>'의 '>'를 JSX 태그 닫힘으로 오인하면 다음 '<'까지의 실코드(멀티라인)가
    # 지워져 zero-index·훅 검사가 눈이 먼다.
    code = """
function App() {
  const onTick = (v) => v + 1;
  const angle = useDial(0);
  return <div>{angle}</div>;
}
export default App;
"""
    errors = validate_hybrid_modi_code(code)
    assert any("index는 1부터" in e for e in errors)


def test_hybrid_single_file_error_lists_extra_files():
    # 에러 문구의 파일 목록은 _fix_system_prompt가 병합 대상 코드를 싣는 근거다.
    code_map = {
        "App.tsx": "function App() { return null; }\nexport default App;",
        "components/Gauge.tsx": "function Gauge() { return null; }",
    }
    errors = validate_hybrid_code_map(code_map)
    assert any("components/Gauge.tsx" in e for e in errors)


def test_generate_code_skips_lucide_import_injection_in_hybrid():
    # hybrid 런타임은 import 금지 — lucide 자동 import 보강이 켜지면 검증(import 금지)과
    # 자동 주입이 서로를 되돌리는 무한 수정 루프가 된다(리뷰 확정 건). react는 기존 유지.
    from agent.context import SessionState
    from agent.tools import handle_tool_call

    code = "function App() { return <div><Play /></div>; }\nexport default App;"

    hybrid = SessionState()
    hybrid.coding_type = "hybrid"
    handle_tool_call("generate_code", {"file_path": "App.tsx", "code": code, "description": "t"}, hybrid)
    assert "import" not in hybrid.generated_code_map["App.tsx"]

    react = SessionState()
    react.coding_type = "react"
    handle_tool_call("generate_code", {"file_path": "App.tsx", "code": code, "description": "t"}, react)
    assert "lucide-react" in react.generated_code_map["App.tsx"]
