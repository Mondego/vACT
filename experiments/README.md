# Experiments

Scripts that run the study on top of the tools in this repository. Every command runs from the **repository root**, and outputs go to `results/` unless another directory is given.

| Part | What it does | How to run | Output |
|---|---|---|---|
| RQ1 | Classical and prompt-based JML synthesis on SpecGenBench and FormalBench | `experiments/scripts/<approach>.run.sh` | one `.jsonl` per approach × benchmark × model × prompt |
| RQ2 | GEPA prompt optimization of the FormalBench seed prompts | `experiments/scripts/run_optimizer.sh` | optimized prompts and their verification results |
| RQ3 | Spec-Harness scores of every verified RQ1 specification | `experiments/runners/rq3_run.py` | per-specification results, merged by `collect.py` |
| RQ4 | Coding agents with and without Spec-Harness feedback | `experiments/runners/rq4_one.sh` or `rq4_run.py` | per-task results, merged by `collect.py` |
| Figures | Verification Rate heatmap of RQ1 | `experiments/figures/fig3_vr.py` | CSV and PDF/PNG |

## RQ1: baselines

```bash
experiments/scripts/houdini.run.sh  benchmarks/specgenbench/sgb.json results/rq1
experiments/scripts/daikon.run.sh   benchmarks/formalbench/fb_v2_feedback.json results/rq1   # run name: fb_run
MODEL=gpt-4o-2024-08-06 PROMPT=two_shot experiments/scripts/specgen.run.sh benchmarks/formalbench/fb.json results/rq1
```

- **Approaches:** Houdini and Daikon (classical); SpecGen and AutoSpec (`zero_shot`, `two_shot`, `four_shot`); FormalBench prompts (`zero_shot`, `two_shot`, `zs_cot`, `fs_cot`, `fs_ltm`).
- **Models:** GPT-4o, Gemini 2.0 Flash and Claude Sonnet 4.6 through their APIs, and Qwen2.5-Coder-32B-Instruct, DeepSeek-Coder-33B-Instruct and CodeLlama-34B-Instruct served locally with vLLM (`tools/vllm/`).
- **Settings:** temperature 0.7; up to 10 refinement rounds for SpecGen and AutoSpec and 3 for the FormalBench prompts; OpenJML 21-0.21 with a limit of 300 s per verification.
- **Daikon's executions:** in the study, Daikon observes the feedback pairs (`*_v2_feedback.json`), so it never sees the pairs that Spec-Harness scores on. The run name is still `<bench>_run`; pass a third argument to override it.
- **Requirements:** Houdini needs Java 1.6.0_21 (`baselines/houdini/README.md`). Daikon runs on JDK 11, with `DAIKONDIR` set (and `DAIKON_JAVA` / `DAIKON_JAVAC` if JDK 11 is not the default Java).

## RQ2: prompt optimization

```bash
experiments/scripts/run_optimizer.sh zero     # seeds: zero | cot | ltm
```

GEPA runs with GPT-4o as both the generating and the reflecting model. See `optimizer/README.md`.

## RQ3 and RQ4: Spec-Harness scoring and agents

Both run with the standalone runners in `experiments/runners/` (see its README). They score on the v2 test suites in `benchmarks/` (see `benchmarks/README.md`): the agents of RQ4 see only the feedback suite, and every final score comes from the disjoint evaluation suite.

```bash
python experiments/runners/rq3_run.py run --out results/rq3_raw --pairs 5 --jvm-slots 60
experiments/runners/rq4_one.sh results/rq4_raw with_harness codex sgb
python experiments/runners/collect.py rq3 --in results/rq3_raw --out results/rq3 --rq1-dir results/rq1 --pairs 5
python experiments/runners/collect.py rq4 --in results/rq4_raw --out results/rq4 --pairs 5
```

To score a single baseline output file with Spec-Harness, outside the runners:

```bash
experiments/scripts/run_spec_harness.sh results/rq1/specgen__sgb_run__gpt-4o-2024-08-06__zero_shot.jsonl
```

## Figures

```bash
python experiments/figures/fig3_vr.py csv --rq1-dir results/rq1 --out results/rq1
python experiments/figures/fig3_vr.py plot --out results/rq1
```
