from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, Field


class ModuleInputField(BaseModel):
    key: str
    label: str
    type: Literal["text", "textarea", "number", "file_path", "dir_path", "password"] = "text"
    required: bool = True
    placeholder: str = ""
    default: str | int | float | None = None
    help_text: str = ""


class ModuleDefinition(BaseModel):
    id: str
    name: str
    description: str = ""
    executable: str
    working_dir: str = "."
    config_mode: Literal["none", "json_file"] = "none"
    command_template: list[str] = Field(default_factory=list)
    inputs: list[ModuleInputField] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    tool_type: str = "cloud"
    enabled: bool = True


class ModuleRunRequest(BaseModel):
    module_id: str
    inputs: dict[str, Any] = Field(default_factory=dict)


class WorkflowStep(BaseModel):
    module_id: str
    inputs: dict[str, Any] = Field(default_factory=dict)


class WorkflowRunRequest(BaseModel):
    name: str = "workflow"
    mode: Literal["sequential", "parallel"] = "sequential"
    steps: list[WorkflowStep]


class TaskInfo(BaseModel):
    id: str
    module_id: str
    module_name: str
    kind: Literal["module", "workflow"] = "module"
    status: Literal["queued", "running", "success", "failed", "cancelled"] = "queued"
    created_at: str
    started_at: str | None = None
    ended_at: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    command: list[str] = Field(default_factory=list)
    logs: list[str] = Field(default_factory=list)
    return_code: int | None = None
    pid: int | None = None
    minimized: bool = False
    children: list[str] = Field(default_factory=list)
