# MODI Core Rules

Blockly XML 생성 시 참조하는 핵심 규칙.

## 통신

```json
{ "type": "LOAD_BLOCK_FROM_FILE_REQUEST", "data": { "xmlCode": "<xml ...>...</xml>" } }
```

`window.parent.postMessage(JSON.stringify(msg), "*")` 로 전송.

## XML 래퍼

모든 XML은 아래 래퍼로 감싼다. `network_upload` 없으면 업로드 불가, `controls_whileInfinite` 없으면 1회 실행 후 종료.

```xml
<xml xmlns="http://www.w3.org/1999/xhtml">
  <variables><!-- 변수 사용 시 선언 --></variables>
  <block type="network_upload" deletable="false">
    <next>
      <block type="controls_whileInfinite">
        <statement name="DO">
          <!-- 모든 로직은 여기 안에 -->
        </statement>
      </block>
    </next>
  </block>
</xml>
```

## 블록 타입

### 입력 (Boolean)

| 모듈 | 블록 타입 | 필드 |
|------|-----------|------|
| 버튼 | `input_button_status` | INDEX, FUNC |
| 다이얼 | `input_dial_position` | INDEX, OP, VALUE |
| 조이스틱 | `input_joystick_status` | INDEX, FUNC |
| 환경-소리 | `input_environment_volume` | INDEX, OP, VALUE |
| 환경-조도 | `input_environment_illuminance` | INDEX, OP, VALUE |
| 환경-온도 | `input_environment_celsius` | INDEX, OP, VALUE |
| 환경-습도 | `input_environment_humidity` | INDEX, OP, VALUE |
| TOF | `input_tof_cm` | INDEX, OP, VALUE |
| IMU흔들림 | `input_imu_shaking` | INDEX, OP, VALUE |
| IMU각도 | `input_imu_angle` | INDEX, FUNC, OP, VALUE |

### 입력 (Number)

| 모듈 | 블록 타입 | 필드 |
|------|-----------|------|
| 다이얼 | `input_dial_value` | INDEX, FUNC |
| 환경센서 | `input_environment_value` | INDEX, FUNC |
| TOF | `input_tof_value` | INDEX, FUNC |
| IMU | `input_imu_value` | INDEX, FUNC |

### 출력

| 모듈 | 블록 타입 | 필드 |
|------|-----------|------|
| LED | `output_led_rgb` | INDEX, RED, GREEN, BLUE |
| LED | `output_led_color` | INDEX, COLOUR |
| LED | `output_led_clear` | INDEX |
| 디스플레이 | `output_display_text` | INDEX, VALUE |
| 디스플레이 | `output_display_drawing` | INDEX, FUNC |
| 디스플레이 | `output_display_variable` | INDEX, FUNC, VALUE |
| 디스플레이 | `output_display_clear` | INDEX |
| 스피커 | `output_speaker_note` | INDEX, FUNC, VALUE(볼륨) |
| 스피커 | `output_speaker_melody` | INDEX, FUNC, VALUE(볼륨) |
| 스피커 | `output_speaker_clear` | INDEX |
| 모터A | `output_motorA_speed` | INDEX, VALUE |
| 모터A | `output_motorA_stop` | INDEX |
| 모터B | `output_motorB_speed` | INDEX, VALUE |
| 모터B | `output_motorB_stop` | INDEX |

### 제어

| 블록 | 타입명 | 필드 |
|------|--------|------|
| 무한 반복 | `controls_whileInfinite` | DO(statement) |
| 조건 반복 | `controls_whileUntil` | MODE(WHILE), BOOL, DO(statement) |
| N번 반복 | `controls_repeat_ext` | TIMES, DO(statement) |
| if-only | `controls_ifonly` | IF0, DO0(statement) |
| if-else | `controls_ifelse` | IF0, DO0(statement), ELSE(statement) |
| 대기 | `control_wait` | TIME |

### 연산 / 변수

| 블록 | 타입명 | 필드 |
|------|--------|------|
| 숫자(0~100) | `math_number_min0_max100` | NUM |
| 숫자(-100~100) | `math_number_min-100_max100` | NUM |
| 숫자(소수) | `math_decimal_number_min-99999_max99999` | NUM |
| 비교 | `logic_compare` | OP(EQ/NEQ/LT/LTE/GT/GTE), A, B |
| 논리 | `logic_operation` | OP(AND/OR), A, B |
| 랜덤 정수 | `math_random_int` | FROM, TO |
| 변수 설정 | `variables_set` | VAR, VALUE |
| 변수 읽기 | `variables_get` | VAR |
| 변수 증감 | `variables_add` | VAR, DELTA |

### 블록 타입 참고

- `logic_compare` OP: `EQ`/`GT`/`LT` 등. MODI 센서 블록 OP: `>`/`<`/`>=`/`<=`/`==`/`!=`
- INDEX는 0-based (첫 번째 모듈 = `0`)

**LED 색상 — 두 방식 구분**

- `output_led_rgb`: RED/GREEN/BLUE 각각 `<value>`로 숫자 블록(0~100) 연결
- `output_led_color`: COLOUR는 `<value>` 안에 `colour_hsv_sliders` 블록을 넣고, 그 블록의 `<field name="COLOUR">`에 hex(`#ff0000`)를 쓴다. COLOUR를 `<field>`로 쓰거나 숫자/hex를 직접 넣지 말 것.

```xml
<block type="output_led_color">
  <field name="INDEX">0</field>
  <value name="COLOUR">
    <block type="colour_hsv_sliders"><field name="COLOUR">#ff0000</field></block>
  </value>
</block>
```

## 제어 흐름

- 단발 동작(LED) → `controls_ifonly` / 연속 동작(모터, 스피커) → `controls_whileUntil`
- if로 모터 제어 시 끊김 발생 — 반드시 `controls_whileUntil`로 감싸기
- 다중 조건: `controls_ifelse`의 ELSE에 다시 `controls_ifelse` 중첩
- 변수 초기화: 무한반복 첫 줄에서 `variables_set(변수, 0)`
- 타이머: `variables_set(0)` → `controls_whileUntil(조건)` → `control_wait(0.1)` + `variables_add(0.1)`

## 흔한 실수

| 실수 | 올바른 방법 |
|------|-------------|
| `network_upload`/`controls_whileInfinite` 누락 | 래퍼 필수 |
| 모터를 `controls_ifonly`로 제어 | `controls_whileUntil` 사용 |
| LED/스피커 안 끔 | else 또는 `*_clear` |
| 스피커 음 사이 간격 없음 | `speaker_clear` → `control_wait(0.01)` → 다음 음 |
| 변수 초기화 안 함 | 무한반복 첫 줄 `variables_set(변수, 0)` |
| `display_clear` 루프 안에서 사용 | 덮어쓰기로 갱신 (clear는 깜빡임) |
| 센서 OP에 `GT`/`LT` 사용 | MODI 센서 OP는 `>`/`<` 등 기호 |
| INDEX를 1부터 시작 | 0-based |
| `variables_change` 사용 | 실제 블록명: `variables_add` |
| `output_led_color` COLOUR에 숫자/직접 hex | `<value>` → `colour_hsv_sliders` |
| 필수 `<field>` 누락 | 블록표의 모든 필드 빠짐없이 (특히 INDEX, FUNC) |

## XML 연결

- `<next>` — 순차 연결
- `<statement>` — 루프/조건문 본문
- `<value>` — 입력값 (숫자, 센서, 변수)
- `<field>` — 고정값 (드롭다운, 텍스트)
- OP `<`는 XML에서 `&lt;`로 이스케이프

## 사용 가능한 모듈

`button` `dial` `joystick` `environment` `tof` `imu` `led` `display` `speaker` `motor`
