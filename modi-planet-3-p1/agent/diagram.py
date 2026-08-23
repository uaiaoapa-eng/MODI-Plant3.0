from __future__ import annotations

import re
from typing import List, Optional
from agent.models import DiagramData, Component


def _sanitize_mermaid(code: str) -> str:
    """LLM이 자주 틀리는 mermaid 문법을 보정한다."""
    code = code.strip()
    # 코드펜스 제거
    if code.startswith("```"):
        code = re.sub(r'^```\w*\n?', '', code)
        code = re.sub(r'\n?```$', '', code)
        code = code.strip()

    lines = code.split("\n")
    cleaned = []
    for line in lines:
        # 노드 라벨 안 특수문자 이스케이프: A[로그인/회원가입] → A["로그인/회원가입"]
        # 괄호류 라벨([], (), {}, (()))에서 한글+특수문자 포함 시 따옴표로 감싸기
        line = re.sub(
            r'(\w+)\[([^\]"]+)\]',
            lambda m: f'{m.group(1)}["{m.group(2)}"]'
            if re.search(r'[/\\|<>&(){}\[\]#]', m.group(2))
            else f'{m.group(1)}[{m.group(2)}]',
            line,
        )
        line = re.sub(
            r'(\w+)\(([^)"]+)\)',
            lambda m: f'{m.group(1)}("{m.group(2)}")'
            if re.search(r'[/\\|<>&\[\]{}#]', m.group(2))
            else f'{m.group(1)}({m.group(2)})',
            line,
        )
        cleaned.append(line)

    return "\n".join(cleaned)


class DiagramManager:
    def __init__(self):
        self.data = DiagramData()

    def update(self, mermaid_code: str, components: Optional[List[dict]] = None) -> str:
        mermaid_code = _sanitize_mermaid(mermaid_code)
        self.data.mermaid_code = mermaid_code
        if components:
            self.data.components = [Component(**c) for c in components]
        return self.data.mermaid_code

    def get_mermaid(self) -> str:
        return self.data.mermaid_code or "(아직 다이어그램이 없습니다)"

    def get_summary(self) -> str:
        if not self.data.components:
            return "컴포넌트가 아직 정의되지 않았습니다."
        lines = []
        for c in self.data.components:
            children = f" → [{', '.join(c.children)}]" if c.children else ""
            lines.append(f"- {c.name}: {c.description}{children}")
        return "\n".join(lines)

    def reset(self):
        self.data = DiagramData()
