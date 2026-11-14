# Benchmarks

We use two established benchmarks for evaluation.

- [SpecGenBench](./specgenbench/): 120 Java method tasks
- [FormalBench](./formalbench/): 700 Java method tasks. We exclude methods that return nothing (`void`) or use `Object` as a parameter or return type, which leaves 662 tasks.

We normalize all benchmark tasks by renaming class names to `Solution` and method names to `solve()`.

Every file is stored twice, as `.json` (one list) and `.jsonl` (one task per line). The two have the same content. All files share the same ten fields: `task_id, code, test_code, test_inputs, generated_test_cases, class_name, test_name, javadoc, category, origin_id`. The `category` field holds one of five control-flow categories: `branch`, `sequential`, `single_path_loop`, `multi_path_loop`, `nested`.

## Which file is used where

| File | Tasks | Used by | Content |
|---|---|---|---|
| `specgenbench/sgb.json` | 120 | RQ1/RQ2 baselines and prompt optimizer (`experiments/scripts/*.run.sh`, `optimizer/`), VeriAct examples | Original tasks. `test_inputs` holds the original test pairs; `generated_test_cases` holds 100–200 extra pairs from random test generation |
| `specgenbench/sgb_aug.json` | 120 | `tests/smoke_harness.py` only | `sgb.json` with recorded outputs for 4 void tasks that update an array in place (made by `spec_harness/augment_void_outputs.py`) |
| `formalbench/fb.json` | 662 | RQ1/RQ2 baselines and prompt optimizer | Same as `sgb.json`, for FormalBench |
| `formalbench/fb_120.json` | 120 | The smoke test (`tests/smoke_harness.py`) | First 120-task sample, stratified to match SpecGenBench's category counts. **Not** the RQ4 set |
| `formalbench/fb_120_fbdist.json` | 120 | Source of the `fb_120_v2_*` files | **FormalBench-120**, the RQ4 sample: 120 tasks drawn per category in proportion to the full FormalBench |
| `formalbench/fb_120_fbdist_report.json` | – | – | How `fb_120_fbdist` was sampled: category counts of the population and the sample, and its overlap with `fb_120` (24 tasks) |

### The v2 test suites (Spec-Harness scoring and feedback)

The `*_v2_*` files are the test suites that Spec-Harness uses. They keep the same tasks and the same ten fields, but replace the tests:

- `test_inputs` holds the selected test pairs `{input, output}`, and `generated_test_cases` is empty.
- `invalid_inputs` holds validated invalid inputs `{inputs, exception, sources}` that the program rejects. They are used to check precondition completeness.
- `v2` records provenance: which list (`evaluation` or `feedback`), the number of pairs `n`, the source of each pair, and the invalid-input counts.
- `observer` marks the 4 SpecGenBench void tasks whose outputs are the modified argument.

| File | Used by | `test_inputs` |
|---|---|---|
| `<bench>_v2_eval.json` | Final scoring: RQ3 (`experiments/runners/rq3_run.py`) and RQ4 (`experiments/runners/rq4_run.py`), plus `experiments/runners/collect.py` | The first 10 pairs of the ranked evaluation list |
| `<bench>_v2_feedback.json` | RQ4 agents (the Spec-Harness feedback tool and the scaffold) and the executions Daikon observes in RQ1 | 10 feedback pairs, disjoint from the evaluation pairs. Its invalid inputs are also disjoint from the evaluation ones |
| `<bench>_v2_eval50.json` | Sensitivity to the number of pairs N only (`spec_harness/score_subsets.py`) | The whole ranked evaluation list, up to 50 pairs. Its first 10 pairs are exactly `<bench>_v2_eval.json` |

`<bench>` is `sgb` (SpecGenBench, 120 tasks), `fb` (FormalBench, 662 tasks) or `fb_120` (FormalBench-120, the 120 tasks of `fb_120_fbdist`).

A few tasks have fewer than 10 pairs because the program has few distinct inputs: `SGB_Conjunction`, `SGB_Disjunction`, `SGB_XOR` (2 each), `FB_SumOfPrimes` (2), `FB_CommonPrefix` (7). `FB_Array3d` has no pairs, so `experiments/runners/collect.py` excludes it from the results (661 FormalBench tasks in RQ3).

## Other files

- `example/data.json`: 3 tasks (`SGB_LinearSearch`, `FB_AddPairwise`, `FB_FindMissing`) in the same format as `sgb.json`, for a quick trial run. No runner reads it by default.
