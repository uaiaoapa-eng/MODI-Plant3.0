# MODI Playbook

MODI Blockly XML 코드 생성 시 반드시 참고해야 할 규칙 모음.
PDF 예제들에서 발굴한 패턴과 실제 코드베이스(`modi_blockly`)에서 추출한 블록 정의를 기반으로 작성.

> 이 문서는 새 PDF 분석 시 계속 업데이트된다.

---

## 0. 통신 프로토콜 (postMessage)

생성한 Blockly XML은 `LOAD_BLOCK_FROM_FILE_REQUEST`로 전달:

```json
{
  "type": "LOAD_BLOCK_FROM_FILE_REQUEST",
  "data": {
    "xmlCode": "<xml xmlns=\"http://www.w3.org/1999/xhtml\">...</xml>"
  }
}
```

- `window.parent.postMessage(JSON.stringify(message), "*")` 형태로 전송
- XML은 표준 Blockly XML 포맷 (`<block>`, `<field>`, `<value>`, `<statement>`, `<next>`)
- 블록 저장/로드 시 XML 뒤에 `JSON.stringify(modelInfos)` 배열이 붙을 수 있음 (AI 모델 사용 시)

---

## 1. 모듈 체계

### 1.1 모듈 분류
| 구분 | 모듈 | 개수 |
|------|------|------|
| 입력 (초록) | 버튼, 환경, IMU, TOF, 다이얼, 조이스틱 | 6 |
| 출력 (빨강) | LED, 스피커, 디스플레이, 모터A, 모터B | 5 |
| 셋업 (노랑) | 네트워크 | 1 |

### 1.2 신호 라우팅 (언플러그드 모드)
- **1 입력 → N 출력**: 모든 출력 모듈에 **100%** 신호 전달
- **N 입력 → 1 출력**: 각 입력 모듈의 신호를 **1/N**로 나누어 수신
- 프로그래밍 모드에서는 코드로 직접 제어하므로 이 규칙 적용 안 됨

### 1.3 네트워크 모듈
- 모든 프로그래밍 프로젝트의 **첫 번째 블록**은 `network_upload`
- 네트워크 모듈이 컴퓨터와 MODI 모듈 간 통신 담당
- USB 케이블로 PC와 연결하여 전원 공급

---

## 2. 코드 구조 필수 패턴

### 2.1 기본 골격 (절대 규칙)

> **⚠ 모든 생성 XML은 반드시 아래 래퍼로 감싸야 한다. 예외 없음.**

```xml
<xml xmlns="http://www.w3.org/1999/xhtml">
  <variables>
    <!-- 사용하는 변수가 있으면 여기에 선언 -->
  </variables>
  <block type="network_upload" deletable="false">
    <next>
      <block type="controls_whileInfinite">
        <statement name="DO">
          <!-- ★ 모든 로직은 여기 안에 들어간다 ★ -->
        </statement>
      </block>
    </next>
  </block>
</xml>
```

**위반 시 결과:**
- `network_upload` 없음 → 모듈에 코드가 업로드되지 않음 (아무 동작 안 함)
- `controls_whileInfinite` 없음 → 코드가 한 번만 실행되고 즉시 종료
- 이 두 블록 바깥에 로직을 배치 → 실행되지 않음

**체크리스트:**
1. XML 최상위에 `<block type="network_upload">` 존재하는가?
2. 그 안에 `<block type="controls_whileInfinite">` 존재하는가?
3. 모든 로직 블록이 `controls_whileInfinite`의 `<statement name="DO">` 안에 있는가?
4. 변수를 사용한다면 `<variables>` 섹션에 선언했는가?

### 2.2 변수 초기화
- 무한반복 **첫 줄**에서 `variables_set`으로 변수를 0으로 초기화
- 안 하면 이전 루프의 값이 누적되어 오동작
- 예: `Record = 0` → 루프 시작 시마다 리셋

### 2.3 블록 타입 총정리

#### 셋업
| 블록 | 타입명 | 용도 |
|------|--------|------|
| 네트워크 업로드 | `network_upload` | 코드 시작점 (필수) |
| 네트워크 셋업 | `network_setup` | 네트워크 설정 |
| 네트워크 실행 | `network_execute` | 실행 모드 |

#### 입력 (Boolean 반환 — 조건문에 직접 연결 가능)
| 블록 | 타입명 | 필드 |
|------|--------|------|
| 버튼 상태 | `input_button_status` | INDEX, FUNC |
| 다이얼 위치 비교 | `input_dial_position` | INDEX, OP, VALUE(input) |
| 다이얼 각도 비교 | `input_dial_angle` | INDEX, OP, VALUE(input) |
| 다이얼 구간 비교 | `input_dial_section` | INDEX, OP, FUNC |
| 다이얼 속도 비교 | `input_dial_speed` | INDEX, OP, VALUE(input) |
| 조이스틱 방향 | `input_joystick_status` | INDEX, FUNC |
| 조이스틱 축 비교 | `input_joystick_axis` | INDEX, FUNC, OP, VALUE(input) |
| 환경-온도(℃) | `input_environment_celsius` | INDEX, OP, VALUE(input) |
| 환경-온도(℉) | `input_environment_fahrenheit` | INDEX, OP, VALUE(input) |
| 환경-습도 | `input_environment_humidity` | INDEX, OP, VALUE(input) |
| 환경-조도 | `input_environment_illuminance` | INDEX, OP, VALUE(input) |
| 환경-소리크기 | `input_environment_volume` | INDEX, OP, VALUE(input) |
| TOF 거리(cm) | `input_tof_cm` | INDEX, OP, VALUE(input) |
| TOF 거리(inch) | `input_tof_inch` | INDEX, OP, VALUE(input) |
| IMU 각도 | `input_imu_angle` | INDEX, FUNC, OP, VALUE(input) |
| IMU 가속도 | `input_imu_acceleration` | INDEX, FUNC, OP, VALUE(input) |
| IMU 각속도 | `input_imu_velocity` | INDEX, FUNC, OP, VALUE(input) |
| IMU 흔들림 | `input_imu_shaking` | INDEX, OP, VALUE(input) |

#### 입력 (Number 반환 — 값으로 사용)
| 블록 | 타입명 | 필드 |
|------|--------|------|
| 버튼 값 | `input_button_value` | INDEX, FUNC |
| 다이얼 값 | `input_dial_value` | INDEX, FUNC |
| 조이스틱 방향 값 | `input_joystick_value` | INDEX |
| 조이스틱 축 값 | `input_joystick_axis_value` | INDEX, FUNC |
| 환경센서 값 | `input_environment_value` | INDEX, FUNC |
| TOF 값 | `input_tof_value` | INDEX, FUNC |
| IMU 값 | `input_imu_value` | INDEX, FUNC |

#### 출력
| 블록 | 타입명 | 필드/입력 |
|------|--------|----------|
| LED RGB | `output_led_rgb` | INDEX, RED(input), GREEN(input), BLUE(input) |
| LED 색상 | `output_led_color` | INDEX, COLOUR(input) |
| LED 끄기 | `output_led_clear` | INDEX |
| 디스플레이 텍스트 | `output_display_text` | INDEX, VALUE(input:text) |
| 디스플레이 이미지 | `output_display_drawing` | INDEX, FUNC |
| 디스플레이 변수 | `output_display_variable` | INDEX, FUNC, VALUE(input) |
| 디스플레이 지우기 | `output_display_clear` | INDEX |
| 디스플레이 오프셋 | `output_display_offset` | INDEX, OFFSET_X(input), OFFSET_Y(input) |
| 디스플레이 위치 | `output_display_position` | INDEX, FUNC, AXIS_X_VALUE(input), FUNC2, AXIS_Y_VALUE(input) |
| 스피커 음계 | `output_speaker_note` | INDEX, FUNC, VALUE(input:볼륨) |
| 스피커 멜로디 | `output_speaker_melody` | INDEX, FUNC, VALUE(input:볼륨) |
| 스피커 주파수 | `output_speaker_frequency` | INDEX, FREQUENCY(input), VALUE(input:볼륨) |
| 스피커 끄기 | `output_speaker_clear` | INDEX |
| 모터A 속도 | `output_motorA_speed` | INDEX, VALUE(input) |
| 모터A 멈추기 | `output_motorA_stop` | INDEX |
| 모터A 각도 | `output_motorA_angle` | INDEX, VALUE(input) |
| 모터A 상대각도 | `output_motorA_relative_angle` | INDEX, FUNC, VALUE(input) |
| 모터A 각도+속도 | `output_motorA_angle_speed` | INDEX, VALUE_ANGLE(input), VALUE_SPEED(input) |
| 모터B 속도 | `output_motorB_speed` | INDEX, VALUE(input) |
| 모터B 멈추기 | `output_motorB_stop` | INDEX |
| 모터B 각도 | `output_motorB_angle` | INDEX, VALUE(input) |
| 모터B 상대각도 | `output_motorB_relative_angle` | INDEX, FUNC, VALUE(input) |
| 모터B 각도+속도 | `output_motorB_angle_speed` | INDEX, VALUE_ANGLE(input), VALUE_SPEED(input) |

#### 제어
| 블록 | 타입명 | 필드/입력 |
|------|--------|----------|
| 무한 반복 | `controls_whileInfinite` | DO(statement) |
| 조건 반복 | `controls_whileUntil` | MODE, BOOL(input), DO(statement) |
| N번 반복 | `controls_repeat_ext` | TIMES(input), DO(statement) |
| if-only | `controls_ifonly` | IF0(input), DO0(statement) |
| if-else | `controls_ifelse` | IF0(input), DO0(statement), ELSE(statement) |
| 대기 | `control_wait` | TIME(input) |
| 반복 탈출 | `loop_break` | — |
| 반복 계속 | `loop_continue` | — |

#### 연산 / 논리
| 블록 | 타입명 | 필드/입력 |
|------|--------|----------|
| 숫자(0~100) | `math_number_min0_max100` | NUM |
| 숫자(-100~100) | `math_number_min-100_max100` | NUM |
| 숫자(0~360) | `math_number_min0_max360` | NUM |
| 숫자(범용 소수) | `math_decimal_number_min-99999_max99999` | NUM |
| 산술 연산 | `math_arithmetic` | OP(ADD/MINUS/MULTIPLY/DIVIDE/POWER), A(input), B(input) |
| 랜덤 정수 | `math_random_int` | FROM(input), TO(input) |
| 나머지 연산 | `math_modulo` | DIVIDEND(input), DIVISOR(input) |
| 비교 연산 | `logic_compare` | OP(EQ/NEQ/LT/LTE/GT/GTE), A(input), B(input) |
| 논리 연산 | `logic_operation` | OP(AND/OR), A(input), B(input) |
| 논리 부정 | `logic_negate` | BOOL(input) |

#### 변수
| 블록 | 타입명 | 필드/입력 |
|------|--------|----------|
| 변수 설정 | `variables_set` | VAR(변수명), VALUE(input) |
| 변수 읽기 | `variables_get` | VAR(변수명) |
| 변수 증감 | `variables_add` | VAR(변수명), DELTA(input) |

> **주의**: `logic_compare`의 OP는 `EQ/NEQ/LT/LTE/GT/GTE` (표준 Blockly).
> MODI 센서 Boolean 블록의 OP는 `==`, `!=`, `<`, `<=`, `>`, `>=` (문자열 기호).

---

## 3. 드롭다운 값 레퍼런스

### 3.1 센서 블록 OP 값 (비교 연산자)
MODI 입력 Boolean 블록(`input_*`)에서 사용:
```
>   >=   <   <=   ==   !=
```
XML 예: `<field name="OP">></field>`

### 3.2 버튼 FUNC 값 (`input_button_status`)
| UI 라벨 | FUNC 값 |
|---------|---------|
| 클릭 | `getClick()` |
| 더블클릭 | `getDoubleClick()` |
| 누름 | `getPressStatus()` |
| 토글 | `getToggle()` |

### 3.3 조이스틱 FUNC 값 (`input_joystick_status`)
| 방향 | FUNC 값 |
|------|---------|
| 중앙 | `0` |
| 위 | `100` |
| 아래 | `-100` |
| 왼쪽 | `-50` |
| 오른쪽 | `50` |

### 3.4 조이스틱 축 FUNC 값 (`input_joystick_axis`)
`X`, `Y`

### 3.5 다이얼 값 FUNC (`input_dial_value`)
`getTurn`, `getTurnAngle`, `getSection`, `getTurnSpeed`

### 3.6 IMU FUNC 값
- **각도** (`input_imu_angle`): `getRoll`, `getPitch`, `getYaw`
- **가속도** (`input_imu_acceleration`): `getAccelerationX`, `getAccelerationY`, `getAccelerationZ`
- **각속도** (`input_imu_velocity`): `getAngularVelocityX`, `getAngularVelocityY`, `getAngularVelocityZ`

### 3.7 환경센서 값 FUNC (`input_environment_value`)
`getTemperature_C`, `getTemperature_F`, `getHumidity`, `getIntensity`, `getVolume`, `getRed`, `getGreen`, `getBlue`

### 3.8 TOF 값 FUNC (`input_tof_value`)
`cm`, `inch`

### 3.9 스피커 음계 FUNC 값 (`output_speaker_note`)
3 옥타브 × 7음 = 21개:
| 음 | 옥타브5 (낮은) | 옥타브6 (중간) | 옥타브7 (높은) |
|----|---------------|---------------|---------------|
| 도 | `F_DO_5` | `F_DO_6` | `F_DO_7` |
| 레 | `F_RE_5` | `F_RE_6` | `F_RE_7` |
| 미 | `F_MI_5` | `F_MI_6` | `F_MI_7` |
| 파 | `F_PA_5` | `F_PA_6` | `F_PA_7` |
| 솔 | `F_SOL_5` | `F_SOL_6` | `F_SOL_7` |
| 라 | `F_RA_5` | `F_RA_6` | `F_RA_7` |
| 시 | `F_SI_5` | `F_SI_6` | `F_SI_7` |

> PDF 예제에서 "솔"은 기본 `F_SOL_5`, "높은 도"는 `F_DO_6`에 대응.

### 3.10 스피커 멜로디 FUNC 값 (`output_speaker_melody`)
**클래식/동요 (MIDI)**: `Delibes.mid`, `London.mid`, `OldMac.mid`, `Mozart21.mid`, `Vivaldi.mid`, `Bizet.mid`, `twinkle.mid`, `Birthday.mid`, `Jingle.mid`, `Merry.mid` 등
**효과음 (WAV)**: `Alarm.wav`, `Siren.wav`, `Camera.wav`, `Bomb.wav`, `Car.wav`, `Start.wav`, `Complete.wav`, `Win.wav`, `Success.wav`, `Robot.wav`, `Exciting.wav`, `Bouncing.wav`
**감정/경고 (MIDI)**: `Emotion1.mid`, `Emotion2.mid`, `Emotion3.mid`, `Warning1.mid`, `Warning2.mid`, `Start1.mid`, `Complet1.mid`

### 3.11 디스플레이 이미지 FUNC 값 (`output_display_drawing`)
**표정**: `smileb`, `love`, `smiling`, `angry`, `tired`, `surprise`, `cry`, `dizzy`, `bilnd`, `sleeping`, `emv`, `proud`
**동물**: `dog`, `cat`, `rabbit`, `chick`, `lion`, `turtle`, `sparrow`, `penguin`, `butfly`, `fish`, `dolphin`, `hedgeh`
**자연**: `flower`, `tree`, `sun`, `star`, `moon`, `earth`, `cloud`, `rain`, `snow`, `wind`, `thunder`, `fire`
**음식**: `apple`, `banana`, `strawb`, `peach`, `waterm`, `chicken`, `pizza`, `hamburg`, `cake`, `nuddle`, `donut`, `candy`
**인터페이스**: `check`, `x`, `play`, `stop2`, `pause`, `power`, `up`, `down`, `righta`, `lefta`, `heart`, `note`

### 3.12 디스플레이 줄 위치 FUNC 값 (`output_display_variable`)
| 줄 | FUNC 값 |
|----|---------|
| 첫째 줄 | `0` |
| 둘째 줄 | `20` |
| 셋째 줄 | `40` |

### 3.13 모터 상대각도 FUNC 값 (`output_motorA/B_relative_angle`)
`clock-wise`, `counter-clock-wise`

### 3.14 INDEX 값
모든 모듈의 INDEX는 **0-based**: 첫 번째 모듈 = `0`, 두 번째 = `1`
(UI에서는 "1", "2"로 표시되지만, XML에서는 `0`, `1`)

---

## 4. 입력 모듈별 사용 규칙

### 4.1 TOF (거리 센서)
- **범위**: 0~100cm (100cm 넘으면 100 고정)
- **Boolean**: `input_tof_cm` — INDEX, OP, VALUE
- **Number**: `input_tof_value` — INDEX, FUNC(`cm` 또는 `inch`)
- **임계값 예시**: 50cm 미만이면 "가까움"

### 4.2 환경 센서
- **온도**: -10~60°C / **습도**: 0~100% / **조도**: 0~100% / **소리**: 0~100%
- **소리 임계값**: 10%(폐활량), 25%(표정봇), 30%(소음감지)
- `controls_whileUntil`과 조합: "소리가 감지되는 동안" 패턴

### 4.3 버튼
- **클릭** (`getClick()`): 눌렀다 뗀 순간 감지 — 가장 자주 사용
- **더블클릭** (`getDoubleClick()`): 빠르게 두 번
- **누름** (`getPressStatus()`): 누르고 있는 동안 유지
- **토글** (`getToggle()`): 누를 때마다 0↔100 전환

### 4.4 IMU (관성 센서)
- **흔들림**: `input_imu_shaking` — 진동 세기 (0~100%)
- **임계값**: 보통 20% (지진경보기)
- **각도**: `input_imu_angle` — getRoll/getPitch/getYaw (-180~180)

### 4.5 다이얼
- **위치**: 0~100% — `input_dial_position`
- **중앙 기준 분기**: 위치 ≤ 50% = 왼쪽(잠금), > 50% = 오른쪽(해제)
- **Number 값으로 사용**: `input_dial_value` FUNC=`getTurn` → 스피커 볼륨 등에 동적 연결

### 4.6 조이스틱
- **방향 감지**: `input_joystick_status` — 4방향 + 중앙 (Boolean 반환)
- **FUNC 값**: 위=`100`, 아래=`-100`, 왼쪽=`-50`, 오른쪽=`50`, 중앙=`0`
- **핵심 패턴**: `controls_whileUntil` + `input_joystick_status` — 방향 유지 동안 모터 연속 제어
- 조이스틱 놓으면 자동 복귀 → whileUntil 탈출 → 기본 상태(멈춤)

---

## 5. 출력 모듈별 사용 규칙

### 5.1 LED
- **RGB**: `output_led_rgb` — RED, GREEN, BLUE 각 0~100 (input)
- **색상 선택기**: `output_led_color` — COLOUR input (색상 피커, 프리셋 아님)
- **끄기**: `output_led_clear`
- **else 분기에서 반드시 `output_led_clear`** — 안 끄면 이전 색 유지

### 5.2 디스플레이
- **3줄 표시**: `output_display_variable` FUNC 값으로 줄 선택 (`0`/`20`/`40`)
- **이미지**: `output_display_drawing` — FUNC에 이미지명 (예: `sleeping`, `smileb`)
- **덮어쓰기**: 같은 줄에 다시 쓰면 이전 내용 덮어씀 (clear 불필요)
- **`output_display_clear`는 화면 전체 초기화 시에만** — 루프 안에서 쓰면 깜빡임

### 5.3 스피커
- **음계**: `output_speaker_note` — FUNC에 음 (예: `F_SOL_5`), VALUE에 볼륨(0~100)
- **멜로디**: `output_speaker_melody` — FUNC에 파일명 (예: `Alarm.wav`), VALUE에 볼륨
- **끄기**: `output_speaker_clear`
- **필수 패턴**: `note` → `control_wait(재생시간)` → `speaker_clear`
  - 끄지 않으면 계속 울림
- **음 사이 간격**: `speaker_clear` → `control_wait(0.01)` → 다음 음 (0.01초로 음 구분)
- **음표 길이**: wait 시간으로 조절 (0.3초=1박, 0.6초=2박)
- **동적 볼륨**: VALUE input에 `input_dial_value`(FUNC=`getTurn`) 블록 연결 → 다이얼로 실시간 볼륨 조절

### 5.4 모터
- **모터A / 모터B 별도 블록**: `output_motorA_speed` / `output_motorB_speed`
- **속도**: VALUE에 -100~100% (음수=역방향, 양수=정방향)
  - 30% = 일반 속도, 15% = 느리고 부드러움
- **멈추기**: `output_motorA_stop` / `output_motorB_stop`
- **듀얼 모터 차동 조향** (차체 양쪽 대칭 배치):
  - 전진: A=-30%, B=30% (반대 부호)
  - 후진: A=30%, B=-30% (반대 부호)
  - 좌회전(제자리): A=30%, B=30% (같은 부호)
  - 우회전(제자리): A=-30%, B=-30% (같은 부호)
- **기본 상태 패턴**: 무한반복 첫 줄에서 `motorA_stop` + `motorB_stop`
- **주의: if로 모터 제어 → 끊김!** 반드시 `controls_whileUntil`로 감싸기

---

## 6. 제어 흐름 패턴

### 6.1 if-else (조건 분기)
```
controls_ifelse
  ├─ IF0: 센서 Boolean 블록 (input_*)
  ├─ DO0: 조건 참일 때
  └─ ELSE: 조건 거짓일 때 (출력 clear 포함)
```

### 6.2 if-only (조건만)
```
controls_ifonly
  ├─ IF0: 조건
  └─ DO0: 조건 참일 때만 실행
```

### 6.3 조건 반복 (whileUntil)
```
controls_whileUntil (MODE=WHILE)
  ├─ BOOL: 센서 Boolean 블록
  └─ DO: 조건 유지 동안 반복
```
- **if vs whileUntil 선택 기준**:
  - 단발 동작 (LED 색 변경) → `controls_ifonly`로 충분
  - 연속 동작 (모터, 스피커 유지) → 반드시 `controls_whileUntil`

### 6.4 N번 반복
```
controls_repeat_ext
  ├─ TIMES: 숫자 블록
  └─ DO: N번 반복할 내용
```

### 6.5 논리 연산 (AND / OR)
- `logic_operation` OP=`AND`: 두 조건 모두 참
- `logic_operation` OP=`OR`: 하나라도 참
- 범위 표현: `소리크기 > 33 AND 소리크기 <= 66`

### 6.6 다중 조건 체이닝 (if-elseif-else)
- `controls_ifelse`의 ELSE에 다시 `controls_ifelse` 중첩

---

## 7. 타이밍 / 대기 패턴

### 7.1 control_wait
- TIME input에 숫자 블록 연결 (초 단위)
- **블로킹** 방식

### 7.2 타이머 구현 패턴
```
variables_set(Record, 0)
controls_whileUntil(센서 조건)
  control_wait(0.1)
  variables_add(Record, 0.1)    ← variables_add로 누적
```

---

## 8. 변수 사용 패턴

### 8.1 초기화
- `variables_set` — 루프 시작 시 0 리셋
- XML `<variables>` 섹션에 변수 선언 필요

### 8.2 누적
- `variables_add` — DELTA에 증감값 (예: 0.1씩 누적)

### 8.3 랜덤값
- `math_random_int` → 변수에 저장 후 사용 (직접 출력 블록에 넣지 않음)
  - 같은 랜덤 블록을 여러 곳에서 참조하면 매번 다른 값이 됨

### 8.4 비교
- `logic_compare` — OP(`EQ`/`GT`/`LT`/`GTE`/`LTE`/`NEQ`), A, B
- 변수값을 숫자 블록과 비교하는 패턴이 일반적

---

## 9. 레시피

> **모든 레시피는 Section 2.1의 래퍼(`network_upload` → `controls_whileInfinite`) 안에 들어간다.**
> 아래 의사코드에서 "무한반복:" 이하가 `controls_whileInfinite`의 DO statement 내부에 해당한다.
> XML 생성 시 반드시 래퍼로 감싸야 하며, 변수 사용 시 `<variables>` 선언도 포함할 것.

### 9.1 거리 기반 LED 제어 (현관문센서등)
```
무한반복:
  만약 input_tof_cm(OP:<, VALUE:50):
    output_led_color(빨강)
    output_display_drawing(FUNC:smileb)
  아니면:
    output_led_clear
    output_display_drawing(FUNC:sleeping)
```
- **모듈**: TOF + LED + 디스플레이

### 9.2 소리 기반 시간 측정 (폐활량측정기)
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
- **모듈**: 환경센서 + 디스플레이

### 9.3 스피커 알람 (폐활량측정기 미션2)
```
만약 logic_compare(Record GTE 3):
  output_speaker_melody(FUNC:Alarm.wav, VALUE:100)
  output_display_variable(FUNC:0, VALUE:Record)
  control_wait(3)
  output_speaker_clear
```

### 9.4 랜덤 LED (폐활량측정기 미션3)
```
만약 logic_compare(Record GTE 2):
  variables_set(Red, math_random_int(1,100))
  variables_set(Green, math_random_int(1,100))
  variables_set(Blue, math_random_int(1,100))
  output_led_rgb(RED:Red, GREEN:Green, BLUE:Blue)
output_display_variable(FUNC:0, VALUE:Record)
control_wait(3)
output_led_clear
```

### 9.5 다이얼 기반 상태 표시 (화장실사용중사인)
```
무한반복:
  만약 input_dial_position(OP:<=, VALUE:50):
    output_led_color(초록)
  아니면:
    output_led_color(빨강)
```

### 9.6 거리 기반 경고음 (자동차후방감지센서)
```
무한반복:
  output_speaker_clear
  controls_whileUntil(input_tof_cm(OP:<=, VALUE:10)):
    output_speaker_note(FUNC:F_SOL_5, VALUE:100)
```

### 9.7 학교종 멜로디 (스피커 음계 패턴)
```
output_speaker_note(FUNC:F_SOL_5, VALUE:100)
control_wait(0.3)
output_speaker_clear
control_wait(0.01)
output_speaker_note(FUNC:F_SOL_5, VALUE:100)
control_wait(0.3)
output_speaker_clear
control_wait(0.01)
output_speaker_note(FUNC:F_RA_5, VALUE:100)
control_wait(0.3)
output_speaker_clear
control_wait(0.01)
...
```
- **핵심**: 음 → wait(길이) → clear → wait(0.01) → 다음 음

### 9.8 소리 반응 표정봇 (잠자는표정봇)
```
무한반복:
  output_display_drawing(FUNC:sleeping)
  만약 input_environment_volume(OP:>=, VALUE:25):
    output_display_drawing(FUNC:smileb)
    control_wait(2)
```

### 9.9 타이머 알람 (다이얼+버튼)
```
무한반복:
  output_display_text("Timer Alarm")
  output_speaker_clear
  만약 logic_operation(AND,
    input_dial_position(OP:>, VALUE:50),
    input_button_status(FUNC:getClick())):
    output_display_text("3 seconds")
    controls_repeat_ext(3):
      output_speaker_note(FUNC:F_PA_5, VALUE:100)
      control_wait(0.2)
      output_speaker_note(FUNC:F_PA_5, VALUE:0)
      control_wait(0.8)
    output_speaker_note(FUNC:F_DO_6, VALUE:100)
    control_wait(1)
```

### 9.10 지진 경보기 (IMU 지연확인)
```
무한반복:
  output_led_clear
  output_speaker_clear
  만약 input_imu_shaking(OP:>, VALUE:20):
    control_wait(1.5)
    만약 input_imu_shaking(OP:>, VALUE:20):
      control_wait(1.5)
      controls_whileUntil(input_imu_shaking(OP:>, VALUE:20)):
        controls_repeat_ext(8):
          output_led_color(빨강) + output_speaker_note(FUNC:F_RA_5)
          control_wait(0.2)
          output_led_color(노랑) + output_speaker_note(FUNC:F_MI_5)
          control_wait(0.2)
```

### 9.11 뮤직박스 (버튼+스피커+다이얼 볼륨)
```
무한반복:
  output_speaker_clear
  만약 input_button_status(FUNC:getClick()):
    controls_repeat_ext(2):
      output_speaker_note(FUNC:F_DO_5, VALUE:input_dial_value(getTurn))
      control_wait(0.3)
      output_speaker_clear → control_wait(0.01)
      output_speaker_note(FUNC:F_MI_5, VALUE:input_dial_value(getTurn))
      control_wait(0.3)
      output_speaker_clear → control_wait(0.01)
      output_speaker_note(FUNC:F_SOL_5, VALUE:input_dial_value(getTurn))
      control_wait(0.3)
      output_speaker_clear → control_wait(0.01)
    controls_repeat_ext(3):
      output_speaker_note(FUNC:F_RA_5, VALUE:input_dial_value(getTurn))
      control_wait(0.3)
      output_speaker_clear → control_wait(0.01)
    output_speaker_note(FUNC:F_SOL_5, VALUE:input_dial_value(getTurn))
    control_wait(0.6)
    output_speaker_clear
```
- **핵심**: 버튼 클릭 트리거, 다이얼 동적 볼륨, N번 반복으로 악보 구조화

### 9.12 조이스틱 자동차 (조이스틱+듀얼모터)
```
무한반복:
  output_motorA_stop
  output_motorB_stop
  controls_whileUntil(input_joystick_status(FUNC:100)):   ← 위
    output_motorA_speed(VALUE:-30)
    output_motorB_speed(VALUE:30)
  controls_whileUntil(input_joystick_status(FUNC:-100)):  ← 아래
    output_motorA_speed(VALUE:30)
    output_motorB_speed(VALUE:-30)
  controls_whileUntil(input_joystick_status(FUNC:-50)):   ← 왼쪽
    output_motorA_speed(VALUE:30)
    output_motorB_speed(VALUE:30)
  controls_whileUntil(input_joystick_status(FUNC:50)):    ← 오른쪽
    output_motorA_speed(VALUE:-30)
    output_motorB_speed(VALUE:-30)
```
- **핵심**: 방향별 독립 whileUntil, 듀얼 모터 차동 조향 (반대 부호=직진, 같은 부호=회전)

---

## 10. 흔한 실수 / 주의사항

| 실수 | 올바른 방법 |
|------|----------|
| 변수 초기화 안 함 | 무한반복 첫 줄에서 `variables_set(변수, 0)` |
| 디스플레이에 `display_clear` 남발 | 덮어쓰기로 갱신 (clear는 깜빡임 유발) |
| LED/스피커 안 끔 | else 또는 사용 후 반드시 `*_clear` 호출 |
| 랜덤값 직접 출력 블록에 삽입 | 변수에 저장 후 변수를 참조 |
| `network_upload` 누락 | 코드 최상단에 반드시 포함 |
| whileUntil과 whileInfinite 혼동 | whileUntil = 조건부 탈출, whileInfinite = 메인 루프 |
| TOF 100cm 초과 기대 | 최대 100cm, 그 이상은 100 고정 |
| 모터를 if로 제어 → 끊김 | 연속 동작은 반드시 `controls_whileUntil`로 감싸기 |
| 스피커 음 사이 간격 없음 | `speaker_clear` → `control_wait(0.01)` → 다음 음 |
| 센서 1회 감지로 바로 반응 | 중첩 if + wait로 N회 확인 (debounce) |
| 독립 if 3개로 다단계 분기 | if-elseif-else 체이닝이 효율적 |
| 듀얼 모터 직진 시 같은 부호 | 대칭 배치이므로 A/B 반대 부호가 직진 |
| 조이스틱 방향을 if로 제어 | 연속 동작이므로 반드시 `controls_whileUntil` 사용 |
| `variables_change` 사용 | 실제 블록명은 `variables_add` |
| `output_motor_speed` 사용 | 실제는 `output_motorA_speed` / `output_motorB_speed` |
| `output_display_image` 사용 | 실제 블록명은 `output_display_drawing` |
| `input_tof_distance` 사용 | 실제 블록명은 `input_tof_cm` / `input_tof_inch` |
| `input_imu_vibration` 사용 | 실제 블록명은 `input_imu_shaking` |
| 센서 OP에 `GT`/`LT` 사용 | MODI 센서 OP는 `>`/`<`/`>=`/`<=`/`==`/`!=` (기호 문자열) |
| INDEX를 1부터 시작 | XML에서 INDEX는 0-based (`0`=첫번째 모듈) |

---

## 11. XML 구조 참고

### 11.1 변수 선언
```xml
<variables>
  <variable id="var_Record">Record</variable>
</variables>
```

### 11.2 기본 골격 XML
```xml
<xml xmlns="http://www.w3.org/1999/xhtml">
  <variables>
    <variable id="var_Record">Record</variable>
  </variables>
  <block type="network_upload" deletable="false">
    <next>
      <block type="controls_whileInfinite">
        <statement name="DO">
          <!-- 여기에 로직 -->
        </statement>
      </block>
    </next>
  </block>
</xml>
```

### 11.3 센서 Boolean 블록 (조건문에 직접 연결)
```xml
<block type="input_environment_volume">
  <field name="INDEX">0</field>
  <field name="OP">></field>
  <value name="VALUE">
    <block type="math_number_min0_max100">
      <field name="NUM">10</field>
    </block>
  </value>
</block>
```

### 11.4 버튼 상태 블록
```xml
<block type="input_button_status">
  <field name="INDEX">0</field>
  <field name="FUNC">getClick()</field>
</block>
```

### 11.5 조이스틱 방향 블록
```xml
<block type="input_joystick_status">
  <field name="INDEX">0</field>
  <field name="FUNC">100</field>
</block>
```

### 11.6 스피커 음계 (고정 볼륨)
```xml
<block type="output_speaker_note">
  <field name="INDEX">0</field>
  <field name="FUNC">F_SOL_5</field>
  <value name="VALUE">
    <block type="math_number_min0_max100">
      <field name="NUM">100</field>
    </block>
  </value>
</block>
```

### 11.7 스피커 음계 (다이얼 동적 볼륨)
```xml
<block type="output_speaker_note">
  <field name="INDEX">0</field>
  <field name="FUNC">F_DO_5</field>
  <value name="VALUE">
    <block type="input_dial_value">
      <field name="INDEX">0</field>
      <field name="FUNC">getTurn</field>
    </block>
  </value>
</block>
```

### 11.8 모터A 속도 설정
```xml
<block type="output_motorA_speed">
  <field name="INDEX">0</field>
  <value name="VALUE">
    <block type="math_number_min-100_max100">
      <field name="NUM">-30</field>
    </block>
  </value>
</block>
```

### 11.9 if-else + 센서 조건
```xml
<block type="controls_ifelse">
  <value name="IF0">
    <block type="input_tof_cm">
      <field name="INDEX">0</field>
      <field name="OP"><</field>
      <value name="VALUE">
        <block type="math_number_min0_max100">
          <field name="NUM">50</field>
        </block>
      </value>
    </block>
  </value>
  <statement name="DO0">
    <block type="output_led_color">
      <field name="INDEX">0</field>
      <value name="COLOUR">
        <block type="colour_hsv_sliders">
          <field name="COLOUR">#ff0000</field>
        </block>
      </value>
    </block>
  </statement>
  <statement name="ELSE">
    <block type="output_led_clear">
      <field name="INDEX">0</field>
    </block>
  </statement>
</block>
```

### 11.10 whileUntil + 조이스틱 + 모터
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
        <block type="math_number_min-100_max100">
          <field name="NUM">-30</field>
        </block>
      </value>
      <next>
        <block type="output_motorB_speed">
          <field name="INDEX">0</field>
          <value name="VALUE">
            <block type="math_number_min-100_max100">
              <field name="NUM">30</field>
            </block>
          </value>
        </block>
      </next>
    </block>
  </statement>
</block>
```

### 11.11 변수 비교 (logic_compare)
```xml
<block type="logic_compare">
  <field name="OP">GT</field>
  <value name="A">
    <block type="variables_get">
      <field name="VAR" id="var_Record">Record</field>
    </block>
  </value>
  <value name="B">
    <block type="math_decimal_number_min-99999_max99999">
      <field name="NUM">0.5</field>
    </block>
  </value>
</block>
```

### 11.12 변수 누적 (variables_add)
```xml
<block type="variables_add">
  <field name="VAR" id="var_Record">Record</field>
  <value name="DELTA">
    <block type="math_decimal_number_min-99999_max99999">
      <field name="NUM">0.1</field>
    </block>
  </value>
</block>
```

### 11.13 블록 연결 규칙
- `<next>` — 순차 연결 (같은 레벨에서 다음 블록)
- `<statement>` — 내부 블록 (루프/조건문 본문)
- `<value>` — 입력값 연결 (숫자, 센서값, 변수 등)
- `<field>` — 고정값 (드롭다운, 텍스트 등)

### 11.14 완전한 프로그램 예시 (현관문센서등)

> 모든 XML 생성 시 이 구조를 따를 것. `network_upload` → `controls_whileInfinite` → 로직.

```xml
<xml xmlns="http://www.w3.org/1999/xhtml">
  <block type="network_upload" deletable="false">
    <next>
      <block type="controls_whileInfinite">
        <statement name="DO">
          <block type="controls_ifelse">
            <value name="IF0">
              <block type="input_tof_cm">
                <field name="INDEX">0</field>
                <field name="OP">&lt;</field>
                <value name="VALUE">
                  <block type="math_number_min0_max100">
                    <field name="NUM">50</field>
                  </block>
                </value>
              </block>
            </value>
            <statement name="DO0">
              <block type="output_display_drawing">
                <field name="INDEX">0</field>
                <field name="FUNC">smileb</field>
                <next>
                  <block type="output_led_color">
                    <field name="INDEX">0</field>
                    <value name="COLOUR">
                      <block type="colour_hsv_sliders">
                        <field name="COLOUR">#ff0000</field>
                      </block>
                    </value>
                  </block>
                </next>
              </block>
            </statement>
            <statement name="ELSE">
              <block type="output_display_drawing">
                <field name="INDEX">0</field>
                <field name="FUNC">sleeping</field>
                <next>
                  <block type="output_led_clear">
                    <field name="INDEX">0</field>
                  </block>
                </next>
              </block>
            </statement>
          </block>
        </statement>
      </block>
    </next>
  </block>
</xml>
```

이 예시에서 확인할 포인트:
1. `network_upload` → `controls_whileInfinite` 래퍼 필수
2. 센서 Boolean 블록(`input_tof_cm`)이 `IF0`에 직접 연결
3. 같은 레벨의 블록은 `<next>`로 연결 (display → led)
4. else에서 `output_led_clear`로 출력 정리
5. OP의 `<`는 XML에서 `&lt;`로 이스케이프

---

*마지막 업데이트: 2026-06-12*
*분석 완료: 01~11 (01.언플러그드, 02.CodeEditor, 03.현관문센서등, 04.화장실사용중사인, 05.자동차후방감지센서, 06.잠자는표정봇, 07.타이머알람, 08.지진경보기, 09.폐활량측정기, 10.뮤직박스, 11.조이스틱자동차)*
*블록 타입명: modi_blockly 코드베이스에서 추출 (2026-06-12 검증)*
