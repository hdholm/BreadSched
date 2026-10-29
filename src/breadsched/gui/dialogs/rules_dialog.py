"""Manage categorization rules and review their proposals through the book server.

Rules propose a category for imported transactions still on an import placeholder
(Uncategorized CSV or OFX). The first matching rule decides, and a later rule that
would choose differently is shown as a conflict. A category the user chose is never
replaced, and nothing changes until proposals are accepted. Every write is one undo
step, and a rejected request leaves the book unchanged.
"""

from __future__ import annotations

from functools import partial

from ...api import ApiError
from ...api.local import book_server
from ...gen.services.categorization import (
    AddRule,
    AppliedCategories,
    CategoryRule,
)
from ..gi_setup import Gtk
from ..remote_book import OpenBook
from ..widgets.bounded import BoundedWindow
from ..widgets.choice import bounded_dropdown
from ..widgets.confirm import delete_button
from ..widgets.help import help_row

__all__ = ["RulesDialog"]


class RulesDialog(BoundedWindow):
    """Ordered rule list and editor above the proposals awaiting acceptance."""

    def __init__(self, parent: Gtk.Window | None, db: OpenBook) -> None:
        super().__init__(title="Categorization rules", transient_for=parent, modal=True)
        self.set_destroy_with_parent(True)
        self.set_default_size(900, 640)
        self.db = db
        self.book = book_server(db)
        self.proposal_checks: dict[str, Gtk.CheckButton] = {}

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)
        box.prepend(help_row("rules"))
        box.append(
            Gtk.Label(
                label=(
                    "Rules propose a category for imported transactions still in "
                    "Uncategorized CSV or Uncategorized OFX. The first matching rule "
                    "decides; a later rule that would choose differently is listed as a "
                    "conflict. A category you chose is never replaced, and nothing changes "
                    "until you accept."
                ),
                xalign=0,
                wrap=True,
            )
        )

        rule_scroller = Gtk.ScrolledWindow(min_content_height=120, vexpand=True)
        self.rule_rows = Gtk.Grid(column_spacing=14, row_spacing=4)
        rule_scroller.set_child(self.rule_rows)
        box.append(rule_scroller)

        self.categories = self.book.categorization_rules().categories
        form = Gtk.Box(spacing=8)
        self.description_entry = Gtk.Entry(
            hexpand=True, placeholder_text="Example description, e.g. CORNER GROCER #1234"
        )
        self.category_picker = bounded_dropdown(
            [choice.name for choice in self.categories] or ["(no categories)"]
        )
        add_button = Gtk.Button(label="Add rule")
        add_button.add_css_class("suggested-action")
        add_button.connect("clicked", lambda _b: self.add_rule())
        for widget in (
            Gtk.Label(label="Matching description"),
            self.description_entry,
            Gtk.Label(label="Category"),
            self.category_picker,
            add_button,
        ):
            form.append(widget)
        box.append(form)

        box.append(Gtk.Label(label="Proposals", xalign=0, css_classes=["heading"]))
        proposal_scroller = Gtk.ScrolledWindow(min_content_height=160, vexpand=True)
        self.proposal_rows = Gtk.Grid(column_spacing=14, row_spacing=4)
        proposal_scroller.set_child(self.proposal_rows)
        box.append(proposal_scroller)
        self.accept_button = Gtk.Button(label="Accept selected")
        self.accept_button.connect("clicked", lambda _b: self.accept_selected())
        box.append(self.accept_button)

        self.status = Gtk.Label(xalign=0, wrap=True, selectable=True)
        box.append(self.status)
        self.refresh()

    # ------------------------------------------------------------------ views

    def refresh(self) -> None:
        """Reload the rules and the current proposals."""
        view = self.book.categorization_rules()
        rules = view.rules
        _clear(self.rule_rows)
        for column, heading in enumerate(("Rule", "Matches", "Category")):
            label = Gtk.Label(label=heading, xalign=0)
            label.add_css_class("dim")
            self.rule_rows.attach(label, column, 0, 1, 1)
        if not rules:
            self.rule_rows.attach(Gtk.Label(label="No rules yet.", xalign=0), 0, 1, 3, 1)
        for line in rules:
            position, rule = line.position, line.rule
            texts = (str(position), f"Description {rule.key}", line.category_name)
            for column, text in enumerate(texts):
                self.rule_rows.attach(Gtk.Label(label=text, xalign=0), column, position, 1, 1)
            for column, (label, target) in enumerate(
                (("Up", position - 1), ("Down", position + 1)), start=3
            ):
                button = Gtk.Button(label=label)
                button.set_sensitive(1 <= target <= len(rules))
                button.connect(
                    "clicked", lambda _b, handle=rule.handle, to=target: self.move(handle, to)
                )
                self.rule_rows.attach(button, column, position, 1, 1)
            delete = delete_button(
                f"the rule for “{rule.key}”",
                partial(self.delete, rule.handle),
            )
            self.rule_rows.attach(delete, 5, position, 1, 1)

        proposals = view.proposals
        self.proposal_checks = {}
        _clear(self.proposal_rows)
        headings = ("Accept", "Date", "Description", "Amount", "Category", "Rule", "Also")
        for column, heading in enumerate(headings):
            label = Gtk.Label(label=heading, xalign=1 if heading in {"Amount", "Rule"} else 0)
            label.add_css_class("dim")
            self.proposal_rows.attach(label, column, 0, 1, 1)
        if not proposals:
            self.proposal_rows.attach(
                Gtk.Label(label="No uncategorized imported transactions match a rule.", xalign=0),
                0,
                1,
                len(headings),
                1,
            )
        for row, entry in enumerate(proposals, start=1):
            item = entry.proposal
            check = Gtk.CheckButton(active=True)
            self.proposal_checks[item.transaction] = check
            self.proposal_rows.attach(check, 0, row, 1, 1)
            conflicts = "; ".join(
                f"rule {conflict.rule_position}: {name}"
                for conflict, name in zip(item.conflicts, entry.conflict_names, strict=True)
            )
            cells = (
                (item.when.isoformat(), 0),
                (item.description, 0),
                (item.amount.format(parens_negative=True), 1),
                (entry.category_name, 0),
                (str(item.rule_position), 1),
                (conflicts or "—", 0),
            )
            for column, (text, xalign) in enumerate(cells, start=1):
                self.proposal_rows.attach(
                    Gtk.Label(label=text, xalign=xalign, selectable=True), column, row, 1, 1
                )
        self.accept_button.set_sensitive(bool(proposals))

    # ----------------------------------------------------------------- writes

    def add_rule(self) -> CategoryRule | None:
        """Append a rule from the form; None when refused."""
        selected_category = self.category_picker.get_selected()
        request = AddRule(
            category=(
                self.categories[selected_category].handle
                if selected_category < len(self.categories)
                else ""
            ),
            description=self.description_entry.get_text(),
        )
        try:
            rule = self.book.add_rule(request)
        except ApiError as error:
            self._message(str(error), True)
            return None
        self.description_entry.set_text("")
        self.refresh()
        self._message("Rule added.", False)
        return rule

    def move(self, handle: str, position: int) -> bool:
        try:
            self.book.move_rule(handle, position)
        except ApiError as error:
            self._message(str(error), True)
            return False
        self.refresh()
        return True

    def delete(self, handle: str) -> bool:
        """Delete a rule; Edit → Undo restores it."""
        try:
            self.book.delete_rule(handle)
        except ApiError as error:
            self._message(str(error), True)
            return False
        self.refresh()
        self._message("Rule deleted. Edit → Undo restores it.", False)
        return True

    def accept_selected(self) -> AppliedCategories | None:
        """Accept the checked proposals as one undo step."""
        chosen = tuple(
            handle for handle, check in self.proposal_checks.items() if check.get_active()
        )
        try:
            applied = self.book.apply_category_proposals(chosen)
        except ApiError as error:
            self._message(str(error), True)
            return None
        self.refresh()
        self._message(
            f"Categorized {applied.assigned} transaction(s); {applied.unchanged} left unchanged.",
            False,
        )
        return applied

    def _message(self, text: str, error: bool) -> None:
        self.status.set_text(text)
        if error:
            self.status.add_css_class("negative")
        else:
            self.status.remove_css_class("negative")


def _clear(grid: Gtk.Grid) -> None:
    child = grid.get_first_child()
    while child is not None:
        following = child.get_next_sibling()
        grid.remove(child)
        child = following
