#!/usr/bin/env bash
# Check an installed .deb or .rpm: versions, a book round trip, quotes, and GTK.
#
#   packaging/linux/smoke-test.sh EXPECTED_VERSION
set -euo pipefail
expected=${1:?usage: smoke-test.sh EXPECTED_VERSION}

actual=$(breadsched --version)
case "$actual" in
  "breadsched ${expected} (native schema "*) ;;
  *) echo "installed package reports '$actual'"; exit 1 ;;
esac
gtk=$(breadsched-gtk --version)
test "$gtk" = "breadsched-gtk ${actual#breadsched }" || {
  echo "breadsched-gtk reports '$gtk'"
  exit 1
}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
breadsched sample "$work/sample.breadsched" >/dev/null
breadsched verify "$work/sample.breadsched" >/dev/null
status=$(breadsched quotes "$work/sample.breadsched" --list --json)
# Finance::Quote is recommended, so where it is installed the package must find it.
if perl -MFinance::Quote -MJSON::PP -e 1 2>/dev/null; then
  python3 -c 'import json, sys; assert json.load(sys.stdin)["finance_quote"]["available"]' <<< "$status"
fi

# The GUI's runtime comes from the distribution's packages.
python3 - <<'PY'
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: F401
import cairo  # noqa: F401
PY
test -f /usr/share/applications/org.breadsched.BreadSched.desktop
test -f /usr/share/metainfo/org.breadsched.BreadSched.metainfo.xml
test -f /usr/share/icons/hicolor/scalable/apps/org.breadsched.BreadSched.svg
echo "installed package checks passed: $actual"
