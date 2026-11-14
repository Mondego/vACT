# Spec-Harness and vACT: Behavioral Adequacy of LLM-Synthesized JML Specifications

<div align="center">

[![arXiv](https://img.shields.io/badge/arXiv-2604.00280-b31b1b.svg)](https://arxiv.org/abs/2604.00280)
[![License](https://img.shields.io/badge/License-GNU_GPL_v3-blue)](./LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10+-green.svg)](https://www.python.org/)

</div>

## ✨ Overview

A specification that passes a verifier is not necessarily a good one: `ensures true` verifies for every method. This repository contains tools to measure and improve how much of a program's behavior an LLM-synthesized JML specification actually captures:

- **Spec-Harness** scores a specification on four metrics: the correctness and completeness of its precondition and of its postcondition. It replaces the method body with stubs that pin concrete inputs and outputs, and decides every check with an OpenJML proof.
- **vACT** is a ReAct-style JML synthesis agent that uses the verifier results and the Spec-Harness scores as feedback.
- **Agent tools** expose the verifier and Spec-Harness to general-purpose coding agents (Claude Code, Codex) without changing them.
- **Baselines**: classical (Houdini, Daikon) and prompt-based (SpecGen, AutoSpec, FormalBench prompts) JML synthesis, plus a GEPA prompt optimizer.

### 💎 Key Components

- **[spec_harness](./spec_harness/)**: the Spec-Harness metrics, stub generation and scoring.
- **[vact](./vact/)**: the vACT agent (verifier and Spec-Harness feedback).
- **[agent_harness](./agent_harness/)**: `verify` / `run_specharness` / `submit` tools and task scaffolding for Claude Code and Codex.
- **[baselines](./baselines/)**: Houdini, Daikon, SpecGen, AutoSpec and the FormalBench prompts.
- **[optimizer](./optimizer/)**: GEPA prompt optimization with OpenJML feedback.
- **[benchmarks](./benchmarks/)**: SpecGenBench and FormalBench (normalized), and the test suites Spec-Harness scores on.
- **[experiments](./experiments/)**: scripts that run the study (RQ1–RQ4) and collect the results.

## 🚀 Getting Started

### 💻 Prerequisites

- Python ≥ 3.10
- Java 21 and [OpenJML](https://www.openjml.org/downloads/) **21-0.21**, on `PATH` as `openjml` (check with `openjml --version`)
- For the baselines only: Java 1.6.0_21 for Houdini, and [Daikon](https://plse.cs.washington.edu/daikon/download/doc/daikon.html#Installing-Daikon) with JDK 11
- For the agent experiments only: [Claude Code](https://docs.claude.com/en/docs/claude-code) and [Codex](https://github.com/openai/codex) CLIs

### ⏳ Install

```bash
git clone https://github.com/Mondego/vACT.git
cd vACT
uv sync --all-extras            # or: pip install -e ".[all,dev]"
source .venv/bin/activate
```

### 🔑 API Keys

Create a `.env` file in [config](./config/) with the keys for the models you intend to use:

<details>
<summary>API Keys</summary>

```bash
OPENAI_API_KEY=...       # GPT models (baselines, vACT, Codex)
ANTHROPIC_API_KEY=...    # Claude models
GOOGLE_API_KEY=...       # Gemini models
DEEPSEEK_API_KEY=...     # DeepSeek API
MISTRAL_API_KEY=...      # Mistral API
VLLM_API_KEY=...         # Local vLLM server (tools/vllm/)
VLLM_API_BASE=...        # e.g. http://localhost:8000/v1
```

`config/.env` is ignored by git.
</details>

### ✅ Tests

```bash
python -m pytest tests/test_harness.py -q      # Spec-Harness unit tests, no OpenJML needed
```

## 💣 Usage

<details>
<summary>Run Spec-Harness on baseline outputs</summary>

```bash
python -m spec_harness.eval_llm_response \
  --benchmark_path benchmarks/formalbench/fb_v2_eval.json \
  --llm_response_path path/to/responses.jsonl \
  --openjml openjml \
  --output spec_harness_results \
  --threads 8 \
  --max-pairs 5 \
  --verbose
```

`responses.jsonl` is the output of a baseline run. `--max-pairs` is the number of input/output test pairs per task. The `*_v2_eval.json` suites hold the evaluation pairs and invalid inputs (see [benchmarks/README.md](./benchmarks/README.md)).
</details>

<details>
<summary>Run vACT</summary>

```bash
# One task at a time
python -m vact.run.run_single \
    --benchmark benchmarks/specgenbench/sgb_v2_feedback.json \
    --model gpt-6.1-sol \
    --output-dir <output_dir> \
    --max-steps 20 \
    --max-tool-calls 10

# Tasks in parallel
python -m vact.run.run_batch \
    --benchmark benchmarks/specgenbench/sgb_v2_feedback.json \
    --model gpt-6.1-sol \
    --threads 4 \
    --output-dir <output_dir>
```

`--no-harness` runs vACT with the verifier only. See [vact/README.md](./vact/README.md).
</details>

<details>
<summary>Run Claude Code or Codex with the Spec-Harness tools</summary>

```bash
python -m agent_harness.scaffold \
    --benchmark benchmarks/specgenbench/sgb_v2_feedback.json --out-root out/sgb
```

This creates one task directory per task with the tools and an instruction file. `--no-harness` leaves out the Spec-Harness tool. See [agent_harness/README.md](./agent_harness/README.md) for running the agents and collecting their results.
</details>

<details>
<summary>Run Daikon</summary>

```bash
python -m baselines.daikon.run \
    --name <experiment_name> \
    --input <path/to/benchmark.json> \
    --output <output_dir> \
    --openjml_timeout 300 \
    --daikon_timeout 600 \
    --threads 4 \
    --verbose
```

Daikon runs on JDK 11: set `DAIKONDIR`, and `DAIKON_JAVA` / `DAIKON_JAVAC` if JDK 11 is not the default Java.
</details>

<details>
<summary>Run Houdini</summary>

```bash
python -m baselines.houdini.run \
    --name <experiment_name> \
    --input <path/to/benchmark.json> \
    --output <output_dir> \
    --openjml_timeout 300 \
    --threads 4 \
    --verbose
```

Houdini needs Java [`1.6.0_21`](https://www.oracle.com/java/technologies/javase/6u21.html).
</details>

<details>
<summary>Run SpecGen</summary>

```bash
python -m baselines.specgen.run \
    --name <experiment_name> \
    --input <path/to/benchmark.json> \
    --output <output_dir> \
    --model gpt-4o \
    --temperature 0.7 \
    --prompt_type zero_shot \
    --max_iterations 10 \
    --openjml_timeout 300 \
    --threads 4 \
    --verbose
```

`--prompt_type`: `zero_shot` | `two_shot` | `four_shot`
</details>

<details>
<summary>Run AutoSpec</summary>

```bash
python -m baselines.autospec.run \
    --name <experiment_name> \
    --input <path/to/benchmark.json> \
    --output <output_dir> \
    --model gpt-4o \
    --temperature 0.7 \
    --prompt_type zero_shot \
    --max_iterations 10 \
    --openjml_timeout 300 \
    --threads 4 \
    --verbose
```

`--prompt_type`: `zero_shot` | `two_shot` | `four_shot`. `--simplify` turns on AutoSpec's optional simplification step (off in our experiments).
</details>

<details>
<summary>Run the FormalBench prompts</summary>

```bash
python -m baselines.formalbench.run \
    --name <experiment_name> \
    --input <path/to/benchmark.json> \
    --output <output_dir> \
    --model gpt-4o \
    --temperature 0.7 \
    --prompt_type zero_shot \
    --max_iterations 3 \
    --openjml_timeout 300 \
    --threads 4 \
    --verbose
```

`--prompt_type`: `zero_shot` | `two_shot` | `zs_cot` | `fs_cot` | `fs_ltm`
</details>

<details>
<summary>Run the prompt optimizer</summary>

```bash
python -m optimizer.prompt_optimizer \
    --formalbench_path benchmarks/formalbench/fb.json \
    --specgenbench_path benchmarks/specgenbench/sgb.json \
    --optimizers gepa \
    --best_seed zero \
    --model openai/gpt-4o \
    --reflection_model openai/gpt-4o \
    --log_dir optimizer_logs \
    --output_dir optimizer_results \
    --openjml_output_dir openjml_output
```

`--best_seed`: `zero` | `cot` | `ltm`
</details>

## 🔬 Reproducing the Experiments

[experiments/README.md](./experiments/README.md) lists which script produces which result: the baseline runs (RQ1), prompt optimization (RQ2), Spec-Harness scoring of every verified specification (RQ3), and the agents with and without Spec-Harness feedback (RQ4). The large runs (RQ3, RQ4) use the standalone runners in [experiments/runners](./experiments/runners/), which can split the work over several machines.

## 📁 Repository Structure

```
├── spec_harness            # Spec-Harness: metrics, stubs, scoring
├── vact                    # vACT agent
├── agent_harness           # Tools and task scaffolding for Claude Code and Codex
├── baselines               # Houdini, Daikon, SpecGen, AutoSpec, FormalBench prompts
├── optimizer               # GEPA prompt optimizer
├── benchmarks              # Normalized benchmarks and test suites (see its README)
│   ├── specgenbench        # 120 tasks from LeetCode and JML examples
│   └── formalbench         # 662 tasks from FormalBench (MBJP)
├── experiments             # Scripts that run the study (see its README)
│   ├── scripts             # Baseline, optimizer and Spec-Harness run scripts
│   ├── runners             # Standalone runners for Spec-Harness scoring and agents
│   └── figures             # Figure scripts
├── tests                   # Spec-Harness unit and smoke tests
├── tools/vllm              # Serving the open-weight models with vLLM
├── config                  # API keys in .env
├── pyproject.toml
└── README.md
```

`agent_harness/templates/harness_tool.py` and `vact/tools/harness_tool.py` are generated from `spec_harness/` by `python agent_harness/build_harness_tool.py`. Do not edit them by hand. After changing `spec_harness/`, rebuild them, and check with `--check`.

## 📝 Citation

```bibtex
@article{DBLP:journals/corr/abs-2604-00280,
  author       = {Md Rakib Hossain Misu and
                  Iris Ma and
                  Cristina V. Lopes},
  title        = {Spec-Harness: Assessing and Guiding LLM-Synthesized Formal Specifications
                  Beyond Verifiability},
  journal      = {CoRR},
  volume       = {abs/2604.00280},
  year         = {2026},
  url          = {https://doi.org/10.48550/arXiv.2604.00280},
  doi          = {10.48550/ARXIV.2604.00280},
  eprinttype   = {arXiv},
  eprint       = {2604.00280},
  timestamp    = {Thu, 07 May 2026 09:02:12 +0200},
  biburl       = {https://dblp.org/rec/journals/corr/abs-2604-00280.bib},
  bibsource    = {dblp computer science bibliography, https://dblp.org}
}
```

## 📧 Contact

If you have any questions or find any issues, please contact us at [mdrh@uci.edu](mailto:mdrh@uci.edu).

## 📄 License

This repository is licensed under the [GNU General Public License v3.0](LICENSE).
