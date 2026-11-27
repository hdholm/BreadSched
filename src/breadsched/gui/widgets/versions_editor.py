"""A schedule's versions on the schedule editors' Future changes page.

Each version is the whole transaction for some occurrences (``gen.lib.
schedule_versions``): from a date on, on one date only, or in one calendar month
every year. The list names each version; choosing one shows its legs on the
register's own split grid (:class:`~breadsched.gui.version_sheet.VersionSheet`),
where a leg is added, removed, or changed by typing and its direction is its
column. An amount may be a formula over ``base`` (the leg's amount below) and
``base_total`` (the transaction below).

A new version starts from what the schedule is then:

* **From a date on**: every leg in force before that date, as it is defined there.
* **Every year in a month**: every leg as ``base * 1.0``, so the month is the usual
  transaction until a factor is typed (``base * 1.2`` for 20% more).
* **On one date only**: that occurrence's legs as amounts, ready to change.

Nothing is forced to change while it is typed. Each version keeps its lines as
typed, readable or not, so another version can be chosen at any time; a leg is
left out by clearing its cells. The versions are read quietly as the user types
(the cursor never moves), and the first that cannot be read is named in the
editor's status, with Save unavailable until it reads. Enter on the grid shows that
version and puts the cursor on the line to correct.

The schedule's own validation (each version balancing, its date on the schedule)
is the book server's; this page reports a version it cannot read.
"""

from __future__ import annotations

from collections.abc import Callable

from ...gen.lib.schedule_versions import (
    MONTH_NAMES,
    ScheduledVersion,
    VersionKind,
    sorted_versions,
    starting_version,
)
from ...gen.lib.scheduled import ScheduledTransaction
from ..gi_setup import Gtk
from ..split_entry import Line
from ..version_sheet import VersionSheet
from .choice import bounded_dropdown
from .date_entry import attach_date_keys, date_hint, parse_date_text, shown_date
from .editor_parts import SplitsPanel

__all__ = ["KINDS", "VersionsEditor", "version_label"]

#: The kinds a new version can be, in the order the picker lists them.
KINDS = (
    (VersionKind.FROM, "From a date on"),
    (VersionKind.ONCE, "On one date only"),
    (VersionKind.MONTH, "Every year in a month"),
)

HINT = (
    "Each version is the whole transaction, typed as a register's splits: add a "
    "leg, leave one out, or change any amount. An amount may be a formula: "
    "base is the leg's amount before this version (in its own column), and "
    "base_total the amount the transaction moved; base * 1.2 is 20% more. A month's "
    "version applies on top of the dated one in force, and a one-date version on "
    "top of both."
)


def version_label(version: ScheduledVersion) -> str:
    """A version's name in the list, its date shown as the desktop shows dates."""
    if version.kind is VersionKind.MONTH:
        assert version.month is not None
        return f"Every {MONTH_NAMES[version.month - 1]}"
    if version.kind is VersionKind.ONCE:
        return f"On {shown_date(version.start)} only"
    return f"From {shown_date(version.start)}"


class VersionsEditor(Gtk.Box):
    """The versions of one schedule: a list, the chosen one's legs, and Add/Remove.

    ``records`` are the accounts (``AccountRecord``) the legs may use. ``draft``
    gives the schedule the editor describes now (template and versions), or None
    when it cannot be read; a new version starts from it.
    """

    def __init__(
        self,
        db,
        records,
        on_changed: Callable[..., None],
        draft: Callable[[], ScheduledTransaction | None],
        currency: str | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.db = db
        self.records = list(records)
        self.on_changed = on_changed
        self.draft = draft
        #: Each version's kind, date, and month (its legs as last read)...
        self.versions: list[ScheduledVersion] = []
        #: ...and its lines as typed, in the same order.
        self.lines: list[list[Line]] = []
        self.selected: int | None = None
        #: Why the chosen version's legs cannot be read, or "".
        self.problem = ""
        self._loading = False

        hint = Gtk.Label(label=HINT, xalign=0, wrap=True)
        hint.add_css_class("dim-label")
        self.append(hint)

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.list.add_css_class("boxed-list")
        self.list.connect("row-selected", self._on_row_selected)
        self.append(self.list)

        adder = Gtk.Box(spacing=6)
        self.kind = bounded_dropdown([label for _kind, label in KINDS])
        self.kind.update_property([Gtk.AccessibleProperty.LABEL], ["New version"])
        self.kind.connect("notify::selected", lambda *_: self._show_when())
        adder.append(self.kind)
        self.when_entry = attach_date_keys(Gtk.Entry(placeholder_text=date_hint(), width_chars=12))
        self.when_entry.update_property([Gtk.AccessibleProperty.LABEL], ["Version date"])
        adder.append(self.when_entry)
        self.month = bounded_dropdown(list(MONTH_NAMES))
        self.month.update_property([Gtk.AccessibleProperty.LABEL], ["Version month"])
        adder.append(self.month)
        add = Gtk.Button(label="Add version")
        add.connect("clicked", lambda *_: self.add_version())
        adder.append(add)
        self.remove_button = Gtk.Button(label="Remove version")
        self.remove_button.connect("clicked", lambda *_: self.remove_selected())
        adder.append(self.remove_button)
        self.append(adder)
        self.message = Gtk.Label(xalign=0, wrap=True)
        self.message.add_css_class("negative")
        self.append(self.message)

        self.sheet = VersionSheet(db, self.records, currency=currency)
        self.sheet.on_enter = self.show_problem
        self.panel = SplitsPanel(self.sheet, self._on_sheet_changed)
        self.append(self.panel)
        self._show_when()
        self._render()

    # ---------------------------------------------------------------- values

    def set_versions(self, versions) -> None:
        self.versions = sorted_versions(version.copy() for version in versions)
        self.lines = [self.sheet.lines_of(version.splits) for version in self.versions]
        self.selected = 0 if self.versions else None
        self.problem = ""
        self._render()

    def values(self) -> list[ScheduledVersion] | None:
        """Every version as typed; None when one cannot be read (the reason, naming
        the version, is :attr:`problem`). Reading moves nothing on the grid."""
        versions, bad, problem = self._read()
        if bad is not None:
            self.problem = f"{version_label(self.versions[bad])}: {problem}"
            return None
        self.problem = ""
        return versions

    def show_problem(self) -> bool:
        """Show the first version that cannot be read, with the cursor on the line
        to correct and the reason beneath; True when every version reads."""
        _versions, bad, _problem = self._read()
        if bad is None:
            self.message.set_text("")
            return True
        if bad != self.selected:
            self.selected = bad
            self._render()
        self.sheet.legs()
        self.message.set_text(self.sheet.message)
        self.panel.grid.sync(focus=False)
        return False

    def __len__(self) -> int:
        return len(self.versions)

    # ---------------------------------------------------------------- editing

    def _show_when(self) -> None:
        kind = KINDS[self.kind.get_selected()][0]
        self.when_entry.set_visible(kind is not VersionKind.MONTH)
        self.month.set_visible(kind is VersionKind.MONTH)

    def add_version(self) -> ScheduledVersion | None:
        """Add a version of the kind, date, or month chosen beside **Add version**,
        filled in from the schedule as it then is, and choose it."""
        kind = KINDS[self.kind.get_selected()][0]
        start = None
        month = None
        if kind is VersionKind.MONTH:
            month = self.month.get_selected() + 1
        else:
            try:
                start = parse_date_text(self.when_entry.get_text().strip())
            except ValueError:
                self.message.set_text("Enter the version's date.")
                return None
        if any(
            item.kind is kind and item.start == start and item.month == month
            for item in self.versions
        ):
            self.message.set_text("There is already a version for that date or month.")
            return None
        schedule = self.draft()
        if schedule is None:
            version = ScheduledVersion(kind, [], start=start, month=month)
        else:
            # Started from the versions that read now; one still being typed is
            # left out of what the new one starts from.
            schedule.versions = self._readable()
            version = starting_version(schedule, kind, start, month)
        self._keep_shown()
        pairs = sorted(
            [
                *zip(self.versions, self.lines, strict=True),
                (version, self.sheet.lines_of(version.splits)),
            ],
            key=lambda pair: pair[0].sort_key(),
        )
        self.versions = [pair[0] for pair in pairs]
        self.lines = [pair[1] for pair in pairs]
        self.selected = self.versions.index(version)
        self.message.set_text("")
        self._render()
        self.on_changed()
        return version

    def remove_selected(self) -> None:
        if self.selected is None:
            return
        del self.versions[self.selected]
        del self.lines[self.selected]
        self.selected = 0 if self.versions else None
        self.problem = ""
        self.message.set_text("")
        self._render()
        self.on_changed()

    # --------------------------------------------------------------- showing

    def _keep_shown(self) -> None:
        """Keep the shown version's lines as typed."""
        if self.selected is not None and not self._loading:
            self.lines[self.selected] = self.sheet.typed_lines()

    def _read(self) -> tuple[list[ScheduledVersion], int | None, str]:
        """Each version's legs read quietly from its lines: the versions read, the
        index of the first that cannot be read (or None), and why."""
        self._keep_shown()
        versions: list[ScheduledVersion] = []
        for index, (version, lines) in enumerate(zip(self.versions, self.lines, strict=True)):
            legs, problem = self.sheet.legs_of(lines)
            if legs is None:
                return versions, index, problem or "check its legs."
            versions.append(
                ScheduledVersion(version.kind, legs, start=version.start, month=version.month)
            )
        return versions, None, ""

    def _readable(self) -> list[ScheduledVersion]:
        self._keep_shown()
        readable = []
        for version, lines in zip(self.versions, self.lines, strict=True):
            legs, _problem = self.sheet.legs_of(lines)
            if legs is not None:
                readable.append(
                    ScheduledVersion(version.kind, legs, start=version.start, month=version.month)
                )
        return readable

    def _render(self) -> None:
        self._loading = True
        try:
            self.list.remove_all()
            for version in self.versions:
                self.list.append(Gtk.Label(label=version_label(version), xalign=0))
            if self.selected is not None:
                self.list.select_row(self.list.get_row_at_index(self.selected))
                self.sheet.load_lines(self.lines[self.selected])
            else:
                self.sheet.load_lines([])
            self.panel.set_visible(self.selected is not None)
            self.remove_button.set_sensitive(self.selected is not None)
            self.panel.grid.sync(focus=False)
        finally:
            self._loading = False

    def _on_row_selected(self, _list, row) -> None:
        if self._loading or row is None:
            return
        index = row.get_index()
        if index == self.selected:
            return
        # Another version may be chosen at any time: this one keeps its lines as
        # typed, readable or not.
        self._keep_shown()
        self.message.set_text("")
        self.selected = index
        self._render()

    def _on_sheet_changed(self, *_args) -> None:
        if self._loading:
            return
        if self.message.get_text():
            # A reason shown by Enter stays until the versions read; it follows
            # what is typed meanwhile, without moving the cursor.
            _versions, bad, problem = self._read()
            self.message.set_text("" if bad is None else problem)
        self.on_changed()
