import hashlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from breadsched import __version__
from breadsched.gen.db.migrations import MIN_SUPPORTED_SCHEMA_VERSION
from breadsched.gen.db.sqlite import SCHEMA_VERSION

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_release_notes.py"
_SPEC = importlib.util.spec_from_file_location("check_release_notes", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
validate_release_notes = _MODULE.validate_release_notes


def _notes() -> str:
    return f"""\
# BreadSched {__version__}

Application version: `{__version__}`
Native schema version: `{SCHEMA_VERSION}`
Supported native schemas: `{MIN_SUPPORTED_SCHEMA_VERSION}–{SCHEMA_VERSION}`

## Upgrade and rollback

Back up first.

## Verified artifacts

Check SHA256SUMS.
"""


def test_current_release_notes_are_valid(tmp_path: Path):
    tag = f"v{__version__}"
    path = tmp_path / f"{tag}.md"
    path.write_text(_notes(), encoding="utf-8")

    assert validate_release_notes(tag, path) == []


def test_checked_in_release_notes_are_valid():
    tag = f"v{__version__}"

    assert validate_release_notes(tag, Path("docs/releases") / f"{tag}.md") == []


def test_release_notes_must_match_the_application_and_schema(tmp_path: Path):
    path = tmp_path / "v0.0.0.md"
    path.write_text("# incomplete", encoding="utf-8")

    problems = validate_release_notes("v0.0.0", path)

    assert any("does not match application version" in problem for problem in problems)
    assert any("Native schema version" in problem for problem in problems)
    assert any("Upgrade and rollback" in problem for problem in problems)


def test_release_workflow_sets_an_annotated_tag_identity():
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert '-f tag="$TAG" -f message="BreadSched $TAG"' in workflow
    assert '-f object="$CANDIDATE_SHA" -f type=commit' in workflow
    assert '-f ref="refs/tags/$TAG" -f sha="$tag_object"' in workflow


def test_release_write_job_does_not_execute_checked_out_code():
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    preparation, publication = workflow.split("\n  publish:\n", 1)

    assert "permissions:\n  contents: read" in preparation
    assert "github.event.workflow_run.event == 'push'" in preparation
    assert "head_repository.full_name == github.repository" in preparation
    assert "ref: main" in preparation
    assert "persist-credentials: false" in preparation
    assert 'test "$(git rev-parse HEAD)" = "$TESTED_SHA"' in preparation
    assert preparation.index("Require the tested main checkout") < preparation.index(
        "Select an explicitly documented release"
    )
    assert "contents: write" in publication
    assert "actions/checkout" not in publication
    assert "python -m build" not in publication
    assert "python -m pip install" not in publication
    assert "Validate transferred files without executing them" in publication
    assert 'git/ref/heads/main" --jq' in publication


def test_ci_jobs_use_read_only_repository_token():
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "permissions:\n  contents: read\n\njobs:" in workflow
    assert "contents: write" not in workflow


@pytest.mark.skipif(sys.platform == "win32", reason="release validation runs on Linux")
def test_release_publisher_treats_artifacts_as_fixed_name_data(tmp_path: Path):
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    section = workflow.split("      - name: Validate transferred files without executing them", 1)[
        1
    ]
    lines = []
    for line in section.split("        run: |\n", 1)[1].splitlines():
        if not line:
            lines.append("")
            continue
        if not line.startswith("          "):
            break
        lines.append(line[10:])
    script = "\n".join(lines)

    version = "0.2.0a102"
    tag = f"v{version}"
    root = tmp_path / "release-inputs"
    dist = root / "dist"
    notes = root / "docs" / "releases"
    dist.mkdir(parents=True)
    notes.mkdir(parents=True)
    (notes / f"{tag}.md").write_text("release notes", encoding="utf-8")
    names = (f"breadsched-{version}-py3-none-any.whl", f"breadsched-{version}.tar.gz")
    for name in names:
        (dist / name).write_bytes(name.encode())
    (root / "SHA256SUMS").write_text(
        "".join(f"{hashlib.sha256(name.encode()).hexdigest()}  {name}\n" for name in names),
        encoding="utf-8",
    )

    def validate() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-e", "-c", script],
            cwd=tmp_path,
            env={**os.environ, "VERSION": version, "TAG": tag},
            text=True,
            capture_output=True,
            check=False,
        )

    assert validate().returncode == 0
    extra = dist / "unexpected.whl"
    extra.write_bytes(b"extra")
    assert validate().returncode != 0
    extra.unlink()
    wheel = dist / names[0]
    wheel.unlink()
    wheel.symlink_to(notes / f"{tag}.md")
    assert validate().returncode != 0


@pytest.mark.skipif(sys.platform == "win32", reason="release publish step runs on Linux")
def test_release_workflow_marks_only_alpha_versions_as_prereleases():
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    command = workflow.split("      - name: Publish release and verified artifacts", 1)[1]
    assert "VERSION: ${{ needs.prepare.outputs.version }}" in workflow
    script = command.split("          release_flags=()\n", 1)[1]
    lines = ["release_flags=()"]
    for line in script.splitlines():
        if not line.startswith("          "):
            break
        lines.append(line[10:])
    shell = "\n".join(lines).replace("gh release create", "printf '%s\\n' gh release create")
    for version, prerelease in (("0.2.0a100", True), ("0.2.0", False)):
        result = subprocess.run(
            ["bash", "-c", shell],
            env={
                "VERSION": version,
                "TAG": f"v{version}",
                "NOTES": "notes.md",
                "GITHUB_REPOSITORY": "test/repo",
            },
            text=True,
            capture_output=True,
            check=True,
        )
        assert ("--prerelease" in result.stdout) is prerelease
