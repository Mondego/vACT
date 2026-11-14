"""Agent memory: step tracking, message serialization, and vACT attempt tracking."""

from __future__ import annotations
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, TypedDict, Union

from vact.core.models import ChatMessage, MessageRole
from vact.core.monitoring import AgentLogger, LogLevel
from vact.core.utility import AgentError, make_json_serializable

logger = logging.getLogger(__name__)


class Message(TypedDict):
    role: MessageRole
    content: str | list[dict]


@dataclass
class ToolCall:
    name: str
    arguments: Any
    id: str

    def dict(self):
        return {"id": self.id, "type": "function", "function": {"name": self.name, "arguments": make_json_serializable(self.arguments)}}


@dataclass
class MemoryStep:
    def dict(self):
        return asdict(self)

    def to_messages(self, **kwargs) -> List[Dict[str, Any]]:
        raise NotImplementedError


@dataclass
class ActionStep(MemoryStep):
    model_input_messages: List[Message] | None = None
    tool_calls: List[ToolCall] | None = None
    start_time: float | None = None
    end_time: float | None = None
    step_number: int | None = None
    error: AgentError | None = None
    duration: float | None = None
    model_output_message: ChatMessage = None
    model_output: str | None = None
    observations: str | None = None
    action_output: Any = None
    code_action: str | None = None
    thought: str | None = None
    is_action_executed: bool = False
    tool_outputs: List[Dict[str, Any]] | None = None

    def dict(self):
        return {
            "model_input_messages": self.model_input_messages,
            "tool_calls": [tc.dict() for tc in self.tool_calls] if self.tool_calls else [],
            "start_time": self.start_time, "end_time": self.end_time,
            "step": self.step_number,
            "error": self.error.dict() if self.error else None,
            "duration": self.duration,
            "model_output_message": self.model_output_message,
            "model_output": self.model_output,
            "observations": self.observations,
            "action_output": make_json_serializable(self.action_output),
        }

    def to_messages(self, summary_mode=False, show_model_input_messages=False) -> List[Message]:
        msgs = []
        if self.model_input_messages and show_model_input_messages:
            msgs.append(Message(role=MessageRole.SYSTEM, content=self.model_input_messages))
        if self.model_output is not None and not summary_mode:
            msgs.append(Message(role=MessageRole.ASSISTANT, content=[{"type": "text", "text": self.model_output.strip()}]))
        if self.tool_calls is not None:
            msgs.append(Message(role=MessageRole.TOOL_CALL, content=[{"type": "text", "text": str([tc.dict() for tc in self.tool_calls])}]))
        if self.observations is not None:
            msgs.append(Message(role=MessageRole.TOOL_RESPONSE, content=[{"type": "text", "text": self.observations}]))
        if self.error is not None:
            msgs.append(Message(role=MessageRole.TOOL_RESPONSE, content=[{"type": "text", "text": str(self.error)}]))
        return msgs


@dataclass
class PlanningStep(MemoryStep):
    model_input_messages: List[Message] = field(default_factory=list)
    model_output_message: ChatMessage = None
    plan: str = ""

    def to_messages(self, summary_mode=False, **kwargs) -> List[Message]:
        if summary_mode:
            return []
        return [
            Message(role=MessageRole.ASSISTANT, content=[{"type": "text", "text": self.plan.strip()}]),
            Message(role=MessageRole.USER, content=[{"type": "text", "text": "Now go on and execute this plan."}]),
        ]


@dataclass
class TaskStep(MemoryStep):
    task: str = ""
    def to_messages(self, summary_mode=False, **kwargs) -> List[Message]:
        return [Message(role=MessageRole.USER, content=[{"type": "text", "text": f"New task:\n{self.task}"}])]


@dataclass
class SystemPromptStep(MemoryStep):
    system_prompt: str = ""
    def to_messages(self, summary_mode=False, **kwargs) -> List[Message]:
        if summary_mode:
            return []
        return [Message(role=MessageRole.SYSTEM, content=[{"type": "text", "text": self.system_prompt}])]


@dataclass
class TaskCompleteStep(MemoryStep):
    task_complete: Any = None
    def to_messages(self, **kwargs) -> List[Message]:
        return []


# Framework AgentMemory

class AgentMemory:
    def __init__(self, system_prompt: str):
        self.system_prompt = SystemPromptStep(system_prompt=system_prompt)
        self.steps: List[Union[TaskStep, ActionStep, PlanningStep]] = []

    def reset(self):
        self.steps = []

    def get_succinct_steps(self):
        return [{k: v for k, v in s.dict().items() if k != "model_input_messages"} for s in self.steps]

    def get_full_steps(self):
        return [s.dict() for s in self.steps]

    def replay(self, logger: AgentLogger, detailed=False):
        logger.console.log("Replaying agent steps:")
        for step in self.steps:
            if isinstance(step, TaskStep):
                logger.log_task(step.task, "", level=LogLevel.ERROR)
            elif isinstance(step, ActionStep):
                logger.log_rule(f"Step {step.step_number}", level=LogLevel.ERROR)
                if step.model_output:
                    logger.log_markdown(title="Agent output:", content=step.model_output, level=LogLevel.ERROR)
            elif isinstance(step, PlanningStep):
                logger.log_rule("Planning step", level=LogLevel.ERROR)
                if hasattr(step, 'plan'):
                    logger.log_markdown(title="Plan:", content=step.plan, level=LogLevel.ERROR)


