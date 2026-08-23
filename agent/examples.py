"""유저가 "그냥 만들어줘", "뭐든 만들어줘" 등 구체적 설명 없이 요청할 때 제공하는 예시 프로젝트"""

EXAMPLE_SUGGESTIONS = [
    {
        "name": "할 일 관리 앱",
        "description": "할 일을 추가하고, 완료 체크하고, 삭제할 수 있는 투두 앱",
        "features": ["할 일 추가", "완료 체크", "삭제", "localStorage 저장"],
    },
    {
        "name": "날씨 대시보드",
        "description": "도시를 검색하면 현재 날씨와 주간 예보를 보여주는 대시보드",
        "features": ["도시 검색", "현재 날씨 표시", "주간 예보", "최근 검색 기록"],
    },
    {
        "name": "메모장 앱",
        "description": "메모를 작성하고 카테고리별로 정리할 수 있는 노트 앱",
        "features": ["메모 작성/수정/삭제", "카테고리 분류", "검색", "localStorage 저장"],
    },
]

VAGUE_PATTERNS = [
    "그냥 만들어", "아무거나", "뭐든", "잘 모르겠", "뭘 만들지",
    "추천해", "예시", "샘플", "아이디어",
]


def is_vague_request(user_input: str) -> bool:
    lowered = user_input.lower().strip()
    return any(p in lowered for p in VAGUE_PATTERNS)


def get_suggestion_message() -> str:
    lines = [
        "어떤 걸 만들지 아직 정하지 못했군요! 아래 중에 하나 골라볼까요?",
        "",
    ]
    for i, ex in enumerate(EXAMPLE_SUGGESTIONS, 1):
        features = ", ".join(ex["features"])
        lines.append(f"**{i}. {ex['name']}**")
        lines.append(f"   {ex['description']}")
        lines.append(f"   주요 기능: {features}")
        lines.append("")

    lines.append('번호로 골라도 되고, "1번으로 할게" 같이 말해도 돼요.')
    lines.append("물론 다른 아이디어가 있으면 자유롭게 말해주세요!")
    return "\n".join(lines)


def get_example_by_index(index: int) -> dict | None:
    if 1 <= index <= len(EXAMPLE_SUGGESTIONS):
        return EXAMPLE_SUGGESTIONS[index - 1]
    return None
