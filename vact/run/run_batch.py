"""
batch_usage.py — Run vACT agents in parallel across a benchmark dataset.

Usage:
    python run_batch.py \
        --benchmark benchmarks/specgenbench/sgb.json \
        --threads 4 \
        --model gpt-4o \
        --output-dir vact_outputs \
        --openjml-path openjml \
        --max-steps 11 \
        --planning_interval 3
"""

import argparse
import json
import logging
import os
import threading
import traceback
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from vact import (
    AnthropicModel,
    GeminiModel,
    OpenAIServerModel,
    vACTAgent,
    VLLMModel,
)
from vact.core.data_types import Task
from vact.core.file_utility import dump_json, load_json, load_jsonl
import vact.config

logger = logging.getLogger(__name__)

# Provider detection based on model name prefix
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


def create_model(model_id: str):
    # vLLM override: if VLLM_API_BASE is set, use VLLMModel regardless of name
    vllm_base = os.getenv("VLLM_API_BASE")
    if vllm_base:
        return VLLMModel(model_id=model_id, api_base=vllm_base)

    provider = detect_provider(model_id)
    if provider == "openai":
        return OpenAIServerModel(model_id=model_id)
    elif provider == "anthropic":
        return AnthropicModel(model_id=model_id)
    elif provider == "gemini":
        return GeminiModel(model_id=model_id)


def load_benchmark(path: str) -> list[dict]:
    if path.endswith(".jsonl"):
        return load_jsonl(path)
    return load_json(path)


def run_single_task(
    task: Task,
    model,
    openjml_path: str,
    dataset_path: str,
    run_dir: str,
    max_steps: int,
    planning_interval: int,
    no_harness: bool = False,
) -> dict:
    """Run a fresh vACTAgent for a single task, sharing the same run directory."""
    agent = vACTAgent(
        model=model,
        openjml_path=openjml_path,
        dataset_path=dataset_path,
        max_steps=max_steps,
        planning_interval=planning_interval,
        no_harness=no_harness,
        _run_dir=run_dir,
    )
    return agent.run(task)


# Lock for appending to the shared trajectory JSONL file
_jsonl_lock = threading.Lock()


def append_trajectory_jsonl(trajectory: dict, filepath: str) -> None:
    with _jsonl_lock:
        with open(filepath, "a") as f:
            f.write(json.dumps(trajectory) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Run vACT in batch mode.")
    parser.add_argument("--benchmark", required=True, help="Path to benchmark dataset (.json or .jsonl)")
    parser.add_argument("--threads", type=int, default=4, help="Number of parallel workers (default: 4)")
    parser.add_argument("--model", default="gpt-4o", help="Model ID (default: gpt-4o)")
    parser.add_argument("--output-dir", default="vact_outputs", help="Output directory (default: vact_outputs)")
    parser.add_argument("--openjml-path", default="openjml", help="Path to OpenJML binary (default: openjml)")
    parser.add_argument("--max-steps", type=int, default=11, help="Max agent steps per task (default: 11 = 10 tool calls + submit, matching agent_harness --max-attempts 10)")
    parser.add_argument("--planning_interval", type=int, default=5, help="Planning interval (default: 5)")
    parser.add_argument("--no-harness", action="store_true", help="Ablation: run with verify + task_complete only (no run_spec_harness); success = verification passes")
    parser.add_argument("--task-ids", default=None, help="Path to a text file with one task_id per line to filter the benchmark")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # Load tasks
    raw_tasks = load_benchmark(args.benchmark)
    tasks = [Task.from_dict(t) for t in raw_tasks]
    logger.info(f"Loaded {len(tasks)} tasks from {args.benchmark}")

    # Filter by task IDs file if provided
    if args.task_ids:
        with open(args.task_ids) as f:
            allowed_ids = {line.strip() for line in f if line.strip()}
        tasks = [t for t in tasks if t.task_id in allowed_ids]
        logger.info(f"Filtered to {len(tasks)} tasks from {args.task_ids}")

    # Create model once — shared across all tasks (HTTP clients are thread-safe)
    model = create_model(args.model)

    # Compute a single run directory for the entire batch
    model_id = getattr(model, "model_id", "unknown") or "unknown"
    safe_model_id = model_id.replace("/", "_")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(args.output_dir, f"vact__{safe_model_id}__{timestamp}")
    os.makedirs(run_dir, exist_ok=True)
    logger.info(f"Output directory: {run_dir}")

    trajectory_jsonl_path = os.path.join(run_dir, "trajectories.jsonl")

    completed = []
    failed = []

    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        future_to_task = {
            pool.submit(
                run_single_task,
                task=task,
                model=model,
                openjml_path=args.openjml_path,
                dataset_path=args.benchmark,
                run_dir=run_dir,
                max_steps=args.max_steps,
                planning_interval=args.planning_interval,
                no_harness=args.no_harness,
            ): task
            for task in tasks
        }

        for future in as_completed(future_to_task):
            task = future_to_task[future]
            try:
                result = future.result()
                completed.append(task.task_id)
                append_trajectory_jsonl(result, trajectory_jsonl_path)
                logger.info(f"[DONE] {task.task_id} — success={result.get('success')}, iterations={result.get('iterations')}")
            except Exception:
                tb = traceback.format_exc()
                failed.append({"task_id": task.task_id, "error": tb})
                logger.error(f"[FAIL] {task.task_id}\n{tb}")

    # Save failed tasks report
    if failed:
        dump_json(failed, os.path.join(run_dir, "failed_tasks.json"))
        logger.warning(f"{len(failed)} task(s) failed. See {run_dir}/failed_tasks.json")

    logger.info(f"Batch complete: {len(completed)} succeeded, {len(failed)} failed out of {len(tasks)} total")


if __name__ == "__main__":
    main()
