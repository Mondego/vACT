# Daikon
[The Daikon system for dynamic detection of likely invariants](https://www.sciencedirect.com/science/article/pii/S016764230700161X)

## Usage

```bash
cd vACT

python -m baselines.daikon.run \
    --name <experiment_name> \
    --input <path/to/benchmark.json> \
    --output <output_dir> \
    --openjml_timeout 300 \
    --daikon_timeout 600 \
    --threads 4 \
    --verbose
```

| Flag | Default | Description |
|------|---------|-------------|
| `--name` | required | Experiment name |
| `--input` | required | Dataset JSON path |
| `--output` | required | Output directory |
| `--openjml_timeout` | 300 | OpenJML timeout in seconds |
| `--daikon_timeout` | 600 | Full Daikon pipeline timeout in seconds |
| `--threads` | 1 | Parallel workers |
| `--verbose` | off | Enable verbose logging |

