"""The storage benchmark behind decision 0001 compares layouts that read back exactly.

``scripts/storage_benchmark.py`` asserts that every layout returns each transaction
as written; this runs it on a small book so a layout that stops round-tripping, or a
minimized encoding that stops saving space, fails here rather than in a re-run.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "storage_benchmark.py"


def _benchmark():
    spec = importlib.util.spec_from_file_location("storage_benchmark", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_layout_reads_back_and_minimized_json_is_smaller():
    results = dict(_benchmark().measure(400, 1))
    current = results["blob + split_index (current)"]
    minimized = results["blob + split_index, minimized JSON"]
    assert set(results) >= {"normalized tables (candidate)"}
    assert (
        minimized["mean transaction JSON (bytes)"] < 0.7 * current["mean transaction JSON (bytes)"]
    )
    assert minimized["file size (MiB)"] < current["file size (MiB)"]


def test_minimizing_leaves_out_only_what_reading_restores():
    benchmark = _benchmark()
    _accounts, transactions = benchmark.household(50)
    sample = transactions[0].serialize()
    defaults = benchmark._omittable(benchmark._txn_read, sample)
    assert "_schema" not in defaults and "handle" not in defaults
    for transaction in transactions:
        data = transaction.serialize()
        kept = {k: v for k, v in data.items() if k not in defaults or defaults[k] != v}
        assert benchmark.Transaction.from_dict(kept).serialize() == data
