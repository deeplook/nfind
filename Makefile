.DEFAULT_GOAL := help

.PHONY: install lint format test coverage clean install-tool check-all publish-test publish serve-docs build-docs semantic-eval semantic-eval-all semantic-eval-run semantic-eval-run-all help

EVAL_ARGS ?=

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-22s %s\n", $$1, $$2}'

install:  ## Install all dependencies
	uv sync

format:  ## Auto-format and fix lint issues
	uv run ruff format src tests
	uv run ruff check --fix src tests

lint:  ## Run ruff and mypy
	uv run ruff check src tests
	uv run mypy src tests

test:  ## Run the test suite
	uv run pytest

coverage:  ## Run tests with HTML + terminal coverage report
	uv run pytest --cov=src --cov-report=html --cov-report=term

semantic-eval:  ## Plan the 20-case semantic evaluation (free; no model calls)
	uv run python -m tests.semantic.evaluate $(EVAL_ARGS)

semantic-eval-all:  ## Plan all 60 semantic prompt evaluations (free; no model calls)
	uv run python -m tests.semantic.evaluate --all-prompts $(EVAL_ARGS)

semantic-eval-run:  ## Run 20 paid canonical-prompt semantic evaluations
	uv run python -m tests.semantic.evaluate --run $(EVAL_ARGS)

semantic-eval-run-all:  ## Run all 60 paid semantic prompt evaluations
	uv run python -m tests.semantic.evaluate --all-prompts --run $(EVAL_ARGS)

check-all: install format lint test clean  ## Run format, lint, test, and clean
	@echo "All checks passed!"

install-tool:  ## Install nfind as a uv tool (reinstall)
	uv tool install --reinstall .

serve-docs:  ## Serve the MkDocs documentation locally (http://localhost:8000)
	uv run --with mkdocs-material mkdocs serve

build-docs:  ## Build the MkDocs site into ./site
	uv run --with mkdocs-material mkdocs build

clean:  ## Remove build artifacts and caches
	rm -rf dist build *.egg-info
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	rm -rf htmlcov .coverage coverage.xml
	rm -rf site
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name .DS_Store -exec rm {} +

publish-test:  ## Build and publish to TestPyPI
	rm -rf dist/
	uv build
	uv publish --index testpypi --token $(TEST_PYPI_TOKEN)

publish:  ## Build and publish to PyPI
	rm -rf dist/
	uv build
	uv publish --token $(PYPI_TOKEN)
