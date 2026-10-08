#!/usr/bin/env bash
# Build a Debian/Ubuntu .deb or a Fedora .rpm from BreadSched's wheel.
#
#   packaging/linux/build-package.sh deb|rpm WHEEL OUTPUT_DIR
#
# Prints the absolute path of the package it wrote; install it by that path.
#
# Both formats install the same tree: the pure-Python package in a private
# directory (/usr/lib/breadsched), small launchers in /usr/bin that use the
# system Python 3, and the desktop entry, AppStream metadata, and icon under
# /usr/share. PyGObject, pycairo, and GTK 4 come from the distribution, and
# Finance::Quote is recommended so that its quote sources work (it cannot from
# the Flatpak). The .deb is assembled with dpkg-deb; the .rpm with rpmbuild, so
# run the rpm mode on Fedora (or another rpmbuild host).
set -euo pipefail

format=${1:?usage: build-package.sh deb|rpm WHEEL OUTPUT_DIR}
wheel=$(realpath "${2:?missing wheel}")
output=$(realpath -m "${3:?missing output directory}")
root=$(cd "$(dirname "$0")/../.." && pwd)

name=$(basename "$wheel")
[[ "$name" =~ ^breadsched-([0-9]+\.[0-9]+\.[0-9]+)(a([0-9]+))?-py3-none-any\.whl$ ]] || {
  echo "not a BreadSched wheel: $name" >&2
  exit 2
}
release=${BASH_REMATCH[1]}
alpha=${BASH_REMATCH[3]}
version=$release${alpha:+a$alpha}
# A tilde sorts before the release itself in both dpkg and rpm: 0.2.0~a257 < 0.2.0.
package_version=$release${alpha:+~a$alpha}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
stage="$work/stage"

# Staging into a private --target directory touches nothing the system's Python
# manages, so PEP 668's guard (Ubuntu's "externally managed" pip) does not apply.
PIP_BREAK_SYSTEM_PACKAGES=1 python3 -m pip install --quiet --no-deps --no-compile --target "$stage/usr/lib/breadsched" "$wheel"
# pip's install record and source URL describe this build machine, not the package.
rm -rf "$stage/usr/lib/breadsched/bin" "$stage/usr/lib/breadsched"/breadsched-*.dist-info/{RECORD,direct_url.json,REQUESTED}

launcher() {
  local path=$1 module=$2
  install -d "$(dirname "$path")"
  cat > "$path" <<EOF
#!/usr/bin/python3
# BreadSched $version, installed from the $format package.
import sys

sys.path.insert(0, "/usr/lib/breadsched")
from $module import main

sys.exit(main())
EOF
  chmod 0755 "$path"
}
launcher "$stage/usr/bin/breadsched" breadsched.cli.main
launcher "$stage/usr/bin/breadsched-gtk" breadsched.gui.launcher

install -Dm644 "$root/data/org.breadsched.BreadSched.desktop" \
  "$stage/usr/share/applications/org.breadsched.BreadSched.desktop"
install -Dm644 "$root/data/org.breadsched.BreadSched.metainfo.xml" \
  "$stage/usr/share/metainfo/org.breadsched.BreadSched.metainfo.xml"
install -Dm644 "$root/data/icons/org.breadsched.BreadSched.svg" \
  "$stage/usr/share/icons/hicolor/scalable/apps/org.breadsched.BreadSched.svg"
install -Dm644 "$root/LICENSE" "$stage/usr/share/doc/breadsched/copyright"

summary="Household finance: ledger, dated planning, and multi-year projection"
description="BreadSched imports GnuCash books and adds a dated cash-flow Plan,
scenario projections, FSA and reimbursement tracking, and online quotes.
It has a GTK 4 desktop interface, a local browser interface, and a
command line."

mkdir -p "$output"
case "$format" in
  deb)
    install -d "$stage/DEBIAN"
    cat > "$stage/DEBIAN/control" <<EOF
Package: breadsched
Version: $package_version
Architecture: all
Maintainer: Howard Holm <hdholm@alumni.iastate.edu>
Section: misc
Priority: optional
Homepage: https://github.com/hdholm/BreadSched
Depends: python3 (>= 3.11), python3-gi, python3-gi-cairo, python3-cairo, gir1.2-gtk-4.0
Recommends: libfinance-quote-perl
Description: $summary
$(sed 's/^/ /' <<< "$description")
EOF
    # Compile for whichever Python 3 the system has; remove what was compiled.
    cat > "$stage/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
python3 -m compileall -q /usr/lib/breadsched >/dev/null 2>&1 || true
EOF
    cat > "$stage/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
find /usr/lib/breadsched -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
EOF
    chmod 0755 "$stage/DEBIAN/postinst" "$stage/DEBIAN/prerm"
    # The file name keeps the plain version: release hosts may rewrite "~".
    file="$output/breadsched_${version}_all.deb"
    dpkg-deb --root-owner-group -Zxz --build "$stage" "$file" >/dev/null
    ;;
  rpm)
    top="$work/rpmbuild"
    mkdir -p "$top"/{SOURCES,SPECS,BUILD,RPMS,SRPMS}
    tar -C "$stage" -czf "$top/SOURCES/breadsched-tree.tar.gz" .
    cat > "$top/SPECS/breadsched.spec" <<EOF
Name:           breadsched
Version:        $package_version
Release:        1
Summary:        $summary
License:        AGPL-3.0-or-later
URL:            https://github.com/hdholm/BreadSched
Source0:        breadsched-tree.tar.gz
BuildArch:      noarch
Requires:       python3 >= 3.11
Requires:       python3-gobject
Requires:       python3-cairo
Requires:       gtk4
Recommends:     perl-Finance-Quote
# rpmbuild's byte-compilation step needs a Python for files outside site-packages.
%global __python %{__python3}

%description
$description

%prep
%setup -q -c -n breadsched-tree

%install
cp -a . %{buildroot}/

# As in the .deb: compile for whichever Python 3 the system has, and remove the
# caches (compiled here or by Python at run time) before the files themselves go,
# so removing the package leaves nothing behind in /usr/lib/breadsched.
%post
python3 -m compileall -q /usr/lib/breadsched >/dev/null 2>&1 || :

%preun
if [ \$1 -eq 0 ]; then
  find /usr/lib/breadsched -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || :
fi

%files
/usr/lib/breadsched
/usr/bin/breadsched
/usr/bin/breadsched-gtk
/usr/share/applications/org.breadsched.BreadSched.desktop
/usr/share/metainfo/org.breadsched.BreadSched.metainfo.xml
/usr/share/icons/hicolor/scalable/apps/org.breadsched.BreadSched.svg
/usr/share/doc/breadsched/copyright
EOF
    rpmbuild --quiet --define "_topdir $top" -bb "$top/SPECS/breadsched.spec"
    file="$output/breadsched-${version}-1.noarch.rpm"
    cp "$(find "$top/RPMS" -name '*.rpm' -print -quit)" "$file"
    ;;
  *)
    echo "unknown format: $format (expected deb or rpm)" >&2
    exit 2
    ;;
esac
echo "$file"
