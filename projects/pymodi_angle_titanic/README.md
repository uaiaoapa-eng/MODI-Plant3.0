# MODI 각도 모듈 기하학 시각화 + 타이타닉 OST

PyMODI로 **각도(다이얼) 모듈**의 현재 상태를 **단위원 기하학** 위에 실시간으로 보여주고,
동시에 **스피커 모듈**로 타이타닉 주제가(My Heart Will Go On)의 교육용 간단 편곡
모티프를 반복 재생하는 예제입니다.

## 화면 구성

- 단위원 + 좌표축 (0° / 90° / 180° / 270° 라벨)
- 현재 각도 θ의 **호(arc)** 와 **반지름 바늘**
- 원 위의 점 `(cos θ, sin θ)` 좌표와 **sin/cos 사영선**
- 도(°) · 라디안 · sin · cos · 사분면 수치 판넬
- 현재 재생 중인 음 표시

다이얼을 돌리면 바늘·호·사영이 즉시 따라 움직여서, 각도 ↔ 삼각비 관계를
눈으로 확인할 수 있습니다.

## 필요한 것

| 구분 | 내용 |
|---|---|
| 모듈 | 네트워크 + 다이얼(각도) + 스피커 |
| 라이브러리 | `pip install pymodi-plus` (MODI Plus) 또는 `pip install pymodi` (1세대) |
| 파이썬 | 3.8+ (tkinter 포함 배포판 — 표준 설치면 기본 포함) |

## 실행

```bash
python main.py              # 하드웨어 연결 상태에서 실행
python main.py --sim        # 하드웨어 없이 시뮬레이션 (각도 자동 회전)
python main.py --no-music   # 시각화만
python main.py --volume 80  # 스피커 볼륨 조절 (0~100)
```

다이얼 펌웨어가 회전값을 0~100이 아니라 도 단위(0~360)로 주는 경우:

```bash
python main.py --raw-max 360
```

## 코드 구조

- `raw_to_degree`, `angle_state` — 하드웨어/GUI 없이 임포트 가능한 순수 기하 계산
- `connect_bundle`, `read_dial_raw`, `set_speaker_tune` — pymodi / pymodi-plus 차이를 흡수하는 어댑터
- `MusicPlayer` — 백그라운드 스레드에서 멜로디를 반복 재생 (박자의 90%만 소리 내서 음 분리)
- `run_ui` — tkinter 캔버스 30fps 렌더링

멜로디는 원곡을 그대로 옮긴 악보가 아니라, 스피커 모듈의 단음(tune) 출력에 맞춰
분위기만 살린 짧은 교육용 모티프입니다.
