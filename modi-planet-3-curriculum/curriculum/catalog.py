"""Validated, runtime-neutral access to the published curriculum catalog."""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path
from typing import Any


_CURRICULUM_DIR = Path(__file__).parent
_STANDARDS_PATH = _CURRICULUM_DIR / "standards_official.json"
_GRADE_BANDS = ("elementary", "middle", "high")
_COURSE_PATHS = {
    grade_band: _CURRICULUM_DIR / f"{grade_band}.json"
    for grade_band in _GRADE_BANDS
}
_PROJECT_TYPES = {"web", "hw", "webhw"}
_SLIDE_TYPES = {
    "title",
    "goals",
    "hook",
    "vocabulary",
    "concept",
    "example",
    "check",
    "setup",
    "plan",
    "build",
    "checkpoint",
    "troubleshoot",
    "differentiate",
    "rubric",
    "exit",
    # Kept for compatibility with reviewed outline sources.
    "talk",
    "activity",
    "ai",
    "quiz",
    "wrapup",
}
_PHASES = {"도입", "전개", "정리"}
_CODING_TYPES = {"react", "blockly", "hybrid"}
_TARGET_SLIDES = {"elementary": 17, "middle": 19, "high": 21}


def _fail(path: Path, location: str, message: str) -> None:
    raise ValueError(f"{path.name}:{location}: {message}")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_text(path: Path, location: str, value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        _fail(path, location, "non-empty string required")


def _require_text_list(path: Path, location: str, value: Any) -> None:
    if not isinstance(value, list) or not value:
        _fail(path, location, "non-empty list required")
    for index, item in enumerate(value):
        _require_text(path, f"{location}[{index}]", item)


def _require_object_list(path: Path, location: str, value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        _fail(path, location, "non-empty list required")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            _fail(path, f"{location}[{index}]", "object required")
    return value


def _validate_rubric(path: Path, location: str, value: Any) -> None:
    rows = _require_object_list(path, location, value)
    for index, row in enumerate(rows):
        for field in ("criterion", "basic", "proficient", "advanced"):
            _require_text(path, f"{location}[{index}].{field}", row.get(field))


def _validate_quiz_like(path: Path, location: str, slide: dict[str, Any]) -> None:
    _require_text(path, f"{location}.question", slide.get("question"))
    _require_text_list(path, f"{location}.choices", slide.get("choices"))
    if len(slide["choices"]) != 4:
        _fail(path, f"{location}.choices", "exactly four choices required")
    answer = slide.get("answer")
    if not _is_int(answer) or not 0 <= answer < len(slide["choices"]):
        _fail(path, f"{location}.answer", "valid zero-based choice index required")
    _require_text(path, f"{location}.explanation", slide.get("explanation"))


def _validate_slide(path: Path, lesson_no: int, slide_index: int, slide: Any) -> int:
    location = f"lessons[{lesson_no}].slides[{slide_index}]"
    if not isinstance(slide, dict):
        _fail(path, location, "object required")

    if slide.get("phase") not in _PHASES:
        _fail(path, f"{location}.phase", f"one of {sorted(_PHASES)} required")
    if slide.get("type") not in _SLIDE_TYPES:
        _fail(path, f"{location}.type", f"one of {sorted(_SLIDE_TYPES)} required")
    _require_text(path, f"{location}.title", slide.get("title"))
    _require_text(path, f"{location}.teacherNote", slide.get("teacherNote"))

    minutes = slide.get("minutes")
    if not _is_int(minutes) or minutes <= 0:
        _fail(path, f"{location}.minutes", "positive integer required")

    if slide["type"] in {"quiz", "check", "exit"}:
        _validate_quiz_like(path, location, slide)

    if slide["type"] in {"ai", "setup", "build"} and slide.get("codingType") not in _CODING_TYPES:
        _fail(path, f"{location}.codingType", f"one of {sorted(_CODING_TYPES)} required")

    if slide["type"] == "goals":
        _require_text_list(path, f"{location}.objectives", slide.get("objectives"))
        _require_text_list(path, f"{location}.successCriteria", slide.get("successCriteria"))
    elif slide["type"] == "vocabulary":
        terms = _require_object_list(path, f"{location}.terms", slide.get("terms"))
        for index, term in enumerate(terms):
            for field in ("term", "meaning", "example"):
                _require_text(path, f"{location}.terms[{index}].{field}", term.get(field))
    elif slide["type"] == "example":
        compare = slide.get("compare")
        if compare is not None:
            if not isinstance(compare, dict):
                _fail(path, f"{location}.compare", "object required")
            for field in ("good", "bad"):
                _require_text(path, f"{location}.compare.{field}", compare.get(field))
        elif not any(isinstance(slide.get(field), list) and slide[field] for field in ("input", "process", "output")):
            _fail(path, location, "example flow or comparison required")
    elif slide["type"] == "setup":
        _require_text_list(path, f"{location}.checklist", slide.get("checklist"))
    elif slide["type"] == "plan":
        _require_text_list(path, f"{location}.steps", slide.get("steps"))
        _require_text_list(path, f"{location}.studentArtifacts", slide.get("studentArtifacts"))
    elif slide["type"] == "build":
        _require_text_list(path, f"{location}.instructions", slide.get("instructions"))
        _require_text(path, f"{location}.checkpoint", slide.get("checkpoint"))
        _require_text_list(path, f"{location}.prompts", slide.get("prompts"))
        step_number = slide.get("stepNumber")
        step_total = slide.get("stepTotal")
        if not _is_int(step_number) or not _is_int(step_total) or not 1 <= step_number <= step_total:
            _fail(path, f"{location}.stepNumber", "valid guided-build step required")
    elif slide["type"] == "checkpoint":
        _require_text_list(path, f"{location}.criteria", slide.get("criteria"))
        if "rubric" in slide:
            _validate_rubric(path, f"{location}.rubric", slide.get("rubric"))
        _require_text_list(path, f"{location}.studentArtifacts", slide.get("studentArtifacts"))
    elif slide["type"] == "troubleshoot":
        issues = _require_object_list(path, f"{location}.issues", slide.get("issues"))
        for index, issue in enumerate(issues):
            for field in ("symptom", "cause", "fix"):
                _require_text(path, f"{location}.issues[{index}].{field}", issue.get(field))
    elif slide["type"] == "differentiate":
        _require_text_list(path, f"{location}.support", slide.get("support"))
        _require_text_list(path, f"{location}.challenge", slide.get("challenge"))
    elif slide["type"] == "rubric":
        _validate_rubric(path, f"{location}.rows", slide.get("rows"))
        _require_text_list(path, f"{location}.studentArtifacts", slide.get("studentArtifacts"))
    elif slide["type"] == "exit":
        _require_text_list(path, f"{location}.takeaways", slide.get("takeaways"))

    return minutes


def _validate_lesson(
    path: Path,
    lesson: Any,
    expected_no: int,
    class_minutes: int,
    grade_band: str,
) -> None:
    location = f"lessons[{expected_no}]"
    if not isinstance(lesson, dict):
        _fail(path, location, "object required")
    if lesson.get("no") != expected_no:
        _fail(path, f"{location}.no", f"expected {expected_no}")

    for field in ("title", "projectLabel", "summary"):
        _require_text(path, f"{location}.{field}", lesson.get(field))
    if lesson.get("projectType") not in _PROJECT_TYPES:
        _fail(path, f"{location}.projectType", f"one of {sorted(_PROJECT_TYPES)} required")
    for field in ("objectives", "materials"):
        _require_text_list(path, f"{location}.{field}", lesson.get(field))
    if lesson.get("deckVersion") != 3:
        _fail(path, f"{location}.deckVersion", "expected 3")
    for field in ("successCriteria", "studentArtifacts"):
        _require_text_list(path, f"{location}.{field}", lesson.get(field))
    vocabulary = _require_object_list(path, f"{location}.vocabulary", lesson.get("vocabulary"))
    for index, term in enumerate(vocabulary):
        for field in ("term", "meaning", "example"):
            _require_text(path, f"{location}.vocabulary[{index}].{field}", term.get(field))
    _validate_rubric(path, f"{location}.rubric", lesson.get("rubric"))
    differentiation = lesson.get("differentiation")
    if not isinstance(differentiation, dict):
        _fail(path, f"{location}.differentiation", "object required")
    for field in ("support", "challenge"):
        _require_text_list(path, f"{location}.differentiation.{field}", differentiation.get(field))

    standards = lesson.get("standards")
    if not isinstance(standards, list) or not standards:
        _fail(path, f"{location}.standards", "non-empty list required")
    for index, standard in enumerate(standards):
        standard_location = f"{location}.standards[{index}]"
        if not isinstance(standard, dict):
            _fail(path, standard_location, "object required")
        _require_text(path, f"{standard_location}.code", standard.get("code"))
        _require_text(path, f"{standard_location}.text", standard.get("text"))

    slides = lesson.get("slides")
    if not isinstance(slides, list) or len(slides) != _TARGET_SLIDES[grade_band]:
        _fail(path, f"{location}.slides", f"exactly {_TARGET_SLIDES[grade_band]} slides required")
    total_minutes = sum(
        _validate_slide(path, expected_no, index, slide)
        for index, slide in enumerate(slides)
    )
    if total_minutes != class_minutes:
        _fail(
            path,
            f"{location}.slides",
            f"minutes total {total_minutes}, expected {class_minutes}",
        )


def _validate_course(path: Path, course: Any, grade_band: str) -> None:
    if not isinstance(course, dict):
        _fail(path, "$", "object required")
    if course.get("level") != grade_band:
        _fail(path, "level", f"expected {grade_band!r}")
    if course.get("deckVersion") != 3:
        _fail(path, "deckVersion", "expected 3")
    if not isinstance(course.get("deckProfile"), dict):
        _fail(path, "deckProfile", "object required")
    for field in (
        "label",
        "grade",
        "subject",
        "theme",
        "overview",
        "curriculumNote",
        "finalGoal",
    ):
        _require_text(path, field, course.get(field))

    class_minutes = course.get("classMinutes")
    if not _is_int(class_minutes) or class_minutes <= 0:
        _fail(path, "classMinutes", "positive integer required")

    lessons = course.get("lessons")
    if not isinstance(lessons, list) or len(lessons) != 9:
        _fail(path, "lessons", "exactly 9 lessons required")
    for expected_no, lesson in enumerate(lessons, start=1):
        _validate_lesson(path, lesson, expected_no, class_minutes, grade_band)


def _add_compatibility_aliases(course: dict[str, Any]) -> dict[str, Any]:
    """Keep the original LMS fields while preserving the v3 shell contract."""
    grade_band = course["level"]
    class_minutes = course["classMinutes"]
    course["id"] = grade_band
    course["lesson_count"] = len(course["lessons"])
    course["class_minutes"] = class_minutes
    course["curriculum_note"] = course["curriculumNote"]
    course["final_goal"] = course["finalGoal"]

    for lesson in course["lessons"]:
        lesson_no = lesson["no"]
        lesson["id"] = f"{grade_band}-{lesson_no:02d}"
        lesson["lesson_no"] = lesson_no
        lesson["status"] = "published"
        lesson["project_type"] = lesson["projectType"]
        lesson["duration_min"] = class_minutes
    return course


def _read_official_standards() -> dict[str, Any]:
    try:
        with _STANDARDS_PATH.open(encoding="utf-8") as standards_file:
            mapping = json.load(standards_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"unable to load curriculum file {_STANDARDS_PATH.name}: {exc}"
        ) from exc
    if not isinstance(mapping, dict) or not isinstance(mapping.get("source"), dict):
        raise ValueError(f"{_STANDARDS_PATH.name}: source object required")
    courses = mapping.get("courses")
    if not isinstance(courses, dict) or set(courses) != set(_GRADE_BANDS):
        raise ValueError(
            f"{_STANDARDS_PATH.name}: courses must contain {list(_GRADE_BANDS)}"
        )
    return mapping


def _validate_official_mapping(
    path: Path,
    course: dict[str, Any],
    grade_band: str,
    mapping: dict[str, Any],
) -> None:
    if course.get("standardsSource") != mapping["source"]:
        _fail(path, "standardsSource", "must match standards_official.json source")
    lesson_map = mapping["courses"][grade_band]
    expected_keys = {str(number) for number in range(1, 10)}
    if not isinstance(lesson_map, dict) or set(lesson_map) != expected_keys:
        raise ValueError(
            f"{_STANDARDS_PATH.name}:{grade_band}: lessons 1 through 9 required"
        )
    for lesson in course["lessons"]:
        expected = lesson_map[str(lesson["no"])]
        if lesson.get("standards") != expected:
            _fail(
                path,
                f"lessons[{lesson['no']}].standards",
                "must match the reviewed official mapping",
            )


@lru_cache(maxsize=1)
def _read_catalog() -> dict[str, Any]:
    official_standards = _read_official_standards()
    grade_bands: list[dict[str, Any]] = []
    for grade_band in _GRADE_BANDS:
        path = _COURSE_PATHS[grade_band]
        try:
            with path.open(encoding="utf-8") as course_file:
                course = json.load(course_file)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"unable to load curriculum file {path.name}: {exc}") from exc
        _validate_course(path, course, grade_band)
        _validate_official_mapping(path, course, grade_band, official_standards)
        grade_bands.append(_add_compatibility_aliases(course))
    return {"version": 3, "grade_bands": grade_bands}


def validate_catalog() -> None:
    """Load and validate every published course, raising ``ValueError`` on failure."""
    _read_catalog()


def list_grade_bands() -> list[dict[str, Any]]:
    """Return detached course data so callers cannot mutate process state."""
    return deepcopy(_read_catalog()["grade_bands"])


def get_grade_band(grade_band: str) -> dict[str, Any] | None:
    for band in _read_catalog()["grade_bands"]:
        if band["id"] == grade_band:
            return deepcopy(band)
    return None


def get_lesson(grade_band: str, lesson_no: int) -> dict[str, Any] | None:
    """Return one lesson plus its course context, or ``None`` for an unknown key."""
    if not _is_int(lesson_no):
        return None
    band = get_grade_band(grade_band)
    if band is None:
        return None
    for lesson in band["lessons"]:
        if lesson["lesson_no"] == lesson_no:
            lesson["grade_band"] = band["id"]
            lesson["grade_label"] = band["label"]
            lesson["grade"] = band["grade"]
            lesson["subject"] = band["subject"]
            lesson["classMinutes"] = band["classMinutes"]
            lesson["class_minutes"] = band["class_minutes"]
            return lesson
    return None
