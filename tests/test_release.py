import importlib.util
import subprocess
from pathlib import Path

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

    name = workflow.index('git config user.name "github-actions[bot]"')
    email = workflow.index("git config user.email")
    tag = workflow.index('git tag -a "$TAG"')
    assert name < tag
    assert email < tag


def test_release_workflow_marks_only_alpha_versions_as_prereleases():
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    command = workflow.split("      - name: Publish release and verified artifacts", 1)[1]
    assert "VERSION: ${{ steps.release.outputs.version }}" in command
    script = command.split("        run: |\n", 1)[1]
    lines = []
    for line in script.splitlines():
        if not line.startswith("          "):
            break
        lines.append(line[10:])
    shell = "\n".join(lines).replace("gh release create", "printf '%s\\n' gh release create")
    for version, prerelease in (("0.2.0a100", True), ("0.2.0", False)):
        result = subprocess.run(
            ["bash", "-c", shell],
            env={"VERSION": version, "TAG": f"v{version}", "NOTES": "notes.md", "GITHUB_REPOSITORY": "test/repo"},
            text=True, capture_output=True, check=True,
        )
        assert ("--prerelease" in result.stdout) is prerelease
