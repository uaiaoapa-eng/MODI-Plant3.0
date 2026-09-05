# -*- coding: utf-8 -*-
"""MODI 각도(다이얼) 모듈 기하학 시각화 + 타이타닉 OST 재생 예제.

다이얼 모듈이 보내는 회전값을 단위원 위의 각도로 해석해서
- 각도 호(arc), 반지름 바늘, sin/cos 사영(projection)을 실시간으로 그리고
- 도(°)/라디안/sin/cos/사분면 수치를 함께 보여준다.
동시에 스피커 모듈로 'My Heart Will Go On'(타이타닉 주제가) 모티프를
간단 편곡 버전으로 반복 재생한다.

실행:
    python main.py            # MODI 네트워크 모듈 + 다이얼 + 스피커 연결 상태에서
    python main.py --sim      # 하드웨어 없이 시뮬레이션(각도 자동 회전, 노트는 콘솔 출력)
    python main.py --no-music # 시각화만

pymodi(구형 MODI)와 pymodi-plus(MODI Plus) 어느 쪽이 설치돼 있어도 동작한다.
"""

import argparse
import math
import threading
import time

# ---------------------------------------------------------------------------
# 기하 계산 (하드웨어/GUI 없이도 임포트 가능하도록 순수 함수로 분리)
# ---------------------------------------------------------------------------

# 다이얼 모듈은 펌웨어에 따라 회전값을 0~100으로 보고한다.
# 이를 0~360°로 펼쳐서 단위원 각도로 쓴다. 펌웨어가 도 단위(0~360)를
# 바로 주는 경우 --raw-max 360 으로 실행하면 된다.
DEFAULT_RAW_MAX = 100


def raw_to_degree(raw, raw_max=DEFAULT_RAW_MAX):
    """다이얼 원시값(0~raw_max)을 0~360° 각도로 변환한다."""
    if raw_max <= 0:
        return 0.0
    return (float(raw) / raw_max * 360.0) % 360.0


def angle_state(degree):
    """각도 하나에서 시각화에 필요한 기하 상태를 모두 계산한다."""
    rad = math.radians(degree)
    cos_v = math.cos(rad)
    sin_v = math.sin(rad)
    if degree % 90 == 0:
        quadrant = "축 위"
    else:
        quadrant = str(int(degree // 90) % 4 + 1) + "사분면"
    return {
        "degree": degree,
        "radian": rad,
        "cos": cos_v,
        "sin": sin_v,
        "quadrant": quadrant,
    }


# ---------------------------------------------------------------------------
# 타이타닉 주제가 (My Heart Will Go On) — 스피커용 간단 편곡 모티프
# ---------------------------------------------------------------------------

NOTE_FREQ = {
    "G4": 392, "A4": 440, "B4": 494,
    "C5": 523, "D5": 587, "E5": 659, "F5": 698, "G5": 784,
    "REST": 0,
}

# (음이름, 박자) — 4/4 기준, 1.0 = 4분음표. 원곡 분위기를 느낄 수 있게
# 아주 단순하게 재구성한 교육용 모티프다.
TITANIC_MELODY = [
    # 구절 1
    ("C5", 1), ("C5", 1), ("C5", 1), ("C5", 1), ("B4", 1), ("C5", 2),
    ("C5", 1), ("B4", 1), ("A4", 2),
    ("A4", 1), ("B4", 1), ("C5", 2),
    # 구절 2
    ("C5", 1), ("D5", 1), ("C5", 1), ("B4", 1), ("A4", 1), ("G4", 1), ("A4", 3),
    ("REST", 1),
    # 후렴 모티프
    ("E5", 3), ("D5", 3),
    ("C5", 1), ("D5", 1), ("E5", 1), ("D5", 1), ("C5", 1), ("B4", 1),
    ("E5", 1), ("E5", 1), ("E5", 1), ("D5", 1), ("C5", 1), ("D5", 1), ("C5", 3),
    ("REST", 2),
]

TEMPO_BPM = 100  # 4분음표 기준 빠르기


# ---------------------------------------------------------------------------
# MODI 하드웨어 어댑터 (pymodi / pymodi-plus 공용)
# ---------------------------------------------------------------------------

def connect_bundle():
    """설치된 라이브러리에 맞춰 MODI 번들에 연결한다."""
    try:
        import modi_plus
        return modi_plus.MODIPlus()
    except ImportError:
        pass
    try:
        import modi
        return modi.MODI()
    except ImportError:
        raise SystemExit(
            "pymodi가 설치돼 있지 않습니다. 다음 중 하나를 설치하세요:\n"
            "  pip install pymodi-plus   # MODI Plus\n"
            "  pip install pymodi        # MODI (1세대)\n"
            "하드웨어 없이 체험하려면: python main.py --sim"
        )


def read_dial_raw(dial):
    """라이브러리마다 다른 다이얼 속성 이름(turn/degree)을 흡수한다."""
    for attr in ("turn", "degree", "angle"):
        if hasattr(dial, attr):
            return getattr(dial, attr)
    raise AttributeError("다이얼 모듈에서 회전값 속성을 찾지 못했습니다.")


def set_speaker_tune(speaker, frequency, volume):
    """pymodi-plus는 set_tune(), pymodi는 tune 프로퍼티를 쓴다."""
    if hasattr(speaker, "set_tune"):
        speaker.set_tune(frequency, volume)
    else:
        speaker.tune = (frequency, volume)


class SimDial:
    """--sim 모드용 가짜 다이얼: 시간에 따라 천천히 회전한다."""

    def __init__(self, deg_per_sec=30.0, raw_max=DEFAULT_RAW_MAX):
        self._t0 = time.monotonic()
        self._speed = deg_per_sec
        self._raw_max = raw_max

    @property
    def turn(self):
        deg = (time.monotonic() - self._t0) * self._speed % 360.0
        return deg / 360.0 * self._raw_max


class SimSpeaker:
    """--sim 모드용 가짜 스피커: 재생 중인 음을 콘솔에 찍는다."""

    def set_tune(self, frequency, volume):
        if volume > 0 and frequency > 0:
            print(f"[스피커] {frequency}Hz (vol {volume})")


# ---------------------------------------------------------------------------
# 음악 재생 스레드
# ---------------------------------------------------------------------------

class MusicPlayer(threading.Thread):
    def __init__(self, speaker, volume, now_playing):
        super().__init__(daemon=True)
        self._speaker = speaker
        self._volume = volume
        # 주의: threading.Thread가 내부적으로 _stop 메서드를 쓰므로 그 이름은 피한다.
        self._stop_event = threading.Event()
        self._now_playing = now_playing  # UI와 공유하는 dict

    def stop(self):
        self._stop_event.set()
        try:
            set_speaker_tune(self._speaker, 1000, 0)
        except Exception:
            pass

    def run(self):
        beat_sec = 60.0 / TEMPO_BPM
        while not self._stop_event.is_set():
            for name, beats in TITANIC_MELODY:
                if self._stop_event.is_set():
                    return
                freq = NOTE_FREQ[name]
                self._now_playing["note"] = "쉼표" if name == "REST" else name
                try:
                    set_speaker_tune(self._speaker, max(freq, 1),
                                     self._volume if freq else 0)
                except Exception:
                    pass
                # 음 사이가 뭉개지지 않게 박자의 90%만 소리내고 10%는 끊는다.
                duration = beats * beat_sec
                self._stop_event.wait(duration * 0.9)
                try:
                    set_speaker_tune(self._speaker, max(freq, 1), 0)
                except Exception:
                    pass
                self._stop_event.wait(duration * 0.1)
            self._now_playing["note"] = "―"
            self._stop_event.wait(1.0)


# ---------------------------------------------------------------------------
# tkinter 기하학 시각화
# ---------------------------------------------------------------------------

CANVAS = 560          # 캔버스 한 변(px)
RADIUS = 190          # 단위원 반지름(px)
BG = "#101828"
FG = "#e2e8f0"
ACCENT = "#38bdf8"    # 바늘/점
ARC = "#f59e0b"       # 각도 호
COS_COLOR = "#34d399"
SIN_COLOR = "#f472b6"


def run_ui(read_degree, now_playing, on_close):
    import tkinter as tk

    root = tk.Tk()
    root.title("MODI 각도 모듈 · 단위원 기하학")
    root.configure(bg=BG)

    canvas = tk.Canvas(root, width=CANVAS, height=CANVAS, bg=BG,
                       highlightthickness=0)
    canvas.pack(padx=12, pady=(12, 4))

    info = tk.Label(root, font=("Consolas", 13), bg=BG, fg=FG, justify="left")
    info.pack(padx=12, pady=(0, 4), anchor="w")

    song = tk.Label(root, font=("Consolas", 12), bg=BG, fg=ARC)
    song.pack(padx=12, pady=(0, 12), anchor="w")

    cx = cy = CANVAS // 2

    def to_canvas(x, y):
        """수학 좌표(단위원) → 캔버스 좌표(y축 반전)."""
        return cx + x * RADIUS, cy - y * RADIUS

    def draw():
        state = angle_state(read_degree())
        deg, cos_v, sin_v = state["degree"], state["cos"], state["sin"]
        px, py = to_canvas(cos_v, sin_v)

        canvas.delete("all")
        # 좌표축과 단위원
        canvas.create_line(cx - RADIUS - 20, cy, cx + RADIUS + 20, cy,
                           fill="#475569")
        canvas.create_line(cx, cy - RADIUS - 20, cx, cy + RADIUS + 20,
                           fill="#475569")
        canvas.create_oval(cx - RADIUS, cy - RADIUS, cx + RADIUS, cy + RADIUS,
                           outline=FG, width=2)
        for label, lx, ly in (("0°", cx + RADIUS + 8, cy),
                              ("90°", cx, cy - RADIUS - 10),
                              ("180°", cx - RADIUS - 16, cy),
                              ("270°", cx, cy + RADIUS + 10)):
            canvas.create_text(lx, ly, text=label, fill="#94a3b8",
                               font=("Consolas", 10))

        # 각도 호 (tkinter의 create_arc는 반시계 방향이 양수 → 수학 관례와 동일)
        r = 52
        canvas.create_arc(cx - r, cy - r, cx + r, cy + r,
                          start=0, extent=deg, style="arc",
                          outline=ARC, width=3)

        # cos/sin 사영 (점에서 축으로 내리는 수선)
        canvas.create_line(px, py, px, cy, fill=SIN_COLOR, dash=(4, 3), width=2)
        canvas.create_line(px, py, cx, py, fill=COS_COLOR, dash=(4, 3), width=2)
        canvas.create_line(cx, cy, px, cy, fill=COS_COLOR, width=3)
        canvas.create_line(px, cy, px, py, fill=SIN_COLOR, width=3)

        # 반지름 바늘과 원 위의 점
        canvas.create_line(cx, cy, px, py, fill=ACCENT, width=3)
        canvas.create_oval(px - 6, py - 6, px + 6, py + 6,
                           fill=ACCENT, outline="")
        canvas.create_text(px + 14, py - 14,
                           text=f"({cos_v:+.2f}, {sin_v:+.2f})",
                           fill=ACCENT, font=("Consolas", 11), anchor="w")

        info.config(text=(
            f"각도 θ  = {deg:7.2f}°   ({state['radian']:.3f} rad)\n"
            f"cos θ  = {cos_v:+.3f}    sin θ = {sin_v:+.3f}\n"
            f"위치    = {state['quadrant']}"
        ))
        song.config(text=f"♪ My Heart Will Go On — 현재 음: {now_playing['note']}")
        root.after(33, draw)  # 약 30fps

    def close():
        on_close()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    draw()
    root.mainloop()


# ---------------------------------------------------------------------------
# 엔트리 포인트
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sim", action="store_true",
                        help="하드웨어 없이 시뮬레이션으로 실행")
    parser.add_argument("--no-music", action="store_true",
                        help="음악 재생 없이 시각화만")
    parser.add_argument("--volume", type=int, default=60,
                        help="스피커 볼륨 0~100 (기본 60)")
    parser.add_argument("--raw-max", type=float, default=DEFAULT_RAW_MAX,
                        help="다이얼 원시값의 최댓값 (기본 100, 도 단위 펌웨어면 360)")
    args = parser.parse_args()

    if args.sim:
        dial, speaker = SimDial(raw_max=args.raw_max), SimSpeaker()
        print("시뮬레이션 모드: 각도가 자동으로 회전합니다.")
    else:
        print("MODI 모듈을 찾는 중입니다... (네트워크 + 다이얼 + 스피커)")
        bundle = connect_bundle()
        if not bundle.dials:
            raise SystemExit("다이얼(각도) 모듈이 연결돼 있지 않습니다.")
        dial = bundle.dials[0]
        speaker = bundle.speakers[0] if bundle.speakers else None
        if speaker is None and not args.no_music:
            print("스피커 모듈이 없어 음악 없이 시각화만 실행합니다.")
            args.no_music = True

    now_playing = {"note": "―"}
    player = None
    if not args.no_music:
        player = MusicPlayer(speaker, max(0, min(args.volume, 100)), now_playing)
        player.start()

    def read_degree():
        return raw_to_degree(read_dial_raw(dial), args.raw_max)

    def on_close():
        if player:
            player.stop()

    try:
        run_ui(read_degree, now_playing, on_close)
    except KeyboardInterrupt:
        on_close()


if __name__ == "__main__":
    main()
