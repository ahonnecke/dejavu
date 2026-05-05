.PHONY: help install dev test lint clean dist upload-test upload smoke

PY ?= python3
VENV ?= .venv

help:
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  %-15s %s\n", $$1, $$2}'

install: ## Install dejavu in the current environment
	$(PY) -m pip install .

dev: ## Install dejavu + dev deps in editable mode
	$(PY) -m pip install -e '.[dev]'

test: ## Run pytest
	$(PY) -m pytest -q

smoke: ## Smoke test: --help and --list against the running user's projects
	$(PY) dejavu.py --help >/dev/null
	$(PY) dejavu.py --list >/dev/null || true
	@echo "smoke ok"

lint: ## Run a minimal syntax check
	$(PY) -m py_compile dejavu.py

clean: ## Remove build artifacts and caches
	@rm -rf build dist *.egg-info __pycache__ .pytest_cache
	@find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@find . -name '*.pyc' -delete

dist: clean ## Build sdist + wheel
	$(PY) -m build

upload-test: dist ## Upload to TestPyPI (requires TWINE_USERNAME/TWINE_PASSWORD or ~/.pypirc)
	$(PY) -m twine upload --repository testpypi dist/*

upload: dist ## Upload to PyPI
	$(PY) -m twine upload dist/*
