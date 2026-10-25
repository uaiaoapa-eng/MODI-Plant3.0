"""현장 사진으로 30종 전용 모델 학습 (정확도 향상용).

데이터 폴더 구조 (YOLO 형식):
  dataset/images/{train,val}/*.jpg
  dataset/labels/{train,val}/*.txt   # 한 줄: <클래스번호> <cx> <cy> <w> <h> (0~1 정규화)
클래스 번호 = animals.ANIMALS 순서 (python -m wildlife_guard.train --print-classes 로 확인)

예)
  python -m wildlife_guard.train --data-dir dataset --epochs 100
  → runs/detect/train/weights/best.pt 를 guard.py --backend custom --weights 로 사용
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .animals import ANIMALS


def write_data_yaml(data_dir: Path) -> Path:
    lines = [f"path: {data_dir.resolve()}", "train: images/train", "val: images/val", "names:"]
    lines += [f"  {i}: {a.key}" for i, a in enumerate(ANIMALS)]
    out = data_dir / "data.yaml"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="30종 커스텀 YOLO 학습")
    ap.add_argument("--data-dir", default="dataset")
    ap.add_argument("--base", default="yolov8s.pt", help="시작 가중치")
    ap.add_argument("--epochs", type=int, default=100)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--print-classes", action="store_true")
    args = ap.parse_args(argv)

    if args.print_classes:
        for i, a in enumerate(ANIMALS):
            print(f"{i:2d} {a.key:18s} {a.ko}")
        return

    data_yaml = write_data_yaml(Path(args.data_dir))
    from ultralytics import YOLO

    YOLO(args.base).train(data=str(data_yaml), epochs=args.epochs, imgsz=args.imgsz)


if __name__ == "__main__":
    main()
