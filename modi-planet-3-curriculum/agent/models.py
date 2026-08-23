from __future__ import annotations

from enum import Enum
from typing import List
from pydantic import BaseModel, Field


class Phase(str, Enum):
    DESIGN = "design"
    IMPLEMENT = "implement"
    VERIFY = "verify"


class Component(BaseModel):
    name: str
    description: str
    children: List[str] = Field(default_factory=list)


class DiagramData(BaseModel):
    mermaid_code: str = ""
    components: list[Component] = Field(default_factory=list)
    diagram_type: str = "flowchart"


class GeneratedFile(BaseModel):
    path: str
    description: str
    language: str = ""


class Message(BaseModel):
    role: str  # "user" or "assistant"
    content: str


class Feature(BaseModel):
    name: str
    description: str = ""
    priority: str = "mvp"  # "mvp" | "nice_to_have" | "future"


class Page(BaseModel):
    name: str
    description: str = ""
    components: List[str] = Field(default_factory=list)


class DataModel(BaseModel):
    name: str
    fields: List[str] = Field(default_factory=list)
    description: str = ""


class DesignDoc(BaseModel):
    project_name: str = ""
    description: str = ""
    users: List[str] = Field(default_factory=list)
    features: List[Feature] = Field(default_factory=list)
    pages: List[Page] = Field(default_factory=list)
    data_models: List[DataModel] = Field(default_factory=list)
    user_flows: List[str] = Field(default_factory=list)
    strengths: List[str] = Field(default_factory=list)
    weaknesses: List[str] = Field(default_factory=list)


class Task(BaseModel):
    id: int
    name: str
    description: str = ""
    files: List[str] = Field(default_factory=list)
    status: str = "pending"  # "pending" | "in_progress" | "done"


class TaskPlan(BaseModel):
    tasks: List[Task] = Field(default_factory=list)

    def next_task(self):
        for t in self.tasks:
            if t.status == "pending":
                return t
        return None

    def complete_task(self, task_id: int):
        for t in self.tasks:
            if t.id == task_id:
                t.status = "done"
                return t
        return None

    def all_done(self) -> bool:
        return all(t.status == "done" for t in self.tasks) and len(self.tasks) > 0

    def progress_summary(self) -> str:
        done = sum(1 for t in self.tasks if t.status == "done")
        total = len(self.tasks)
        lines = []
        for t in self.tasks:
            mark = "v" if t.status == "done" else (" " if t.status == "pending" else ">")
            lines.append(f"[{mark}] {t.id}. {t.name}")
        return f"진행: {done}/{total}\n" + "\n".join(lines)


class ProjectState(BaseModel):
    phase: Phase = Phase.DESIGN
    project_name: str = ""
    project_description: str = ""
    tech_stack: str = ""
    diagram: DiagramData = Field(default_factory=DiagramData)
    design_doc: DesignDoc = Field(default_factory=DesignDoc)
    task_plan: TaskPlan = Field(default_factory=TaskPlan)
    generated_files: List[GeneratedFile] = Field(default_factory=list)
    conversation_history: List[Message] = Field(default_factory=list)
    design_decisions: List[str] = Field(default_factory=list)
