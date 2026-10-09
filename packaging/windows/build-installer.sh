#!/usr/bin/env bash
# Build the BreadSched Windows installer from an MSYS2 UCRT64 shell.
#
# The installer carries its own Python, GTK 4, PyGObject, and cairo from the
# MSYS2 UCRT64 prefix, so nothing else has to be installed on the machine. The
# script installs the BreadSched wheel into that prefix, stages the prefix
# without headers, static libraries, and documentation, adds launchers and the
# application icon, and compiles packaging/windows/breadsched.nsi with NSIS.
#
# Usage (UCRT64 shell): packaging/windows/build-installer.sh [OUTPUT_DIR]
# Required packages: see the "windows-installer" job in .github/workflows/ci.yml.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
out=$(mkdir -p "${1:-$root/dist/windows}" && cd "${1:-$root/dist/windows}" && pwd)
prefix=${MINGW_PREFIX:?run this from an MSYS2 UCRT64 shell}
stage="$out/stage"

version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$root/src/breadsched/__init__.py")
[ -n "$version" ] || { echo "no __version__ in src/breadsched/__init__.py" >&2; exit 1; }
echo "Building BreadSched $version installer into $out"

# The wheel goes into the prefix being staged, next to MSYS2's PyGObject.
rm -rf "$out/wheel"
python -m pip wheel --no-deps --no-build-isolation -w "$out/wheel" "$root"
python -m pip install --no-deps --force-reinstall --break-system-packages \
    "$out"/wheel/breadsched-*.whl

rm -rf "$stage"
runtime="$stage/runtime"
mkdir -p "$runtime"
cp -a "$prefix"/. "$runtime"/
# Development files and documentation are not needed at run time.
rm -rf "$runtime"/include "$runtime"/share/doc "$runtime"/share/man \
    "$runtime"/share/info "$runtime"/share/gtk-doc "$runtime"/lib/cmake \
    "$runtime"/lib/pkgconfig "$runtime"/var
find "$runtime"/lib -maxdepth 1 -name '*.a' -delete
find "$runtime" -name '__pycache__' -type d -prune -exec rm -rf {} +

# The application icon, resolved through the themed icon name at run time.
icon_dir="$runtime/share/icons/hicolor/scalable/apps"
mkdir -p "$icon_dir"
cp "$root/data/icons/org.breadsched.BreadSched.svg" "$icon_dir/"
if [ -x "$prefix/bin/gtk4-update-icon-cache.exe" ]; then
    "$prefix/bin/gtk4-update-icon-cache.exe" -f -t "$runtime/share/icons/hicolor" || true
fi

# Launchers: the command line through python.exe, the desktop through pythonw.exe.
cat > "$stage/breadsched.cmd" <<'CMD'
@echo off
"%~dp0runtime\bin\python.exe" -m breadsched.cli.main %*
CMD
cat > "$stage/breadsched-gtk.cmd" <<'CMD'
@echo off
start "" "%~dp0runtime\bin\pythonw.exe" -m breadsched.gui %*
CMD
cp "$root/LICENSE" "$stage/LICENSE.txt"
# Run by the installer and uninstaller to edit the user PATH when asked to.
cp "$here/user_path.py" "$stage/user_path.py"
# Run by the installer and uninstaller to stop gdbus.exe helpers from this runtime.
cp "$here/stop-helpers.ps1" "$stage/stop-helpers.ps1"

# Build with one NSIS throughout: makensis, its stubs, and its x86-unicode plugins
# (nsDialogs for the MUI pages, nsExec for the helper scripts) must come from the
# same release. MSYS2's NSIS ships no x86-unicode plugins (only amd64-unicode ones),
# so CI installs the official build and that is preferred; MSYS2's makensis is used
# only with x86-unicode plugins of its own.
makensis_bin=""
for nsis in "/c/Program Files (x86)/NSIS" "/c/Program Files/NSIS"; do
    if [ -x "$nsis/makensis.exe" ] && [ -f "$nsis/Plugins/x86-unicode/nsDialogs.dll" ]; then
        makensis_bin="$nsis/makensis.exe"
        plugin_dir="$nsis/Plugins/x86-unicode"
        break
    fi
done
if [ -z "$makensis_bin" ]; then
    plugin_dll=$(find "$prefix" -ipath '*x86-unicode*' -iname 'nsDialogs.dll' -print -quit \
        2>/dev/null || true)
    if [ -n "$plugin_dll" ]; then
        makensis_bin=makensis
        plugin_dir=$(dirname "$plugin_dll")
    fi
fi
if [ -z "$makensis_bin" ] || [ ! -f "$plugin_dir/nsExec.dll" ]; then
    echo "error: no NSIS with x86-unicode nsDialogs and nsExec plugins found" >&2
    find "$prefix" "/c/Program Files (x86)/NSIS" "/c/Program Files/NSIS" \
        -iname '*.dll' -ipath '*nsis*' 2>/dev/null | head -n 40 >&2 || true
    exit 1
fi
echo "NSIS: $("$makensis_bin" -VERSION) at $makensis_bin, plugins $plugin_dir"

"$makensis_bin" -V2 -X"!addplugindir /x86-unicode \"$(cygpath -w "$plugin_dir")\"" \
    -DVERSION="$version" -DSTAGE="$(cygpath -w "$stage")" \
    -DOUTFILE="$(cygpath -w "$out/BreadSched-$version-setup.exe")" \
    "$(cygpath -w "$here/breadsched.nsi")"

( cd "$out" && sha256sum "BreadSched-$version-setup.exe" > "BreadSched-$version-setup.exe.sha256" )
echo "Built $out/BreadSched-$version-setup.exe"
