"""출현 알림: 콘솔 · 스냅샷/CSV 기록 · 웹훅 · MODI 스피커/LED(선택)."""

from __future__ import annotations

import csv
import json
import time
import urllib.request
from pathlib import Path

from .animals import Animal


def format_message(animal: Animal, conf: float, camera: str) -> str:
    level = {3: "🚨 긴급", 2: "⚠️ 경고", 1: "ℹ️ 알림"}[animal.risk]
    msg = f"{level} [{camera}] {animal.ko} 출현 (확신도 {conf:.0%}) — {animal.damage}"
    if animal.protected:
        msg += " ※ 보호종: 포획·위해 금지, 소리/빛 등 비살상 퇴치만"
    if animal.key == "black_bear":
        msg += " ※ 접근 금지, 관할 국립공원공단·지자체에 신고"
    return msg


class EventLogger:
    """출현 순간 사진 저장 + CSV 누적 기록 (나중에 학습 데이터로도 재활용)."""

    def __init__(self, out_dir: str | Path):
        self.out_dir = Path(out_dir)
        (self.out_dir / "snapshots").mkdir(parents=True, exist_ok=True)
        self.csv_path = self.out_dir / "events.csv"
        if not self.csv_path.exists():
            with self.csv_path.open("w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(["time", "camera", "key", "ko", "risk", "conf", "snapshot"])

    def log(self, frame, animal: Animal, conf: float, camera: str) -> Path:
        import cv2

        ts = time.strftime("%Y%m%d_%H%M%S")
        snap = self.out_dir / "snapshots" / f"{ts}_{camera}_{animal.key}.jpg"
        cv2.imwrite(str(snap), frame)
        with self.csv_path.open("a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([ts, camera, animal.key, animal.ko, animal.risk,
                                    f"{conf:.3f}", snap.name])
        return snap


def send_webhook(url: str, text: str, timeout: float = 5.0) -> None:
    """Slack/Discord/사내 서버 등 JSON 웹훅으로 전송. 실패해도 감시는 계속한다."""
    body = json.dumps({"text": text, "content": text}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=timeout).close()
    except OSError as e:
        print(f"[webhook] 전송 실패: {e}")


class ModiDeterrent:
    """MODI 스피커/LED 로 비살상 퇴치 (pymodi 설치 + MODI 네트워크 모듈 연결 시)."""

    def __init__(self):
        import modi

        self.bundle = modi.MODI()
        self.speaker = self.bundle.speakers[0] if self.bundle.speakers else None
        self.led = self.bundle.leds[0] if self.bundle.leds else None

    def trigger(self, animal: Animal, seconds: float = 3.0) -> None:
        color = {3: (255, 0, 0), 2: (255, 120, 0), 1: (255, 255, 0)}[animal.risk]
        try:
            if self.led:
                self.led.rgb = color
            if self.speaker:
                # 새는 고음, 포유류는 저음 경고음
                self.speaker.tune = (2000 if animal.group == "bird" else 800), 100
            time.sleep(seconds)
        finally:
            if self.speaker:
                self.speaker.tune = 0, 0
            if self.led:
                self.led.rgb = (0, 0, 0)
