"""Add or remove the BreadSched directory on the current user's PATH.

The Windows installer runs this with its own bundled Python when the user opts
in to the command line on ``PATH`` (and the uninstaller always runs ``remove``).
It edits only ``HKCU\\Environment\\Path``, keeps every other entry exactly as
written, including unexpanded ``%VARIABLES%``, and keeps the value's registry
type, so a long or expandable PATH is never truncated or flattened. Other
programs see the change after the usual settings-change broadcast.

Usage: python user_path.py add|remove <directory>
"""

from __future__ import annotations

import sys


def _same(entry: str, directory: str) -> bool:
    return entry.strip().rstrip("\\/").casefold() == directory.rstrip("\\/").casefold()


def with_entry(path: str, directory: str) -> str:
    """Return ``path`` with ``directory`` appended unless it is already listed.

    A trailing separator stays trailing, so ``without_entry`` gives back the
    original value exactly.
    """
    entries = path.split(";") if path else []
    if any(_same(entry, directory) for entry in entries):
        return path
    if not path:
        return directory
    return f"{path}{directory};" if path.endswith(";") else f"{path};{directory}"


def without_entry(path: str, directory: str) -> str:
    """Return ``path`` with every occurrence of ``directory`` removed."""
    entries = path.split(";")
    kept = [entry for entry in entries if not _same(entry, directory)]
    return path if len(kept) == len(entries) else ";".join(kept)


def _update(action: str, directory: str) -> None:  # pragma: no cover - Windows only
    import ctypes
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE
    ) as key:
        try:
            current, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current, kind = "", winreg.REG_EXPAND_SZ
        change = with_entry if action == "add" else without_entry
        updated = change(current, directory)
        if updated == current:
            return
        if updated:
            winreg.SetValueEx(key, "Path", 0, kind, updated)
        else:
            winreg.DeleteValue(key, "Path")
    # Tell Explorer (and so newly started programs) that the environment changed.
    hwnd_broadcast, wm_settingchange, smto_abortifhung = 0xFFFF, 0x001A, 0x0002
    result = ctypes.c_ulong()
    ctypes.windll.user32.SendMessageTimeoutW(  # type: ignore[attr-defined]
        hwnd_broadcast,
        wm_settingchange,
        0,
        "Environment",
        smto_abortifhung,
        5000,
        ctypes.byref(result),
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("add", "remove") or not argv[1]:
        print("usage: user_path.py add|remove <directory>", file=sys.stderr)
        return 2
    _update(argv[0], argv[1])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
