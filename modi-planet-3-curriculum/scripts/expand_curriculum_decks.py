"""Build the published, classroom-ready MODI Planet lesson decks.

The short curriculum files in ``curriculum/outlines`` preserve the reviewed
course map.  The structured examples in ``curriculum/enrichment`` add the
worked examples, guided build, troubleshooting, differentiation, and
assessment evidence needed for a complete lesson.  Running this script merges
both sources into the three JSON files served by the LMS.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
CURRICULUM = ROOT / "curriculum"
LEVELS = ("elementary", "middle", "high")
DECK_CONFIG = {
    "elementary": {
        "count": 17,
        "minutes": [2, 2, 3, 2, 3, 2, 2, 2, 1, 3, 3, 3, 4, 2, 2, 2, 2],
        "build_slots": 4,
    },
    "middle": {
        "count": 19,
        "minutes": [2, 2, 3, 2, 3, 2, 3, 2, 2, 2, 2, 3, 3, 3, 3, 2, 2, 2, 2],
        "build_slots": 5,
    },
    "high": {
        "count": 21,
        "minutes": [2, 2, 3, 2, 3, 2, 3, 2, 2, 2, 2, 2, 3, 3, 3, 4, 2, 2, 2, 2, 2],
        "build_slots": 5,
    },
}
CODING_TYPES = {"web": "react", "hw": "blockly", "webhw": "hybrid"}


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as source:
        data = json.load(source)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: JSON object required")
    return data


def text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def flatten(values: Iterable[Iterable[str]]) -> list[str]:
    return [item for group in values for item in group]


def first_note(slides: list[dict[str, Any]], types: set[str]) -> str:
    for slide in slides:
        if slide.get("type") in types and str(slide.get("teacherNote", "")).strip():
            return str(slide["teacherNote"]).strip()
    return ""


def slide_bodies(slides: list[dict[str, Any]], types: set[str]) -> list[dict[str, Any]]:
    return [
        {"title": str(slide.get("title", "")).strip(), "body": text_list(slide.get("body"))}
        for slide in slides
        if slide.get("type") in types and text_list(slide.get("body"))
    ]


def four_choices(choices: Any, correct: str) -> tuple[list[str], int]:
    values: list[str] = []
    for candidate in [*text_list(choices), correct]:
        if candidate and candidate not in values:
            values.append(candidate)
    if correct not in values[:4]:
        values = values[:3] + [correct]
    else:
        values = values[:4]
    for candidate in ["조건을 확인하지 않고 바로 실행한다", "결과가 달라도 그대로 둔다", "근거 없이 기능을 계속 추가한다"]:
        if len(values) == 4:
            break
        if candidate not in values:
            values.append(candidate)
    return values, values.index(correct)


def make_check(source_slides: list[dict[str, Any]], vocabulary: list[dict[str, Any]], lesson_title: str) -> dict[str, Any]:
    source_quiz = next((slide for slide in source_slides if slide.get("type") == "quiz"), None)
    if source_quiz:
        raw_choices = text_list(source_quiz.get("choices"))
        raw_answer = source_quiz.get("answer", 0)
        if isinstance(raw_answer, int) and 0 <= raw_answer < len(raw_choices):
            correct = raw_choices[raw_answer]
            choices, answer = four_choices(raw_choices, correct)
            return {
                "title": str(source_quiz.get("title") or "개념 확인"),
                "question": str(source_quiz.get("question") or f"{lesson_title}의 핵심을 고르세요."),
                "choices": choices,
                "answer": answer,
                "explanation": str(source_quiz.get("explanation") or f"'{correct}'이 이번 차시에서 확인할 핵심입니다."),
            }

    first = vocabulary[0]
    correct = str(first["meaning"])
    distractors = [str(item["meaning"]) for item in vocabulary[1:]]
    choices, answer = four_choices(distractors, correct)
    return {
        "title": "핵심 개념 확인",
        "question": f"'{first['term']}'을 가장 잘 설명한 것은 무엇인가요?",
        "choices": choices,
        "answer": answer,
        "explanation": f"{first['term']}은(는) {correct} 예: {first['example']}",
    }


def partition_steps(steps: list[dict[str, Any]], slots: int) -> list[list[dict[str, Any]]]:
    if len(steps) < slots:
        raise ValueError(f"guided build needs at least {slots} steps, got {len(steps)}")
    groups: list[list[dict[str, Any]]] = []
    base_size, extra = divmod(len(steps), slots)
    sizes = [base_size] * (slots - extra) + [base_size + 1] * extra
    start = 0
    for size in sizes:
        groups.append(steps[start : start + size])
        start += size
    return groups


def teacher_note(lesson: dict[str, Any], purpose: str, action: str) -> str:
    return f"{lesson['no']}차시 '{lesson['title']}' {purpose}: {action}"


def base_slide(phase: str, slide_type: str, title: str, body: list[str], note: str) -> dict[str, Any]:
    return {
        "phase": phase,
        "type": slide_type,
        "title": title,
        "body": body,
        "teacherNote": note,
    }


def build_slides(lesson: dict[str, Any], enrichment: dict[str, Any], slots: int) -> list[dict[str, Any]]:
    coding_type = CODING_TYPES[lesson["projectType"]]
    groups = partition_steps(enrichment["buildSteps"], slots)
    slides: list[dict[str, Any]] = []
    for index, group in enumerate(groups, start=1):
        instructions = flatten(text_list(step.get("instructions")) for step in group)
        checkpoints = [str(step.get("checkpoint", "")).strip() for step in group]
        checkpoints = [item for item in checkpoints if item]
        prompts = [str(step.get("prompt", "")).strip() for step in group]
        prompts = [item for item in prompts if item]
        if not prompts:
            prompts = [f"{lesson['title']}에서 {group[0]['title']} 단계를 구현하고, 확인 방법도 알려 줘."]
        title = str(group[0]["title"])
        if len(group) > 1:
            title += " + " + " · ".join(str(step["title"]) for step in group[1:])
        slide = base_slide(
            "전개",
            "build",
            title,
            instructions,
            teacher_note(
                lesson,
                f"제작 {index}/{slots}",
                "학생이 결과를 바로 확인한 뒤 다음 단계로 이동하게 하고, 막히면 예시 문장을 한 번만 사용하게 하세요.",
            ),
        )
        slide.update(
            {
                "stepNumber": index,
                "stepTotal": slots,
                "instructions": instructions,
                "checkpoint": " / ".join(checkpoints),
                "prompt": prompts[0],
                "prompts": prompts,
                "codingType": coding_type,
                "substeps": [str(step["title"]) for step in group],
            }
        )
        slides.append(slide)
    return slides


def concept_sources(source_slides: list[dict[str, Any]], minimum: int) -> list[dict[str, Any]]:
    # Original student activities happen after a product exists.  Treating them
    # as pre-build concepts creates impossible sequences (for example, peer
    # testing an app before it is made), so concepts come from explanation
    # slides only; activities are absorbed by the guided build/checkpoints.
    sources = slide_bodies(source_slides, {"talk"})
    if not sources:
        sources = [{"title": "핵심 개념", "body": ["문제를 관찰하고 필요한 기능을 작은 단계로 나눕니다."]}]
    while len(sources) < minimum:
        longest_index = max(range(len(sources)), key=lambda index: len(sources[index]["body"]))
        original = sources.pop(longest_index)
        split_at = max(1, len(original["body"]) // 2)
        first = {"title": original["title"], "body": original["body"][:split_at]}
        second_body = original["body"][split_at:] or original["body"][-1:]
        second = {"title": original["title"] + " — 적용하기", "body": second_body}
        sources[longest_index:longest_index] = [first, second]
    return sources


def expand_lesson(level: str, course: dict[str, Any], lesson: dict[str, Any], enrichment: dict[str, Any]) -> dict[str, Any]:
    source_slides = deepcopy(lesson["slides"])
    cfg = DECK_CONFIG[level]
    vocabulary = enrichment["vocabulary"]
    check = make_check(source_slides, vocabulary, lesson["title"])
    example = enrichment["workedExample"]
    concepts = concept_sources(source_slides, 2 if level in {"middle", "high"} else 1)
    wrap = slide_bodies(source_slides, {"wrapup"})
    takeaways = wrap[0]["body"] if wrap else enrichment["successCriteria"]
    title_note = first_note(source_slides, {"title"}) or teacher_note(
        lesson, "수업 시작", "완성 작품을 먼저 말해 주고 학생이 오늘 만들고 싶은 변형 한 가지를 떠올리게 하세요."
    )
    coding_type = CODING_TYPES[lesson["projectType"]]
    safety = (
        "개인정보·저작권을 확인하고 결과를 새로고침해도 유지되는지 점검합니다."
        if lesson["projectType"] == "web"
        else "전원을 끈 상태에서 모듈을 연결하고, 구동부 주변에 손과 물건이 없는지 확인합니다."
    )

    slides: list[dict[str, Any]] = []
    slides.append(
        {
            "phase": "도입",
            "type": "title",
            "title": lesson["title"],
            "subtitle": (
                f"{lesson['projectLabel']} · {lesson['no']}/9차시 · "
                f"학생 산출물: {text_list(enrichment['studentArtifacts'])[0]} 외 "
                f"{len(text_list(enrichment['studentArtifacts'])) - 1}개"
            ),
            "body": [],
            "teacherNote": title_note,
        }
    )
    goals = base_slide(
        "도입",
        "goals",
        "오늘의 목표와 성공 모습",
        text_list(lesson["objectives"]),
        teacher_note(lesson, "목표 공유", "목표를 읽는 데서 끝내지 말고 성공 기준 중 한 가지를 학생 말로 다시 설명하게 하세요."),
    )
    goals["objectives"] = text_list(lesson["objectives"])
    goals["successCriteria"] = text_list(enrichment["successCriteria"])
    slides.append(goals)
    slides.append(
        base_slide(
            "도입",
            "hook",
            str(enrichment["hook"]["title"]),
            text_list(enrichment["hook"]["body"]),
            teacher_note(lesson, "도입 질문", "정답을 바로 주지 말고 30초 생각-짝 대화-전체 공유 순서로 학생 경험을 끌어내세요."),
        )
    )
    vocab_slide = base_slide(
        "도입",
        "vocabulary",
        "먼저 익힐 핵심 낱말",
        [f"{item['term']}: {item['meaning']}" for item in vocabulary],
        teacher_note(lesson, "용어 확인", "뜻만 외우게 하지 말고 각 용어의 예시가 이번 작품 어디에 나타나는지 짚게 하세요."),
    )
    vocab_slide["terms"] = vocabulary
    slides.append(vocab_slide)

    concept_one = base_slide(
        "전개",
        "concept",
        concepts[0]["title"],
        concepts[0]["body"],
        first_note(source_slides, {"talk"}) or teacher_note(lesson, "개념 설명", "학생 생활 사례와 작품 기능을 한 문장으로 연결하고 이해 여부를 손 신호로 확인하세요."),
    )
    slides.append(concept_one)
    if level in {"middle", "high"}:
        slides.append(
            base_slide(
                "전개",
                "concept",
                concepts[1]["title"],
                concepts[1]["body"],
                teacher_note(lesson, "개념 적용", "두 선택지의 결과를 예측하게 한 뒤 실제 제작 단계에서 어느 쪽을 택했는지 근거를 남기게 하세요."),
            )
        )

    example_slide = base_slide(
        "전개",
        "example",
        str(example["title"]),
        text_list(example["scenario"] if isinstance(example.get("scenario"), list) else [example.get("scenario")]),
        teacher_note(lesson, "작동 예시", "입력-처리-출력을 순서대로 가리키고, 좋은 예와 부족한 예의 차이를 학생에게 근거로 말하게 하세요."),
    )
    example_slide.update(
        {
            "scenario": example["scenario"],
            "input": text_list(example["input"]),
            "process": text_list(example["process"]),
            "output": text_list(example["output"]),
        }
    )
    if level != "high":
        example_slide["compare"] = example["compare"]
    slides.append(example_slide)
    if level == "high":
        compare = example["compare"]
        compare_slide = base_slide(
            "전개",
            "example",
            "설계 선택 비교 — 무엇이 더 나은가",
            [
                "성능: 응답 시간·정확도·자원 사용이 요구 범위 안에 있는가?",
                "안전: 센서 오류나 통신 끊김에도 위험한 동작을 막는가?",
                "유지보수: 규칙과 책임이 분리되어 다음 수정의 영향 범위를 설명할 수 있는가?",
            ],
            teacher_note(lesson, "트레이드오프 토론", "성능·안전·유지보수 중 두 기준을 골라 선택의 근거를 1분 동안 기록하게 하세요."),
        )
        compare_slide["compare"] = compare
        compare_slide["scenario"] = example["scenario"]
        compare_slide["input"] = []
        compare_slide["process"] = []
        compare_slide["output"] = []
        compare_slide["decisionQuestion"] = f"'{compare['good']}'을 선택할 근거를 성능·안전·유지보수 중 두 기준으로 설명하세요."
        slides.append(compare_slide)

    check_slide = base_slide(
        "전개",
        "check",
        check["title"],
        [],
        teacher_note(lesson, "형성평가", "먼저 모든 학생이 선택하게 한 뒤 정답과 오답의 이유를 비교하고, 설명을 읽은 후 다시 답하게 하세요."),
    )
    check_slide.update({key: check[key] for key in ("question", "choices", "answer", "explanation")})
    slides.append(check_slide)

    setup = base_slide(
        "전개",
        "setup",
        "제작 준비와 안전 점검",
        [*text_list(lesson["materials"]), safety],
        teacher_note(lesson, "준비 점검", "2인 1조라면 조작자와 기록자 역할을 정하고, 준비물과 안전 항목을 모두 확인한 모둠부터 시작시키세요."),
    )
    setup["checklist"] = [*text_list(lesson["materials"]), safety]
    setup["codingType"] = coding_type
    slides.append(setup)

    build_steps = enrichment["buildSteps"]
    plan = base_slide(
        "전개",
        "plan",
        "완성까지의 제작 지도",
        [str(step["title"]) for step in build_steps],
        teacher_note(lesson, "제작 계획", "각자 먼저 할 단계에 표시하고, 단계가 끝날 때마다 결과 증거를 남길 위치를 정하게 하세요."),
    )
    plan["steps"] = [str(step["title"]) for step in build_steps]
    plan["studentArtifacts"] = text_list(enrichment["studentArtifacts"])
    slides.append(plan)
    slides.extend(build_slides(lesson, enrichment, cfg["build_slots"]))

    checkpoint = base_slide(
        "전개",
        "checkpoint",
        "작품 검증 체크포인트",
        text_list(enrichment["successCriteria"]),
        teacher_note(lesson, "중간 검증", "완료 여부만 묻지 말고 화면·센서값·실행 기록 중 하나를 근거로 제시하게 하세요."),
    )
    checkpoint["criteria"] = text_list(enrichment["successCriteria"])
    checkpoint["studentArtifacts"] = text_list(enrichment["studentArtifacts"])
    if level != "high":
        checkpoint["rubric"] = enrichment["rubric"]
    slides.append(checkpoint)

    troubleshoot = base_slide(
        "전개",
        "troubleshoot",
        "안 될 때는 이렇게 확인해요",
        [f"{item['symptom']} → {item['fix']}" for item in enrichment["troubleshooting"]],
        teacher_note(lesson, "오류 해결", "정답을 대신 고쳐 주지 말고 증상-원인-수정 순서로 한 항목씩 확인하게 하며 수정 전후 결과를 비교하게 하세요."),
    )
    troubleshoot["issues"] = enrichment["troubleshooting"]
    slides.append(troubleshoot)

    differentiation = base_slide(
        "정리" if level != "high" else "전개",
        "differentiate",
        "내 수준에 맞게 완성하기",
        [],
        teacher_note(lesson, "개별화", "지원 과제는 필수 기능 완성을 돕는 데 사용하고, 일찍 끝낸 학생만 도전 과제를 선택하게 하세요."),
    )
    differentiation["support"] = text_list(enrichment["differentiation"]["support"])
    differentiation["challenge"] = text_list(enrichment["differentiation"]["challenge"])
    slides.append(differentiation)

    if level == "high":
        rubric = base_slide(
            "정리",
            "rubric",
            "결과물과 근거로 평가하기",
            [str(row["criterion"]) for row in enrichment["rubric"]],
            teacher_note(lesson, "루브릭 평가", "학생이 먼저 자기 수준과 근거를 표시한 뒤 동료 피드백 한 가지를 반영해 최종 증거를 제출하게 하세요."),
        )
        rubric["rows"] = enrichment["rubric"]
        rubric["studentArtifacts"] = text_list(enrichment["studentArtifacts"])
        slides.append(rubric)

    exit_ticket = enrichment["exitTicket"]
    exit_choices = text_list(exit_ticket["choices"])
    raw_answer = exit_ticket["answer"]
    if isinstance(raw_answer, int) and not isinstance(raw_answer, bool) and 0 <= raw_answer < len(exit_choices):
        correct = exit_choices[raw_answer]
    elif isinstance(raw_answer, str) and raw_answer.strip() in exit_choices:
        correct = raw_answer.strip()
    else:
        raise ValueError(f"{level} lesson {lesson['no']}: invalid exit answer")
    exit_choices, exit_answer = four_choices(exit_choices, correct)
    exit_slide = base_slide(
        "정리",
        "exit",
        "마무리 확인과 다음 연결",
        takeaways,
        teacher_note(lesson, "마무리", "개별 응답을 받은 뒤 성공 기준 한 가지와 다음 차시에서 개선할 점 한 가지를 짝에게 말하게 하세요."),
    )
    exit_slide.update(
        {
            "question": str(exit_ticket["question"]),
            "choices": exit_choices,
            "answer": exit_answer,
            "explanation": str(exit_ticket["explanation"]),
            "takeaways": takeaways,
        }
    )
    slides.append(exit_slide)

    if len(slides) != cfg["count"]:
        raise AssertionError(f"{level} lesson {lesson['no']}: built {len(slides)} slides, expected {cfg['count']}")
    if len(cfg["minutes"]) != len(slides):
        raise AssertionError(f"{level}: minute schedule length mismatch")
    for slide, minutes in zip(slides, cfg["minutes"], strict=True):
        slide["minutes"] = minutes
    if sum(slide["minutes"] for slide in slides) != course["classMinutes"]:
        raise AssertionError(f"{level} lesson {lesson['no']}: class time mismatch")

    result = {key: deepcopy(value) for key, value in lesson.items() if key != "slides"}
    result.update(
        {
            "deckVersion": 3,
            "successCriteria": text_list(enrichment["successCriteria"]),
            "vocabulary": vocabulary,
            "rubric": enrichment["rubric"],
            "differentiation": enrichment["differentiation"],
            "studentArtifacts": text_list(enrichment["studentArtifacts"]),
            "slides": slides,
        }
    )
    return result


def build_course(level: str) -> dict[str, Any]:
    outline_path = CURRICULUM / "outlines" / f"{level}.json"
    enrichment_path = CURRICULUM / "enrichment" / f"{level}.json"
    course = load_json(outline_path)
    enrichment = load_json(enrichment_path)
    if enrichment.get("level") != level:
        raise ValueError(f"{enrichment_path}: expected level {level!r}")
    entries = enrichment.get("lessons")
    if not isinstance(entries, list) or len(entries) != 9:
        raise ValueError(f"{enrichment_path}: exactly 9 lesson entries required")
    entry_by_no = {entry.get("no"): entry for entry in entries if isinstance(entry, dict)}
    if set(entry_by_no) != set(range(1, 10)):
        raise ValueError(f"{enrichment_path}: lesson numbers 1 through 9 required")

    result = {key: deepcopy(value) for key, value in course.items() if key != "lessons"}
    result["deckVersion"] = 3
    result["deckProfile"] = {
        "purpose": "교실 실행형 수업덱",
        "slideCountPerLesson": DECK_CONFIG[level]["count"],
        "includes": ["작동 예시", "단계별 제작", "오류 해결", "수준별 과제", "형성평가", "루브릭"],
    }
    result["lessons"] = [
        expand_lesson(level, course, lesson, entry_by_no[lesson["no"]])
        for lesson in course["lessons"]
    ]
    return result


def main() -> None:
    courses = {level: build_course(level) for level in LEVELS}
    totals: list[str] = []
    for level in LEVELS:
        course = courses[level]
        target = CURRICULUM / f"{level}.json"
        target.write_text(json.dumps(course, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        totals.append(f"{level}: {sum(len(lesson['slides']) for lesson in course['lessons'])} slides")
    print("Published curriculum decks generated — " + ", ".join(totals))


if __name__ == "__main__":
    main()
