# 디스플레이 (Display)

## 블록 타입
| 블록 | 타입명 | 필드/입력 |
|------|--------|----------|
| 텍스트 | `output_display_text` | INDEX, VALUE(input:text) |
| 이미지 | `output_display_drawing` | INDEX, FUNC |
| 변수 표시 | `output_display_variable` | INDEX, FUNC(줄), VALUE(input) |
| 지우기 | `output_display_clear` | INDEX |
| 오프셋 | `output_display_offset` | INDEX, OFFSET_X(input), OFFSET_Y(input) |
| 위치 | `output_display_position` | INDEX, FUNC, AXIS_X_VALUE, FUNC2, AXIS_Y_VALUE |

## 줄 위치 FUNC 값 (`output_display_variable`)
| 줄 | FUNC 값 |
|----|---------|
| 첫째 줄 | `0` |
| 둘째 줄 | `20` |
| 셋째 줄 | `40` |

## 이미지 FUNC 값 (`output_display_drawing`)
**표정**: `smileb`, `love`, `smiling`, `angry`, `tired`, `surprise`, `cry`, `dizzy`, `bilnd`, `sleeping`, `emv`, `proud`
**동물**: `dog`, `cat`, `rabbit`, `chick`, `lion`, `turtle`, `sparrow`, `penguin`, `butfly`, `fish`, `dolphin`, `hedgeh`
**자연**: `flower`, `tree`, `sun`, `star`, `moon`, `earth`, `cloud`, `rain`, `snow`, `wind`, `thunder`, `fire`
**음식**: `apple`, `banana`, `strawb`, `peach`, `waterm`, `chicken`, `pizza`, `hamburg`, `cake`, `nuddle`, `donut`, `candy`
**사람**: `baby`, `girl`, `boy`, `women`, `men`, `grandm`, `grandf`, `teacher`, `program`, `police`, `doctor`, `farmer`
**탈것**: `car`, `ship`, `airplane`, `train`, `bus`, `policec`, `ambul`, `rocket`, `hotair`, `helicop`, `sportsc`, `bicycle`
**판타지**: `devil`, `angel`, `dragon`, `santa`, `ludolf`, `ghost`, `witch`, `pumpkin`, `wand`, `hat`, `ball`, `potion`
**배경**: `school`, `park`, `hospital`, `build`, `apart`, `amuse`, `brick`, `cabin`, `straw`, `vacant`, `field`, `mountain`
**물건**: `game`, `microp`, `speaker`, `watch`, `tele`, `camera`, `tv`, `radio`, `book`, `micros`, `teles`, `waste`, `mask`, `flag`, `letter`, `soccer`, `basket`, `piano`, `gittar`, `drum`, `siren`, `giftbox`, `crown`, `dice`, `medal`, `key`, `jewerly`, `coin`
**인터페이스**: `comm`, `battery`, `download`, `check`, `x`, `play`, `stop2`, `pause`, `power`, `bulb`, `straigh`, `lefts`, `rights`, `stop`, `prize`, `losing`, `retry`, `thumbs`, `scissors`, `rock`, `paper`, `up`, `down`, `righta`, `lefta`, `heart`, `note`, `1bird`, `2birds`, `3birds`

## 사용 규칙
- **덮어쓰기**: 같은 줄에 다시 쓰면 이전 내용 자동 교체 — `display_clear` 불필요
- **`output_display_clear`는 화면 전체 초기화 시에만** — 루프 안에서 쓰면 깜빡임
- 변수 + 텍스트를 함께 보여주려면 줄을 나눠서 출력

## XML 예시

### 이미지 표시
```xml
<block type="output_display_drawing">
  <field name="INDEX">0</field>
  <field name="FUNC">sleeping</field>
</block>
```

### 변수 표시 (첫째 줄)
```xml
<block type="output_display_variable">
  <field name="INDEX">0</field>
  <field name="FUNC">0</field>
  <value name="VALUE">
    <block type="variables_get">
      <field name="VAR" id="var_Record">Record</field>
    </block>
  </value>
</block>
```

### 텍스트 표시
```xml
<block type="output_display_text">
  <field name="INDEX">0</field>
  <value name="VALUE">
    <shadow type="text">
      <field name="TEXT">Ready...</field>
    </shadow>
  </value>
</block>
```
