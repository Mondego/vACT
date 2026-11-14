.PHONY: quality style test docs clean install

# Define directories to check (adjust based on your project structure)
check_dirs := spec_harness vact agent_harness baselines optimizer experiments tests

# Check code quality and formatting (no changes made)
quality:
	ruff check $(check_dirs)
	ruff format --check $(check_dirs)

# Format source code automatically with Google style
style:
	ruff check $(check_dirs) --fix
	ruff format $(check_dirs)

# Run tests
test:
	pytest ./tests/

# Run tests with coverage
test-cov:
	pytest ./tests/ --cov=spec_harness --cov=vact --cov-report=html --cov-report=term

# Install package in editable mode with dev dependencies
install:
	pip install -e ".[dev]"

# Clean up build artifacts
clean:
	rm -rf build/
	rm -rf dist/
	rm -rf *.egg-info/
	rm -rf .pytest_cache/
	rm -rf htmlcov/
	find . -type d -name __pycache__ -delete
	find . -type f -name "*.pyc" -delete

# Build package
build:
	python -m build

# Run all checks (quality + tests)
check: quality test

# Help target
help:
	@echo "Available targets:"
	@echo "  quality     - Check code quality and formatting"
	@echo "  style       - Auto-format code with Google style"
	@echo "  test        - Run tests"
	@echo "  test-cov    - Run tests with coverage"
	@echo "  install     - Install package in editable mode"
	@echo "  clean       - Clean build artifacts"
	@echo "  build       - Build package"
	@echo "  check       - Run quality checks and tests"
	@echo "  help        - Show this help message"