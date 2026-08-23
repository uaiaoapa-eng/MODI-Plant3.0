# 환경센서 (Environment)

## 블록 타입
| 블록 | 타입명 | 반환 | 필드 |
|------|--------|------|------|
| 온도(℃) 비교 | `input_environment_celsius` | Boolean | INDEX, OP, VALUE |
| 온도(℉) 비교 | `input_environment_fahrenheit` | Boolean | INDEX, OP, VALUE |
| 습도 비교 | `input_environment_humidity` | Boolean | INDEX, OP, VALUE |
| 조도 비교 | `input_environment_illuminance` | Boolean | INDEX, OP, VALUE |
| 소리크기 비교 | `input_environment_volume` | Boolean | INDEX, OP, VALUE |
| 환경값 | `input_environment_value` | Number | INDEX, FUNC |

## OP 값
`>`, `<`, `>=`, `<=`, `==`, `!=`

## FUNC 값 (`input_environment_value`)
`getTemperature_C`, `getTemperature_F`, `getHumidity`, `getIntensity`, `getVolume`, `getRed`, `getGreen`, `getBlue`

## 측정 범위
- **온도**: -10~60°C / **습도**: 0~100% / **조도**: 0~100% / **소리**: 0~100%

## 사용 규칙
- **소리 임계값** (용도별): 10%(폐활량), 25%(표정봇), 30%(소음감지)
- `controls_whileUntil`과 조합: "소리가 감지되는 동안" 패턴

## XML 예시
```xml
<block type="input_environment_volume">
  <field name="INDEX">0</field>
  <field name="OP">></field>
  <value name="VALUE">
    <shadow type="math_number_min0_max100">
      <field name="NUM">10</field>
    </shadow>
  </value>
</block>
```

## 관련 레시피

### 폐활량측정기 (환경센서+디스플레이)
```
무한반복:
  variables_set(Record, 0)
  output_display_text("Ready...")
  controls_whileUntil(input_environment_volume(OP:>, VALUE:10)):
    control_wait(0.1)
    variables_add(Record, 0.1)
  만약 logic_compare(Record GT 0.5):
    output_display_variable(FUNC:0, VALUE:Record)
    control_wait(3)
```

### 잠자는표정봇 (환경센서+디스플레이)
```
무한반복:
  output_display_drawing(FUNC:sleeping)
  만약 input_environment_volume(OP:>=, VALUE:25):
    output_display_drawing(FUNC:smileb)
    control_wait(2)
```
