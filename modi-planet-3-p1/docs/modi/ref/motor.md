# 모터 (Motor A / Motor B)

## 블록 타입
| 블록 | 타입명 | 필드/입력 |
|------|--------|----------|
| 모터A 속도 | `output_motorA_speed` | INDEX, VALUE(input: -100~100) |
| 모터A 멈추기 | `output_motorA_stop` | INDEX |
| 모터A 각도 | `output_motorA_angle` | INDEX, VALUE(input) |
| 모터A 상대각도 | `output_motorA_relative_angle` | INDEX, FUNC, VALUE(input) |
| 모터A 각도+속도 | `output_motorA_angle_speed` | INDEX, VALUE_ANGLE(input), VALUE_SPEED(input) |
| 모터B 속도 | `output_motorB_speed` | INDEX, VALUE(input: -100~100) |
| 모터B 멈추기 | `output_motorB_stop` | INDEX |
| 모터B 각도 | `output_motorB_angle` | INDEX, VALUE(input) |
| 모터B 상대각도 | `output_motorB_relative_angle` | INDEX, FUNC, VALUE(input) |
| 모터B 각도+속도 | `output_motorB_angle_speed` | INDEX, VALUE_ANGLE(input), VALUE_SPEED(input) |

## 상대각도 FUNC 값
`clock-wise`, `counter-clock-wise`

## 사용 규칙
- **속도 범위**: -100~100% (음수=역방향, 양수=정방향)
  - 30% = 일반, 15% = 느리고 부드러움
- **듀얼 모터 차동 조향** (차체 양쪽 대칭 배치):
  - 전진: A=-30%, B=30% (반대 부호)
  - 후진: A=30%, B=-30% (반대 부호)
  - 좌회전(제자리): A=30%, B=30% (같은 부호)
  - 우회전(제자리): A=-30%, B=-30% (같은 부호)
- **기본 상태 패턴**: 무한반복 첫 줄에서 `motorA_stop` + `motorB_stop`
- **⚠ if로 모터 제어 → 끊김!** 반드시 `controls_whileUntil`로 감싸기
  - 무한반복 안에서 if로 모터를 켜면 매 루프마다 잠깐 실행 → 끊김
  - whileUntil은 조건 유지 동안 내부를 연속 반복 → 부드러운 동작

## XML 예시

### 모터A 속도 설정
```xml
<block type="output_motorA_speed">
  <field name="INDEX">0</field>
  <value name="VALUE">
    <shadow type="math_number_min-100_max100">
      <field name="NUM">-30</field>
    </shadow>
  </value>
</block>
```

### 모터A 멈추기
```xml
<block type="output_motorA_stop">
  <field name="INDEX">0</field>
</block>
```

## 관련 레시피

### 조이스틱 자동차 (조이스틱+듀얼모터)
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
- 속도값 30%→15%로 낮추면 완만한 주행
