# Run source-tree commands against this checkout even before an editable install.
# Preserve any caller-provided PYTHONPATH entries after the local src directory.
export PYTHONPATH := $(CURDIR)/src$(if $(PYTHONPATH),:$(PYTHONPATH))

.PHONY: install test test-ordered lint fmt typecheck build check demo cov all

install:
	pip install -e ".[dev]"

test:
	pytest

# Deterministic order, for bisecting a failure found by the randomised run.
test-ordered:
	pytest -p no:randomly

cov:
	pytest --cov=cashperspective --cov-report=term-missing

lint:
	ruff check src tests examples

fmt:
	ruff check --fix src tests examples

typecheck:
	mypy src/cashperspective/gen src/cashperspective/plugins

build:
	python -m build

check: lint typecheck test demo build

demo:
	python examples/demo.py

all: check
