"""Installed desktop integration stays consistent with the application identity.

The Flatpak manifest installs a desktop entry, AppStream metadata, and a themed
icon. Their identifiers must agree with ``APP_ID`` and the installed launcher, or
the desktop shows an entry that cannot start, has no icon, or is rejected by
software centres. The Windows installer gives its shortcut, its Apps entry, and
itself the BreadSched icon, the same images as the packaged hicolor icons.
"""

from __future__ import annotations

import configparser
import importlib.util
import json
import shutil
import struct
import subprocess
import tomllib
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

import pytest

from breadsched import APP_ID

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "data" / f"{APP_ID}.desktop"
METAINFO = ROOT / "data" / f"{APP_ID}.metainfo.xml"
ICON = ROOT / "data" / "icons" / f"{APP_ID}.svg"
WINDOWS_ICON = ROOT / "data" / "icons" / f"{APP_ID}.ico"
HICOLOR = ROOT / "src" / "breadsched" / "gui" / "resources" / "icons" / "hicolor"
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


def test_metainfo_lists_every_release_newest_first_with_its_notes():
    """Software centres show the metainfo's releases, so the newest is this version,
    in the stage it is released at, and each listed release has its notes."""
    from datetime import date

    from breadsched import RELEASE_STAGE, __version__

    releases = ET.parse(METAINFO).getroot().find("releases")
    assert releases is not None
    listed = releases.findall("release")
    versions = [str(item.get("version")) for item in listed]

    def key(version: str) -> tuple[int, ...]:
        return tuple(int(part) for part in version.split("."))

    assert versions[0] == __version__
    assert versions == sorted(set(versions), key=key, reverse=True)
    assert listed[0].get("type") == ("development" if RELEASE_STAGE == "beta" else "stable")
    for item, version in zip(listed, versions, strict=True):
        year, month, day, _patch = key(version)
        # A calendar version names the day it was prepared; a patch may ship later.
        assert date.fromisoformat(str(item.get("date"))) >= date(year, month, day)
        assert item.findtext("url") == (
            f"https://github.com/hdholm/BreadSched/releases/tag/v{version}"
        )
        assert (ROOT / "docs" / "releases" / f"v{version}.md").is_file(), version


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
    """The scalable icon carries the 512-pixel artwork that ``scripts/brand_assets.py``
    makes from ``data/brand``, filling the whole view."""
    import base64

    root = ET.parse(ICON).getroot()
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    assert root.get("viewBox") == "0 0 512 512"
    [image] = root.findall("{http://www.w3.org/2000/svg}image")
    assert (image.get("width"), image.get("height")) == ("512", "512")
    location = image.get("{http://www.w3.org/1999/xlink}href")
    prefix = "data:image/png;base64,"
    assert location.startswith(prefix)
    png = base64.b64decode(location[len(prefix) :])
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    # The PNG header's width and height.
    assert (int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")) == (512, 512)


def _png_rgba(data: bytes) -> tuple[int, int, bytes]:
    """Width, height, and RGBA rows of an 8-bit RGBA, non-interlaced PNG."""
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    position, chunks, header = 8, b"", b""
    while position < len(data):
        (length,) = struct.unpack(">I", data[position : position + 4])
        kind = data[position + 4 : position + 8]
        body = data[position + 8 : position + 8 + length]
        if kind == b"IHDR":
            header = body
        elif kind == b"IDAT":
            chunks += body
        position += 12 + length
    width, height, depth, colour, _c, _f, interlace = struct.unpack(">IIBBBBB", header)
    assert (depth, colour, interlace) == (8, 6, 0), "an 8-bit RGBA, non-interlaced PNG"
    raw, stride, rows = zlib.decompress(chunks), width * 4, []
    previous = bytearray(stride)
    for y in range(height):
        kind, line = (
            raw[y * (stride + 1)],
            bytearray(raw[y * (stride + 1) + 1 : (y + 1) * (stride + 1)]),
        )
        for x in range(stride):
            left = line[x - 4] if x >= 4 else 0
            up, corner = previous[x], previous[x - 4] if x >= 4 else 0
            if kind == 1:
                line[x] = (line[x] + left) & 0xFF
            elif kind == 2:
                line[x] = (line[x] + up) & 0xFF
            elif kind == 3:
                line[x] = (line[x] + (left + up) // 2) & 0xFF
            elif kind == 4:
                guess = left + up - corner
                near = min(
                    (abs(guess - left), 0, left),
                    (abs(guess - up), 1, up),
                    (abs(guess - corner), 2, corner),
                )[2]
                line[x] = (line[x] + near) & 0xFF
        rows.append(bytes(line))
        previous = line
    return width, height, b"".join(rows)


def test_windows_icon_holds_each_packaged_icon_size_as_a_bitmap():
    """``data/icons/*.ico`` is the hicolor PNGs from 16 to 256 pixels, each a 32-bit
    bitmap with alpha that shows exactly the packaged image."""
    data = WINDOWS_ICON.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1)
    sizes = []
    for index in range(count):
        width, height, _colours, _r, planes, bits, length, offset = struct.unpack(
            "<BBBBHHII", data[6 + 16 * index : 22 + 16 * index]
        )
        side = width or 256
        assert height == width and (planes, bits) == (1, 32)
        bitmap = data[offset : offset + length]
        size, dib_width, dib_height, _planes, dib_bits, compression = struct.unpack(
            "<IiiHHI", bitmap[:20]
        )
        assert (size, dib_width, dib_height, dib_bits, compression) == (40, side, 2 * side, 32, 0)
        stride = side * 4
        pixels = bitmap[40 : 40 + stride * side]
        assert len(bitmap) == 40 + stride * side + ((side + 31) // 32) * 4 * side
        # Bottom-up BGRA rows, as Windows stores them.
        rgba = b"".join(
            bytes(
                value
                for x in range(side)
                for value in (row[4 * x + 2], row[4 * x + 1], row[4 * x], row[4 * x + 3])
            )
            for row in reversed([pixels[y * stride : (y + 1) * stride] for y in range(side)])
        )
        png = HICOLOR / f"{side}x{side}" / "apps" / f"{APP_ID}.png"
        assert _png_rgba(png.read_bytes()) == (side, side, rgba), side
        sizes.append(side)
    assert sizes == [16, 24, 32, 48, 64, 128, 256]


def test_windows_installer_shows_the_breadsched_icon():
    """The shortcut runs pythonw.exe, so without its own icon it shows Python's."""
    windows = ROOT / "packaging" / "windows"
    build = (windows / "build-installer.sh").read_text(encoding="utf-8")
    script = (windows / "breadsched.nsi").read_text(encoding="utf-8")
    check = (windows / "test-installer.ps1").read_text(encoding="utf-8")
    desktop = (ROOT / "scripts" / "windows_desktop_checks.py").read_text(encoding="utf-8")

    assert f'cp "$root/data/icons/{WINDOWS_ICON.name}" "$stage/breadsched.ico"' in build
    assert '!define MUI_ICON "${STAGE}\\breadsched.ico"' in script
    assert '!define MUI_UNICON "${STAGE}\\breadsched.ico"' in script
    assert script.index("MUI_ICON") < script.index("!insertmacro MUI_PAGE_LICENSE")
    shortcut = script[script.index('CreateShortcut "$SMPROGRAMS\\${APP}\\${APP}.lnk"') :]
    assert shortcut.split("\n")[1].strip() == '"$INSTDIR\\breadsched.ico" 0'
    assert 'WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\\breadsched.ico"' in script
    assert 'Delete "$INSTDIR\\breadsched.ico"' in script.split('Section "Uninstall"', 1)[1]
    # On Windows, every install of this build checks the shortcut and Apps entry,
    # and the running window must carry an icon of its own.
    assert "if ($Setup -eq $Installer) { Test-Branding $Target }" in check
    assert '$shortcut.IconLocation -ne "$icon,0"' in check
    assert "(Get-ItemProperty $uninstallKey).DisplayIcon" in check
    assert "icon left behind after uninstall" in check
    assert 'wait_for(branded, "the main window\'s own icon")' in desktop
    assert "WM_GETICON = 0x007F" in desktop


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


def test_ci_uses_books_outside_documents_through_the_real_document_portal():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    check = (ROOT / "scripts" / "flatpak_portal_check.sh").read_text(encoding="utf-8")

    assert "xdg-desktop-portal fuse3 dbus" in workflow
    assert "dbus-run-session -- bash scripts/flatpak_portal_check.sh" in workflow
    assert "tests/fixtures/native/schema-13.sql" in workflow
    # The real document portal grants the installed app one existing book (Open)
    # and one that does not exist yet (Save).
    assert "/usr/libexec/xdg-document-portal --replace" in check
    exporter = (ROOT / "scripts" / "portal_export.py").read_text(encoding="utf-8")
    assert 'export_document "$old_book"' in check
    assert 'export_document "$new_book" --new' in check
    for call in ('"Add"', '"AddNamed"', '"GrantPermissions"'):
        assert call in exporter, call
    assert 'run migrate "$old_doc" --json' in check
    # The backup lands in the app's data folder, and nothing hidden beside the book.
    assert "/data/breadsched/beside-documents/" in check
    assert 'name.startswith(".xdp-")' in check


def test_ci_drives_the_portal_file_chooser_and_print_dialog_in_the_sandbox():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    harness = (ROOT / "scripts" / "flatpak_desktop_checks.sh").read_text(encoding="utf-8")
    checks = (ROOT / "scripts" / "flatpak_desktop_checks.py").read_text(encoding="utf-8")
    backend = (ROOT / "scripts" / "portal_test_backend.py").read_text(encoding="utf-8")

    assert "bash scripts/flatpak_desktop_checks.sh" in workflow
    # The chooser returns the host's document path, which the sandbox shows only
    # when the portal is mounted at the standard /run/user/UID/doc.
    assert "PORTAL_TAKE_OVER_RUNTIME=1" in workflow
    assert "XDG_RUNTIME_DIR=/run/user/$(id -u)" in harness
    assert '--command=python3 "$app"' in workflow
    assert "python3-gi" in workflow
    # The real portal frontend sits between the application and the stand-in
    # backend, which answers only as the person would.
    assert "/usr/libexec/xdg-desktop-portal --replace" in harness
    assert "UseIn=breadschedtest" in harness and "default=breadschedtest" in harness
    for interface in ("impl.portal.FileChooser", "impl.portal.Print"):
        assert interface in backend and interface in harness
    for method in ("OpenFile", "SaveFile", "PreparePrint", '"Print"'):
        assert method in backend, method
    # The application's own actions, through the portal.
    for action in ("app.on_open()", "app.on_export", "app.on_backup", "print_action.activate"):
        assert action in checks, action
    assert "portal_document_id(opened)" in checks
    assert "printing.optional_choice_label(document)" in checks
    # Checked on the host: the caller, where each file went, and every printout.
    assert '{entry["app_id"] for entry in requests} == {app_id}' in harness
    assert 'data.startswith(b"%PDF")' in harness
    assert '"plan" in report["asked"]' in harness


def test_the_sandbox_smoke_prints_each_report_through_gtk():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    smoke = (ROOT / "scripts" / "flatpak_gtk_smoke.py").read_text(encoding="utf-8")

    assert "Gtk.PrintOperationAction.EXPORT" in smoke
    assert '{"dashboard", "plan", "projection"} <= set(report["printed"])' in workflow


def test_windows_installer_carries_its_runtime_and_is_tested_in_ci():
    windows = ROOT / "packaging" / "windows"
    build = (windows / "build-installer.sh").read_text(encoding="utf-8")
    script = (windows / "breadsched.nsi").read_text(encoding="utf-8")
    check = (windows / "test-installer.ps1").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    # The installed icon and launchers match the application identity.
    assert f"data/icons/{ICON.name}" in build
    assert "-m breadsched.cli.main" in build and "-m breadsched.gui" in script
    # The NSIS plugin directory is named explicitly: MSYS2's NSIS has no x86-unicode one.
    assert "!addplugindir /x86-unicode" in build and "nsDialogs.dll" in build
    assert '"$makensis_bin" -V2 -X"!addplugindir /x86-unicode' in build
    # makensis, its stubs, and its plugins come from one NSIS release.
    assert '"$nsis/makensis.exe"' in build and '"$nsis/Plugins/x86-unicode"' in build
    # MSYS2's NSIS carries no x86-unicode plugins, so CI provides the official build's.
    for name in ("ci.yml", "release.yml"):
        text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        assert text.index("choco install nsis") < text.index("build-installer.sh"), name
        # Chocolatey exits 0 when its feed fails, so the step checks and retries.
        step = text[text.index("Provide NSIS plugins") : text.index("build-installer.sh")]
        assert "x86-unicode\\nsDialogs.dll" in step and "Test-Path" in step, name
        assert "$attempt -le 4" in step and "exit 1" in step, name
        job = text[text.index("runs-on: windows-latest\n") :]
        assert "timeout-minutes: 30" in job[: job.index("steps:")], name
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


def _user_path_module():
    source = ROOT / "packaging" / "windows" / "user_path.py"
    spec = importlib.util.spec_from_file_location("breadsched_user_path", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_user_path_adds_the_install_directory_once_and_keeps_other_entries():
    user_path = _user_path_module()
    install = r"C:\Users\A B\AppData\Local\Programs\BreadSched"
    original = r"%USERPROFILE%\AppData\Local\Microsoft\WindowsApps;C:\Tools;"

    added = user_path.with_entry(original, install)
    assert added == original + install + ";"
    # Already listed (any case, trailing separator): unchanged.
    assert user_path.with_entry(added, install) == added
    assert user_path.with_entry(added, install.upper() + "\\") == added
    assert user_path.with_entry("", install) == install

    # Removal restores the original entries, unexpanded variables included.
    assert user_path.without_entry(added, install) == original
    # Without a trailing separator, too, removal is the exact inverse of adding.
    assert user_path.with_entry("C:\\Tools", install) == f"C:\\Tools;{install}"
    assert user_path.without_entry(f"C:\\Tools;{install}", install) == "C:\\Tools"
    assert user_path.without_entry(install, install) == ""
    assert user_path.without_entry(f"{install};C:\\Tools;{install}\\", install) == "C:\\Tools"
    # Nothing to remove: the value is returned exactly as it was.
    assert user_path.without_entry(original, install) == original
    # Only the exact directory is removed, not one that merely shares a prefix.
    assert user_path.without_entry(install + "Beta", install) == install + "Beta"
    assert user_path.main(["replace", install]) == 2


def test_windows_installer_offers_path_as_an_opt_in_that_uninstall_removes():
    windows = ROOT / "packaging" / "windows"
    script = (windows / "breadsched.nsi").read_text(encoding="utf-8")
    build = (windows / "build-installer.sh").read_text(encoding="utf-8")
    check = (windows / "test-installer.ps1").read_text(encoding="utf-8")

    assert 'cp "$here/user_path.py" "$stage/user_path.py"' in build
    # Off by default (/o), chosen on the Components page or with /ADDTOPATH.
    assert 'Section /o "Add the breadsched command to PATH" SecPath' in script
    assert "!insertmacro MUI_PAGE_COMPONENTS" in script
    assert '${GetOptions} $1 "/ADDTOPATH" $2' in script
    assert 'ReadRegDWORD $0 HKCU "Software\\${APP}" "AddToPath"' in script
    uninstall = script.split('Section "Uninstall"', 1)[1]
    # The bundled Python removes the entry before the runtime is deleted.
    assert uninstall.index('remove "$INSTDIR"') < uninstall.index('RMDir /r "$INSTDIR\\runtime"')
    assert 'Delete "$INSTDIR\\user_path.py"' in uninstall
    # CI checks default, opt-in, remembered choice, and exact restoration.
    assert '"a default install changed PATH"' in check
    assert '@("/ADDTOPATH")' in check
    assert "Get-Command breadsched" in check
    assert '"a later install did not keep the PATH choice"' in check
    assert '"DoNotExpandEnvironmentNames"' in check
    assert "uninstall did not restore the user PATH" in check


def test_windows_installer_stops_runtime_helpers_before_touching_the_runtime():
    windows = ROOT / "packaging" / "windows"
    script = (windows / "breadsched.nsi").read_text(encoding="utf-8")
    build = (windows / "build-installer.sh").read_text(encoding="utf-8")
    helpers = (windows / "stop-helpers.ps1").read_text(encoding="utf-8")

    # GLib's gdbus.exe session bus outlives the application and holds runtime
    # files open, so both install and uninstall stop it before removing runtime\.
    assert 'cp "$here/stop-helpers.ps1" "$stage/stop-helpers.ps1"' in build
    install, uninstall = script.split('Section "Uninstall"', 1)
    main = install.split('Section "BreadSched" SecMain', 1)[1]
    assert main.index('"$PLUGINSDIR\\stop-helpers.ps1"') < main.index(
        'RMDir /r "$INSTDIR\\runtime"'
    )
    assert uninstall.index('"$INSTDIR\\stop-helpers.ps1"') < uninstall.index(
        'RMDir /r "$INSTDIR\\runtime"'
    )
    assert 'Delete "$INSTDIR\\stop-helpers.ps1"' in uninstall
    # Only helpers running from this installation's runtime are stopped.
    assert "Name = 'gdbus.exe'" in helpers
    assert "StartsWith($runtime, [StringComparison]::OrdinalIgnoreCase)" in helpers


def test_windows_installer_drives_the_native_file_chooser_and_printing():
    windows = ROOT / "packaging" / "windows"
    check = (windows / "test-installer.ps1").read_text(encoding="utf-8")
    script = (ROOT / "scripts" / "windows_desktop_checks.py").read_text(encoding="utf-8")

    # The installed copy's own Python runs the check, after the GTK smoke.
    assert check.index("flatpak_gtk_smoke.py") < check.index("windows_desktop_checks.py")
    assert (
        'runtime\\bin\\python.exe") (Join-Path $root "scripts\\windows_desktop_checks.py")' in check
    )
    # Real application actions and the real native dialogs, found by title.
    for action in ("app.on_open()", "app.on_export()", "window.print_action.activate(None)"):
        assert action in script, action
    assert 'choose("Open book", target)' in script
    assert 'choose("Export transactions", ' in script
    # The export is checked where the native save dialog told the application.
    assert 'recording("save", Gtk.FileDialog.save_finish)' in script
    assert 'finished.get("save")' in script
    assert 'win32.find("#32770", title)' in script
    assert "WM_COMMAND, IDOK" in script
    # The chosen book must actually open, and every printed report must have a
    # default handler that opens it.
    assert "Path(app.book_path).resolve() == target" in script
    assert "Gio.AppInfo.get_default_for_type" in script
    assert "launch_default_for_uri" in script


@pytest.mark.skipif(shutil.which("desktop-file-validate") is None, reason="validator missing")
def test_desktop_entry_passes_desktop_file_validate():
    subprocess.run(["desktop-file-validate", str(DESKTOP)], check=True)


@pytest.mark.skipif(shutil.which("appstreamcli") is None, reason="validator missing")
def test_metainfo_passes_appstream_validation():
    subprocess.run(["appstreamcli", "validate", "--no-net", str(METAINFO)], check=True)
