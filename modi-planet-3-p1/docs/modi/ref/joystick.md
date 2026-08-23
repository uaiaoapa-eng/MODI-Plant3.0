# 조이스틱 (Joystick)

## 블록 타입
| 블록 | 타입명 | 반환 | 필드 |
|------|--------|------|------|
| 방향 감지 | `input_joystick_status` | Boolean | INDEX, FUNC |
| 방향 값 | `input_joystick_value` | Number | INDEX |
| 축 비교 | `input_joystick_axis` | Boolean | INDEX, FUNC, OP, VALUE |
| 축 값 | `input_joystick_axis_value` | Number | INDEX, FUNC |

## FUNC 값 (`input_joystick_status`)
| 방향 | FUNC 값 |
|------|---------|
| 중앙 | `0` |
| 위 | `100` |
| 아래 | `-100` |
| 왼쪽 | `-50` |
| 오른쪽 | `50` |

## 축 FUNC 값 (`input_joystick_axis`)
`X`, `Y`

## 사용 규칙
- **핵심 패턴**: `controls_whileUntil` + `input_joystick_status` — 방향 유지 동안 모터 연속 제어
- 조이스틱 놓으면 자동 복귀 → whileUntil 탈출 → 기본 상태(멈춤)
- **if로 제어하면 안 됨** — 연속 동작이므로 반드시 whileUntil

## XML 예시

### 방향 감지 (Boolean)
```xml
<block type="input_joystick_status">
  <field name="INDEX">0</field>
  <field name="FUNC">100</field>
</block>
```

### whileUntil + 모터 (전진)
```xml
<block type="controls_whileUntil">
  <field name="MODE">WHILE</field>
  <value name="BOOL">
    <block type="input_joystick_status">
      <field name="INDEX">0</field>
      <field name="FUNC">100</field>
    </block>
  </value>
  <statement name="DO">
    <block type="output_motorA_speed">
      <field name="INDEX">0</field>
      <value name="VALUE">
        <shadow type="math_number_min-100_max100">
          <field name="NUM">-30</field>
        </shadow>
      </value>
      <next>
        <block type="output_motorB_speed">
          <field name="INDEX">0</field>
          <value name="VALUE">
            <shadow type="math_number_min-100_max100">
              <field name="NUM">30</field>
            </shadow>
          </value>
        </block>
      </next>
    </block>
  </statement>
</block>
```

## 관련 레시피

### 조이스틱 자동차 (조이스틱+듀얼모터)
```
무한반복:
  output_motorA_stop / output_motorB_stop
  조이스틱 위(100) 동안: A=-30, B=30 (전진)
  조이스틱 아래(-100) 동안: A=30, B=-30 (후진)
  조이스틱 왼쪽(-50) 동안: A=30, B=30 (좌회전)
  조이스틱 오른쪽(50) 동안: A=-30, B=-30 (우회전)
```
