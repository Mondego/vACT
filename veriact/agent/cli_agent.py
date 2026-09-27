"""CLI-ReAct agent — think -> run a CLI tool -> observe its stdout -> repeat.

Replaces VeriAct's CodeAct (Python-code execution) with command-line tool calls.
At each step the model emits a JSON object ``{"thought", "tool", "tool_input"}``;
the agent runs the corresponding subcommand of [cli.py] as a **subprocess** and
feeds the CLI's JSON stdout back as the observation. Memory, monitoring, planning,
and trajectory recording are inherited unchanged from ``MultiStepAgent``.
"""

from __future__ import annotations

import importlib.resources
import inspect
import json
import os
import subprocess
import sys
import time
from collections import deque
from logging import getLogger
from typing import Any, Dict, Generator, Optional, TypedDict

import yaml
from jinja2 import StrictUndefined, Template
from rich.console import Group
from rich.text import Text

from veriact.core.agent_types import AgentType, handle_agent_output_types
from veriact.tools.default_tools import TaskCompletionTool
from veriact.core.memory import (
    ActionStep,
    AgentMemory,
    PlanningStep,
    SystemPromptStep,
    TaskCompleteStep,
    TaskStep,
    ToolCall,
)
from veriact.core.models import MessageRole
from veriact.core.monitoring import GREEN_HEX, AgentLogger, LogLevel, Monitor
from veriact.tools.base import Tool
from veriact.core.utility import (
    AgentError,
    AgentExecutionError,
    AgentGenerationError,
    AgentMaxStepsError,
    AgentParsingError,
    is_valid_name,
    make_json_serializable,
    truncate_content,
)

logger = getLogger(__name__)

try:  # same integrity check as the Spec-Harness evaluation (generated vendored copy)
    from veriact.tools.harness_tool import JavaMethodParser, check_integrity
except Exception:  # pragma: no cover
    JavaMethodParser = check_integrity = None


def integrity_violation(original_code: str, code: str) -> Optional[str]:
    """
    Reason string if `code` changes the class name, the method signature, or the
    method body of `original_code` (JML annotations and braces added around single
    statements are allowed); None if unchanged or if either side cannot be parsed
    (then the call is let through and OpenJML reports real syntax errors).
    """
    if not original_code or check_integrity is None:
        return None
    try:
        parser = JavaMethodParser()
        orig = parser.parse(original_code)
        new = parser.parse(code, orig["method_name"])
        r = check_integrity(original_code, code, orig, new)
    except Exception:
        return None
    if r["class_renamed"] or r["signature_changed"] or r["body_modified"]:
        return r["reason"] or "code differs from the original program"
    return None


# Path to the tool CLI invoked as a subprocess (veriact/tools/cli.py).
_PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # veriact/
CLI_PATH = os.path.join(_PKG_DIR, "tools", "cli.py")

def populate_template(template: str, variables: Dict[str, Any]) -> str:
    return Template(template, undefined=StrictUndefined).render(**variables)


class PlanningPromptTemplate(TypedDict):
    initial_plan: str
    update_plan_pre_messages: str
    update_plan_post_messages: str


class TaskCompletePromptTemplate(TypedDict):
    pre_messages: str
    post_messages: str


class PromptTemplates(TypedDict):
    system_prompt: str
    planning: PlanningPromptTemplate
    task_complete: TaskCompletePromptTemplate


EMPTY_PROMPT_TEMPLATES = PromptTemplates(
    system_prompt="",
    planning=PlanningPromptTemplate(
        initial_plan="",
        update_plan_pre_messages="",
        update_plan_post_messages="",
    ),
    task_complete=TaskCompletePromptTemplate(pre_messages="", post_messages=""),
)


# ============================================================
# MultiStepAgent — generic ReAct loop (memory / monitoring / planning)
# ============================================================

class MultiStepAgent:
    """ReAct-style agent: think -> act -> observe -> repeat."""

    def __init__(
        self,
        tools,
        model,
        prompt_templates=None,
        max_steps=11,
        verbosity_level=LogLevel.INFO,
        grammar=None,
        step_callbacks=None,
        planning_interval=None,
        name=None,
        description=None,
        task_complete_checks=None,
        harness_threshold=0.5,
    ):
        self.agent_name = self.__class__.__name__
        self.model = model
        self.prompt_templates = prompt_templates or EMPTY_PROMPT_TEMPLATES
        self.max_steps = max_steps
        self.step_number = 0
        self.grammar = grammar
        self.planning_interval = planning_interval
        self.state = {}
        self.name = name if (name is None or is_valid_name(name)) else None
        self.description = description
        self.task_complete_checks = task_complete_checks
        self.harness_threshold = harness_threshold

        assert all(isinstance(t, Tool) for t in tools)
        self.tools = {t.name: t for t in tools}
        self.tools.setdefault("task_complete", TaskCompletionTool())

        self.task = None
        self.system_prompt = self.initialize_system_prompt()
        self.memory = AgentMemory(self.system_prompt)
        self.logger = AgentLogger(level=verbosity_level)
        self.monitor = Monitor(self.model, self.logger)
        self.step_callbacks = list(step_callbacks or [])
        self.step_callbacks.append(self.monitor.update_metrics)

    def run(self, task, stream=False, reset=True, additional_args=None, max_steps=None):
        max_steps = max_steps or self.max_steps
        self.task = task
        self.interrupt_switch = False
        if additional_args:
            self.state.update(additional_args)
            self.task += f"\nAdditional args: {additional_args}"

        self.system_prompt = self.initialize_system_prompt(task=self.task)
        self.memory.system_prompt = SystemPromptStep(system_prompt=self.system_prompt)
        if reset:
            self.memory.reset()
            self.monitor.reset()

        self.logger.log_task(
            content=self.task.strip(),
            subtitle=f"{type(self.model).__name__} - {getattr(self.model, 'model_id', '')}",
            level=LogLevel.INFO,
            title=getattr(self, "name", None),
        )
        self.memory.steps.append(TaskStep(task=self.task))

        if stream:
            return self._run(task=self.task, max_steps=max_steps)
        return deque(self._run(task=self.task, max_steps=max_steps), maxlen=1)[0].task_complete

    def _run(self, task, max_steps) -> Generator[ActionStep | AgentType, None, None]:
        task_complete = None
        self.step_number = 1
        step_start_time = time.time()
        while task_complete is None and self.step_number <= max_steps:
            if self.interrupt_switch:
                raise AgentError("Agent interrupted.", self.logger)
            step_start_time = time.time()
            if self.planning_interval and (
                self.step_number == 1
                or (self.step_number - 1) % self.planning_interval == 0
            ):
                ps = self._create_planning_step(
                    task, is_first_step=(self.step_number == 1), step=self.step_number
                )
                self.memory.steps.append(ps)
                yield ps
            action_step = ActionStep(
                step_number=self.step_number, start_time=step_start_time
            )
            try:
                task_complete = self._execute_step(task, action_step)
            except AgentGenerationError:
                raise
            except AgentError as e:
                action_step.error = e
            finally:
                action_step.end_time = time.time()
                action_step.duration = action_step.end_time - step_start_time
                for cb in self.step_callbacks:
                    (
                        cb(action_step)
                        if len(inspect.signature(cb).parameters) == 1
                        else cb(action_step, agent=self)
                    )
                self.memory.steps.append(action_step)
                yield action_step
                self.step_number += 1

        if task_complete is None and self.step_number == max_steps + 1:
            task_complete = self.provide_task_complete(task)
            final = ActionStep(
                step_number=self.step_number,
                error=AgentMaxStepsError("Reached max steps.", self.logger),
            )
            final.action_output = task_complete
            final.end_time = time.time()
            final.duration = final.end_time - step_start_time
            self.memory.steps.append(final)
            for cb in self.step_callbacks:
                (
                    cb(final)
                    if len(inspect.signature(cb).parameters) == 1
                    else cb(final, agent=self)
                )
            yield final
        yield TaskCompleteStep(task_complete=handle_agent_output_types(task_complete))

    def _execute_step(self, task, memory_step):
        self.logger.log_rule(f"Step {self.step_number}", level=LogLevel.INFO)
        tc = self.step(memory_step)
        if tc is not None and self.task_complete_checks:
            for check in self.task_complete_checks:
                try:
                    assert check(tc, self.memory)
                except Exception as e:
                    raise AgentError(f"Check {check.__name__} failed: {e}", self.logger)
        return tc

    def _create_planning_step(self, task, is_first_step, step):
        if is_first_step:
            input_messages = [
                {
                    "role": MessageRole.USER,
                    "content": [
                        {
                            "type": "text",
                            "text": populate_template(
                                self.prompt_templates["planning"]["initial_plan"],
                                variables={"task": task, "tools": self.tools},
                            ),
                        }
                    ],
                }
            ]
            plan_message = self.model(input_messages, stop_sequences=["<end_plan>"])
            plan = f"Here are the facts and plan:\n```\n{plan_message.content}\n```"
        else:
            memory_messages = self.write_memory_to_messages(summary_mode=True)
            pre = {
                "role": MessageRole.SYSTEM,
                "content": [
                    {
                        "type": "text",
                        "text": populate_template(
                            self.prompt_templates["planning"]["update_plan_pre_messages"],
                            variables={"task": task},
                        ),
                    }
                ],
            }
            post = {
                "role": MessageRole.USER,
                "content": [
                    {
                        "type": "text",
                        "text": populate_template(
                            self.prompt_templates["planning"]["update_plan_post_messages"],
                            variables={
                                "task": task,
                                "tools": self.tools,
                                "remaining_steps": self.max_steps - step,
                            },
                        ),
                    }
                ],
            }
            input_messages = [pre] + memory_messages + [post]
            plan_message = self.model(input_messages, stop_sequences=["<end_plan>"])
            plan = f"Updated plan:\n```\n{plan_message.content}\n```"

        return PlanningStep(
            model_input_messages=input_messages,
            plan=plan,
            model_output_message=plan_message,
        )

    def initialize_system_prompt(self, task=None):
        if self.prompt_templates.get("system_prompt"):
            task_value = task if task is not None else getattr(self, "task", "")
            return populate_template(
                self.prompt_templates["system_prompt"],
                variables={
                    "tools": self.tools,
                    "task": task_value,
                    "harness_threshold": self.harness_threshold,
                },
            )
        return ""

    def write_memory_to_messages(self, summary_mode=False):
        messages = self.memory.system_prompt.to_messages(summary_mode=summary_mode)
        for step in self.memory.steps:
            messages.extend(step.to_messages(summary_mode=summary_mode))
        return messages

    def provide_task_complete(self, task):
        pre = self.prompt_templates.get("task_complete", {}).get("pre_messages", "")
        messages = self.write_memory_to_messages(summary_mode=True)
        if pre:
            messages = [
                {"role": MessageRole.SYSTEM, "content": [{"type": "text", "text": pre}]}
            ] + messages
        try:
            return self.model(messages).content
        except Exception as e:
            return f"Error generating final output: {e}"

    def step(self, memory_step):
        raise NotImplementedError

    def interrupt(self):
        self.interrupt_switch = True

    def to_dict(self):
        return {
            "provided_tools": [t.name for t in self.tools.values()],
            "model": {
                "class": self.model.__class__.__name__,
                "data": self.model.to_dict(),
            },
            "prompt_templates": self.prompt_templates,
            "max_steps": self.max_steps,
            "planning_interval": self.planning_interval,
            "name": self.name,
            "description": self.description,
        }


# ============================================================
# CLIAgent — emits {thought, tool, tool_input}; runs the tool CLI as a subprocess
# ============================================================

class CLIAgent(MultiStepAgent):
    """Agent that drives the verify / run_spec_harness / task_complete CLI tools."""

    def __init__(
        self,
        tools,
        model,
        task_id: str,
        workspace_dir: str,
        task_json: str,
        openjml_path: str = "openjml",
        cli_path: str = CLI_PATH,
        tool_timeout: int = 600,
        prompt_templates=None,
        prompt_name: str = "veriact_cli_prompt.yaml",
        planning_interval=None,
        harness_threshold=0.5,
        **kwargs,
    ):
        if prompt_templates is None:
            try:
                prompt_templates = yaml.safe_load(
                    importlib.resources.files("veriact.prompts")
                    .joinpath(prompt_name)
                    .read_text()
                )
            except Exception:
                prompt_templates = EMPTY_PROMPT_TEMPLATES

        self.task_id = task_id
        self.workspace_dir = workspace_dir
        self.task_json = task_json
        self.openjml_path = openjml_path
        self.cli_path = cli_path
        self.tool_timeout = tool_timeout
        self._last_code: Optional[str] = None
        self._solution_path = os.path.join(workspace_dir, "Solution.java")
        # Original program, used to reject tool calls that rewrite the code.
        self._original_code = ""
        try:
            with open(task_json) as fh:
                self._original_code = json.load(fh).get("code", "")
        except Exception:
            self._original_code = ""
        self.integrity_blocks = 0

        super().__init__(
            tools=tools,
            model=model,
            prompt_templates=prompt_templates,
            planning_interval=planning_interval,
            harness_threshold=harness_threshold,
            **kwargs,
        )

    # ---- response parsing ---------------------------------------------

    @staticmethod
    def _parse_json_response(text: str) -> dict:
        text = text.strip()
        start = text.find("{")
        if start == -1:
            raise ValueError("No JSON object found in response")
        obj, _ = json.JSONDecoder().raw_decode(text, start)
        if not isinstance(obj, dict):
            raise ValueError(f"Expected a JSON object, got {type(obj).__name__}")
        return obj

    # ---- CLI tool execution -------------------------------------------

    def _run_cli(self, cmd: list[str]) -> str:
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.tool_timeout
            )
            out = (proc.stdout or "").strip()
            return out if out else (proc.stderr or "").strip()
        except subprocess.TimeoutExpired:
            return json.dumps({"error": "tool timed out", "timeout": self.tool_timeout})
        except Exception as e:  # noqa: BLE001
            return json.dumps({"error": f"tool execution failed: {e}"})

    def _execute_tool(self, tool_name: str, tool_input: dict) -> tuple[str, bool, dict]:
        """Run a tool as a CLI subprocess. Returns (observation, is_complete, record)."""
        # Reject tools not provided to this agent (e.g. run_spec_harness in the
        # no-harness ablation arm) so the model is told to use an available one.
        if tool_name not in self.tools:
            available = ", ".join(self.tools.keys())
            obs = json.dumps(
                {"error": f"tool '{tool_name}' is not available", "available_tools": available}
            )
            return obs, False, {"tool_name": tool_name, "args": tool_input, "output": obs}

        if tool_name == "task_complete":
            summary = tool_input.get("summary") or tool_input.get("answer") or ""
            out_path = os.path.join(self.workspace_dir, "submission.json")
            cmd = [sys.executable, self.cli_path, "submit", summary, "--out", out_path]
            if self._last_code is not None:
                cmd += ["--code", self._solution_path]
            observation = self._run_cli(cmd)
            return observation, True, {
                "tool_name": "task_complete",
                "args": {"summary": summary},
                "output": summary,
            }

        code = tool_input.get("jml_annotated_code", "")
        if not code:
            obs = json.dumps({"error": "tool_input.jml_annotated_code is required"})
            return obs, False, {"tool_name": tool_name, "args": tool_input, "output": obs}
        # Reject code that changes the program (class, signature, or method body)
        # before anything is written or run; Solution.java keeps the last valid code,
        # so task_complete can never submit a rewritten program.
        violation = integrity_violation(self._original_code, code)
        if violation:
            self.integrity_blocks += 1
            obs = json.dumps({
                "error": "code_modified",
                "detail": violation,
                "instruction": ("Do not change the program. Keep the class name, the method "
                                "signature, and every statement of the method body exactly as in "
                                "the original; only add JML annotations (requires/ensures/"
                                "loop_invariant/assignable/decreases). The tool was not run."),
            })
            return obs, False, {"tool_name": tool_name, "args": {"task_id": self.task_id},
                                "output": obs, "integrity_blocked": True}

        # persist the latest code so verify/harness read the same file
        with open(self._solution_path, "w") as fh:
            fh.write(code)
        self._last_code = code

        if tool_name == "verify":
            cmd = [
                sys.executable, self.cli_path, "verify",
                "--code", self._solution_path,
                "--openjml", self.openjml_path,
                "--output-dir", os.path.join(self.workspace_dir, "tmp"),
            ]
        elif tool_name == "run_spec_harness":
            cmd = [
                sys.executable, self.cli_path, "harness",
                "--code", self._solution_path,
                "--task-json", self.task_json,
                "--openjml", self.openjml_path,
                "--output-dir", os.path.join(self.workspace_dir, "harness"),
                "--run-id", f"step_{self.step_number}",
            ]
        else:
            obs = json.dumps({"error": f"unknown tool '{tool_name}'"})
            return obs, False, {"tool_name": tool_name, "args": tool_input, "output": obs}

        observation = self._run_cli(cmd)
        return observation, False, {
            "tool_name": tool_name,
            "args": {"task_id": self.task_id},
            "output": observation,
        }

    # ---- one ReAct step -----------------------------------------------

    def step(self, memory_step):
        memory_messages = self.write_memory_to_messages()
        memory_step.model_input_messages = memory_messages.copy()
        # No response_format / function-calling: the model emits the JSON object
        # described in the system prompt; we parse it and invoke the CLI ourselves.
        try:
            chat_message = self.model(
                memory_messages,
                stop_sequences=["observation:"],
            )
            memory_step.model_output_message = chat_message
            memory_step.model_output = chat_message.content
        except Exception as e:
            raise AgentGenerationError(f"Error generating model output:\n{e}", self.logger) from e

        self.logger.log_markdown(
            content=memory_step.model_output or "", title="LLM Output:", level=LogLevel.DEBUG
        )

        try:
            parsed = self._parse_json_response(memory_step.model_output)
            tool_name = parsed["tool"]
            tool_input = parsed.get("tool_input", {}) or {}
            memory_step.thought = parsed.get("thought", "")
        except Exception as e:
            raise AgentParsingError(
                f"Error parsing action:\n{e}\nRaw output:\n{(memory_step.model_output or '')[:500]}",
                self.logger,
            )

        # record the JML code for trajectory / last-code retrieval
        memory_step.code_action = tool_input.get("jml_annotated_code", "") or memory_step.code_action
        memory_step.tool_calls = [
            ToolCall(
                name=tool_name,
                arguments=tool_input,
                id=f"call_{len(self.memory.steps)}",
            )
        ]
        self.logger.log(
            Text(f"-> {tool_name}({', '.join(tool_input.keys())})"), level=LogLevel.INFO
        )

        try:
            observation, is_complete, record = self._execute_tool(tool_name, tool_input)
            memory_step.is_action_executed = True
        except Exception as e:
            memory_step.is_action_executed = False
            raise AgentExecutionError(str(e), self.logger)

        memory_step.observations = truncate_content(observation)
        memory_step.tool_outputs = [record]
        memory_step.action_output = observation

        self.logger.log(
            Group(
                Text(
                    f"{'Task Completed - ' if is_complete else 'Observation'}:\n{memory_step.observations}",
                    style=f"bold {GREEN_HEX}" if is_complete else "",
                )
            ),
            level=LogLevel.INFO,
        )
        return observation if is_complete else None

    # ---- helpers used by the wrapper ----------------------------------

    def get_last_jml_code(self) -> Optional[str]:
        return self._last_code

    def to_dict(self):
        d = super().to_dict()
        d["trajectories"] = self._generate_trajectory()
        return d

    def _generate_trajectory(self):
        task_steps = [s for s in self.memory.steps if isinstance(s, TaskStep)]
        planning_steps = [s for s in self.memory.steps if isinstance(s, PlanningStep)]
        action_steps = [s for s in self.memory.steps if isinstance(s, ActionStep)]
        steps = []
        for step in action_steps:
            tool_calls = step.tool_calls or []
            invoked = [tc.name for tc in tool_calls]
            steps.append(
                {
                    "step_no": step.step_number,
                    "thought": step.thought,
                    "tool_calls": [tc.dict() for tc in tool_calls],
                    "code": step.code_action,
                    "observations": step.observations,
                    "tool_outputs": make_json_serializable(step.tool_outputs or []),
                    "invoked_tools": invoked,
                    "is_tool_executed": step.is_action_executed,
                }
            )
        return {
            "task": [t.task for t in task_steps if t.task],
            "plan": [p.plan for p in planning_steps if p.plan],
            "steps": steps,
        }
