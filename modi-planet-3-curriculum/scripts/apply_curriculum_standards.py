"""Apply the reviewed 2022 curriculum standards mapping to lesson source JSON.

Run from the repository root. The mapping is kept separately so reviewers can
audit every code and official sentence without diffing full lesson documents.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CURRICULUM_DIR = ROOT / "curriculum"
MAPPING_PATH = CURRICULUM_DIR / "standards_official.json"

NOTES = {
    "elementary": (
        "2022 개정 교육과정 초등 실과(5~6학년) 디지털 영역을 기반으로 구성하고, "
        "차시별 성취기준은 교육부 고시 제2022-33호 별책 10 원문과 대조하여 매핑했습니다."
    ),
    "middle": (
        "2022 개정 교육과정 중학교 정보 영역을 기반으로 구성하고, 차시별 성취기준은 "
        "교육부 고시 제2022-33호 별책 10 원문과 대조하여 매핑했습니다."
    ),
    "high": (
        "2022 개정 교육과정 고등학교 정보 영역을 기반으로 구성하고, 차시별 성취기준은 "
        "교육부 고시 제2022-33호 별책 10 원문과 대조하여 매핑했습니다."
    ),
}


def main() -> None:
    mapping = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
    courses = mapping["courses"]
    source = mapping["source"]

    if set(courses) != set(NOTES):
        raise ValueError("standards mapping must contain elementary, middle, and high")

    for level, note in NOTES.items():
        path = CURRICULUM_DIR / f"{level}.json"
        course = json.loads(path.read_text(encoding="utf-8"))
        lesson_map = courses[level]
        if set(lesson_map) != {str(number) for number in range(1, 10)}:
            raise ValueError(f"{level}: standards mapping must cover lessons 1 through 9")

        for lesson in course["lessons"]:
            lesson["standards"] = deepcopy(lesson_map[str(lesson["no"])])
        course["curriculumNote"] = note
        course["standardsSource"] = deepcopy(source)
        path.write_text(
            json.dumps(course, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
