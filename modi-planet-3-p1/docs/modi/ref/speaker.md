# 스피커 (Speaker)

## 블록 타입
| 블록 | 타입명 | 필드/입력 |
|------|--------|----------|
| 음계 재생 | `output_speaker_note` | INDEX, FUNC(음), VALUE(input:볼륨) |
| 멜로디 재생 | `output_speaker_melody` | INDEX, FUNC(파일명), VALUE(input:볼륨) |
| 주파수 재생 | `output_speaker_frequency` | INDEX, FREQUENCY(input), VALUE(input:볼륨) |
| 끄기 | `output_speaker_clear` | INDEX |

## 음계 FUNC 값 (`output_speaker_note`)
| 음 | 옥타브5 (낮은) | 옥타브6 (중간) | 옥타브7 (높은) |
|----|---------------|---------------|---------------|
| 도 | `F_DO_5` | `F_DO_6` | `F_DO_7` |
| 레 | `F_RE_5` | `F_RE_6` | `F_RE_7` |
| 미 | `F_MI_5` | `F_MI_6` | `F_MI_7` |
| 파 | `F_PA_5` | `F_PA_6` | `F_PA_7` |
| 솔 | `F_SOL_5` | `F_SOL_6` | `F_SOL_7` |
| 라 | `F_RA_5` | `F_RA_6` | `F_RA_7` |
| 시 | `F_SI_5` | `F_SI_6` | `F_SI_7` |

> "솔" = `F_SOL_5`, "높은 도" = `F_DO_6`

## 멜로디 FUNC 값 (`output_speaker_melody`)
**클래식/동요**: `Delibes.mid`, `London.mid`, `OldMac.mid`, `Mozart21.mid`, `Vivaldi.mid`, `Bizet.mid`, `Sousa.mid`, `twinkle.mid`, `Birthday.mid`, `Jingle.mid`, `Merry.mid`, `Mary.mid`, `Spider.mid`, `Farmer.mid`, `yankee.mid`
**효과음**: `Alarm.wav`, `Siren.wav`, `Camera.wav`, `Bomb.wav`, `Car.wav`, `Start.wav`, `Complete.wav`, `Win.wav`, `Success.wav`, `Robot.wav`, `Exciting.wav`, `Bouncing.wav`
**감정/경고**: `Emotion1.mid`, `Emotion2.mid`, `Emotion3.mid`, `Warning1.mid`, `Warning2.mid`

## 사용 규칙
- **필수 패턴**: `note/melody` → `control_wait(재생시간)` → `output_speaker_clear`
  - 끄지 않으면 계속 울림
- **음 사이 간격**: `speaker_clear` → `control_wait(0.01)` → 다음 음
  - 0.01초 없으면 같은 음이 이어져서 구분 안 됨
- **음표 길이**: wait 시간으로 조절 (0.3초=1박, 0.6초=2박)
- **동적 볼륨**: VALUE에 `input_dial_value(getTurn)` 연결 → 다이얼로 실시간 조절
- **볼륨 0%로 끄기**: `output_speaker_note(VALUE:0)` = `output_speaker_clear`와 동일 효과

## XML 예시

### 음계 재생 (고정 볼륨)
```xml
<block type="output_speaker_note">
  <field name="INDEX">0</field>
  <field name="FUNC">F_SOL_5</field>
  <value name="VALUE">
    <shadow type="math_number_min0_max100">
      <field name="NUM">100</field>
    </shadow>
  </value>
</block>
```

### 음계 재생 (다이얼 동적 볼륨)
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

### 멜로디 재생
```xml
<block type="output_speaker_melody">
  <field name="INDEX">0</field>
  <field name="FUNC">Alarm.wav</field>
  <value name="VALUE">
    <shadow type="math_number_min0_max100">
      <field name="NUM">100</field>
    </shadow>
  </value>
</block>
```

## 관련 레시피

### 음계 멜로디 패턴 (음→대기→끄기→간격→다음음)
```
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

### 뮤직박스 (버튼+스피커+다이얼)
```
무한반복:
  output_speaker_clear
  만약 input_button_status(FUNC:getClick()):
    controls_repeat_ext(2):
      도→미→솔 (각 0.3초, 볼륨=다이얼값)
    controls_repeat_ext(3):
      라 (0.3초, 볼륨=다이얼값)
    솔 (0.6초, 볼륨=다이얼값)
```

### 경고음 패턴 (N번 비프)
```
controls_repeat_ext(3):
  output_speaker_note(FUNC:F_PA_5, VALUE:100)
  control_wait(0.2)
  output_speaker_note(FUNC:F_PA_5, VALUE:0)    ← 볼륨 0으로 끄기
  control_wait(0.8)
```
