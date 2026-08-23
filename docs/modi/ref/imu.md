# IMU (관성 센서)

## 블록 타입
| 블록 | 타입명 | 반환 | 필드 |
|------|--------|------|------|
| 각도 비교 | `input_imu_angle` | Boolean | INDEX, FUNC, OP, VALUE |
| 가속도 비교 | `input_imu_acceleration` | Boolean | INDEX, FUNC, OP, VALUE |
| 각속도 비교 | `input_imu_velocity` | Boolean | INDEX, FUNC, OP, VALUE |
| 흔들림 비교 | `input_imu_shaking` | Boolean | INDEX, OP, VALUE |
| IMU 값 | `input_imu_value` | Number | INDEX, FUNC |

## OP 값
`>`, `<`, `>=`, `<=`, `==`, `!=`

## FUNC 값
- **각도**: `getRoll`, `getPitch`, `getYaw` (범위: -180~180)
- **가속도**: `getAccelerationX`, `getAccelerationY`, `getAccelerationZ`
- **각속도**: `getAngularVelocityX`, `getAngularVelocityY`, `getAngularVelocityZ`
- **흔들림**: FUNC 없음 (0~100%)

## 사용 규칙
- **흔들림 임계값**: 보통 **20%** (지진경보기 기준)
- **순간 오감지 방지**: 중첩 if + wait로 N회 확인 후 확정 (debounce)

## XML 예시
```xml
<block type="input_imu_shaking">
  <field name="INDEX">0</field>
  <field name="OP">></field>
  <value name="VALUE">
    <shadow type="math_number_min0_max100">
      <field name="NUM">20</field>
    </shadow>
  </value>
</block>
```

## 관련 레시피

### 지진 경보기 (IMU+LED+스피커) — debounce 패턴
```
무한반복:
  output_led_clear / output_speaker_clear
  만약 input_imu_shaking(OP:>, VALUE:20):
    control_wait(1.5)
    만약 input_imu_shaking(OP:>, VALUE:20):     ← 2차 확인
      control_wait(1.5)
      controls_whileUntil(input_imu_shaking(OP:>, VALUE:20)):
        controls_repeat_ext(8):
          output_led_color(빨강) + output_speaker_note(F_RA_5)
          control_wait(0.2)
          output_led_color(노랑) + output_speaker_note(F_MI_5)
          control_wait(0.2)
```
