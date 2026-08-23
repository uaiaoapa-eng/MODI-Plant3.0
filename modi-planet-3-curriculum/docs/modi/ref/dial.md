# 다이얼 (Dial)

## 블록 타입
| 블록 | 타입명 | 반환 | 필드 |
|------|--------|------|------|
| 위치 비교 | `input_dial_position` | Boolean | INDEX, OP, VALUE |
| 각도 비교 | `input_dial_angle` | Boolean | INDEX, OP, VALUE |
| 구간 비교 | `input_dial_section` | Boolean | INDEX, OP, FUNC |
| 속도 비교 | `input_dial_speed` | Boolean | INDEX, OP, VALUE |
| 다이얼 값 | `input_dial_value` | Number | INDEX, FUNC |

## OP 값 (Boolean 블록)
`>`, `<`, `>=`, `<=`, `==`, `!=`

## FUNC 값 (`input_dial_value`)
| 동작 | FUNC 값 | 범위 |
|------|---------|------|
| 회전 위치 | `getTurn` | 0~100% (가장 자주 사용) |
| 회전 각도 | `getTurnAngle` | 0~360° |
| 구간 | `getSection` | 0~10 |
| 회전 속도 | `getTurnSpeed` | -100~100 |

## 사용 규칙
- **위치 50% 기준 이진 분기**: ≤ 50% = 왼쪽(잠금), > 50% = 오른쪽(해제)
- **동적 값으로 활용**: `input_dial_value(getTurn)`을 스피커 볼륨 등에 직접 연결
  - 다이얼을 돌리면 실시간으로 볼륨/밝기 등이 변함

## XML 예시

### Boolean (조건 분기)
```xml
<block type="input_dial_position">
  <field name="INDEX">0</field>
  <field name="OP">&lt;=</field>
  <value name="VALUE">
    <shadow type="math_number_min0_max100">
      <field name="NUM">50</field>
    </shadow>
  </value>
</block>
```

### Number (값으로 사용 — 스피커 볼륨에 연결)
```xml
<block type="input_dial_value">
  <field name="INDEX">0</field>
  <field name="FUNC">getTurn</field>
</block>
```

## 관련 레시피

### 화장실사용중사인 (다이얼+LED)
```
무한반복:
  만약 input_dial_position(OP:<=, VALUE:50):
    output_led_color(초록)
  아니면:
    output_led_color(빨강)
```
