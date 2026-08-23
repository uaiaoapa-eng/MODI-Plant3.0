# 버튼 (Button)

## 블록 타입
| 블록 | 타입명 | 반환 | 필드 |
|------|--------|------|------|
| 버튼 상태 | `input_button_status` | Boolean | INDEX, FUNC |
| 버튼 값 | `input_button_value` | Number | INDEX, FUNC |

## FUNC 값
| 동작 | FUNC 값 | 설명 |
|------|---------|------|
| 클릭 | `getClick()` | 눌렀다 뗀 순간 (가장 자주 사용) |
| 더블클릭 | `getDoubleClick()` | 빠르게 두 번 |
| 누름 | `getPressStatus()` | 누르고 있는 동안 유지 |
| 토글 | `getToggle()` | 누를 때마다 0↔100 전환 |

## 사용 규칙
- 클릭 이벤트는 **순간 감지** (Boolean pulse) — if-only 안에서 사용
- 토글은 on/off 스위치 용도 — 상태 유지됨

## XML 예시
```xml
<block type="input_button_status">
  <field name="INDEX">0</field>
  <field name="FUNC">getClick()</field>
</block>
```

## 관련 레시피

### 뮤직박스 (버튼+스피커+다이얼)
```
무한반복:
  output_speaker_clear
  만약 input_button_status(FUNC:getClick()):
    controls_repeat_ext(2):
      output_speaker_note(FUNC:F_DO_5, VALUE:input_dial_value(getTurn))
      control_wait(0.3) → output_speaker_clear → control_wait(0.01)
      ...반복...
```
- 버튼 클릭이 멜로디 전체의 트리거 (if-only)

### 타이머 알람 (다이얼+버튼+스피커)
```
만약 logic_operation(AND,
  input_dial_position(OP:>, VALUE:50),
  input_button_status(FUNC:getClick())):
  → 알람 시퀀스 실행
```
- AND 복합조건으로 사용
