#!/usr/bin/env bash
# Use books outside Documents the way the file chooser hands them to the Flatpak.
#
# The Flatpak may use Documents directly; a book chosen anywhere else reaches it
# through the document portal as /run/user/UID/doc/ID/NAME, a directory holding
# only that file. This starts the real document portal, grants the installed app
# an existing schema 6 book (as Open does) and a book that does not exist yet (as
# Save does), and checks inside the sandbox that BreadSched creates, migrates,
# writes, and verifies them, that the host sees each change in the real file,
# and that the pre-migration backup lands in the app's data folder rather than
# as a hidden file beside the book.
#
# Usage (CI runs it with dbus-run-session): flatpak_portal_check.sh OLD_BOOK NEW_BOOK
set -euo pipefail

app=org.breadsched.BreadSched
old_book=$1
new_book=$2
uid=$(id -u)
# A private runtime directory: the desktop session (a CI runner has one) may already
# run its own document portal at $XDG_RUNTIME_DIR/doc, where a second portal
# cannot mount. Inside the sandbox Flatpak still shows ours as /run/user/UID/doc.
XDG_RUNTIME_DIR=$(mktemp -d)
export XDG_RUNTIME_DIR
chmod 700 "$XDG_RUNTIME_DIR"

/usr/libexec/xdg-document-portal --replace &
portal=$!
trap 'kill "$portal" 2>/dev/null || true
  fusermount3 -u "$XDG_RUNTIME_DIR/doc" 2>/dev/null || true' EXIT
for _ in $(seq 50); do
  mountpoint -q "$XDG_RUNTIME_DIR/doc" && break
  sleep 0.2
done
mountpoint -q "$XDG_RUNTIME_DIR/doc"

sandbox_path() {
  # document-export prints the host path; the sandbox sees the same document
  # under its own runtime directory.
  local exported=$1
  local id
  case "$exported" in
    "$XDG_RUNTIME_DIR"/doc/?*/?*) ;;
    *) echo "document export failed: '$exported'" >&2; exit 1 ;;
  esac
  id=$(basename "$(dirname "$exported")")
  printf '/run/user/%s/doc/%s/%s\n' "$uid" "$id" "$(basename "$exported")"
}

run() {
  flatpak run --user --unshare=network --command=breadsched "$app" "$@"
}

old_export=$(flatpak document-export --app="$app" -r -w "$old_book")
new_export=$(flatpak document-export --app="$app" -r -w -n "$new_book")
old_doc=$(sandbox_path "$old_export")
new_doc=$(sandbox_path "$new_export")
echo "old book in sandbox: $old_doc"
echo "new book in sandbox: $new_doc"

# Save: a new book created through the portal is the real file on the host.
run sample "$new_doc" --as-of 2026-09-15
run add "$new_doc" --description "Through the portal" \
  --from Checking --to Groceries --amount 1.25 --date 2026-09-15
run verify "$new_doc"
test -s "$new_book"

# Open: an older book migrates in place, with its backup in the data folder.
run migrate "$old_doc" --json > "$RUNNER_TEMP/portal-migrate.json"
run verify "$old_doc"
flatpak run --user --unshare=network --command=python3 "$app" - "$old_doc" "$new_doc" <<'PY'
import os
import sys

for book in sys.argv[1:]:
    beside = sorted(os.listdir(os.path.dirname(book)))
    assert beside == [os.path.basename(book)], beside
print("portal directories hold only their books")
PY

python3 - "$RUNNER_TEMP/portal-migrate.json" "$old_book" "$new_book" "$app" <<'PY'
import json
import os
import sqlite3
import sys

report_path, old_book, new_book, app = sys.argv[1:]
with open(report_path, encoding="utf-8") as source:
    report = json.load(source)
assert report["migrated"] and report["schema_before"] == 6, report
backup = report["backup"]
data = os.path.expanduser(f"~/.var/app/{app}/data/breadsched/beside-documents/")
assert backup.startswith(data), backup
assert backup.endswith("/old.breadsched.pre-migration-v6.bak"), backup
assert os.path.isfile(backup), backup
with sqlite3.connect(f"file:{backup}?mode=ro", uri=True) as raw:
    version = raw.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
assert version == ("6",), version
with sqlite3.connect(f"file:{old_book}?mode=ro", uri=True) as raw:
    version = raw.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
assert version == ("10",), version
with sqlite3.connect(f"file:{new_book}?mode=ro", uri=True) as raw:
    found = raw.execute("SELECT COUNT(*) FROM txn WHERE description = ?", ("Through the portal",))
    assert found.fetchone() == (1,)
for folder in {os.path.dirname(old_book), os.path.dirname(new_book)}:
    hidden = [name for name in os.listdir(folder) if name.startswith(".xdp-")]
    assert not hidden, hidden
print("host sees both books; backup at", backup)
PY
