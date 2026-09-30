#!/usr/bin/env bash
# Drive the file chooser and print dialog through the real desktop portal.
#
# Starts portal_test_backend.py as the desktop's portal backend and the real
# xdg-desktop-portal frontend in front of it, then runs flatpak_desktop_checks.py
# with the command given after "--" (CI: `flatpak run ... --command=python3 APP`).
# Afterwards it checks on the host that the book the backend chose outside
# Documents was opened, the export and backup were written where the portal
# said, every printable report reached the print portal as a PDF, and each
# request came from EXPECT_APP_ID (org.breadsched.BreadSched in the sandbox).
#
# Run it inside dbus-run-session and a display (xvfb-run). WORK_DIR must be
# visible to the application (inside Documents for the Flatpak); OUTSIDE_DIR
# must not be.
#
# Usage: flatpak_desktop_checks.sh WORK_DIR OUTSIDE_DIR BOOK -- COMMAND...
set -euo pipefail

work=$1
outside=$2
book=$3
shift 3
test "$1" = "--"
shift

here=$(cd "$(dirname "$0")" && pwd)
host_python=${HOST_PYTHON:-/usr/bin/python3}
expect_app_id=${EXPECT_APP_ID-org.breadsched.BreadSched}

if [ -z "${XDG_RUNTIME_DIR:-}" ] || [ ! -w "${XDG_RUNTIME_DIR}" ]; then
  XDG_RUNTIME_DIR=$(mktemp -d)
  export XDG_RUNTIME_DIR
fi
chmod 700 "$XDG_RUNTIME_DIR"

mkdir -p "$work/prints" "$work/portals" "$work/portal-config/xdg-desktop-portal"
cp "$book" "$outside/chosen.breadsched"
cat > "$work/answers.json" <<JSON
{
  "open": {"Open book": "$outside/chosen.breadsched"},
  "save": {
    "Export transactions": "$outside/exported.csv",
    "Back up book": "$outside/chosen.breadsched.backup"
  },
  "print_dir": "$work/prints"
}
JSON
cat > "$work/portals/breadschedtest.portal" <<'PORTAL'
[portal]
DBusName=org.freedesktop.impl.portal.desktop.breadschedtest
Interfaces=org.freedesktop.impl.portal.FileChooser;org.freedesktop.impl.portal.Print;
UseIn=breadschedtest
PORTAL
cat > "$work/portal-config/xdg-desktop-portal/breadschedtest-portals.conf" <<'CONF'
[preferred]
default=breadschedtest
CONF

log="$work/portal-log.jsonl"
: > "$log"
"$host_python" "$here/portal_test_backend.py" "$work/answers.json" "$log" &
backend=$!
/usr/libexec/xdg-document-portal --replace &
documents=$!
trap 'kill "$backend" "${frontend:-}" "$documents" 2>/dev/null || true
  fusermount3 -u "$XDG_RUNTIME_DIR/doc" 2>/dev/null || true' EXIT
for _ in $(seq 50); do
  grep -q '"ready"' "$log" && break
  sleep 0.2
done
grep -q '"ready"' "$log"
env XDG_CURRENT_DESKTOP=breadschedtest \
  XDG_DESKTOP_PORTAL_DIR="$work/portals" \
  XDG_CONFIG_HOME="$work/portal-config" \
  /usr/libexec/xdg-desktop-portal --replace &
frontend=$!
for _ in $(seq 50); do
  gdbus call --session --dest org.freedesktop.portal.Desktop \
    --object-path /org/freedesktop/portal/desktop \
    --method org.freedesktop.DBus.Properties.Get \
    org.freedesktop.portal.FileChooser version >/dev/null 2>&1 && break
  sleep 0.2
done

"$@" "$work/flatpak_desktop_checks.py" "$book" "$work/prints" "$work/report.json"

"$host_python" - "$work/report.json" "$log" "$outside" "$expect_app_id" <<'PY'
import json
import sqlite3
import sys
from pathlib import Path

report_path, log_path, outside, app_id = sys.argv[1:]
report = json.loads(Path(report_path).read_text(encoding="utf-8"))
log = [json.loads(line) for line in Path(log_path).read_text(encoding="utf-8").splitlines()]
requests = [entry for entry in log if entry["method"] not in ("ready", "name-lost")]
assert not [entry for entry in log if "error" in entry], log
assert {entry["app_id"] for entry in requests} == {app_id}, requests
titles = [(entry["method"], entry["title"]) for entry in requests]
assert ("open", "Open book") in titles, titles
assert ("save", "Export transactions") in titles, titles
assert ("save", "Back up book") in titles, titles

outside = Path(outside)
if app_id:
    # Sandboxed: the book outside Documents arrived through the document portal.
    assert report["opened_through_portal"], report["opened"]
    assert report["open_notice"] and "Keep books in Documents" in report["open_notice"][0]
exported = outside / "exported.csv"
assert exported.read_text(encoding="utf-8").startswith("date,"), exported
backup = outside / "chosen.breadsched.backup"
with sqlite3.connect(f"file:{backup}?mode=ro", uri=True) as raw:
    assert raw.execute("PRAGMA integrity_check").fetchone() == ("ok",)
hidden = [path.name for path in outside.iterdir() if path.name.startswith(".xdp-")]
assert not hidden, hidden

prints = [entry for entry in requests if entry["method"] == "Print"]
assert len(prints) == len(report["printed"]) >= 3, (prints, report["printed"])
for entry in prints:
    data = Path(entry["path"]).read_bytes()
    assert data.startswith(b"%PDF") and entry["bytes"] > 1000, entry
assert "plan" in report["asked"], report["asked"]
assert not report["errors"], report["errors"]
print("portal checks passed:", json.dumps(report["printed"]))
PY
