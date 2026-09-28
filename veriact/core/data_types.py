"""VeriAct domain types: dataclasses and constants."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


HARNESS_PASS_THRESHOLD = 0.75

# RQ4 agent-tool settings, identical for VeriAct and the Claude Code / Codex tools:
MAX_TOOL_CALLS = 10          # verify + run_spec_harness calls per task (rejected calls count too)
HARNESS_FEEDBACK_PAIRS = 5   # run_spec_harness scores the task's first 5 feedback pairs (+ all its invalid inputs)
TOOL_TIMEOUT_S = 1800        # outer timeout of one tool call (the tool process and its OpenJML groups are killed)
DEFAULT_MAX_STEPS = 20       # LLM steps: room for the 10 tool calls + task_complete + unparseable outputs
DEFAULT_REASONING_EFFORT = "medium"   # gpt-5.4 at effort medium, as Codex runs it


@dataclass
class TestCase:
    input: str
    output: str

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "TestCase":
        return cls(input=data["input"], output=data["output"])


@dataclass
class Task:
    task_id: str
    code: str
    class_name: str
    test_name: str
    javadoc: str
    category: str
    origin_id: str
    test_code: str = ""
    test_inputs: list[TestCase] = field(default_factory=list)
    generated_test_cases: list[TestCase] = field(default_factory=list)
    invalid_inputs: list[dict] | None = None      # validated T- (testset T6); None -> heuristic

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        return cls(
            task_id=data["task_id"],
            code=data["code"],
            class_name=data["class_name"],
            test_name=data["test_name"],
            javadoc=data.get("javadoc", ""),
            category=data.get("category", ""),
            origin_id=data.get("origin_id", ""),
            test_code=data.get("test_code", ""),
            test_inputs=[TestCase.from_dict(tc) for tc in data.get("test_inputs", [])],
            generated_test_cases=[
                TestCase.from_dict(tc) for tc in data.get("generated_test_cases", [])
            ],
            invalid_inputs=data.get("invalid_inputs"),
        )
