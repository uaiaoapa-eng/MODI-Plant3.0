# TOF (거리 센서)

## 블록 타입
| 블록 | 타입명 | 반환 | 필드 |
|------|--------|------|------|
| 거리(cm) 비교 | `input_tof_cm` | Boolean | INDEX, OP, VALUE |
| 거리(inch) 비교 | `input_tof_inch` | Boolean | INDEX, OP, VALUE |
| 거리 값 | `input_tof_value` | Number | INDEX, FUNC |

## OP 값
`>`, `<`, `>=`, `<=`, `==`, `!=`

## FUNC 값 (`input_tof_value`)
`cm`, `inch`

## 사용 규칙
- **범위**: 0~100cm (100cm 넘으면 100 고정 — 먼 거리 구분 불가)
- **임계값 예시**: 50cm 미만 = "가까움", 10cm 이하 = "매우 가까움"

## XML 예시
```xml
<block type="input_tof_cm">
  <field name="INDEX">0</field>
  <field name="OP">&lt;</field>
  <value name="VALUE">
    <shadow type="math_number_min0_max100">
      <field name="NUM">50</field>
    </shadow>
  </value>
</block>
```

## 관련 레시피

### 현관문센서등 (TOF+LED+디스플레이)
```
무한반복:
  만약 input_tof_cm(OP:<, VALUE:50):
    output_led_color(빨강) + output_display_drawing(FUNC:smileb)
  아니면:
    output_led_clear + output_display_drawing(FUNC:sleeping)
```

### 자동차후방감지센서 (TOF+스피커)
```
무한반복:
  output_speaker_clear
  controls_whileUntil(input_tof_cm(OP:<=, VALUE:10)):
    output_speaker_note(FUNC:F_SOL_5, VALUE:100)
```
