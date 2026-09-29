import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from breadsched import APP_ID, __version__
from breadsched.gen.db.migrations import MIN_SUPPORTED_SCHEMA_VERSION
from breadsched.gen.db.sqlite import SCHEMA_VERSION

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_release_notes.py"
_SPEC = importlib.util.spec_from_file_location("check_release_notes", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
validate_release_notes = _MODULE.validate_release_notes


def test_flatpak_build_input_uses_the_app_id_and_excludes_local_state():
    manifest = json.loads(Path(f"{APP_ID}.json").read_text(encoding="utf-8"))
    assert manifest["id"] == APP_ID
    assert manifest["command"] == "breadsched-gtk"
    assert manifest["runtime"] == "org.gnome.Platform"
    assert manifest["sdk"] == "org.gnome.Sdk"
    (source,) = manifest["modules"][-1]["sources"]
    assert source["type"] == "dir" and source["path"] == "."
    assert {".git", ".venv", "build", "dist"} <= set(source["skip"])
    assert "--filesystem=host" not in manifest["finish-args"]
    assert "--filesystem=home" not in manifest["finish-args"]
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "flatpak install --user --noninteractive local-breadsched" in workflow
    assert "flatpak run --user --command=breadsched" in workflow
    assert "flatpak run --user --unshare=network --command=breadsched" in workflow
    assert "run_offline backup" in workflow and "run_offline restore" in workflow
    assert 'test -f "$marker"' in workflow
    assert '"already open for writing"' in workflow


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


def test_release_smoke_compares_the_installed_version_with_the_source():
    """The wheel check follows the schema; a hard-coded window stopped every release."""
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    preparation = workflow.split("\n  publish:\n", 1)[0]
    smoke = preparation.split("      - name: Build and smoke-test release artifacts", 1)[1]
    smoke = smoke.split("\n      - name:", 1)[0]

    assert "from breadsched.versioning import version_summary" in smoke
    assert '[[ "$actual" != "$expected" ]]' in smoke
    assert smoke.index("expected=$(") < smoke.index('pip install "${GITHUB_WORKSPACE}"/dist/*.whl')
    assert "native schema 7" not in workflow and "supports 6–7" not in workflow
    for schema in range(1, 100):
        assert f"native schema {schema}" not in smoke, schema


def test_release_builds_and_tests_the_windows_installer_from_the_tested_commit():
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    windows = workflow.split("\n  windows-installer:\n", 1)[1].split("\n  publish:\n", 1)[0]
    publication = workflow.split("\n  publish:\n", 1)[1]

    assert "needs: prepare" in windows and "runs-on: windows-latest" in windows
    assert "contents: write" not in windows
    assert "persist-credentials: false" in windows and "ref: main" in windows
    verify = windows.index("(git rev-parse HEAD) -ne $env:TESTED_SHA")
    assert verify < windows.index("packaging/windows/build-installer.sh")
    assert "./packaging/windows/test-installer.ps1 $installer" in windows
    stage = windows.index("./packaging/windows/stage-release.ps1 $installer release-installer")
    assert windows.index("test-installer.ps1") < stage
    assert "BreadSched-$env:VERSION-setup.exe" in windows
    assert "name: release-installer" in windows
    assert "needs: [prepare, windows-installer]" in publication
    assert "name: release-installer" in publication
    assert '"installer/BreadSched-${VERSION}-setup.exe" SHA256SUMS' in publication
    assert "test-installer.ps1" not in publication and "build-installer" not in publication


def test_release_workflow_sets_an_annotated_tag_identity():
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert '-f tag="$TAG" -f message="BreadSched $TAG"' in workflow
    assert '-f object="$CANDIDATE_SHA" -f type=commit' in workflow
    assert '-f ref="refs/tags/$TAG" -f sha="$tag_object"' in workflow


def test_release_selection_skips_a_version_already_tagged_elsewhere():
    """A later commit keeping a released version must not fail the release run."""
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    preparation, _publication = workflow.split("\n  publish:\n", 1)
    selection = preparation.split("      - name: Select an explicitly documented release", 1)[1]
    selection = selection.split("\n      - name:", 1)[0]

    skip = selection.index('git rev-parse -q --verify "refs/tags/${tag}^{commit}"')
    assert selection.index("git fetch origin main --tags") < skip
    assert selection.index('if [[ "$tagged" != "$(git rev-parse HEAD)" ]]; then') > skip
    assert 'echo "selected=false" >> "$GITHUB_OUTPUT"' in selection[skip:]
    assert skip < selection.index('echo "selected=true" >> "$GITHUB_OUTPUT"')


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
    setup = f"BreadSched-{version}-setup.exe"
    installer = root / "installer"
    installer.mkdir()
    (installer / setup).write_bytes(b"installer")
    setup_line = f"{hashlib.sha256(b'installer').hexdigest()}  {setup}\n"
    (installer / f"{setup}.sha256").write_text(setup_line, encoding="utf-8")

    def validate() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-e", "-c", script],
            cwd=tmp_path,
            env={**os.environ, "VERSION": version, "TAG": tag},
            text=True,
            capture_output=True,
            check=False,
        )

    assert validate().returncode == 0, validate().stderr
    # The published SHA256SUMS then covers the wheel, sdist, and installer.
    published = (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    assert [line.split("  ")[1] for line in published] == [*names, setup]
    (root / "SHA256SUMS").write_text("\n".join(published[:2]) + "\n", encoding="utf-8")

    # A tampered installer, a Windows-style checksum line, or a stray file is refused.
    (installer / setup).write_bytes(b"tampered")
    assert validate().returncode != 0
    (installer / setup).write_bytes(b"installer")
    (installer / f"{setup}.sha256").write_text(setup_line.replace("  ", " *"), encoding="utf-8")
    assert validate().returncode != 0
    (installer / f"{setup}.sha256").write_text(setup_line, encoding="utf-8")
    (installer / "other.exe").write_bytes(b"x")
    assert validate().returncode != 0
    (installer / "other.exe").unlink()
    (root / "SHA256SUMS").write_text("\n".join(published[:2]) + "\n", encoding="utf-8")
    assert validate().returncode == 0
    # Each accepted run appends the installer line; start the next cases clean.
    (root / "SHA256SUMS").write_text("\n".join(published[:2]) + "\n", encoding="utf-8")

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
