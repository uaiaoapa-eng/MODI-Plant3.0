"""카메라 프레임 → 동물 인식.

두 가지 백엔드:
- world  : YOLO-World 제로샷. 학습 없이 animals.py 의 영어 문구로 바로 인식(바로 시작용).
           세부 종 구분(고라니↔노루, 참새↔작은 새)은 약하다.
- custom : 현장 사진으로 학습한 YOLO 가중치(train.py). 클래스 순서 = animals.ANIMALS 순서.
"""

from __future__ import annotations

from dataclasses import dataclass

from .animals import ANIMALS, BY_KEY, IGNORE_PROMPTS, Animal


@dataclass
class Detection:
    animal: Animal
    conf: float
    box: tuple[int, int, int, int]  # x1, y1, x2, y2


class AnimalDetector:
    def __init__(self, backend: str = "world", weights: str | None = None,
                 conf: float = 0.25, imgsz: int = 640, device: str | None = None):
        self.conf = conf
        self.imgsz = imgsz
        self.device = device
        if backend == "world":
            from ultralytics import YOLOWorld

            self.model = YOLOWorld(weights or "yolov8s-worldv2.pt")
            # 문구 index → 동물 key (None = 무시할 배경 클래스)
            self._label_map: list[str | None] = []
            prompts: list[str] = []
            for a in ANIMALS:
                for p in a.prompts:
                    prompts.append(p)
                    self._label_map.append(a.key)
            for p in IGNORE_PROMPTS:
                prompts.append(p)
                self._label_map.append(None)
            self.model.set_classes(prompts)
        elif backend == "custom":
            if not weights:
                raise ValueError("custom 백엔드는 --weights 로 학습된 .pt 경로가 필요합니다")
            from ultralytics import YOLO

            self.model = YOLO(weights)
            names = self.model.names  # {idx: name}
            self._label_map = [names[i] if names[i] in BY_KEY else None
                               for i in range(len(names))]
        else:
            raise ValueError(f"알 수 없는 backend: {backend}")

    def detect(self, frame) -> list[Detection]:
        result = self.model.predict(frame, conf=self.conf, imgsz=self.imgsz,
                                    device=self.device, verbose=False)[0]
        best: dict[str, Detection] = {}
        for cls, conf, xyxy in zip(result.boxes.cls.tolist(), result.boxes.conf.tolist(),
                                   result.boxes.xyxy.tolist()):
            key = self._label_map[int(cls)]
            if key is None:
                continue
            det = Detection(BY_KEY[key], float(conf), tuple(int(v) for v in xyxy))
            # 같은 동물이 여러 문구로 중복 검출되면 가장 확신도 높은 것만 남긴다
            if key not in best or det.conf > best[key].conf:
                best[key] = det
        return sorted(best.values(), key=lambda d: d.conf, reverse=True)
