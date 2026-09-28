"""VeriActAgent — CLI-ReAct entry point (drop-in replacement for VeriActAgent).

Per task it materializes a small workspace (Solution.java placeholder, Test.java,
harness/task.json), runs the CLIAgent ReAct loop (which drives the tool CLI as
subprocesses), and records the same trajectory / memory / monitoring artifacts as
the original VeriAct.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import socket
import sys
import time
from datetime import datetime

from veriact.agent.cli_agent import CLIAgent
from veriact.core.data_types import (
    DEFAULT_MAX_STEPS,
    HARNESS_FEEDBACK_PAIRS,
    HARNESS_PASS_THRESHOLD,
    MAX_TOOL_CALLS,
    TOOL_TIMEOUT_S,
    Task,
)
from veriact.core.file_utility import dump_json
from veriact.core.memory import ActionStep
from veriact.core.models import peek_llm_calls, start_llm_call_log, stop_llm_call_log
from veriact.tools.descriptors import RunHarnessTool, VerifyTool
from veriact.core.utility import AgentMaxStepsError

logger = logging.getLogger(__name__)


def _task_to_record(task: Task) -> dict:
    """Serialize a Task to the dict shape harness_tool.Task.from_dict expects."""
    return {
        "task_id": task.task_id,
        "code": task.code,
        "class_name": task.class_name,
        "test_name": task.test_name,
        "javadoc": task.javadoc,
        "category": task.category,
        "origin_id": task.origin_id,
        "test_code": task.test_code,
        "test_inputs": [{"input": tc.input, "output": tc.output} for tc in task.test_inputs],
        "generated_test_cases": [
            {"input": tc.input, "output": tc.output} for tc in task.generated_test_cases
        ],
        "invalid_inputs": task.invalid_inputs,
    }


class VeriActAgent:
    """CLI-driven verification-guided spec synthesis agent."""

    def __init__(
        self,
        model,
        openjml_path="openjml",
        dataset_path="",
        output_dir="veriact_outputs",
        planning_interval=5,
        max_steps=DEFAULT_MAX_STEPS,
        harness_threshold=HARNESS_PASS_THRESHOLD,
        no_harness: bool = False,
        max_tool_calls: int = MAX_TOOL_CALLS,
        tool_timeout: int = TOOL_TIMEOUT_S,
        harness_max_pairs: int = HARNESS_FEEDBACK_PAIRS,
        _run_dir: str | None = None,
        **kwargs,
    ):
        self.model = model
        self.openjml_path = openjml_path
        self.dataset_path = dataset_path
        self.max_steps = max_steps
        self.planning_interval = planning_interval
        self.harness_threshold = harness_threshold
        self.no_harness = no_harness
        self.max_tool_calls = max_tool_calls
        self.tool_timeout = tool_timeout
        self.harness_max_pairs = harness_max_pairs
        self._kwargs = kwargs

        if _run_dir:
            self.output_dir = _run_dir
        else:
            model_id = getattr(model, "model_id", "unknown") or "unknown"
            safe_model_id = model_id.replace("/", "_")
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.output_dir = os.path.join(
                output_dir, f"veriact__{safe_model_id}__{timestamp}"
            )
        os.makedirs(self.output_dir, exist_ok=True)

    def _setup_workspace(self, task: Task) -> tuple[str, str]:
        safe_id = task.task_id.replace("/", "_").replace("\\", "_")
        ws = os.path.join(self.output_dir, "workspaces", safe_id)
        os.makedirs(os.path.join(ws, "harness"), exist_ok=True)
        # Test.java for the agent / harness; Solution.java seeded with the bare code.
        with open(os.path.join(ws, "Test.java"), "w") as fh:
            fh.write(task.test_code or "")
        with open(os.path.join(ws, "Solution.java"), "w") as fh:
            fh.write(task.code or "")
        task_json = os.path.join(ws, "harness", "task.json")
        with open(task_json, "w") as fh:
            json.dump(_task_to_record(task), fh, indent=2)
        return ws, task_json

    def run(self, task: Task) -> dict:
        self._agent = None
        workspace, task_json = self._setup_workspace(task)
        harness_dir = os.path.join(workspace, "harness")
        # Keep earlier sessions' logs of this workspace (never overwrite).
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S_%f")
        for name in ("llm_calls.jsonl", "tool_calls.jsonl", "run_meta.json"):
            p = os.path.join(harness_dir, name)
            if os.path.exists(p):
                dest = os.path.join(harness_dir, "previous_runs", stamp)
                os.makedirs(dest, exist_ok=True)
                shutil.move(p, os.path.join(dest, name))
        start_llm_call_log(os.path.join(harness_dir, "llm_calls.jsonl"))
        ts_start, t0 = datetime.now().isoformat(timespec="milliseconds"), time.time()
        error = None
        try:
            return self._run_task(task, workspace, task_json)
        except BaseException as exc:  # noqa: BLE001  (also SystemExit from run_single's SIGTERM handler)
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            calls = stop_llm_call_log()
            self._write_run_meta(task, workspace, calls, ts_start, time.time() - t0, error)

    def _usage_totals(self, calls: list[dict]) -> dict:
        keys = ["input_tokens", "cached_input_tokens", "cache_creation_input_tokens",
                "output_tokens", "reasoning_tokens", "total_tokens"]
        tot = {k: sum((c.get("usage") or {}).get(k, 0) or 0 for c in calls) for k in keys}
        tot["llm_calls"] = len(calls)
        tot["llm_latency_s"] = round(sum(c.get("latency_s") or 0 for c in calls), 3)
        return tot

    @staticmethod
    def _tool_call_counts(harness_dir: str) -> dict:
        counts: dict[str, int] = {}
        try:
            for line in open(os.path.join(harness_dir, "tool_calls.jsonl")):
                cmd = json.loads(line).get("command", "?")
                counts[cmd] = counts.get(cmd, 0) + 1
        except (OSError, json.JSONDecodeError):
            pass
        return counts

    def _write_run_meta(self, task, workspace, calls, ts_start, wall_s, error) -> None:
        harness_dir = os.path.join(workspace, "harness")
        ag = getattr(self, "_agent", None)   # the CLIAgent of this run (None if it was never built)

        def sha(path):
            try:
                with open(path, "rb") as fh:
                    return hashlib.sha256(fh.read()).hexdigest()
            except OSError:
                return None

        pkg = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        meta = {
            "task_id": task.task_id,
            "agent": "veriact",
            "model": getattr(self.model, "model_id", None),
            "mode": "no-harness" if self.no_harness else "with-harness",
            "reasoning_effort": getattr(self.model, "reasoning_effort", None),
            "model_config": self._model_config(),
            "max_steps": self.max_steps,
            "max_tool_calls": self.max_tool_calls,
            "tool_timeout_s": self.tool_timeout,
            "harness_max_pairs": self.harness_max_pairs,
            "planning_interval": self.planning_interval,
            "harness_threshold": self.harness_threshold,
            "tool_calls_used": getattr(ag, "tool_calls_used", None),
            "budget_refusals": getattr(ag, "budget_refusals", None),
            "tool_timeouts": getattr(ag, "tool_timeouts", None),
            "host": socket.gethostname(),
            "python": sys.version.split()[0],
            "ts_start": ts_start,
            "ts_end": datetime.now().isoformat(timespec="milliseconds"),
            "wall_s": round(wall_s, 3),
            "error": error,
            "usage": self._usage_totals(calls),
            "tool_calls": self._tool_call_counts(harness_dir),
            "integrity_blocks": getattr(ag, "integrity_blocks", None),
            "step_durations_s": getattr(self, "_last_step_durations", None),
            "tool_sha256": {n: sha(os.path.join(pkg, "tools", n))
                            for n in ("cli.py", "harness_tool.py", "verifier_tool.py")},
            "prompt_sha256": sha(os.path.join(pkg, "prompts", "veriact_cli_no_harness_prompt.yaml"
                                              if self.no_harness else "veriact_cli_prompt.yaml")),
            "submission_exists": os.path.exists(os.path.join(workspace, "submission.json")),
        }
        with open(os.path.join(harness_dir, "run_meta.json"), "w") as fh:
            json.dump(meta, fh, indent=2, default=str)
        self._last_meta = meta

    def _model_config(self) -> dict | None:
        try:
            d = self.model.to_dict()
        except Exception:  # noqa: BLE001
            return None
        return {k: v for k, v in d.items()
                if k not in ("last_input_token_count", "last_output_token_count", "api_key")}

    def _run_task(self, task, workspace, task_json) -> dict:

        # Ablation: no-harness arm exposes only verify (+ task_complete) and uses
        # the harness-free prompt; success = verification passes.
        tools = [VerifyTool()] if self.no_harness else [VerifyTool(), RunHarnessTool()]
        prompt_name = (
            "veriact_cli_no_harness_prompt.yaml"
            if self.no_harness
            else "veriact_cli_prompt.yaml"
        )
        agent = CLIAgent(
            tools=tools,
            model=self.model,
            task_id=task.task_id,
            workspace_dir=workspace,
            task_json=task_json,
            openjml_path=self.openjml_path,
            max_steps=self.max_steps,
            planning_interval=self.planning_interval,
            harness_threshold=self.harness_threshold,
            prompt_name=prompt_name,
            max_tool_calls=self.max_tool_calls,
            tool_timeout=self.tool_timeout,
            harness_max_pairs=self.harness_max_pairs,
            **self._kwargs,
        )
        self._agent = agent  # run_meta reads the budget counters even if the run raises

        task_str = f"task_id: {task.task_id}\n\njava_code:\n{task.code}\n\n"
        result = agent.run(task=task_str)
        last_code = agent.get_last_jml_code()

        action_steps = [
            s for s in agent.memory.steps
            if isinstance(s, ActionStep) and s.step_number is not None
        ]
        hit_max_steps = any(isinstance(s.error, AgentMaxStepsError) for s in action_steps)
        if self.no_harness:
            passed = self._check_verified(action_steps)
        else:
            passed = self._check_harness_passed(action_steps, self.harness_threshold)
        success = result is not None and not hit_max_steps and passed
        iterations = sum(
            1 for s in action_steps if not isinstance(s.error, AgentMaxStepsError)
        )

        self._last_integrity_blocks = getattr(agent, "integrity_blocks", None)
        self._last_tool_calls_used = agent.tool_calls_used
        self._last_budget_refusals = agent.budget_refusals
        self._last_tool_timeouts = agent.tool_timeouts
        self._last_step_durations = list(getattr(getattr(agent, "monitor", None), "step_durations", []) or [])
        trajectory = {
            "task_id": task.task_id,
            "success": success,
            "iterations": iterations,
            "agent_output": result,
            "agent_dict": agent.to_dict(),
            "_last_attempted_code": last_code,
            # Filled in from the per-call log (see _write_run_meta for run_meta.json).
            "usage": self._usage_totals(peek_llm_calls()),
            "integrity_blocks": self._last_integrity_blocks,
            "tool_calls_used": self._last_tool_calls_used,
            "max_tool_calls": self.max_tool_calls,
            "budget_refusals": self._last_budget_refusals,
            "tool_timeouts": self._last_tool_timeouts,
            "reasoning_effort": getattr(self.model, "reasoning_effort", None),
            "step_durations_s": self._last_step_durations,
            "llm_calls_log": os.path.join(workspace, "harness", "llm_calls.jsonl"),
            "tool_calls_log": os.path.join(workspace, "harness", "tool_calls.jsonl"),
        }

        trajectories_dir = os.path.join(self.output_dir, "trajectories")
        os.makedirs(trajectories_dir, exist_ok=True)
        dump_json(
            trajectory,
            os.path.join(trajectories_dir, f"{task.task_id}_veriact_trajectory.json"),
        )
        return trajectory

    @staticmethod
    def _check_harness_passed(action_steps: list[ActionStep], threshold: float) -> bool:
        """True if the last run_spec_harness call met the threshold."""
        for step in reversed(action_steps):
            for tool_out in reversed(step.tool_outputs or []):
                if tool_out.get("tool_name") != "run_spec_harness":
                    continue
                raw = tool_out.get("output", "")
                try:
                    scores = json.loads(raw) if isinstance(raw, str) else raw
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(scores, dict):
                    for _tid, metrics in scores.items():
                        if _tid.startswith("_") or not isinstance(metrics, dict):  # "_budget"
                            continue
                        pc = metrics.get("post_correctness") or 0.0        # None = not evaluable
                        pcm = metrics.get("post_completeness") or 0.0
                        return pc >= threshold and pcm >= threshold
        return False

    @staticmethod
    def _check_verified(action_steps: list[ActionStep]) -> bool:
        """No-harness success: True if the last verify call reported verified=True."""
        for step in reversed(action_steps):
            for tool_out in reversed(step.tool_outputs or []):
                if tool_out.get("tool_name") != "verify":
                    continue
                raw = tool_out.get("output", "")
                try:
                    data = json.loads(raw) if isinstance(raw, str) else raw
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(data, dict):
                    return bool(data.get("verified"))
        return False

    def to_dict(self):
        tools = ["verify", "task_complete"] if self.no_harness else [
            "verify", "run_spec_harness", "task_complete"
        ]
        return {
            "mode": "cli-react" + ("-no-harness" if self.no_harness else ""),
            "model_id": getattr(self.model, "model_id", "?"),
            "tools": tools,
        }
