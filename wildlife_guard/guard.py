"""논·밭 감시 카메라 실행기.

예)
  python -m wildlife_guard.guard --source 0                         # USB 웹캠
  python -m wildlife_guard.guard --source rtsp://user:pw@IP/stream  # CCTV(IP 카메라)
  python -m wildlife_guard.guard --source field.mp4 --show          # 녹화 영상 테스트
  python -m wildlife_guard.guard --list                             # 인식 대상 30종 보기
"""

from __future__ import annotations

import argparse
import time

from .animals import ANIMALS
from .events import DetectionGate

RISK_COLOR = {3: (0, 0, 255), 2: (0, 140, 255), 1: (0, 220, 255)}  # BGR


def print_animals() -> None:
    for i, a in enumerate(ANIMALS, 1):
        tag = " [보호종]" if a.protected else ""
        print(f"{i:2d}. {a.ko}{tag} (위험도 {a.risk}) — {a.damage} / {a.season}")


def draw(frame, detections) -> None:
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont  # 한글 라벨은 OpenCV putText 로 안 그려짐

    for d in detections:
        x1, y1, x2, y2 = d.box
        cv2.rectangle(frame, (x1, y1), (x2, y2), RISK_COLOR[d.animal.risk], 2)
    if not detections:
        return
    img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    pen = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("NanumGothic.ttf", 20)
    except OSError:
        font = ImageFont.load_default()
    for d in detections:
        pen.text((d.box[0], max(0, d.box[1] - 24)), f"{d.animal.ko} {d.conf:.0%}",
                 fill=RISK_COLOR[d.animal.risk][::-1], font=font)
    frame[:] = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)


def open_source(source: str):
    import cv2

    cap = cv2.VideoCapture(int(source) if source.isdigit() else source)
    if not cap.isOpened():
        raise SystemExit(f"카메라/영상을 열 수 없습니다: {source}")
    return cap


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="강원도 논·밭 유해 야생동물 카메라 인식")
    ap.add_argument("--source", default="0", help="웹캠 번호, RTSP 주소 또는 영상 파일")
    ap.add_argument("--camera-name", default="cam1", help="알림·기록에 표시할 카메라 이름")
    ap.add_argument("--backend", choices=["world", "custom"], default="world")
    ap.add_argument("--weights", help="모델 가중치(.pt). custom 백엔드는 필수")
    ap.add_argument("--conf", type=float, default=0.25, help="검출 최소 확신도")
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--device", help="cpu, 0(GPU) 등")
    ap.add_argument("--every", type=int, default=3, help="N 프레임마다 1번 추론(CPU 부하 절감)")
    ap.add_argument("--min-hits", type=int, default=3, help="최근 5회 추론 중 몇 번 보여야 확정")
    ap.add_argument("--cooldown", type=float, default=60, help="같은 동물 재알림 간격(초)")
    ap.add_argument("--out", default="wildlife_events", help="스냅샷·CSV 저장 폴더")
    ap.add_argument("--webhook", help="출현 시 JSON POST 할 웹훅 URL")
    ap.add_argument("--modi", action="store_true", help="MODI 스피커/LED 로 퇴치음·경광등")
    ap.add_argument("--show", action="store_true", help="화면에 영상 띄우기 (q 로 종료)")
    ap.add_argument("--list", action="store_true", help="인식 대상 동물 목록 출력 후 종료")
    args = ap.parse_args(argv)

    if args.list:
        print_animals()
        return

    import cv2

    from .alerts import EventLogger, ModiDeterrent, format_message, send_webhook
    from .detector import AnimalDetector

    detector = AnimalDetector(args.backend, args.weights, args.conf, args.imgsz, args.device)
    gate = DetectionGate(window=5, min_hits=args.min_hits, cooldown_s=args.cooldown)
    logger = EventLogger(args.out)
    modi = ModiDeterrent() if args.modi else None
    cap = open_source(args.source)
    print(f"감시 시작: {args.source} (backend={args.backend}) — Ctrl+C 로 종료")

    frame_no, detections = 0, []
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                if args.source.isdigit() or args.source.startswith("rtsp"):
                    print("프레임 수신 실패 — 5초 후 재연결")
                    cap.release()
                    time.sleep(5)
                    cap = open_source(args.source)
                    continue
                break  # 영상 파일 끝
            frame_no += 1
            if frame_no % args.every == 0:
                detections = detector.detect(frame)
                fired = gate.update({d.animal.key for d in detections}, time.time())
                for d in detections:
                    if d.animal.key not in fired:
                        continue
                    msg = format_message(d.animal, d.conf, args.camera_name)
                    snap = logger.log(frame, d.animal, d.conf, args.camera_name)
                    print(f"{time.strftime('%H:%M:%S')} {msg} → {snap.name}")
                    if args.webhook:
                        send_webhook(args.webhook, msg)
                    if modi:
                        modi.trigger(d.animal)
            if args.show:
                draw(frame, detections)
                cv2.imshow("wildlife_guard", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
    except KeyboardInterrupt:
        pass
    finally:
        cap.release()
        if args.show:
            cv2.destroyAllWindows()
        print("감시 종료")


if __name__ == "__main__":
    main()
