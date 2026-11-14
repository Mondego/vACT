# Houdini
[Houdini, an annotation assistant for esc/java](https://link.springer.com/chapter/10.1007/3-540-45251-6_29)


## Changes & Improvements

- Restructured from a flat procedural script into an object-oriented design with separate Houdini (core algorithm) and HoudiniRunner (orchestration) classes
- Introduced typed data models using dataclasses (Task, Annotation, MergedLine) and TypedDicts (HoudiniResult, WorkerResult) to replace untyped dictionaries
- Extracted reusable utilities (file I/O, OpenJML verification, structured logging) into shared modules under baselines/utils/
- Added multi-threaded execution via ThreadPoolExecutor with configurable thread count, each thread operating in an isolated output directory to prevent file collisions
- Replaced os.popen and os.system with subprocess.Popen/subprocess.run using list-based arguments, eliminating shell injection risk
- Added configurable timeouts for both annotation generation and OpenJML verification
- Added process group cleanup (SIGKILL via os.killpg) to prevent orphaned verifier processes
- Fixed an IndexError in annotation merging when trailing annotations reference past the last line of code
- Added bounds checking in the main Houdini loop to guard against out-of-range line numbers from verifier output
- Added a length guard in annotation file parsing to skip malformed lines instead of crashing
- Added defensive parsing in error line extraction with logging for unparseable verifier output
- Replaced the unbounded while True loop with a configurable maximum iteration limit to prevent infinite loops
- Changed verification success detection from empty-string output comparison to return code checking for more reliable behavior across OpenJML versions
- Added structured JSON logging (per-thread log files) with colored console output
- Added result aggregation with JSONL output of all results and a JSON summary including verification rates, timing, and per-case breakdowns



## Usage

```bash

cd vACT

python -m baselines.houdini.run \
    --name <experiment_name> \
    --input <path/to/benchmark.json> \
    --output <output_dir> \
    --openjml_timeout 300 \
    --threads 4 \
    --verbose
```

| Flag | Default | Description |
|------|---------|-------------|
| `--name` | required | Experiment name |
| `--input` | required | Dataset JSON path |
| `--output` | required | Output directory |
| `--openjml_timeout` | 300 | OpenJML timeout in seconds |
| `--threads` | 1 | Parallel workers |
| `--verbose` | off | Enable verbose logging |