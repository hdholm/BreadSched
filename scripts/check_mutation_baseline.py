#!/usr/bin/env python3
"""Run a fresh, bounded mutation slice and ratchet its measured baseline."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE_KILLED = 118
BASELINE_ELIGIBLE = 122
EQUIVALENT = {
    "breadsched.gen.engine.currency.x_reporting_fraction__mutmut_2": (
        "commodity_fraction(db, None) selects book_currency(db), the same currency "
        "as reporting_currency_handle(db), including the empty-book fallback"
    ),
}


def _run(args: list[str], cwd: Path) -> str:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(cwd / "src")
    result = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, check=False)
    output = result.stdout + result.stderr
    if result.returncode:
        raise RuntimeError(f"{' '.join(args)} failed:\n{output[-6000:]}")
    return output


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="breadsched-mutation-") as directory:
        work = Path(directory)
        shutil.copy2(ROOT / "pyproject.toml", work / "pyproject.toml")
        shutil.copytree(ROOT / "src", work / "src", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(
            ROOT / "tests", work / "tests", ignore=shutil.ignore_patterns("__pycache__")
        )
        _run([sys.executable, "-m", "mutmut", "run", "--max-children", "4"], work)
        _run([sys.executable, "-m", "mutmut", "export-cicd-stats"], work)
        results = _run([sys.executable, "-m", "mutmut", "results"], work)
        stats = json.loads((work / "mutants" / "mutmut-cicd-stats.json").read_text())

    if any(stats[key] for key in ("no_tests", "skipped", "suspicious", "timeout", "segfault")):
        raise AssertionError(f"mutation run is incomplete: {stats}")
    for mutant, reason in EQUIVALENT.items():
        if f"{mutant}: survived" not in results:
            raise AssertionError(f"review equivalent mutant before changing exemption: {mutant}")
        print(f"Equivalent excluded: {mutant} — {reason}")
    eligible = stats["total"] - len(EQUIVALENT)
    killed = stats["killed"]
    if stats["killed"] + stats["survived"] != stats["total"]:
        raise AssertionError(f"unexpected mutation result statuses: {stats}")
    print(f"Mutation baseline: {killed}/{eligible} eligible killed ({stats['total']} generated)")
    if eligible <= 0 or killed * BASELINE_ELIGIBLE < BASELINE_KILLED * eligible:
        raise AssertionError("mutation score fell below measured baseline 118/122")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
