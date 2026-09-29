"""Installed desktop integration stays consistent with the application identity.

The Flatpak manifest installs a desktop entry, AppStream metadata, and a themed
icon. Their identifiers must agree with ``APP_ID`` and the installed launcher, or
the desktop shows an entry that cannot start, has no icon, or is rejected by
software centres.
"""

from __future__ import annotations

import configparser
import json
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from breadsched import APP_ID

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on Python 3.10 CI
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "data" / f"{APP_ID}.desktop"
METAINFO = ROOT / "data" / f"{APP_ID}.metainfo.xml"
ICON = ROOT / "data" / "icons" / f"{APP_ID}.svg"
MANIFEST = ROOT / f"{APP_ID}.json"


def _desktop_entry() -> configparser.SectionProxy:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read(DESKTOP, encoding="utf-8")
    return parser["Desktop Entry"]


def test_desktop_entry_launches_the_installed_gtk_command_with_the_app_icon():
    entry = _desktop_entry()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    scripts = project["gui-scripts"]

    assert entry["Type"] == "Application"
    assert entry["Exec"].split()[0] == "breadsched-gtk"
    assert "breadsched-gtk" in scripts
    assert entry["Icon"] == APP_ID
    assert entry["Terminal"] == "false"
    assert "Finance;" in entry["Categories"]


def test_metainfo_names_the_application_and_its_desktop_entry():
    component = ET.parse(METAINFO).getroot()

    assert component.get("type") == "desktop-application"
    assert component.findtext("id") == APP_ID
    assert component.findtext("project_license") == "AGPL-3.0-or-later"
    launchable = component.find("launchable")
    assert launchable is not None and launchable.get("type") == "desktop-id"
    assert launchable.text == DESKTOP.name
    assert component.find("content_rating") is not None


def test_icon_is_a_scalable_svg():
    root = ET.parse(ICON).getroot()
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    assert root.get("viewBox") == "0 0 128 128"


def test_flatpak_manifest_installs_each_desktop_file_under_the_app_id():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    commands = "\n".join(manifest["modules"][0]["build-commands"])

    assert manifest["id"] == APP_ID
    assert manifest["command"] == "breadsched-gtk"
    assert f"data/{DESKTOP.name} /app/share/applications/{DESKTOP.name}" in commands
    assert f"data/{METAINFO.name} /app/share/metainfo/{METAINFO.name}" in commands
    assert f"data/icons/{ICON.name} /app/share/icons/hicolor/scalable/apps/{APP_ID}.svg" in commands
    assert "graft data" in (ROOT / "MANIFEST.in").read_text(encoding="utf-8")


def test_ci_validates_metadata_and_drives_gtk_inside_the_sandbox():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert f"desktop-file-validate data/{DESKTOP.name}" in workflow
    assert f"appstreamcli validate --no-net data/{METAINFO.name}" in workflow
    assert "cp scripts/flatpak_gtk_smoke.py" in workflow
    assert "xvfb-run -a flatpak run --user --unshare=network" in workflow
    assert (ROOT / "scripts" / "flatpak_gtk_smoke.py").is_file()


def test_windows_installer_carries_its_runtime_and_is_tested_in_ci():
    windows = ROOT / "packaging" / "windows"
    build = (windows / "build-installer.sh").read_text(encoding="utf-8")
    script = (windows / "breadsched.nsi").read_text(encoding="utf-8")
    check = (windows / "test-installer.ps1").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    # The installed icon and launchers match the application identity.
    assert f"data/icons/{ICON.name}" in build
    assert "-m breadsched.cli.main" in build and "-m breadsched.gui" in script
    # Per-user, and an upgrade replaces the runtime without touching books.
    assert "RequestExecutionLevel user" in script
    assert 'RMDir /r "$INSTDIR\\runtime"' in script
    for step in ("sample", "verify", "guide --list", "flatpak_gtk_smoke.py", "Uninstall.exe"):
        assert step in check, step
    assert "uninstall removed the book" in check
    # Upgrade from the newest published installer, verified before it runs.
    fetch = (windows / "fetch-previous.ps1").read_text(encoding="utf-8")
    assert '-ne "v$Version"' in fetch and "SHA256SUMS" in fetch
    assert fetch.index("Get-FileHash") < fetch.index("Write-Output $setup.FullName")
    assert "[string]$Previous" in check
    upgrade = check.split("if ($Previous) {", 1)[1].split('\nWrite-Host "== clean install"', 1)[0]
    assert upgrade.index("Install-Once $Previous") < upgrade.index("Install-Once $Installer")
    assert 'StartsWith("breadsched $version ")' in upgrade
    assert "verify $published" in upgrade and "Uninstall-From $upgradeDir" in upgrade
    assert "packaging/windows/fetch-previous.ps1" in workflow
    assert '-Previous "$previous"' in workflow
    assert "windows-installer:" in workflow
    assert "packaging/windows/build-installer.sh" in workflow
    assert "packaging/windows/test-installer.ps1" in workflow
    # CI stages the release checksum on every pull request, so the release
    # job's staging runs before a release depends on it.
    stage = (windows / "stage-release.ps1").read_text(encoding="utf-8")
    assert '"$hash  $name`n"' in stage
    assert "packaging/windows/stage-release.ps1" in workflow
    assert "sha256sum --check BreadSched-*-setup.exe.sha256" in workflow


@pytest.mark.skipif(shutil.which("desktop-file-validate") is None, reason="validator missing")
def test_desktop_entry_passes_desktop_file_validate():
    subprocess.run(["desktop-file-validate", str(DESKTOP)], check=True)


@pytest.mark.skipif(shutil.which("appstreamcli") is None, reason="validator missing")
def test_metainfo_passes_appstream_validation():
    subprocess.run(["appstreamcli", "validate", "--no-net", str(METAINFO)], check=True)
