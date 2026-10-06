# Run source-tree commands against this checkout even before an editable install.
# Preserve any caller-provided PYTHONPATH entries after the local src directory.
export PYTHONPATH := $(CURDIR)/src$(if $(PYTHONPATH),:$(PYTHONPATH))

.PHONY: install test test-core test-performance test-hardening test-gui test-gui-parallel test-ordered lint fmt format-check typecheck typecheck-extended build check demo cov all

install:
	pip install -e ".[dev]"

PYTEST_XDIST_WORKERS ?= auto

test: test-core test-performance test-gui

test-core:
	pytest -n $(PYTEST_XDIST_WORKERS) -m "not gui and not performance"

test-performance:
	pytest -n 0 -m performance

test-hardening:
	pytest -n 0 tests/test_web.py -k TestSafety
	pytest -n 0 -m performance

test-gui:
	pytest -m gui

# Bounded GTK concurrency; test-gui stays the serial diagnostic path.
test-gui-parallel:
	pytest -m gui -n 2

# Deterministic order, for bisecting a failure found by the randomised run.
test-ordered:
	pytest -n 0 -p no:randomly

cov:
	pytest --cov=breadsched --cov-report=term-missing

lint:
	ruff check src tests examples scripts packaging

fmt:
	ruff check --fix src tests examples scripts packaging
	ruff format src tests examples scripts packaging

# Keep formatting consistent with `make fmt` in local and CI checks.
format-check:
	ruff format --check src tests examples scripts packaging

typecheck:
	mypy src/breadsched/gen src/breadsched/plugins tests/typecheck

# Check every presentation layer, including GUI modules and their imports.
typecheck-extended:
	mypy src/breadsched/cli src/breadsched/web src/breadsched/gui

build:
	rm -rf build dist
	python -m build

check: lint format-check typecheck typecheck-extended test demo build

demo:
	python examples/demo.py

all: check
