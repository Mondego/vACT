"""
run_single.py — Run VeriAct sequentially on all tasks in a benchmark dataset.

Usage:
    python run_single.py \
        --benchmark benchmarks/specgenbench/sgb.json \
        --model gpt-4o \
        --output-dir veriact_outputs \
        --openjml-path openjml \
        --max-steps 20 --max-tool-calls 10 --tool-timeout 1800 \
        --reasoning-effort medium \
        --planning_interval 5

Exits with status 1 if any task failed (exception, API error, agent crash);
the failures are also written to <output-dir>/failed_tasks.json.
"""

import argparse
import json
import logging
import os
import signal
import sys

from veriact import (
    AnthropicModel,
    GeminiModel,
    OpenAIServerModel,
    VeriActAgent,
    VLLMModel,
)
from veriact.core.data_types import (
    DEFAULT_MAX_STEPS,
    DEFAULT_REASONING_EFFORT,
    HARNESS_FEEDBACK_PAIRS,
    MAX_TOOL_CALLS,
    TOOL_TIMEOUT_S,
    Task,
)
from veriact.core.file_utility import dump_json, load_json, load_jsonl
import veriact.config

logger = logging.getLogger(__name__)

MODEL_PREFIX_TO_PROVIDER = {
    "gpt": "openai",
    "o1": "openai",
    "o3": "openai",
    "o4": "openai",
    "claude": "anthropic",
    "gemini": "gemini",
}


def detect_provider(model_id: str) -> str:
    model_lower = model_id.lower()
    for prefix, provider in MODEL_PREFIX_TO_PROVIDER.items():
        if model_lower.startswith(prefix):
            return provider
    raise ValueError(
        f"Cannot detect provider from model name '{model_id}'. "
        f"Known prefixes: {list(MODEL_PREFIX_TO_PROVIDER.keys())}. "
        f"For vLLM models, set VLLM_API_BASE env var."
    )


def create_model(model_id: str, reasoning_effort: str | None = DEFAULT_REASONING_EFFORT):
    """reasoning_effort is sent with every OpenAI request (None/"none" = not sent); other backends
    do not take it (a warning is logged)."""
    if reasoning_effort in ("", "none", "None"):
        reasoning_effort = None
    vllm_base = os.getenv("VLLM_API_BASE")
    provider = None if vllm_base else detect_provider(model_id)
    if reasoning_effort is not None and provider != "openai":
        logger.warning(f"--reasoning-effort {reasoning_effort} ignored for {model_id} (OpenAI models only)")
    if vllm_base:
        return VLLMModel(model_id=model_id, api_base=vllm_base)

    if provider == "openai":
        return OpenAIServerModel(model_id=model_id, reasoning_effort=reasoning_effort)
    elif provider == "anthropic":
        return AnthropicModel(model_id=model_id)
    elif provider == "gemini":
        return GeminiModel(model_id=model_id)


def load_benchmark(path: str) -> list[dict]:
    if path.endswith(".jsonl"):
        return load_jsonl(path)
    return load_json(path)


def main():
    parser = argparse.ArgumentParser(description="Run VeriAct on a single task.")
    parser.add_argument("--benchmark", required=True, help="Path to benchmark dataset (.json or .jsonl)")
    parser.add_argument("--model", default="gpt-4o", help="Model ID (default: gpt-4o)")
    parser.add_argument("--output-dir", default="veriact_outputs", help="Output directory (default: veriact_outputs)")
    parser.add_argument("--openjml-path", default="openjml", help="Path to OpenJML binary (default: openjml)")
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS, help="Max LLM action steps per task (default: %(default)s; large enough for the 10 tool calls + task_complete + unparseable outputs)")
    parser.add_argument("--max-tool-calls", type=int, default=MAX_TOOL_CALLS, help="Budget of verify + run_spec_harness calls per task, rejected calls included (default: %(default)s, as the Claude Code / Codex tools)")
    parser.add_argument("--tool-timeout", type=int, default=TOOL_TIMEOUT_S, help="Outer timeout of one tool call in seconds; the tool and its OpenJML processes are killed (default: %(default)s)")
    parser.add_argument("--harness-max-pairs", type=int, default=HARNESS_FEEDBACK_PAIRS, help="Feedback pairs scored by run_spec_harness (default: %(default)s)")
    parser.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT, help="reasoning_effort sent to OpenAI models (default: %(default)s; 'none' = not sent)")
    parser.add_argument("--planning_interval", type=int, default=5, help="Planning interval (default: 5)")
    parser.add_argument("--no-harness", action="store_true", help="Ablation: run with verify + task_complete only (no run_spec_harness); success = verification passes")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # SIGTERM / SIGHUP -> SystemExit, so a running tool CLI and its OpenJML processes are killed
    # (CLIAgent._run_cli) instead of being orphaned in their own sessions.
    def _stop(signum, frame):
        raise SystemExit(128 + signum)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _stop)

    raw_tasks = load_benchmark(args.benchmark)
    tasks = [Task.from_dict(t) for t in raw_tasks]
    logger.info(f"Loaded {len(tasks)} tasks from {args.benchmark}")

    os.makedirs(args.output_dir, exist_ok=True)
    trajectory_jsonl_path = os.path.join(args.output_dir, "trajectories.jsonl")

    completed = []
    failed = []

    for task in tasks:
        print(f"\n{'='*55}")
        print(f"  Task: {task.task_id}  ({task.class_name})")
        print(f"{'='*55}\n")

        try:
            model = create_model(args.model, args.reasoning_effort)
            agent = VeriActAgent(
                model=model,
                openjml_path=args.openjml_path,
                dataset_path=args.benchmark,
                output_dir=args.output_dir,
                max_steps=args.max_steps,
                planning_interval=args.planning_interval,
                no_harness=args.no_harness,
                max_tool_calls=args.max_tool_calls,
                tool_timeout=args.tool_timeout,
                harness_max_pairs=args.harness_max_pairs,
            )
            result = agent.run(task)
            completed.append(task.task_id)
            with open(trajectory_jsonl_path, "a") as f:
                f.write(json.dumps(result) + "\n")
            print(f"  Success:    {result['success']}")
            print(f"  Iterations: {result['iterations']}")
            if result.get("best_spec"):
                print(f"  requires  {result['best_spec'].get('precondition')}")
                print(f"  ensures   {result['best_spec'].get('postcondition')}")
        except Exception:
            import traceback
            tb = traceback.format_exc()
            failed.append({"task_id": task.task_id, "error": tb})
            logger.error(f"[FAIL] {task.task_id}\n{tb}")

    if failed:
        dump_json(failed, os.path.join(args.output_dir, "failed_tasks.json"))
        logger.warning(f"{len(failed)} task(s) failed. See {args.output_dir}/failed_tasks.json")

    logger.info(f"Complete: {len(completed)} succeeded, {len(failed)} failed out of {len(tasks)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
