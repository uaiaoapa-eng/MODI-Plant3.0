from __future__ import annotations

import os
import re
from typing import List
from agent.models import Phase, GeneratedFile, Feature, Page, DataModel, Task
from agent.modi_modules import (
    extract_raw_module_keys_from_xml,
    validate_modi_module_contract,
)

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_modi_core() -> str:
    """docs/modi/modi_core.md 내용을 로드한다."""
    path = os.path.join(_BASE_DIR, "docs", "modi", "modi_core.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def _load_modi_reference(modules: list) -> str:
    """모듈별 레퍼런스 파일(docs/modi/ref/*.md)을 로드한다."""
    ref_dir = os.path.join(_BASE_DIR, "docs", "modi", "ref")
    result = []
    for module in modules:
        path = os.path.join(ref_dir, f"{module}.md")
        try:
            with open(path, "r", encoding="utf-8") as f:
                result.append(f.read())
        except FileNotFoundError:
            result.append(f"# {module}\n모듈 레퍼런스를 찾을 수 없습니다.")
    return "\n\n---\n\n".join(result)

DESIGN_TOOLS = {"update_design_doc", "update_diagram", "web_search", "transition_phase"}  # 설계 에이전트가 "유저가 만들자는지" 판단해 transition_phase로 구현 전환
IMPLEMENT_TOOLS = {"generate_code", "edit_code", "plan_tasks", "complete_task", "update_diagram", "transition_phase"}
BLOCKLY_IMPLEMENT_TOOLS = {"generate_blockly_xml", "update_diagram", "transition_phase"}
HYBRID_IMPLEMENT_TOOLS = IMPLEMENT_TOOLS | {"set_modi_layout"}  # 하이브리드: react 코드 툴 + 모듈 물리 배치
VERIFY_TOOLS = {"update_diagram", "transition_phase"}
POST_IMPLEMENT_TOOLS = {"add_learning_note", "add_code_annotation"}
BLOCKLY_POST_IMPLEMENT_TOOLS = {"add_learning_note"}


def get_tools_for_phase(phase: Phase, coding_type: str = "react") -> list:
    if phase == Phase.DESIGN:
        allowed = DESIGN_TOOLS
    elif phase == Phase.IMPLEMENT:
        if coding_type == "blockly":
            allowed = BLOCKLY_IMPLEMENT_TOOLS
        elif coding_type == "hybrid":
            allowed = HYBRID_IMPLEMENT_TOOLS
        else:
            allowed = IMPLEMENT_TOOLS
    else:
        allowed = VERIFY_TOOLS
    return [t for t in TOOL_DEFINITIONS if t["name"] in allowed]


def get_post_implement_tools(coding_type: str = "react") -> list:
    allowed = BLOCKLY_POST_IMPLEMENT_TOOLS if coding_type == "blockly" else POST_IMPLEMENT_TOOLS
    return [t for t in TOOL_DEFINITIONS if t["name"] in allowed]

TOOL_DEFINITIONS = [
    {
        "name": "update_design_doc",
        "description": "설계 문서를 업데이트합니다. 학습자가 기능, 페이지, 데이터 구조 등을 설명할 때마다 호출하세요. 기존 데이터에 추가/병합됩니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_name": {
                    "type": "string",
                    "description": "프로젝트 이름"
                },
                "description": {
                    "type": "string",
                    "description": "프로젝트 한 줄 설명"
                },
                "users": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "사용자 유형 (예: 구매자, 판매자)"
                },
                "features": {
                    "type": "array",
                    "description": "기능 목록",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                            "priority": {"type": "string", "enum": ["mvp", "nice_to_have", "future"]}
                        },
                        "required": ["name"]
                    }
                },
                "pages": {
                    "type": "array",
                    "description": "페이지/화면 목록",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                            "components": {"type": "array", "items": {"type": "string"}}
                        },
                        "required": ["name"]
                    }
                },
                "data_models": {
                    "type": "array",
                    "description": "데이터 모델 목록",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "fields": {"type": "array", "items": {"type": "string"}},
                            "description": {"type": "string"}
                        },
                        "required": ["name"]
                    }
                },
                "user_flows": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "사용자 흐름 (예: '메인 → 상세 → 장바구니 → 결제')"
                },
                "strengths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "이 설계의 강점 (예: '사용자 흐름이 직관적')"
                },
                "weaknesses": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "이 설계의 약점이나 개선 가능한 점 (예: '에러 상태 처리 미정')"
                }
            }
        }
    },
    {
        "name": "plan_tasks",
        "description": "구현 Phase 진입 시, 설계 문서를 기반으로 태스크 리스트를 생성합니다. 구현할 파일과 순서를 계획합니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "실행할 태스크 목록 (순서대로)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer"},
                            "name": {"type": "string", "description": "태스크 이름"},
                            "description": {"type": "string", "description": "무엇을 만드는지"},
                            "files": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "생성할 파일 경로들"
                            }
                        },
                        "required": ["id", "name", "files"]
                    }
                }
            },
            "required": ["tasks"]
        }
    },
    {
        "name": "complete_task",
        "description": "현재 진행 중인 태스크를 완료로 표시합니다. 코드 생성이 끝난 후 호출하세요.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "integer",
                    "description": "완료할 태스크 ID"
                }
            },
            "required": ["task_id"]
        }
    },
    {
        "name": "update_diagram",
        "description": "대화에서 파악된 설계 구조를 Mermaid 다이어그램으로 업데이트합니다. 학습자가 구조에 대해 결정을 내릴 때마다 호출하세요.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mermaid_code": {
                    "type": "string",
                    "description": "전체 Mermaid 다이어그램 코드. 기존 다이어그램을 완전히 대체합니다."
                },
                "components": {
                    "type": "array",
                    "description": "다이어그램에 포함된 주요 컴포넌트 목록",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                            "children": {
                                "type": "array",
                                "items": {"type": "string"}
                            }
                        },
                        "required": ["name", "description"]
                    }
                }
            },
            "required": ["mermaid_code"]
        }
    },
    {
        "name": "generate_code",
        "description": "학습자의 자연어 지시를 기반으로 코드를 생성하고 파일로 저장합니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "생성할 파일 경로 (output/ 기준 상대경로)"
                },
                "code": {
                    "type": "string",
                    "description": "생성할 코드 내용"
                },
                "description": {
                    "type": "string",
                    "description": "이 파일이 하는 역할에 대한 간단한 설명"
                },
                "language": {
                    "type": "string",
                    "description": "프로그래밍 언어 (예: javascript, python, html)"
                },
                "app_type": {
                    "type": "string",
                    "enum": ["mobile", "desktop"],
                    "description": "미리보기 프레임 타입. desktop: UI가 가로로 넓어야 하는 경우(대시보드, 테이블, 사이드바, 다단 그리드 등). mobile: UI가 세로로 긴 경우(채팅, 피드, 단일 리스트), 학습자가 '앱/모바일'이라고 말한 경우, 또는 네이티브 기능(지도, 카메라, 기울기 센서, GPS, 푸시 알림 등)을 사용하는 경우."
                }
            },
            "required": ["file_path", "code", "description"]
        }
    },
    {
        "name": "edit_code",
        "description": "이미 생성된 파일의 특정 부분만 수정합니다. 파일 전체를 다시 생성하지 않고, 변경할 부분만 지정하세요. 수정할 때는 generate_code 대신 이 도구를 사용하세요.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "수정할 파일 경로"
                },
                "old_code": {
                    "type": "string",
                    "description": "기존 코드에서 교체할 부분 (정확히 일치해야 함)"
                },
                "new_code": {
                    "type": "string",
                    "description": "새로 교체할 코드"
                },
                "description": {
                    "type": "string",
                    "description": "무엇을 왜 수정했는지 간단한 설명"
                }
            },
            "required": ["file_path", "old_code", "new_code", "description"]
        }
    },
    {
        "name": "add_learning_note",
        "description": "코드를 생성한 뒤, 이번 구현에서 사용된 프로그래밍 개념/원리를 학습 노트로 추가합니다. 기술 용어 없이, 비개발자도 이해할 수 있게 작성하세요.",
        "input_schema": {
            "type": "object",
            "properties": {
                "notes": {
                    "type": "array",
                    "description": "학습 노트 목록",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "쉬운 제목 (예: '화면이 알아서 바뀌는 비밀')"
                            },
                            "what": {
                                "type": "string",
                                "description": "이게 뭔지 쉬운 비유로 설명 (3~4문장)"
                            },
                            "why": {
                                "type": "string",
                                "description": "이게 없으면 어떻게 되는지, 왜 중요한지 (3~4문장)"
                            },
                            "where": {
                                "type": "string",
                                "description": "일상에서 쓰는 앱에서의 예시 (3~4문장)"
                            }
                        },
                        "required": ["title", "what", "why", "where"]
                    }
                }
            },
            "required": ["notes"]
        }
    },
    {
        "name": "add_code_annotation",
        "description": "코드의 특정 부분에 프로그래밍 개념 설명을 붙입니다. 초보자가 이해할 수 있는 쉬운 설명으로 작성하세요.",
        "input_schema": {
            "type": "object",
            "properties": {
                "annotations": {
                    "type": "array",
                    "description": "코드 주석 목록",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file": {
                                "type": "string",
                                "description": "파일 경로 (예: App.tsx)"
                            },
                            "line": {
                                "type": "integer",
                                "description": "해당 줄 번호"
                            },
                            "title": {
                                "type": "string",
                                "description": "한 줄 제목 (예: '조건에 따라 다른 화면 보여주기')"
                            },
                            "explanation": {
                                "type": "string",
                                "description": "쉬운 설명 1~2문장. 초보자도 이해할 수 있게."
                            }
                        },
                        "required": ["file", "line", "title", "explanation"]
                    }
                }
            },
            "required": ["annotations"]
        }
    },
    {
        "name": "web_search",
        "description": "학습자가 언급한 서비스나 개념을 잘 모를 때 웹 검색합니다. 예: '배달의민족 같은 앱'이라고 하면 배달의민족의 주요 기능과 UI를 검색합니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "검색할 내용 (예: '배달의민족 앱 주요 기능 UI')"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "generate_blockly_xml",
        "description": "MODI Blockly XML + 흐름도 + 멀티랭 코드를 한번에 생성합니다. blockly 모드에서만 사용하세요. MODI 구성은 network 모듈 1개와 network 외 실제 입력/출력 모듈 최소 1개를 반드시 포함해야 합니다. network 계열 블록은 최상단 network_upload 1개만 사용하고 다른 network_* 블록은 쓰지 않습니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "xml": {
                    "type": "string",
                    "description": "완성된 Blockly XML 문자열. <xml> 태그로 시작해야 합니다."
                },
                "description": {
                    "type": "string",
                    "description": "이 블록 코드가 하는 일에 대한 간단한 설명"
                },
                "flowchart": {
                    "type": "array",
                    "description": "동작 흐름도 노드 목록. 순서대로 나열.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["start", "loop", "condition", "action", "end"], "description": "노드 타입"},
                            "label": {"type": "string", "description": "노드 레이블 (예: '반복', '버튼 클릭이면', '모터A 속도 100')"},
                            "children": {
                                "type": "array",
                                "description": "루프/조건 내부의 자식 노드들",
                                "items": {"$ref": "#/properties/flowchart/items"}
                            },
                            "branches": {
                                "type": "array",
                                "description": "조건 분기 (condition 타입에서만 사용)",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string", "description": "분기 조건 (예: '버튼 클릭', '그 외')"},
                                        "children": {
                                            "type": "array",
                                            "items": {"$ref": "#/properties/flowchart/items"}
                                        }
                                    }
                                }
                            }
                        },
                        "required": ["type", "label"]
                    }
                },
                "code_langs": {
                    "type": "object",
                    "description": "같은 로직을 Python/JavaScript/C로 표현한 코드",
                    "properties": {
                        "python": {"type": "string", "description": "Python 코드 (import modi 포함)"},
                        "javascript": {"type": "string", "description": "JavaScript 코드"},
                        "c": {"type": "string", "description": "C 코드 (#include modi.h 포함)"}
                    }
                },
                "grid": {
                    "type": "array",
                    "description": "모듈 2D 격자 배치. network는 정확히 1개, network 외 실제 입력/출력 모듈은 최소 1개 이상 포함한다. 작품 형태에 맞게 배열한다. 각 행은 string 배열이며 모듈 키 또는 null. 예: 마술봉(길쭉)→[[\"network\"],[\"imu\"],[\"led\"]], 두 바퀴 자동차→맨 아래 행에 [\"motor_b\",\"motor_a\"] 인접 배치하고 본체는 윗 행들에 가로 2열로(2열×약4행). 바퀴 모터는 rotations로 180° 회전, attachments로 wheel 지정. network는 왼쪽 칸을 비워 USB 자리를 남기세요.",
                    "items": {
                        "type": "array",
                        "items": {"type": ["string", "null"]},
                        "description": "한 행의 모듈 키 목록"
                    }
                },
                "rotations": {
                    "type": "object",
                    "description": "모듈별 회전 각도(시계방향 0/90/180/270) 맵. 회전이 필요한 모듈만 넣고 나머지는 생략(=0). 두 바퀴 자동차처럼 모터를 바퀴로 쓰면 축이 바깥(좌·우)을 향하도록 {\"motor_b\":180,\"motor_a\":180}. 예: {\"motor_b\":180,\"motor_a\":180}",
                    "additionalProperties": {"type": "integer", "enum": [0, 90, 180, 270]}
                },
                "attachments": {
                    "type": "object",
                    "description": "모터 축에 끼우는 부착물 맵 (모터 키 → 'wheel' 또는 'i_horn'). 모터는 그냥 축이 회전하는 모듈이라, 바퀴로 쓰면 wheel, 흔드는 막대로 쓰면 i_horn을 축에 끼운다. 부착물이 없으면 생략. 예: 두 바퀴 자동차→{\"motor_b\":\"wheel\",\"motor_a\":\"wheel\"}, 흔드는 팔→{\"motor_a\":\"i_horn\"}",
                    "additionalProperties": {"type": "string", "enum": ["wheel", "i_horn"]}
                }
            },
            "required": ["xml", "description", "grid"]
        }
    },
    {
        "name": "set_modi_layout",
        "description": "하이브리드(소프트웨어+하드웨어) 작품에 쓰는 MODI 모듈들의 물리 배치를 작품 형태·용도에 맞게 2D 격자로 제공한다. 코드 생성(generate_code)과 같은 턴에 함께 호출하세요. network는 정확히 1개만 포함하고, network 외에 코드가 실제로 읽거나 제어하는 MODI 모듈을 최소 1개 이상 포함해야 합니다. 작품에서 쓰는 모든 MODI 모듈을 포함하세요(여러 파일에 흩어져 있어도). 입력 센서 위주면 단순 배치, 모터로 바퀴 자동차를 만들면 모터를 인접 배치 + rotations 180° + attachments wheel로 지정(블록 모드 grid와 동일 규칙). network는 한쪽 끝에 두고 **왼쪽 칸은 비워** USB 꽂는 자리를 남기세요.",
        "input_schema": {
            "type": "object",
            "properties": {
                "grid": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": ["string", "null"]}},
                    "description": "모듈 2D 격자 배치. 각 행은 모듈 키(또는 null) 문자열 배열. network는 정확히 1개만, network 외에는 코드가 실제로 상호작용하는 MODI 모듈을 최소 1개 이상 넣는다. 작품의 모든 MODI 모듈을 각 한 번씩. 예: 막대형→[[\"network\"],[\"imu\"],[\"led\"]], 양손 컨트롤러→[[\"button\",\"network\",\"joystick\"]], 두 바퀴 자동차→맨 아래 [\"motor_b\",\"motor_a\"] 인접."
                },
                "rotations": {
                    "type": "object",
                    "description": "모듈 회전각 맵 (모듈 키 → 0/90/180/270). 바퀴 모터는 보통 180°. 없으면 생략.",
                    "additionalProperties": {"type": "integer"}
                },
                "attachments": {
                    "type": "object",
                    "description": "모터 축 부착물 맵 (모터 키 → 'wheel'/'i_horn'). 바퀴면 wheel. 없으면 생략.",
                    "additionalProperties": {"type": "string", "enum": ["wheel", "i_horn"]}
                }
            },
            "required": ["grid"]
        }
    },
    {
        "name": "transition_phase",
        "description": "현재 Phase를 다음 Phase로 전환합니다. 설계→구현 순서입니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "target_phase": {
                    "type": "string",
                    "enum": ["design", "implement"],
                    "description": "전환할 대상 Phase"
                },
                "reason": {
                    "type": "string",
                    "description": "Phase를 전환하는 이유"
                }
            },
            "required": ["target_phase", "reason"]
        }
    }
]


def handle_tool_call(tool_name: str, tool_input: dict, state) -> str:
    """도구 실행 단일 진입점 — 어떤 입력에도 예외를 밖으로 던지지 않는다.

    도구 입력은 모델 출력이라 신뢰할 수 없다(필수 키 누락, 잘못된 타입 등).
    예외가 새면 턴 전체가 죽으므로(실사고: update_diagram에 mermaid_code 누락 →
    KeyError로 채팅 턴 ERROR), 실패는 "오류:" 프리픽스 문자열로 반환한다 —
    모델이 tool_result로 보고 스스로 고치고, _is_tool_error가 에러로 집계한다.
    """
    try:
        return _handle_tool_call(tool_name, tool_input, state)
    except KeyError as e:
        return f"오류: 도구 입력에 필수 항목 {e}이(가) 없습니다. 누락된 값을 채워 같은 도구를 다시 호출하세요."
    except Exception as e:
        return f"오류: 도구 실행에 실패했습니다 ({type(e).__name__}: {e}). 입력을 고쳐 다시 호출하세요."


def _handle_tool_call(tool_name: str, tool_input: dict, state) -> str:
    if tool_name == "update_design_doc":
        doc = state.project.design_doc
        if "project_name" in tool_input:
            doc.project_name = tool_input["project_name"]
            # 설계 문서에 제목이 잡히면 즉시 히스토리 제목(state.title)에 동기화한다.
            # (전엔 구현 단계 post-agent에서만 title을 설정해서, 설계만 한 세션은 제목이 없었음 —
            #  quick/design 모드가 제목 면에서 달라지던 원인. 이제 phase 무관하게 동일하게 잡힘.)
            if tool_input["project_name"]:
                state.title = tool_input["project_name"]
        if "description" in tool_input:
            doc.description = tool_input["description"]
        if "users" in tool_input:
            for u in tool_input["users"]:
                if u not in doc.users:
                    doc.users.append(u)
        if "features" in tool_input:
            existing_names = {f.name for f in doc.features}
            for f in tool_input["features"]:
                if f["name"] not in existing_names:
                    doc.features.append(Feature(**f))
        if "pages" in tool_input:
            existing_names = {p.name for p in doc.pages}
            for p in tool_input["pages"]:
                if p["name"] not in existing_names:
                    doc.pages.append(Page(**p))
        if "data_models" in tool_input:
            existing_names = {d.name for d in doc.data_models}
            for d in tool_input["data_models"]:
                if d["name"] not in existing_names:
                    doc.data_models.append(DataModel(**d))
        if "user_flows" in tool_input:
            doc.user_flows.extend(tool_input["user_flows"])
        if "strengths" in tool_input:
            doc.strengths = tool_input["strengths"]
        if "weaknesses" in tool_input:
            doc.weaknesses = tool_input["weaknesses"]

        summary = (
            f"설계 문서 업데이트 완료\n"
            f"- 사용자: {', '.join(doc.users)}\n"
            f"- 기능: {len(doc.features)}개\n"
            f"- 페이지: {len(doc.pages)}개\n"
            f"- 데이터 모델: {len(doc.data_models)}개"
        )
        if doc.strengths:
            summary += f"\n- 강점: {', '.join(doc.strengths)}"
        if doc.weaknesses:
            summary += f"\n- 약점: {', '.join(doc.weaknesses)}"
        return summary

    elif tool_name == "plan_tasks":
        tasks = [Task(**t) for t in tool_input["tasks"]]
        state.project.task_plan.tasks = tasks
        return f"태스크 {len(tasks)}개 생성 완료\n\n{state.project.task_plan.progress_summary()}"

    elif tool_name == "complete_task":
        task_id = tool_input["task_id"]
        task = state.project.task_plan.complete_task(task_id)
        if not task:
            return f"오류: 태스크 ID {task_id}를 찾을 수 없습니다."
        result = f"태스크 완료: {task.name}\n\n{state.project.task_plan.progress_summary()}"
        nxt = state.project.task_plan.next_task()
        if nxt:
            result += f"\n\n다음 태스크: {nxt.name} ({', '.join(nxt.files)})"
        elif state.project.task_plan.all_done():
            result += "\n\n모든 태스크가 완료되었습니다!"
        return result

    elif tool_name == "update_diagram":
        if not tool_input.get("mermaid_code"):
            return "오류: mermaid_code가 없습니다. 다이어그램의 Mermaid 코드를 포함해 다시 호출하세요."
        mermaid = state.diagram_manager.update(
            mermaid_code=tool_input["mermaid_code"],
            components=tool_input.get("components")
        )
        state.project.diagram = state.diagram_manager.data
        return f"다이어그램이 업데이트되었습니다.\n\n```mermaid\n{mermaid}\n```"

    elif tool_name == "generate_code":
        file_path = tool_input.get("file_path", "")
        code = tool_input.get("code", "")
        description = tool_input.get("description", "")
        if not file_path or not code:
            return "오류: file_path와 code는 필수입니다. 다시 시도해주세요."
        language = tool_input.get("language", "")

        # 유니코드 이스케이프 → 실제 문자 변환 + lucide 아이콘 자동 수정(무효 치환 + 누락 import 보강)
        # 단, hybrid는 제외 — 런타임이 import를 금지(전역 React/MODI/Chart만 제공)하는데
        # 여기서 import를 자동 주입하면 검증(import 금지)과 서로를 되돌리는 무한 수정 루프가 된다.
        code = fix_unicode_escapes_in_jsx(code)
        if getattr(state, "coding_type", "") != "hybrid":
            code = fix_lucide_icons({file_path: code})[file_path]
        # JSX(닫는태그/self-closing/fragment)가 든 .ts는 .tsx로 교정 (.ts에선 JSX 파싱 불가 → SyntaxError)
        new_path = jsx_safe_ext(file_path, code)
        if new_path != file_path:
            # 확장자가 바뀌면 옛 .ts 항목을 code_map·generated_files에서 제거해
            # 스테일 중복(foo.ts + foo.tsx 공존)을 막는다. (edit_code와 동일한 처리)
            state.generated_code_map.pop(file_path, None)
            state.project.generated_files = [
                f for f in state.project.generated_files if f.path != file_path
            ]
            file_path = new_path
        # #68 O2: 안 바뀐 파일 전체 재출력 차단 — 기존과 완전히 동일한 코드를 generate_code 로
        # 다시 뱉는 것은 순수 출력 낭비다(수정 턴 전체재작성 낭비의 대표 케이스). 재저장·dirty
        # 표시 없이 되돌려 보내 다음 라운드에서 실제 변경만 edit_code 로 하도록 유도한다.
        if state.generated_code_map.get(file_path) == code:
            return (f"'{file_path}'은(는) 기존 내용과 동일합니다. 변경이 없으니 이 파일은 다시 "
                    "출력하지 마세요. 실제로 바꿀 부분이 있으면 그 부분만 edit_code로 고치세요.")
        state.generated_code_map[file_path] = code

        # app_type이 있으면 저장 (첫 호출에서만)
        app_type = tool_input.get("app_type")
        if app_type and not state.app_type:
            state.app_type = app_type

        generated = GeneratedFile(path=file_path, description=description, language=language)
        # 기존 파일이면 업데이트
        existing = next((f for f in state.project.generated_files if f.path == file_path), None)
        if existing:
            existing.description = description
            existing.language = language
        else:
            state.project.generated_files.append(generated)

        state.mark_code_dirty()
        return f"파일이 생성되었습니다: {file_path}\n설명: {description}"

    elif tool_name == "edit_code":
        file_path = tool_input.get("file_path", "")
        old_code = tool_input.get("old_code", "")
        new_code = tool_input.get("new_code", "")
        description = tool_input.get("description", "")

        # generate_code가 JSX 담긴 .ts를 .tsx로 교정했을 수 있으니 형제 확장자도 찾는다.
        file_path = _resolve_code_path(state.generated_code_map, file_path)
        if file_path not in state.generated_code_map:
            return f"오류: '{file_path}' 파일이 존재하지 않습니다."

        current = state.generated_code_map[file_path]
        if old_code not in current:
            # 복구 지침을 구체적으로 — 막연한 "정확한 코드를 지정해주세요"는 소형 모델이
            # 다음 라운드에서 성공한 파일까지 전부 재작성하는 과잉 복구를 유발했다(실사고:
            # 수정 1건에 LLM 3라운드·성공 파일 중복 재작성으로 비용 3배).
            return (f"오류: '{file_path}'에서 교체할 코드를 찾을 수 없습니다. "
                    "위 코드 컨텍스트의 현재 내용과 정확히 일치하는 old_code로 다시 시도하거나, "
                    "어려우면 이 파일만 generate_code로 전체 재작성하세요. "
                    "이미 성공한 다른 파일은 절대 다시 작성하지 마세요.")

        updated = current.replace(old_code, new_code, 1)
        updated = fix_unicode_escapes_in_jsx(updated)
        # generate_code와 동일하게 lucide 자동수정(무효 치환 + 누락 import 보강)을 거쳐 저장한다.
        # 수정 턴(edit_code)이 import를 어긋나게 만들어도 여기서 잡힌다.
        # hybrid 제외 사유는 generate_code와 동일(import 금지 런타임과 무한 수정 루프).
        if getattr(state, "coding_type", "") != "hybrid":
            updated = fix_lucide_icons({file_path: updated})[file_path]
        # 수정으로 JSX가 새로 들어왔으면 .ts→.tsx 교정 (경로 바뀌면 옛 키 제거)
        new_path = jsx_safe_ext(file_path, updated)
        if new_path != file_path:
            state.generated_code_map.pop(file_path, None)
            file_path = new_path
        state.generated_code_map[file_path] = updated
        state.mark_code_dirty()
        return f"파일이 수정되었습니다: {file_path}\n수정 내용: {description}"

    elif tool_name == "add_learning_note":
        notes = tool_input.get("notes", [])
        state.learning_notes.extend(notes)
        return f"학습 노트 {len(notes)}개가 추가되었습니다."

    elif tool_name == "add_code_annotation":
        annotations = tool_input.get("annotations", [])
        if not hasattr(state, 'code_annotations'):
            state.code_annotations = []
        state.code_annotations.extend(annotations)
        return f"코드 주석 {len(annotations)}개가 추가되었습니다."

    elif tool_name == "generate_blockly_xml":
        xml = tool_input.get("xml", "")
        description = tool_input.get("description", "")
        if not xml:
            return "오류: xml은 필수입니다. xml을 포함해 다시 호출하세요."

        # 자동 수정(Number→Boolean) 후 '일단 저장'한다. (react의 generate_code와 동일하게:
        # 코드/XML은 저장하고, 검증·수정은 오케스트레이터가 루프 밖에서 _fix_blockly로 처리.)
        xml = fix_blockly_type_mismatches(xml)
        state.blockly_xml = xml
        state.blockly_flowchart = tool_input.get("flowchart", [])
        state.blockly_code_langs = tool_input.get("code_langs", {})
        state.modi_grid = tool_input.get("grid", [])
        state.modi_rotations = tool_input.get("rotations", {})
        state.modi_attachments = tool_input.get("attachments", {})
        return f"Blockly XML이 저장되었습니다. 설명: {description}"

    elif tool_name == "set_modi_layout":
        # 하이브리드: 모듈 물리 배치를 블록 모드와 동일한 state 필드에 기록 (modi_modules 후처리가 사용)
        state.modi_grid = tool_input.get("grid", []) or []
        state.modi_rotations = tool_input.get("rotations", {}) or {}
        state.modi_attachments = tool_input.get("attachments", {}) or {}
        return "MODI 모듈 배치를 기록했습니다."

    elif tool_name == "transition_phase":
        # 텍스트 프로토콜 도구호출은 스키마 enum을 강제하지 못한다 — 모델이 임의 문자열을
        # 보내도 크래시하지 않도록 방어.
        raw = (tool_input.get("target_phase") or "").strip()
        try:
            target = Phase(raw)
        except ValueError:
            return f"'{raw}'는 알 수 없는 Phase라 전환하지 않았습니다 (design/implement만 가능)."
        current = state.project.phase
        reason = tool_input.get("reason", "")
        # 전환 규칙은 상태 기계 경계(여기)에서 강제한다 — 모델이 헛호출해도 phase가 안 깨진다.
        # 이 도구의 용도는 '구현으로 들어가기'(설계→구현, 검증→구현)뿐이다. 구현에서 나가는
        # 전환은 유저 의사(라우터 phase_change)로만 — 소형 모델이 quick 첫 턴에서 스스로
        # 전환해 산출물 없이 턴이 끝나고 이후 턴 전체가 엉뚱한 phase에서 도는 사고를 막는다.
        if target == current:
            return f"이미 '{target.value}' 단계입니다. 전환 없이 현재 단계의 작업을 계속하세요."
        if target != Phase.IMPLEMENT:
            return (f"'{current.value}'에서 '{target.value}'(으)로는 이 도구로 전환할 수 없습니다. "
                    "단계 전환 없이 현재 작업을 계속하세요.")
        state.project.phase = target
        return f"Phase가 '{target.value}'(으)로 전환되었습니다. 이유: {reason}"

    return f"알 수 없는 도구: {tool_name}"


# ── MODI Blockly XML 검증 ──

VALID_MODI_BLOCK_TYPES = {
    # 설정
    "network_upload", "network_execute", "network_setup",
    # 제어 (controls_if는 사용 금지 — controls_ifonly/controls_ifelse만)
    "controls_whileInfinite", "controls_whileUntil", "controls_repeat_ext",
    "controls_ifonly", "controls_ifelse", "control_wait",
    "loop_break", "loop_continue",
    # 입력
    "input_button_status", "input_button_value",
    "input_dial_position", "input_dial_angle", "input_dial_section", "input_dial_speed", "input_dial_value",
    "input_joystick_status", "input_joystick_value", "input_joystick_axis", "input_joystick_axis_value",
    "input_environment_celsius", "input_environment_fahrenheit", "input_environment_humidity",
    "input_environment_illuminance", "input_environment_volume", "input_environment_value",
    "input_imu_angle", "input_imu_acceleration", "input_imu_velocity", "input_imu_shaking", "input_imu_value",
    "input_tof_cm", "input_tof_inch", "input_tof_value",
    # 출력
    "output_motorA_speed", "output_motorA_angle", "output_motorA_relative_angle", "output_motorA_angle_speed", "output_motorA_stop",
    "output_motorB_speed", "output_motorB_angle", "output_motorB_relative_angle", "output_motorB_angle_speed", "output_motorB_stop",
    "output_led_color", "output_led_rgb", "output_led_clear",
    "output_speaker_note", "output_speaker_melody", "output_speaker_frequency", "output_speaker_clear",
    "output_display_text", "output_display_drawing", "output_display_variable", "output_display_offset", "output_display_position", "output_display_clear",
    # 변수
    "variables_set", "variables_get", "variables_add",
    # 기본 Blockly
    "math_number", "math_number_min0_max100", "math_number_min-100_max100",
    "math_number_min0_max360", "math_decimal_number_min-99999_max99999",
    "math_arithmetic", "math_random_int", "math_modulo", "math_single", "math_only_round",
    "text", "logic_boolean", "logic_compare", "logic_operation", "logic_negate",
    "colour_hsv_sliders",
    # 네트워크 입력
    "network_button_status", "network_button_value",
    "network_switch_status", "network_switch_value",
    "network_dial_status", "network_dial_value",
    "network_joystick_status", "network_joystick_value",
    "network_slide_status", "network_slide_value",
    "network_timer_status", "network_timer_value",
    "network_data_status", "network_data_value",
    "network_send_data", "network_buzzer", "network_camera",
}


# ── Number→Boolean 자동 교체 매핑 ──
# {Number블록타입: {FUNC값: (Boolean블록타입, 새FUNC값|None), "_default": ...}}
# 새FUNC값이 None이면 FUNC 필드 제거 (블록 타입 자체가 측정값을 결정)
_NUMBER_TO_BOOL = {
    "input_tof_value": {
        "cm": ("input_tof_cm", None),
        "inch": ("input_tof_inch", None),
        "_default": ("input_tof_cm", None),
    },
    "input_environment_value": {
        "getTemperature_C": ("input_environment_celsius", None),
        "getTemperature_F": ("input_environment_fahrenheit", None),
        "getHumidity": ("input_environment_humidity", None),
        "getIntensity": ("input_environment_illuminance", None),
        "getVolume": ("input_environment_volume", None),
        "_default": ("input_environment_celsius", None),
    },
    "input_dial_value": {
        "getTurn": ("input_dial_position", None),
        "getTurnAngle": ("input_dial_angle", None),
        "getSection": ("input_dial_section", None),
        "getTurnSpeed": ("input_dial_speed", None),
        "_default": ("input_dial_position", None),
    },
    "input_imu_value": {
        "getRoll": ("input_imu_angle", "getRoll"),
        "getPitch": ("input_imu_angle", "getPitch"),
        "getYaw": ("input_imu_angle", "getYaw"),
        "getAccelerationX": ("input_imu_acceleration", "getAccelerationX"),
        "getAccelerationY": ("input_imu_acceleration", "getAccelerationY"),
        "getAccelerationZ": ("input_imu_acceleration", "getAccelerationZ"),
        "getAngularVelocityX": ("input_imu_velocity", "getAngularVelocityX"),
        "getAngularVelocityY": ("input_imu_velocity", "getAngularVelocityY"),
        "getAngularVelocityZ": ("input_imu_velocity", "getAngularVelocityZ"),
        "getVibration": ("input_imu_shaking", None),
        "_default": ("input_imu_angle", "getRoll"),
    },
    "input_button_value": {
        "click": ("input_button_status", "getClick"),
        "doubleClick": ("input_button_status", "getDoubleClick"),
        "pressedState": ("input_button_status", "getPressStatus"),
        "toggle": ("input_button_status", "getToggle"),
        "_default": ("input_button_status", "getClick"),
    },
    "input_joystick_value": {
        "_default": ("input_joystick_status", "100"),
    },
    "input_joystick_axis_value": {
        "X": ("input_joystick_axis", "X"),
        "Y": ("input_joystick_axis", "Y"),
        "_default": ("input_joystick_axis", "X"),
    },
}

# 비교 연산(OP + VALUE)이 필요한 Boolean 블록 (status 블록은 FUNC만으로 Boolean 반환)
_COMPARISON_BOOL_BLOCKS = {
    "input_tof_cm", "input_tof_inch",
    "input_environment_celsius", "input_environment_fahrenheit",
    "input_environment_humidity", "input_environment_illuminance",
    "input_environment_volume",
    "input_dial_position", "input_dial_angle", "input_dial_section", "input_dial_speed",
    "input_imu_angle", "input_imu_acceleration", "input_imu_velocity", "input_imu_shaking",
    "input_joystick_axis",
}


_REF_DIR = os.path.join(_BASE_DIR, "docs", "modi", "ref")
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_BLOCK_TYPE_RE = re.compile(r"^(input|output|control)_[A-Za-z0-9_]+$")
_func_whitelist_cache: dict | None = None


def _is_block_type(token: str) -> bool:
    return bool(_BLOCK_TYPE_RE.match(token))


def _func_bearing_blocks(text: str) -> set:
    """ref 파일의 '## 블록 타입' 표에서 FUNC 필드를 가진 블록 타입들을 추출."""
    blocks: set = set()
    in_table = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_table = line.startswith("## 블록 타입")
            continue
        if in_table and line.startswith("|") and "FUNC" in line:
            m = _BACKTICK_RE.search(line)
            if m and _is_block_type(m.group(1)):
                blocks.add(m.group(1))
    return blocks


def _func_value_whitelist() -> dict:
    """docs/modi/ref/*.md에서 블록 타입별 유효 FUNC 드롭다운 값을 추출한다.

    '## ... FUNC 값 ...' 섹션의 백틱 토큰을 유효값으로 수집하고, 헤딩에 명시된
    블록 타입으로 스코프한다(없으면 파일 내 FUNC 보유 블록 전체). 결과는 캐시된다.
    """
    global _func_whitelist_cache
    if _func_whitelist_cache is not None:
        return _func_whitelist_cache

    whitelist: dict = {}
    if os.path.isdir(_REF_DIR):
        for fname in sorted(os.listdir(_REF_DIR)):
            if not fname.endswith(".md"):
                continue
            try:
                with open(os.path.join(_REF_DIR, fname), encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                continue

            func_blocks = _func_bearing_blocks(text)
            for section in re.split(r"^## ", text, flags=re.MULTILINE):
                head, _, body = section.partition("\n")
                if "FUNC 값" not in head:
                    continue
                scoped = [t for t in _BACKTICK_RE.findall(head) if _is_block_type(t)]
                targets = scoped or list(func_blocks)
                values = {t for t in _BACKTICK_RE.findall(body) if t not in scoped}
                if not values or not targets:
                    continue
                for bt in targets:
                    whitelist.setdefault(bt, set()).update(values)

    _func_whitelist_cache = whitelist
    return whitelist


# 항상 <field>이며(값 입력과 혼동 없음) 빠지면 안 되는 필드. ref 블록표에서 추출.
_ALWAYS_FIELD_NAMES = {"INDEX", "FUNC"}
_required_fields_cache: dict | None = None


def _required_fields() -> dict:
    """docs/modi/ref/*.md의 '## 블록 타입' 표에서 블록별 필수 <field>를 추출한다.

    INDEX/FUNC만 대상으로 한다 (RED/VALUE/COLOUR 등은 <value> 입력이라 제외).
    """
    global _required_fields_cache
    if _required_fields_cache is not None:
        return _required_fields_cache

    req: dict = {}
    if os.path.isdir(_REF_DIR):
        for fname in sorted(os.listdir(_REF_DIR)):
            if not fname.endswith(".md"):
                continue
            try:
                with open(os.path.join(_REF_DIR, fname), encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                continue

            in_table = False
            for line in text.splitlines():
                if line.startswith("## "):
                    in_table = line.startswith("## 블록 타입")
                    continue
                if not (in_table and line.startswith("|")):
                    continue
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                block_type = next(
                    (m.group(1) for c in cells
                     if (m := _BACKTICK_RE.search(c)) and _is_block_type(m.group(1))),
                    None,
                )
                if not block_type:
                    continue
                names = {
                    re.sub(r"\(.*?\)", "", tok).strip()
                    for tok in cells[-1].split(",")
                }
                needed = names & _ALWAYS_FIELD_NAMES
                if needed:
                    req.setdefault(block_type, set()).update(needed)

    _required_fields_cache = req
    return req


# 필수 value 입력 — 누락 시 검증 에러로 LLM이 의미 있는 값을 채우게 한다.
_REQUIRED_VALUES = {
    "output_led_color": ["COLOUR"],
    "output_led_rgb": ["RED", "GREEN", "BLUE"],
    "output_speaker_note": ["VALUE"],
    "output_speaker_melody": ["VALUE"],
    "output_speaker_frequency": ["FREQUENCY", "VALUE"],
    "output_motorA_speed": ["VALUE"],
    "output_motorB_speed": ["VALUE"],
    "output_motorA_angle": ["VALUE"],
    "output_motorB_angle": ["VALUE"],
    "output_motorA_relative_angle": ["VALUE"],
    "output_motorB_relative_angle": ["VALUE"],
    "output_display_text": ["VALUE"],
    "output_display_variable": ["VALUE"],
    "control_wait": ["TIME"],
    "variables_set": ["VALUE"],
    "variables_add": ["VALUE"],
}


def validate_blockly_xml(xml: str) -> List[str]:
    """MODI Blockly XML을 검증하여 에러 목록을 반환한다.
    fix_blockly_type_mismatches를 먼저 호출한 뒤 실행할 것."""
    import xml.etree.ElementTree as ET

    errors: List[str] = []

    # 1) XML 파싱
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        return [f"XML 파싱 오류: {e}"]

    # namespace 처리: Blockly XML은 xmlns가 있을 수 있음
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    def find(elem, path):
        """namespace를 자동으로 붙여서 검색"""
        parts = path.split("/")
        ns_path = "/".join(f"{ns}{p}" if p and not p.startswith("[") and not p.startswith("{") else p for p in parts)
        return elem.find(ns_path)

    def findall(elem, path):
        parts = path.split("/")
        ns_path = "/".join(f"{ns}{p}" if p and not p.startswith("[") and not p.startswith("{") else p for p in parts)
        return elem.findall(ns_path)

    def iterall(elem, tag):
        return elem.iter(f"{ns}{tag}")

    # 2) 최상단 블록이 network_upload인지
    top_blocks = findall(root, "block")
    if not top_blocks:
        errors.append("최상단 블록이 없습니다. network_upload 블록이 필요합니다.")
        return errors

    top_type = top_blocks[0].get("type", "")
    if top_type != "network_upload":
        errors.append(f"최상단 블록이 '{top_type}'입니다. 'network_upload'여야 합니다.")

    # 3) network_upload → next → controls_whileInfinite 구조 확인
    next_block = find(top_blocks[0], "next/block")
    if next_block is not None:
        next_type = next_block.get("type", "")
        if next_type != "controls_whileInfinite":
            errors.append(f"network_upload 다음 블록이 '{next_type}'입니다. 'controls_whileInfinite'여야 합니다.")
    else:
        errors.append("network_upload 다음에 controls_whileInfinite 블록이 없습니다.")

    # 4) network 계열 블록 제한: 이 Blockly 런타임에서는 루트 실행 블록 network_upload만 쓴다.
    for block in iterall(root, "block"):
        block_type = block.get("type", "")
        if block_type.startswith("network_") and block_type != "network_upload":
            errors.append(
                f"{block_type}: network 계열 블록은 사용하지 않습니다. "
                "Blockly 프로그램은 최상단 network_upload 블록 1개로 시작해야 합니다."
            )

    # 5) 모든 블록 타입 검증
    for block in iterall(root, "block"):
        block_type = block.get("type", "")
        if block_type and block_type not in VALID_MODI_BLOCK_TYPES:
            errors.append(f"알 수 없는 블록 타입: '{block_type}'. MODI에서 지원하지 않습니다.")

    # 6) MODI 구성 검증: network_upload 1개 + 실제 입력/출력 모듈 최소 1개
    errors.extend(validate_modi_module_contract(
        extract_raw_module_keys_from_xml(xml),
        "Blockly MODI 구성",
    ))

    # 7) controls_ifonly/controls_ifelse 구조 검증
    for block in iterall(root, "block"):
        btype = block.get("type", "")

        if btype in ("controls_ifonly", "controls_ifelse"):
            if not any(v.get("name") == "IF0" for v in findall(block, "value")):
                errors.append(f"{btype}: IF0 조건 누락")
            if not any(s.get("name") == "DO0" for s in findall(block, "statement")):
                errors.append(f"{btype}: DO0 누락")

            if btype == "controls_ifelse":
                if not any(s.get("name") == "ELSE" for s in findall(block, "statement")):
                    errors.append(f"{btype}: ELSE 문 누락")

    # 8) 출력 블록 필수 입력값 검증 (_REQUIRED_VALUES는 모듈 상수)
    required_fields = _required_fields()

    for block in iterall(root, "block"):
        btype = block.get("type", "")

        if btype in _REQUIRED_VALUES:
            value_names = {v.get("name") for v in findall(block, "value")}
            for req in _REQUIRED_VALUES[btype]:
                if req not in value_names:
                    errors.append(f"{btype}: 필수 입력 '{req}' 누락. 값을 연결하세요.")

        # 필수 <field> 검증 (INDEX/FUNC) — ref 블록표 기준
        needed = required_fields.get(btype)
        if needed:
            present = {f.get("name") for f in findall(block, "field") if (f.text or "").strip()}
            for req in sorted(needed - present):
                errors.append(
                    f'{btype}: 필수 필드 \'{req}\' 누락. <field name="{req}">값</field>을 넣으세요.'
                )

    # 9) 센서 Boolean 블록 — OP + VALUE 필수
    for block in iterall(root, "block"):
        btype = block.get("type", "")
        if btype in _COMPARISON_BOOL_BLOCKS:
            field_map = {f.get("name"): (f.text or "") for f in findall(block, "field")}
            value_names = {v.get("name") for v in findall(block, "value")}
            if "OP" not in field_map or not field_map["OP"]:
                errors.append(f"{btype}: 비교 연산자(OP) 누락. >, <, >=, <=, ==, != 중 하나를 설정하세요.")
            if "VALUE" not in value_names:
                errors.append(f"{btype}: 비교 값(VALUE) 누락. 숫자 블록을 연결하세요.")

    # 10) 변수 사용 검증 — variables_get이 참조하는 변수가 선언되었는지
    declared_vars = set()
    for var_elem in iterall(root, "variable"):
        name = var_elem.text
        if name:
            declared_vars.add(name)

    set_vars = set()
    for block in iterall(root, "block"):
        if block.get("type") in ("variables_set", "variables_add"):
            for f in findall(block, "field"):
                if f.get("name") == "VAR" and f.text:
                    set_vars.add(f.text)

    for block in iterall(root, "block"):
        if block.get("type") == "variables_get":
            for f in findall(block, "field"):
                if f.get("name") == "VAR" and f.text:
                    var_name = f.text
                    if var_name not in declared_vars:
                        errors.append(f"변수 '{var_name}'이 <variables> 섹션에 선언되지 않았습니다.")
                    if var_name not in set_vars:
                        errors.append(f"변수 '{var_name}'이 variables_set으로 초기화되지 않고 사용되었습니다.")

    # 11) FUNC 드롭다운 값이 모듈 레퍼런스(docs/modi/ref)에 정의된 유효값인지 검증
    func_whitelist = _func_value_whitelist()
    for block in iterall(root, "block"):
        allowed = func_whitelist.get(block.get("type", ""))
        if not allowed:
            continue
        for f in findall(block, "field"):
            if f.get("name") != "FUNC":
                continue
            val = (f.text or "").strip()
            if val and val not in allowed:
                sample = ", ".join(sorted(allowed)[:8])
                errors.append(
                    f"{block.get('type')}: FUNC 값 '{val}'은(는) 모듈 레퍼런스에 없는 "
                    f"값입니다. 가능한 값 예: {sample}"
                )

    # 12) 빈 value 소켓 — shadow/block이 하나도 없는 입력은 빈 구멍으로 렌더링된다.
    #     (fix_blockly_type_mismatches가 기본 shadow를 아는 소켓은 미리 채우므로,
    #      여기 걸리는 건 IF0·logic_compare 등 의미 있는 블록이 필요한 소켓뿐)
    for block in iterall(root, "block"):
        btype = block.get("type", "")
        for v in findall(block, "value"):
            if find(v, "block") is None and find(v, "shadow") is None:
                errors.append(
                    f"{btype}: '{v.get('name')}' 입력이 비어 있습니다. "
                    "shadow(기본값 블록)나 값 블록을 넣으세요."
                )

    return errors


def fix_blockly_type_mismatches(xml_str: str) -> str:
    """Blockly XML 자동 수정: LLM이 생성한 '대략 맞는' XML을 MODI 시스템에 맞게 변환.
    검증(validate_blockly_xml) 전에 호출할 것."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return xml_str

    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"
        # 입력 XML의 실제 네임스페이스를 기본(접두사 없음)으로 등록
        # 안 하면 직렬화 시 html: / ns0: 접두사가 붙어 Blockly가 파싱 못 함
        ET.register_namespace('', ns[1:-1])

    fixed = False

    # ── 1) OP 값 정규화: GT/LT/GTE/LTE/EQ/NEQ → >/</>=/<=/==/!= ──
    _OP_NORMALIZE = {
        "GT": ">", "LT": "<", "GTE": ">=", "LTE": "<=", "EQ": "==", "NEQ": "!=",
        "gt": ">", "lt": "<", "gte": ">=", "lte": "<=", "eq": "==", "neq": "!=",
    }
    for block in root.iter(f"{ns}block"):
        btype = block.get("type", "")
        if btype in _COMPARISON_BOOL_BLOCKS:
            for field in block.findall(f"{ns}field"):
                if field.get("name") == "OP" and field.text in _OP_NORMALIZE:
                    field.text = _OP_NORMALIZE[field.text]
                    fixed = True

    # ── 2) 버튼 FUNC 정규화: click → getClick() 등 ──
    _BUTTON_FUNC_NORMALIZE = {
        "click": "getClick()", "getClick": "getClick()",
        "doubleClick": "getDoubleClick()", "getDoubleClick": "getDoubleClick()",
        "double_click": "getDoubleClick()",
        "press": "getPressStatus()", "pressed": "getPressStatus()",
        "pressedState": "getPressStatus()", "getPressStatus": "getPressStatus()",
        "toggle": "getToggle()", "getToggle": "getToggle()",
    }
    for block in root.iter(f"{ns}block"):
        if block.get("type") in ("input_button_status", "input_button_value"):
            for field in block.findall(f"{ns}field"):
                if field.get("name") == "FUNC" and field.text in _BUTTON_FUNC_NORMALIZE:
                    field.text = _BUTTON_FUNC_NORMALIZE[field.text]
                    fixed = True

    # ── 3) 조이스틱 FUNC 정규화: top/up → 100, bottom/down → -100 등 ──
    _JOYSTICK_FUNC_NORMALIZE = {
        "top": "100", "up": "100", "위": "100",
        "bottom": "-100", "down": "-100", "아래": "-100",
        "left": "-50", "왼쪽": "-50",
        "right": "50", "오른쪽": "50",
        "center": "0", "중앙": "0",
    }
    for block in root.iter(f"{ns}block"):
        if block.get("type") in ("input_joystick_status", "input_joystick_value"):
            for field in block.findall(f"{ns}field"):
                if field.get("name") == "FUNC" and field.text in _JOYSTICK_FUNC_NORMALIZE:
                    field.text = _JOYSTICK_FUNC_NORMALIZE[field.text]
                    fixed = True

    # ── 4) 색상 블록 타입 정규화: colour_environment_selector → colour_hsv_sliders ──
    # colour_environment_selector의 output 타입은 "ColorClass"이고
    # output_led_color의 COLOUR input은 "Colour"을 기대하므로 타입 불일치.
    # colour_hsv_sliders는 output "Colour"이라 LED와 호환됨.
    for block in root.iter(f"{ns}block"):
        if block.get("type") == "colour_environment_selector":
            block.set("type", "colour_hsv_sliders")
            fixed = True

    # ── 5) INDEX 필드 누락 시 자동 추가 (기본값 0) ──
    _NEEDS_INDEX = {t for t in VALID_MODI_BLOCK_TYPES if t.startswith(("input_", "output_"))}
    for block in root.iter(f"{ns}block"):
        btype = block.get("type", "")
        if btype in _NEEDS_INDEX:
            has_index = any(f.get("name") == "INDEX" for f in block.findall(f"{ns}field"))
            if not has_index:
                idx_f = ET.SubElement(block, f"{ns}field")
                idx_f.set("name", "INDEX")
                idx_f.text = "0"
                fixed = True

    # ── 6) network_upload 래퍼 자동 감싸기 (연결은 <next> 사용) ──
    top_blocks = root.findall(f"{ns}block")
    if top_blocks:
        top_type = top_blocks[0].get("type", "")
        if top_type == "controls_whileInfinite":
            # whileInfinite는 있는데 network_upload가 없음 → 감싸기
            while_block = top_blocks[0]
            root.remove(while_block)
            upload = ET.SubElement(root, f"{ns}block")
            upload.set("type", "network_upload")
            upload.set("deletable", "false")
            next_el = ET.SubElement(upload, f"{ns}next")
            next_el.append(while_block)
            fixed = True
        elif top_type == "network_upload":
            # statement name="NEXT"로 잘못 연결된 경우 → <next>로 교정
            stmt_elem = None
            for s in top_blocks[0].findall(f"{ns}statement"):
                if s.get("name") == "NEXT":
                    stmt_elem = s
                    break
            if stmt_elem is not None:
                inner_block = stmt_elem.find(f"{ns}block")
                top_blocks[0].remove(stmt_elem)
                if inner_block is not None:
                    next_el = ET.SubElement(top_blocks[0], f"{ns}next")
                    next_el.append(inner_block)
                fixed = True

            # network_upload은 있는데 whileInfinite가 없을 수 있음 → 체크
            next_elem = top_blocks[0].find(f"{ns}next")
            inner = next_elem.find(f"{ns}block") if next_elem is not None else None

            if inner is not None and inner.get("type") != "controls_whileInfinite":
                # network_upload 바로 다음이 whileInfinite가 아님 → 감싸기
                next_elem.remove(inner)
                while_block = ET.SubElement(next_elem, f"{ns}block")
                while_block.set("type", "controls_whileInfinite")
                do_stmt = ET.SubElement(while_block, f"{ns}statement")
                do_stmt.set("name", "DO")
                do_stmt.append(inner)
                fixed = True

    # ── 6.4) value 안 shadow 보정 ──
    # (a) 리터럴 블록 → shadow 변환
    _SHADOW_LITERAL = {"text", "colour_hsv_sliders"}
    _SHADOW_PREFIXES = ("math_number", "math_decimal_number")
    for value_elem in root.iter(f"{ns}value"):
        inner = value_elem.find(f"{ns}block")
        if inner is None:
            continue
        inner_type = inner.get("type", "")
        if inner_type in _SHADOW_LITERAL or inner_type.startswith(_SHADOW_PREFIXES):
            inner.tag = f"{ns}shadow"
            fixed = True

    # (b) 변수/센서 block이 있지만 shadow가 없으면 기본 shadow 보충
    _DEFAULT_SHADOWS = {
        ("output_motorA_speed", "VALUE"): ("math_number_min-100_max100", "NUM", "100"),
        ("output_motorB_speed", "VALUE"): ("math_number_min-100_max100", "NUM", "100"),
        ("output_motorA_angle", "VALUE"): ("math_number_min0_max360", "NUM", "0"),
        ("output_motorB_angle", "VALUE"): ("math_number_min0_max360", "NUM", "0"),
        ("output_motorA_relative_angle", "VALUE"): ("math_number_min0_max360", "NUM", "0"),
        ("output_motorB_relative_angle", "VALUE"): ("math_number_min0_max360", "NUM", "0"),
        ("output_motorA_angle_speed", "VALUE_ANGLE"): ("math_number_min0_max360", "NUM", "0"),
        ("output_motorA_angle_speed", "VALUE_SPEED"): ("math_number_min0_max100", "NUM", "100"),
        ("output_motorB_angle_speed", "VALUE_ANGLE"): ("math_number_min0_max360", "NUM", "0"),
        ("output_motorB_angle_speed", "VALUE_SPEED"): ("math_number_min0_max100", "NUM", "100"),
        ("output_speaker_note", "VALUE"): ("math_number_min0_max100", "NUM", "100"),
        ("output_speaker_melody", "VALUE"): ("math_number_min0_max100", "NUM", "100"),
        ("output_speaker_frequency", "FREQUENCY"): ("math_number_min500_max4000", "NUM", "1046"),
        ("output_speaker_frequency", "VALUE"): ("math_number_min0_max100", "NUM", "100"),
        ("output_led_rgb", "RED"): ("math_number_min0_max100", "NUM", "100"),
        ("output_led_rgb", "GREEN"): ("math_number_min0_max100", "NUM", "100"),
        ("output_led_rgb", "BLUE"): ("math_number_min0_max100", "NUM", "100"),
        ("output_led_color", "COLOUR"): ("colour_hsv_sliders", "COLOUR", "#ff0000"),
        ("output_display_text", "VALUE"): ("text", "TEXT", "Hello"),
        ("output_display_variable", "VALUE"): ("math_decimal_number_min-99999_max99999", "NUM", "0"),
        ("output_display_offset", "OFFSET_X"): ("math_number_min0_max96", "NUM", "0"),
        ("output_display_offset", "OFFSET_Y"): ("math_number_min0_max96", "NUM", "0"),
        ("output_display_position", "AXIS_X_VALUE"): ("math_number_min-96_max96", "NUM", "0"),
        ("output_display_position", "AXIS_Y_VALUE"): ("math_number_min-96_max96", "NUM", "0"),
        ("control_wait", "TIME"): ("math_decimal_number_min-99999_max99999", "NUM", "1"),
        ("controls_repeat_ext", "TIMES"): ("math_decimal_number_min-99999_max99999", "NUM", "10"),
        ("variables_set", "VALUE"): ("math_decimal_number_min-99999_max99999", "NUM", "0"),
        ("variables_add", "VALUE"): ("math_decimal_number_min-99999_max99999", "NUM", "1"),
    }
    for block in root.iter(f"{ns}block"):
        btype = block.get("type", "")
        for value_elem in block.findall(f"{ns}value"):
            vname = value_elem.get("name", "")
            key = (btype, vname)
            if key not in _DEFAULT_SHADOWS:
                continue
            has_shadow = value_elem.find(f"{ns}shadow") is not None
            has_block = value_elem.find(f"{ns}block") is not None
            if has_block and not has_shadow:
                s_type, f_name, f_val = _DEFAULT_SHADOWS[key]
                shadow_el = ET.SubElement(value_elem, f"{ns}shadow")
                shadow_el.set("type", s_type)
                field_el = ET.SubElement(shadow_el, f"{ns}field")
                field_el.set("name", f_name)
                field_el.text = f_val
                fixed = True

    # ── 6.5) control_wait의 TIME이 field로 들어간 경우 → value input으로 교정 ──
    for block in root.iter(f"{ns}block"):
        if block.get("type") != "control_wait":
            continue
        time_field = None
        for f in block.findall(f"{ns}field"):
            if f.get("name") == "TIME":
                time_field = f
                break
        if time_field is not None:
            time_val = time_field.text or "1"
            block.remove(time_field)
            val_el = ET.SubElement(block, f"{ns}value")
            val_el.set("name", "TIME")
            num_b = ET.SubElement(val_el, f"{ns}block")
            num_b.set("type", "math_decimal_number_min-99999_max99999")
            num_f = ET.SubElement(num_b, f"{ns}field")
            num_f.set("name", "NUM")
            num_f.text = time_val
            fixed = True

    # ── 7) IF0 슬롯의 Number→Boolean 자동 교체 ──
    for value_elem in root.iter(f"{ns}value"):
        if value_elem.get("name") != "IF0":
            continue

        block = value_elem.find(f"{ns}block")
        if block is None:
            continue

        block_type = block.get("type", "")
        if block_type not in _NUMBER_TO_BOOL:
            continue

        index_val = "0"
        func_val = ""
        for field in block.findall(f"{ns}field"):
            if field.get("name") == "INDEX":
                index_val = field.text or "0"
            elif field.get("name") == "FUNC":
                func_val = field.text or ""

        mapping = _NUMBER_TO_BOOL[block_type]
        bool_type, new_func = mapping.get(func_val, mapping["_default"])

        new_block = ET.Element(f"{ns}block")
        new_block.set("type", bool_type)

        idx_f = ET.SubElement(new_block, f"{ns}field")
        idx_f.set("name", "INDEX")
        idx_f.text = index_val

        if new_func is not None:
            func_f = ET.SubElement(new_block, f"{ns}field")
            func_f.set("name", "FUNC")
            func_f.text = new_func

        if bool_type in _COMPARISON_BOOL_BLOCKS:
            op_f = ET.SubElement(new_block, f"{ns}field")
            op_f.set("name", "OP")
            op_f.text = ">"

            val_elem = ET.SubElement(new_block, f"{ns}value")
            val_elem.set("name", "VALUE")
            num_b = ET.SubElement(val_elem, f"{ns}block")
            num_b.set("type", "math_number")
            num_f = ET.SubElement(num_b, f"{ns}field")
            num_f.set("name", "NUM")
            num_f.text = "0"

        value_elem.remove(block)
        value_elem.append(new_block)
        fixed = True

    # ── 8) while 루프 안의 display_clear 제거 (깜빡임 방지) ──
    for block in root.iter(f"{ns}block"):
        if block.get("type") != "output_display_clear":
            continue
        parent = _find_parent(root, block, ns)
        if parent is not None:
            next_of_clear = block.find(f"{ns}next")
            child_after = next_of_clear.find(f"{ns}block") if next_of_clear is not None else None

            if parent.tag == f"{ns}next":
                parent.remove(block)
                if child_after is not None:
                    parent.append(child_after)
            elif parent.tag == f"{ns}statement":
                parent.remove(block)
                if child_after is not None:
                    parent.append(child_after)
            fixed = True

    # ── 9) <variables> 섹션 자동 생성: variables_set/get/add에서 사용된 변수 수집 ──
    used_vars = {}  # {name: id}
    for block in root.iter(f"{ns}block"):
        if block.get("type") in ("variables_set", "variables_get", "variables_add"):
            for field in block.findall(f"{ns}field"):
                if field.get("name") == "VAR" and field.text:
                    var_name = field.text
                    var_id = field.get("id", f"var_{var_name}")
                    used_vars[var_name] = var_id
                    if not field.get("id"):
                        field.set("id", var_id)
                        fixed = True

    if used_vars:
        vars_elem = root.find(f"{ns}variables")
        if vars_elem is None:
            vars_elem = ET.Element(f"{ns}variables")
            root.insert(0, vars_elem)
            fixed = True

        declared = {v.text for v in vars_elem.findall(f"{ns}variable")}
        for name, vid in used_vars.items():
            if name not in declared:
                v = ET.SubElement(vars_elem, f"{ns}variable")
                v.set("id", vid)
                v.text = name
                fixed = True

    if not fixed:
        return xml_str

    return ET.tostring(root, encoding="unicode")


def _find_parent(root, target, ns: str):
    """ElementTree에서 target의 부모 요소를 찾는다."""
    for parent in root.iter():
        for child in parent:
            if child is target:
                return parent
    return None


def _iter_named_imports(code: str):
    """`import { a, b as c, type T } from 'mod'` 구문을 순회하며 (module, [런타임 바인딩 이름])을 산출.
    import type 구문·인라인 `type Foo`는 제외하고, `Foo as Bar`는 실제 바인딩되는 `Bar`를 쓴다."""
    for m in re.finditer(r'import\s*\{([^}]+)\}\s*from\s*[\'"]([^\'"]+)[\'"]', code):
        if re.match(r'import\s+type\s*\{', m.group(0)):
            continue
        names: list[str] = []
        for spec in m.group(1).split(','):
            spec = spec.strip()
            if not spec or spec == 'type' or spec.startswith('type '):
                continue
            names.append(spec.split(' as ')[-1].strip())
        yield m.group(2), names


def validate_generated_code(code_map: dict[str, str], hybrid: bool = False) -> List[str]:
    """생성된 코드 전체를 검사해서 렌더링 에러를 유발하는 문제를 찾아낸다.

    hybrid=True면 미정의 컴포넌트 안내가 '정의/대체'로 바뀐다 — hybrid 런타임은 import가
    금지라 "import 하세요" 안내를 따르면 import 금지 검증과 충돌해 수정 라운드(예산 1)를
    잘못된 방향으로 태운다.
    """
    errors: List[str] = []

    for file_path, code in code_map.items():
        if not any(file_path.endswith(ext) for ext in ('.tsx', '.ts', '.jsx', '.js')):
            continue

        # 0) 같은 import 구문 내 이름 중복 (e.g. { Circle, Circle }) + 전체 import 이름 수집
        #    (모듈 레벨의 진짜 중복 선언은 esbuild build_check가 권위 있게 잡으므로 여기선 생략)
        imported_names: set[str] = set()
        for module, names in _iter_named_imports(code):
            seen: set[str] = set()
            for name in names:
                if name in seen:
                    errors.append(
                        f"[{file_path}] Duplicate import: '{name}'이 "
                        f"'{module}' import에서 여러 번 선언되었습니다. "
                        f"각각 다른 이름으로 교체하세요."
                    )
                    break
                seen.add(name)
            imported_names.update(names)

        # 1) import 이름과 함수/변수 선언 이름 충돌 (Duplicate declaration)
        declared_names: set[str] = set()
        for m in re.finditer(r'(?:export\s+(?:default\s+)?)?(?:function|class)\s+(\w+)', code):
            declared_names.add(m.group(1))
        for m in re.finditer(r'(?:export\s+)?(?:const|let|var)\s+(\w+)\s*[=:]', code):
            declared_names.add(m.group(1))
        for name in imported_names & declared_names:
            errors.append(
                f"[{file_path}] Duplicate declaration: '{name}'이 import와 "
                f"함수/변수 선언에 동시에 존재합니다. import 쪽에 `as {name}Icon` 같은 alias를 사용하세요."
            )

        # 2) import한 모듈이 다른 생성 파일에 존재하는지 확인 (잘못된 경로)
        #    import type은 런타임에 영향 없으므로 제외
        for m in re.finditer(r"import\s+.*?\s+from\s+['\"](\.[^'\"]+)['\"]", code):
            if re.match(r'import\s+type\s', m.group(0)):
                continue
            import_path = m.group(1)
            # ./components/Foo → components/Foo.tsx 등으로 변환해서 확인
            resolved = import_path.lstrip('./')
            candidates = [
                resolved + '.tsx', resolved + '.ts',
                resolved + '.jsx', resolved + '.js',
                resolved + '/index.tsx', resolved + '/index.ts',
            ]
            if not any(c in code_map for c in candidates):
                errors.append(
                    f"[{file_path}] Missing import: '{import_path}'에 해당하는 "
                    f"파일이 생성되지 않았습니다. 해당 파일도 generate_code로 생성하세요."
                )

        # 3) JSX에서 사용하는 컴포넌트가 import 되어 있는지 확인
        #    (?<!\w) : useState<CardType> 등 TS 제네릭은 제외 (단어 뒤 <는 JSX가 아님)
        jsx_tags = set(re.findall(r'(?<!\w)<([A-Z]\w+)', code))
        available_names = imported_names | declared_names | {'React'}
        # default import도 수집
        for m in re.finditer(r"import\s+(\w+)\s+from", code):
            available_names.add(m.group(1))
        missing_tags = jsx_tags - available_names
        if missing_tags:
            for tag in missing_tags:
                if hybrid:
                    errors.append(
                        f"[{file_path}] Undefined component: '<{tag}>'이 사용되었지만 정의되지 않았습니다. "
                        "hybrid에서는 import(lucide 아이콘 포함)가 불가하니 App.tsx 안에서 "
                        "컴포넌트를 직접 정의하거나 이모지/텍스트로 대체하세요."
                    )
                else:
                    errors.append(
                        f"[{file_path}] Undefined component: '<{tag}>'이 사용되었지만 "
                        f"import 되지 않았습니다."
                    )

    return errors


# 폴백용 유효 아이콘 손목록 (자주 쓰이는 것들). 평소엔 _lucide_icon_names()가
# 설치된 lucide-react 패키지에서 전체 목록을 도출해 쓰고, 패키지를 못 읽을 때만 이 목록으로 폴백한다.
_VALID_LUCIDE_ICONS = {
    "Home", "User", "Users", "Settings", "Search", "Plus", "Minus", "X",
    "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown",
    "ChevronLeft", "ChevronRight", "ChevronUp", "ChevronDown",
    "Heart", "Star", "Bell", "BellOff", "Mail", "Phone", "MapPin",
    "Calendar", "Clock", "Eye", "EyeOff", "Send", "Download", "Upload",
    "Edit", "Edit2", "Edit3", "Trash", "Trash2", "Check", "CheckCircle",
    "Filter", "Menu", "LogOut", "LogIn", "CreditCard", "ShoppingCart",
    "ShoppingBag", "Package", "Truck", "Gift", "Bookmark", "BookmarkPlus",
    "Share", "Share2", "Copy", "Clipboard", "ExternalLink", "Link",
    "Image", "Camera", "Film", "Music", "Mic", "Volume2", "VolumeX",
    "Sun", "Moon", "Cloud", "CloudRain", "Zap", "Flame",
    "AlertCircle", "AlertTriangle", "Info", "HelpCircle",
    "Lock", "Unlock", "Shield", "Key",
    "Wifi", "WifiOff", "Bluetooth", "Battery", "Monitor", "Smartphone",
    "Tablet", "Laptop", "Printer", "Cpu", "HardDrive",
    "File", "FileText", "Folder", "FolderOpen", "Save",
    "RefreshCw", "RotateCcw", "Loader", "Loader2",
    "MessageCircle", "MessageSquare", "MessagesSquare",
    "ThumbsUp", "ThumbsDown", "Award", "Trophy", "Target",
    "BarChart", "BarChart2", "LineChart", "PieChart", "TrendingUp", "TrendingDown",
    "Grid", "List", "LayoutGrid", "LayoutList", "Columns",
    "Maximize", "Minimize", "Expand", "Shrink",
    "Move", "GripVertical", "GripHorizontal",
    "Circle", "Square", "Triangle", "Hexagon",
    "Tag", "Tags", "Hash", "AtSign", "Globe", "Navigation",
    "Compass", "Crosshair", "Flag", "Bookmark",
    "MoreHorizontal", "MoreVertical", "Ellipsis",
    "Play", "Pause", "SkipBack", "SkipForward", "FastForward", "Rewind",
    "Power", "ToggleLeft", "ToggleRight",
    "Wallet", "Receipt", "BadgeCheck", "BadgeDollarSign", "PiggyBank",
    "Store", "Building", "Building2", "Warehouse",
    "Car", "Bus", "Train", "Plane", "Ship", "Bike",
    "Utensils", "Coffee", "Wine", "Pizza", "Apple",
    "Shirt", "Watch", "Glasses", "Gem",
    "Code", "Code2", "Terminal", "Bug", "Wrench", "Hammer",
    "Palette", "Paintbrush", "Pen", "PenTool", "Type",
    "Bold", "Italic", "Underline", "AlignLeft", "AlignCenter", "AlignRight",
    "Smile", "Frown", "Meh", "Angry", "PartyPopper",
    "Sparkles", "Wand2", "Lightbulb",
    "ArrowLeftRight", "ArrowUpDown", "MoveHorizontal", "MoveVertical",
    "ChevronFirst", "ChevronLast",
    "CalendarDays", "CalendarCheck", "CalendarX",
    "UserPlus", "UserMinus", "UserCheck", "UserX",
    "FolderPlus", "FolderMinus", "FilePlus", "FileMinus",
    "PlusCircle", "MinusCircle", "XCircle",
    "Scan", "QrCode", "Barcode",
    "Banknote", "Coins", "DollarSign", "Euro", "PoundSterling",
    "History", "Archive", "Inbox", "SendHorizontal",
    "LucideIcon",
    # LLM이 자주 생성하는 아이콘 (누락 방지)
    "Layers", "GitBranch", "BarChart3", "BarChart4",
    "Twitter", "Github", "Linkedin", "Facebook", "Instagram", "Youtube",
    "Rocket", "Feather", "Aperture", "Activity", "Anchor",
    "Box", "Briefcase", "Database", "Server", "Shield",
    "Headphones", "Speaker", "Radio", "Tv",
    "Scissors", "Paperclip", "FileCode", "FileJson",
    "GitCommit", "GitMerge", "GitPullRequest", "GitFork",
    "Workflow", "Cog", "SlidersHorizontal", "Settings2",
    "SquareStack", "Component", "Blocks", "Puzzle",
    "Brain", "Bot", "Sparkle", "Wand",
    "Timer", "Hourglass", "Stopwatch",
    "ShieldCheck", "ShieldAlert", "LockKeyhole",
    "CircleDot", "CircleCheck", "CircleX", "CircleAlert",
    "LayoutDashboard", "PanelLeft", "PanelRight", "SidebarOpen",
}


_LUCIDE_ICONS_CACHE: "set[str] | None" = None


def _lucide_icon_names() -> "set[str]":
    """유효한 lucide-react 아이콘 전체 집합을 반환한다.

    설치된 lucide-react 패키지에서 직접 도출한다(손목록보다 정확·자가유지):
    dist/esm/icons/<kebab>.js → PascalCase 컴포넌트명. 예) circle-check → CircleCheck.
    패키지를 못 읽으면(빌드환경 미설치 등) 위 _VALID_LUCIDE_ICONS 손목록으로 폴백한다.

    손목록 기반이면 목록 밖의 '멀쩡한' 아이콘(예: Webcam)을 무효로 오인해
    Circle로 rename하는 손상이 났는데, 전체 집합을 쓰면 그 오탐이 사라진다.
    """
    global _LUCIDE_ICONS_CACHE
    if _LUCIDE_ICONS_CACHE is not None:
        return _LUCIDE_ICONS_CACHE

    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    icons_dir = os.path.join(
        base, ".build_cache", "node_modules", "lucide-react", "dist", "esm", "icons"
    )
    names: "set[str]" = set()
    try:
        for fn in os.listdir(icons_dir):
            if fn.endswith(".js") and not fn.endswith(".js.map"):
                kebab = fn[:-3]
                names.add("".join(p.capitalize() for p in kebab.split("-")))
    except OSError:
        names = set()

    # 도출 실패 시 손목록으로 폴백
    _LUCIDE_ICONS_CACHE = names or _VALID_LUCIDE_ICONS
    return _LUCIDE_ICONS_CACHE


def fix_unicode_escapes_in_jsx(code: str) -> str:
    """JSX 텍스트에서 \\u{XXXX}, \\uXXXX 유니코드 이스케이프를 실제 문자로 변환.

    JSX 텍스트에서 \\u{270A}는 {270A}가 JSX 표현식으로 해석되어 SyntaxError 발생.
    \\uXXXX도 실제 문자 대신 리터럴 텍스트로 렌더링되므로 함께 변환.
    JS 문자열 내에서도 의미적으로 동일하므로 안전한 변환.
    """
    def _replace_extended(m: re.Match) -> str:
        try:
            return chr(int(m.group(1), 16))
        except (ValueError, OverflowError):
            return m.group(0)

    def _replace_basic(m: re.Match) -> str:
        try:
            return chr(int(m.group(1), 16))
        except (ValueError, OverflowError):
            return m.group(0)

    # \u{XXXX} → 실제 문자 (ES6 extended escape, JSX에서 SyntaxError 유발)
    code = re.sub(r'\\u\{([0-9a-fA-F]+)\}', _replace_extended, code)
    # \uXXXX → 실제 문자 (기본 유니코드 이스케이프)
    code = re.sub(r'\\u([0-9a-fA-F]{4})', _replace_basic, code)
    # 이모지처럼 surrogate pair로 쓰인 이스케이프(😀)는 위 chr()로 변환되며
    # 짝 없는 surrogate 둘로 쪼개진다(😀가 깨져 �·UTF-8 저장 크래시의 원인). utf-16 왕복으로
    # 인접한 surrogate 쌍을 원래 문자로 재결합하고, 짝 없는 surrogate는 replace로 안전 제거.
    code = code.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    return code


def fix_invalid_lucide_icons(code_map: dict) -> dict:
    """lucide-react에 없는 아이콘을 유사한 유효 아이콘으로 자동 교체 (import + JSX 사용처)"""
    fixed = {}
    for path, code in code_map.items():
        if not path.endswith(('.tsx', '.ts', '.jsx', '.js')):
            fixed[path] = code
            continue

        # 먼저 교체할 아이콘 매핑 수집
        renames = {}
        for m in re.finditer(r"import\s*\{([^}]+)\}\s*from\s*['\"]lucide-react['\"]", code):
            for spec in m.group(1).split(','):
                name = spec.strip()
                if not name or ' as ' in name:
                    continue
                if name not in _lucide_icon_names():
                    renames[name] = _find_similar_icon(name)

        if not renames:
            fixed[path] = code
            continue

        # import 교체
        result = code
        for old_name, new_name in renames.items():
            # import 구문 내 교체
            result = re.sub(
                rf'(import\s*\{{[^}}]*)\b{old_name}\b([^}}]*\}}\s*from\s*[\'"]lucide-react[\'"])',
                lambda m: m.group(0).replace(old_name, new_name),
                result
            )
            # 모든 식별자 참조 교체 (import는 위에서, JSX/변수/객체속성 등 전부)
            result = re.sub(rf'\b{re.escape(old_name)}\b', new_name, result)

        # 치환 후 같은 import 구문 내 중복 이름 제거
        def _dedup_import(m: re.Match) -> str:
            names_part = m.group(1)
            from_part = m.group(2)
            names = [n.strip() for n in names_part.split(',') if n.strip()]
            seen: set[str] = set()
            deduped: list[str] = []
            for n in names:
                if n not in seen:
                    seen.add(n)
                    deduped.append(n)
            return f"import {{ {', '.join(deduped)} }} from {from_part}"

        result = re.sub(
            r"import\s*\{([^}]+)\}\s*from\s*(['\"]lucide-react['\"])",
            _dedup_import,
            result,
        )

        fixed[path] = result
    return fixed


def _find_similar_icon(name: str) -> str:
    """잘못된 아이콘 이름에서 가장 비슷한 유효 아이콘을 찾음"""
    # 자주 틀리는 매핑
    common_fixes = {
        "Chip": "Cpu", "Dashboard": "LayoutGrid", "Money": "Banknote",
        "Bank": "Building", "Card": "CreditCard", "Coin": "Coins",
        "Cash": "Banknote", "Food": "Utensils", "Restaurant": "Utensils",
        "Delivery": "Truck", "Order": "ClipboardList", "Cart": "ShoppingCart",
        "Bag": "ShoppingBag", "Notification": "Bell", "Alert": "AlertCircle",
        "Warning": "AlertTriangle", "Close": "X", "Add": "Plus",
        "Remove": "Minus", "Delete": "Trash2", "Left": "ArrowLeft",
        "Right": "ArrowRight", "Up": "ArrowUp", "Down": "ArrowDown",
        "Photo": "Image", "Picture": "Image", "Video": "Film",
        "Like": "ThumbsUp", "Dislike": "ThumbsDown", "Comment": "MessageCircle",
        "Follow": "UserPlus", "Profile": "User", "Account": "User",
        "Avatar": "User", "Location": "MapPin", "Address": "MapPin",
        "Map": "MapPin", "Time": "Clock", "Date": "Calendar",
        "Success": "CheckCircle", "Error": "XCircle", "Chat": "MessageCircle",
        "Message": "MessageCircle", "Login": "LogIn", "Signup": "UserPlus",
        "Register": "UserPlus", "Password": "Lock", "Email": "Mail",
        "Open": "FolderOpen", "Toggle": "ToggleLeft",
        # 2→없는 아이콘 (lucide v0.400+ 이후 이름 변경된 것들)
        "CheckCircle2": "CircleCheck", "XCircle2": "CircleX",
        "AlertCircle2": "CircleAlert", "HelpCircle2": "CircleHelp",
        "ArrowUpCircle": "CircleArrowUp", "ArrowDownCircle": "CircleArrowDown",
        "PlusCircle2": "CirclePlus", "MinusCircle2": "CircleMinus",
        "PlayCircle": "CirclePlay", "StopCircle": "CircleStop",
        "PauseCircle": "CirclePause", "CheckSquare": "SquareCheck",
    }
    if name in common_fixes:
        return common_fixes[name]
    # 매핑에 없으면 Circle로 대체
    return "Circle"


def add_missing_lucide_imports(code_map: dict) -> dict:
    """JSX에서 쓰였지만 import되지 않은 컴포넌트 중 '유효한 lucide 아이콘'인 것을
    lucide-react import에 자동 보강한다 (예: <Circle> 사용 + import 누락 → import에 Circle 추가).

    로컬 컴포넌트 등 lucide가 아닌 누락은 경로를 알 수 없으므로 건드리지 않는다
    (validate_generated_code → _fix_code 루프에 맡긴다)."""
    fixed = {}
    for path, code in code_map.items():
        if not path.endswith(('.tsx', '.ts', '.jsx', '.js')):
            fixed[path] = code
            continue

        # 사용된 JSX 태그 (validate_generated_code의 탐지 규칙과 동일하게 유지)
        jsx_tags = set(re.findall(r'(?<!\w)<([A-Z]\w+)', code))

        # 이미 사용 가능한 이름: named import + default import + 함수/변수 선언
        available: set[str] = {'React'}
        for m in re.finditer(r'import\s*\{([^}]+)\}\s*from', code):
            if re.match(r'import\s+type\s*\{', m.group(0)):
                continue
            for spec in m.group(1).split(','):
                spec = spec.strip()
                if not spec or spec == 'type' or spec.startswith('type '):
                    continue
                # `Foo as Bar` → 실제 사용 가능한 이름은 Bar
                available.add(spec.split(' as ')[-1].strip())
        for m in re.finditer(r"import\s+(\w+)\s+from", code):
            available.add(m.group(1))
        for m in re.finditer(r'(?:export\s+(?:default\s+)?)?(?:function|class)\s+(\w+)', code):
            available.add(m.group(1))
        for m in re.finditer(r'(?:export\s+)?const\s+(\w+)\s*[=:]', code):
            available.add(m.group(1))

        # 누락 태그 중 '유효 lucide 아이콘'만 보강 대상
        missing = sorted((jsx_tags - available) & _lucide_icon_names())
        if not missing:
            fixed[path] = code
            continue

        lucide_re = re.compile(r"import\s*\{([^}]+)\}\s*from\s*(['\"]lucide-react['\"])")
        m = lucide_re.search(code)
        if m:
            existing = [n.strip() for n in m.group(1).split(',') if n.strip()]
            merged = existing + [n for n in missing if n not in existing]
            new_import = f"import {{ {', '.join(merged)} }} from {m.group(2)}"
            result = code[:m.start()] + new_import + code[m.end():]
        else:
            # lucide import가 아예 없으면 첫 import 라인 뒤(없으면 맨 앞)에 새 줄 삽입
            new_line = f"import {{ {', '.join(missing)} }} from 'lucide-react';"
            first_import = re.search(r'(?m)^\s*import\s.*$', code)
            if first_import:
                result = code[:first_import.end()] + "\n" + new_line + code[first_import.end():]
            else:
                result = new_line + "\n" + code
        fixed[path] = result
    return fixed


def fix_lucide_icons(code_map: dict) -> dict:
    """lucide 자동수정 일괄 적용: 무효 아이콘 치환 → 누락 import 보강.
    코드 쓰기/렌더 경로(generate_code·edit_code·스트림 미리보기)에서 동일하게 호출한다."""
    return add_missing_lucide_imports(fix_invalid_lucide_icons(code_map))


# ── JSX 확장자 교정 ──────────────────────────────────────────────
# JSX를 담은 .ts 파일은 번들러(Babel/TS)가 'Unexpected token' SyntaxError를 낸다
# (.ts 에선 JSX 파싱이 꺼져 있고 <Foo> 를 제네릭/비교로 해석). 확장자를 .tsx 로만 바꿔 해결한다.
# 제네릭(useState<Task[]>, Array<Item>)과 구분하려고 '닫는 태그 · self-closing · fragment'만
# JSX 신호로 본다. 여는 태그(<Foo)만 보면 제네릭까지 잡혀 순수 .ts 훅/타입이 오검출된다.
_JSX_MARKER_RE = re.compile(r"</[A-Za-z][\w.]*\s*>|<[A-Za-z][\w.]*(?:\s[^<>]*?)?/>|<>")


def jsx_safe_ext(file_path: str, code: str) -> str:
    """JSX가 든 .ts 파일 경로를 .tsx 로 바꿔 반환. 그 외에는 원본 경로 그대로.

    useTasks.ts 안에 <Context.Provider>…</Context.Provider> 같은 JSX가 있으면 확장자가 .ts라
    파싱 에러가 난다. 확장자만 교정한다. .d.ts(타입 선언)와 이미 .tsx/.js 인 파일은 건드리지 않는다.
    """
    if not file_path.endswith(".ts") or file_path.endswith(".d.ts"):
        return file_path
    if _JSX_MARKER_RE.search(code):
        return file_path[:-len(".ts")] + ".tsx"
    return file_path


def _resolve_code_path(code_map: dict, file_path: str) -> str:
    """요청 경로가 code_map에 없으면 .ts↔.tsx 형제를 찾아 반환.
    (generate_code가 JSX 담긴 .ts를 .tsx로 교정해 키가 바뀐 경우, 이후 edit_code가 옛 경로로 와도 잡음.)"""
    if file_path in code_map:
        return file_path
    if file_path.endswith(".ts") and (alt := file_path[:-len(".ts")] + ".tsx") in code_map:
        return alt
    if file_path.endswith(".tsx") and (alt := file_path[:-len(".tsx")] + ".ts") in code_map:
        return alt
    return file_path
