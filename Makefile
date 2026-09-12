# Run source-tree commands against this checkout even before an editable install.
# Preserve any caller-provided PYTHONPATH entries after the local src directory.
export PYTHONPATH := $(CURDIR)/src$(if $(PYTHONPATH),:$(PYTHONPATH))

.PHONY: install test test-core test-gui test-ordered lint fmt typecheck build check demo cov all

install:
	pip install -e ".[dev]"

PYTEST_XDIST_WORKERS ?= auto

test: test-core test-gui

test-core:
	pytest -n $(PYTEST_XDIST_WORKERS) -m "not gui"

test-gui:
	pytest -m gui

# Deterministic order, for bisecting a failure found by the randomised run.
test-ordered:
	pytest -n 0 -p no:randomly

cov:
	pytest --cov=breadsched --cov-report=term-missing

lint:
	ruff check src tests examples

fmt:
	ruff check --fix src tests examples

typecheck:
	mypy src/breadsched/gen src/breadsched/plugins

build:
	python -m build

check: lint typecheck test demo build

demo:
	python examples/demo.py

all: check
