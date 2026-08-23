# LED

## 블록 타입
| 블록 | 타입명 | 필드/입력 |
|------|--------|----------|
| RGB 색상 | `output_led_rgb` | INDEX, RED(input), GREEN(input), BLUE(input) |
| 색상 선택기 | `output_led_color` | INDEX, COLOUR(input) |
| 끄기 | `output_led_clear` | INDEX |

## 사용 규칙
- **RGB 범위**: RED, GREEN, BLUE 각 0~100
- **색상 선택기**: `colour_hsv_sliders` 블록으로 색상 지정 (hex)
- **else 분기에서 반드시 `output_led_clear`** — 안 끄면 이전 색 유지
- **랜덤 색상**: `math_random_int(1,100)` → 변수 저장 → RGB에 연결

## XML 예시

### 색상 선택기로 빨간색
```xml
<block type="output_led_color">
  <field name="INDEX">0</field>
  <value name="COLOUR">
    <shadow type="colour_hsv_sliders">
      <field name="COLOUR">#ff0000</field>
    </shadow>
  </value>
</block>
```

### RGB로 색상 설정
```xml
<block type="output_led_rgb">
  <field name="INDEX">0</field>
  <value name="RED">
    <shadow type="math_number_min0_max100"><field name="NUM">100</field></shadow>
  </value>
  <value name="GREEN">
    <shadow type="math_number_min0_max100"><field name="NUM">0</field></shadow>
  </value>
  <value name="BLUE">
    <shadow type="math_number_min0_max100"><field name="NUM">0</field></shadow>
  </value>
</block>
```

### 끄기
```xml
<block type="output_led_clear">
  <field name="INDEX">0</field>
</block>
```

## 관련 레시피

### 랜덤 LED
```
variables_set(Red, math_random_int(1,100))
variables_set(Green, math_random_int(1,100))
variables_set(Blue, math_random_int(1,100))
output_led_rgb(RED:Red, GREEN:Green, BLUE:Blue)
...
output_led_clear
```
