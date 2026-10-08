# Validation, packaging, and releases

Part of the [BreadSched design](../../DESIGN.md).

## Independent financial acceptance books

Plan and Projection also have a small acceptance corpus independent of their unit
fixtures. Each golden book is a human-readable declaration with stable account,
transaction, schedule, and scenario identifiers. Its financial assumptions and
hand calculations live beside static expected Plan and Projection results; product
code never generates those expectations.

The tests materialize each declaration as native SQLite, close the writer, reopen the
book, and only then call the shared Plan service and Projection engine. The corpus
covers exact dated cash timing and matched actual retention, classified balance-sheet
flows, mortgage/escrow non-additivity, actual/365 accrual and stock conservation, and
scenario schedule/rate overlays without mutating Base. Large captured user books are
not golden fixtures: compatibility imports, formula schedules, multi-currency,
historical-estimator thresholds, and presentation rendering keep their focused test
ownership.

## Mutation testing

A separate Linux CI job mutation-tests only reporting-currency selection in
`gen/engine/currency.py` and commodity-tagged arithmetic in `gen/lib/amount.py`.
It copies source and selected tests into a fresh temporary workspace before each
run. The measured baseline is a score over tested, non-equivalent mutants, with
untested, skipped, timed-out, or suspicious outcomes failing the gate. This focused
contract keeps mutation testing independent of the platform matrix and ordinary
core test runtime. The measured selection and one reviewed equivalent case are
recorded in `docs/quality/mutation-baseline.md`.

## Performance and property tests

Performance regressions are guarded at the operation boundary rather than by
timing unrelated setup. The performance gate therefore creates one realistic
synthetic 30,000-transaction history outside the measured interval, then times both
one ordinary commit and a 30-year projection. Timing limits are intentionally much looser
than normal performance while remaining below the historical regressions they are
intended to catch.

Recurrence correctness is also tested with generated inputs. Property-based tests
exercise many dates, intervals, weekend adjustments, and semi-monthly shapes while
checking generator-owned occurrence identity and serialization round trips. Named
regression cases remain valuable for explaining specific historical failures; the
property layer complements rather than replaces them.

Money has the same layer (`tests/test_money_properties.py`): arithmetic agrees with
exact rational arithmetic, quantizing and `to_decimal` both round half away from
zero at any denominator, `allocate` never loses or invents a minor unit and keeps
shares within one unit of each other, and the GnuCash pair and text forms read back
what they wrote. `tests/test_import_fuzz.py` feeds generated QIF, OFX, and CSV
statements built from plausible fragments, truncations, and noise to the importers:
an import must finish or refuse with a ValueError (the CSV service with a failed
result), write nothing when it refuses, leave a book that `verify_book()` accepts,
and add nothing when the same file is imported again. Both run in the ordinary
suite with bounded example counts.

## Tests that need outside services

Some behavior can be proven only against the real world: online quote sources,
Finance::Quote under Perl, and an installed package fetching for itself. Those
tests are written in full and gated rather than omitted. `tests/conftest.py` skips
every test marked `network` unless `BREADSCHED_NETWORK_TESTS=1`, with a reason that
says so; `tests/test_online_quotes_live.py` holds the quote tests (each tsp.gov fund,
ECB rates crossed into several reporting currencies, Alpha Vantage with
`ALPHAVANTAGE_API_KEY` or its `demo` key, Finance::Quote's TSP agreeing with the
native reader, keyless Finance::Quote sources tried in turn until one answers (or
exactly the one named by `BREADSCHED_FQ_METHOD` and `BREADSCHED_FQ_SYMBOL`, for a
source you rely on), then the service, CLI, web route, and an installed
`breadsched`), and `tests/test_gui.py` fetches live TSP prices through the dialog.
Assertions hold for any trading day: positive prices in a plausible range, dated
recently, from the expected source, and stored once. The *Live quote sources*
workflow (`.github/workflows/live-quotes.yml`) runs them weekly and on demand
against both Ubuntu's packaged Finance::Quote and its current CPAN release; the
CPAN job decides the result, and the packaged one reports without failing the run
(in October 2026, 1.59 could no longer fetch keyless US stock quotes while 1.71
could), so a provider's format change is reported without making
pull requests depend on outside services. Run them locally with
`BREADSCHED_NETWORK_TESTS=1 pytest -m network -rs`.

## GTK runtime availability

GTK tests never run on the developer's desktop by default. `tests/conftest.py`
starts a private Xvfb for each pytest process (each xdist worker) before any test
module imports GTK, selects GTK's X11 backend, and stops the server at exit
(`tests/private_display.py`); `GUI_VISIBLE=1` or a host without `Xvfb` keeps the
current display. Without it, `pytest -n auto` on a GNOME/Wayland desktop opened
hundreds of windows from a dozen workers on the compositor and crashed GTK inside
its Wayland event dispatch, a failure that neither CI nor `make` (both on Xvfb)
could show.

GUI tests distinguish an unavailable GTK4 runtime from a code failure. Both missing
PyGObject (`ImportError`) and an installed PyGObject without the GTK4 typelib
(`ValueError` from `gi.require_version`) skip the GTK module cleanly; once GTK4 is
available, runtime/widget failures remain real test failures. CI separately installs
PyGObject while deliberately removing the GTK4 typelib, then runs launcher help,
version, and real-launch checks so a partial system installation cannot turn test
collection or startup into a traceback.

## Packaging

BreadSched requires Python 3.11 or later (`requires-python`), and CI tests 3.11
through 3.14 on Linux, macOS, and Windows; the Flatpak and Windows installer
bundle their own interpreter. Python 3.10 support was dropped in 0.2.0a233
(alpha software, and 3.10 reaches end of life in October 2026), which also removed
the `tomli` fallback.

**Flatpak.** The manifest builds the application into `/app` with the GNOME 49 SDK
and grants Documents access (the default book location) and network access (the
loopback web interface). Desktop integration files under `data/` are named by the
application id: a desktop entry launching `breadsched-gtk`, AppStream metainfo, and
a scalable icon that the GTK application also sets as its window icon. CI builds the
Flatpak from the checkout, installs it from a local repository, validates the
desktop files with `desktop-file-validate` and `appstreamcli`, and, with the network
unshared, runs the CLI (Dashboard, CSV export, backup, restore, imported-book
integrity, competing-writer lock) and `scripts/flatpak_gtk_smoke.py` (every view,
every guide part, the icon, settings under `~/.var/app/<id>/config`, and each
printable report through `Gtk.PrintOperation` to PDF).

Portals are exercised for real. `scripts/flatpak_portal_check.sh` starts the
document portal, grants the installed app an existing schema 6 book and a book that
does not exist yet (as Open and Save do), and migrates, writes, and verifies them
through `/run/user/<uid>/doc/<id>/<name>`. `scripts/flatpak_desktop_checks.sh` puts
the real `xdg-desktop-portal` frontend in front of `scripts/portal_test_backend.py`,
a backend that answers FileChooser and Print requests as a person would, and runs
`scripts/flatpak_desktop_checks.py` in the sandbox: Open, Export Transactions,
Back Up Book, and Print on every printable view go through the application's own
actions and the portal, and the host checks the caller's app id, where each file
landed, and that each report arrived as a PDF.

A document-portal directory holds only the chosen file: a file created beside it is
kept by the portal as a hidden temporary and never appears under its own name. So
`user_paths.companion_path` sends every file that belongs beside a book or chosen
file (pre-migration and pre-restore backups, the web upload folder, import logs)
to `<data directory>/beside-documents/<document id>/` when
`user_paths.portal_document_id` recognises a portal path, and
`attachments.attachment_folder` has no default for such a book. SQLite's own
`-journal` and the writer lock still live beside the book as portal temporaries,
which is sufficient while the portal runs. `presentation.book_open_notice` tells
the desktop user where backups go. The portal's print dialog cannot show an
application tab, so `printing.print_report` asks about a report's optional section
first when `uses_print_portal()` (Flatpak, or `GDK_DEBUG=portals`).

The release workflow builds the same manifest from the tested commit into a
single-file bundle whose runtime comes from Flathub, installs it, checks the
installed version, and publishes it with its checksum in `SHA256SUMS`.

**Debian/Ubuntu and Fedora packages** (`packaging/linux/`). `build-package.sh deb|rpm
WHEEL OUT` installs the wheel with `pip --target` into a private
`/usr/lib/breadsched` (removing pip's build-specific `RECORD` and `direct_url.json`),
writes `/usr/bin/breadsched` and `breadsched-gtk` launchers for the system
`/usr/bin/python3`, and installs the desktop entry, metainfo, icon, and licence.
Both formats install that one tree:
- The `.deb` is assembled with `dpkg-deb`. It depends on `python3 (>= 3.11)`,
  `python3-gi`, `python3-gi-cairo`, `python3-cairo`, and `gir1.2-gtk-4.0`, and
  recommends `libfinance-quote-perl`. Its `postinst` byte-compiles for the system
  Python and its `prerm` removes the caches.
- The `.rpm` is built with `rpmbuild` from a generated noarch spec. It requires
  `python3 >= 3.11`, `python3-gobject`, `python3-cairo`, and `gtk4`, and recommends
  `perl-Finance-Quote`.

Package versions use `~` for alphas (`0.2.0~a257`) so that they sort before the
release; file names keep the plain version. Running on the host rather than in a
sandbox is what lets these packages use an installed Finance::Quote
([decision 0002](decisions/0002-dependencies-and-online-quotes.md)). CI builds and
installs each in an `ubuntu:24.04` or `fedora:latest` container with the
distribution's package manager. `smoke-test.sh` then checks both launchers'
versions, a sample book's round trip, the GTK and cairo imports, the desktop files,
and that Finance::Quote is found when installed. CI also checks that removal leaves
nothing behind. The release workflow packages the release's own tested wheel the same
way and publishes both files with their checksums in `SHA256SUMS`.

**Windows installer** (`packaging/windows/`). The installer carries its own runtime
rather than asking users to assemble Python and GTK: `build-installer.sh` installs
the wheel into an MSYS2 UCRT64 prefix beside Python, GTK 4, PyGObject, and cairo,
stages that prefix under `runtime\` without development files, adds launchers and
the icon, and compiles `breadsched.nsi`, naming the x86-unicode NSIS plugin directory
(nsDialogs, nsExec) with `!addplugindir`. MSYS2's NSIS 3.13 ships no plugins, so CI
and release install the official NSIS build with Chocolatey, and the script compiles
with that release's own `makensis.exe` so the stubs and plugins match (mixing
MSYS2's makensis with the official plugins produced an installer that hung in a
silent upgrade). MSYS2's makensis is used only with plugins of its own; otherwise
the script stops before compiling. Both installer jobs time out after 30 minutes.

- It installs per user (no administrator rights) under
  `%LOCALAPPDATA%\Programs\BreadSched` and registers under HKCU. Books never live
  in the installation directory, so neither upgrade nor uninstall touches them.
- An upgrade replaces `runtime\` wholesale, so no stale module survives. GLib starts
  `runtime\bin\gdbus.exe` as a session bus that outlives the application, so the
  installer and uninstaller first run `stop-helpers.ps1`, which stops only the
  `gdbus.exe` inside this installation.
- Adding the command line to `PATH` is opt-in (`/ADDTOPATH`), remembered under
  `HKCU\Software\BreadSched`, and adds only the installation directory, so the
  bundled `python.exe` and DLLs never shadow other programs. NSIS truncates long
  strings and expands `%VARIABLES%`, so `packaging/windows/user_path.py` edits the
  raw registry value with its original type and broadcasts `WM_SETTINGCHANGE`.
- CI (`test-installer.ps1`, outside MSYS2 with a bare `PATH`) upgrades from the
  newest published installer after checking its `SHA256SUMS` entry, then installs
  silently, runs the CLI, sample book, verification, guide, GTK smoke, and
  `scripts/windows_desktop_checks.py` (the native Open and Save dialogs, and PDF
  output of every printable view through `printing.export_pdf`), reinstalls, tests
  the `PATH` option, and uninstalls, requiring the book to remain and the runtime
  and registration to be gone.
- The release's `windows-installer` job builds and tests from the tested `main`
  commit and hands only the installer and a one-line checksum file to the
  publisher, which never executes them.

## Releases

A release is selected explicitly by a checked-in `docs/releases/vVERSION.md`; an
alpha version increment alone is not a release request, and a merge without new
notes publishes nothing. After the full CI push run on `main` succeeds, a read-only
preparation job requires that exact tested commit still to be the tip of `main`,
validates the notes against the application and schema constants
(`versioning.version_summary()`, never a hard-coded window), builds and installs the
wheel, and computes SHA-256 checksums. A separate write-capable job checks the
tested identity again, validates the transferred artifact names and hashes as data
without running them, and creates the annotated tag and GitHub release; PEP 440
alphas also carry GitHub's pre-release flag. An existing tag must target the same
commit, and a version already tagged on an earlier commit selects nothing rather
than failing. Every ordinary CI job has a read-only repository token.

## Documentation

Each document has one job: `README.md` orients and links; this document describes
the implemented design; the packaged User Guide (`src/breadsched/USER_GUIDE.md` for
rules, `src/breadsched/guide/` for each interface's steps) is the offline help;
`ROADMAP.md` is the only list of unfinished work; `CHANGELOG.md` records completed
changes concisely; tests, `docs/quality/`, and `docs/releases/` hold detailed
evidence. `CONTRIBUTING.md` defines how each pull request reviews these documents.
`tests/test_documentation.py` checks local links and anchors in the top-level
documents, and `tests/test_user_guide.py` checks the guide's links.
