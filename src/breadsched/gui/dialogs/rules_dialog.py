"""Manage categorization rules and review their proposals through the shared service.

Rules propose a category for imported transactions still on an import placeholder
(Uncategorized CSV or OFX). The first matching rule decides, and a later rule that
would choose differently is shown as a conflict. A category the user chose is never
replaced, and nothing changes until proposals are accepted. Every write is one undo
step, and a rejected request leaves the book unchanged.
"""

from __future__ import annotations

from ...gen.db.sqlite import DbSQLite
from ...gen.lib.account import AccountClass
from ...gen.services.categorization import (
    AddRule,
    AppliedCategories,
    CategoryRule,
    add_rule,
    apply_category_proposals,
    delete_rule,
    list_rules,
    move_rule,
    preview_category_proposals,
)
from ...presentation import service_error_message
from ..gi_setup import Gtk
from ..widgets.bounded import BoundedWindow
from ..widgets.choice import bounded_dropdown
from ..widgets.help import help_row

__all__ = ["RulesDialog"]

MATCH_KINDS = ("description", "payee")


class RulesDialog(BoundedWindow):
    """Ordered rule list and editor above the proposals awaiting acceptance."""

    def __init__(self, parent: Gtk.Window | None, db: DbSQLite) -> None:
        super().__init__(title="Categorization rules", transient_for=parent, modal=True)
        self.set_destroy_with_parent(True)
        self.set_default_size(900, 640)
        self.db = db
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

        rule_scroller = Gtk.ScrolledWindow(min_content_height=140, vexpand=True)
        self.rule_rows = Gtk.Grid(column_spacing=14, row_spacing=4)
        rule_scroller.set_child(self.rule_rows)
        box.append(rule_scroller)

        self.payees = list(db.iter_payees())
        self.categories = sorted(
            (
                account
                for account in db.iter_accounts()
                if not account.placeholder
                and account.account_class in (AccountClass.INCOME, AccountClass.EXPENSE)
            ),
            key=lambda account: db.full_name(account).casefold(),
        )
        form = Gtk.Box(spacing=8)
        self.kind_picker = bounded_dropdown(["Description", "Payee"])
        self.kind_picker.connect("notify::selected", lambda *_args: self._sync_kind())
        self.description_entry = Gtk.Entry(
            hexpand=True, placeholder_text="Example description, e.g. CORNER GROCER #1234"
        )
        self.payee_picker = bounded_dropdown(
            [payee.name for payee in self.payees] or ["(no payees)"]
        )
        self.category_picker = bounded_dropdown(
            [db.full_name(account) for account in self.categories] or ["(no categories)"]
        )
        add_button = Gtk.Button(label="Add rule")
        add_button.add_css_class("suggested-action")
        add_button.connect("clicked", lambda _b: self.add_rule())
        for widget in (
            Gtk.Label(label="Match"),
            self.kind_picker,
            self.description_entry,
            self.payee_picker,
            Gtk.Label(label="Category"),
            self.category_picker,
            add_button,
        ):
            form.append(widget)
        box.append(form)
        self._sync_kind()

        box.append(Gtk.Label(label="Proposals", xalign=0, css_classes=["heading"]))
        proposal_scroller = Gtk.ScrolledWindow(min_content_height=200, vexpand=True)
        self.proposal_rows = Gtk.Grid(column_spacing=14, row_spacing=4)
        proposal_scroller.set_child(self.proposal_rows)
        box.append(proposal_scroller)
        self.accept_button = Gtk.Button(label="Accept selected")
        self.accept_button.connect("clicked", lambda _b: self.accept_selected())
        box.append(self.accept_button)

        self.status = Gtk.Label(xalign=0, wrap=True, selectable=True)
        box.append(self.status)
        self.refresh()

    def _sync_kind(self) -> None:
        by_payee = MATCH_KINDS[self.kind_picker.get_selected()] == "payee"
        self.description_entry.set_visible(not by_payee)
        self.payee_picker.set_visible(by_payee)

    def _name(self, handle: str) -> str:
        return self.db.full_name(handle) or handle

    # ------------------------------------------------------------------ views

    def refresh(self) -> None:
        """Reload the rules and the current proposals."""
        payee_names = {payee.handle: payee.name for payee in self.db.iter_payees()}
        rules = list_rules(self.db)
        _clear(self.rule_rows)
        for column, heading in enumerate(("Rule", "Matches", "Category")):
            label = Gtk.Label(label=heading, xalign=0)
            label.add_css_class("dim")
            self.rule_rows.attach(label, column, 0, 1, 1)
        if not rules:
            self.rule_rows.attach(Gtk.Label(label="No rules yet.", xalign=0), 0, 1, 3, 1)
        for position, rule in enumerate(rules, start=1):
            matches = (
                f"Payee {payee_names.get(rule.payee, rule.payee)}"
                if rule.payee
                else f"Description {rule.key}"
            )
            for column, text in enumerate((str(position), matches, self._name(rule.category))):
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
            delete = Gtk.Button(label="Delete")
            delete.connect("clicked", lambda _b, handle=rule.handle: self.delete(handle))
            self.rule_rows.attach(delete, 5, position, 1, 1)

        proposals = preview_category_proposals(self.db).value or ()
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
        for row, item in enumerate(proposals, start=1):
            check = Gtk.CheckButton(active=True)
            self.proposal_checks[item.transaction] = check
            self.proposal_rows.attach(check, 0, row, 1, 1)
            conflicts = "; ".join(
                f"rule {conflict.rule_position}: {self._name(conflict.category)}"
                for conflict in item.conflicts
            )
            cells = (
                (item.when.isoformat(), 0),
                (item.description, 0),
                (item.amount.format(parens_negative=True), 1),
                (self._name(item.category), 0),
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
        by_payee = MATCH_KINDS[self.kind_picker.get_selected()] == "payee"
        selected_payee = self.payee_picker.get_selected()
        selected_category = self.category_picker.get_selected()
        request = AddRule(
            category=(
                self.categories[selected_category].handle
                if selected_category < len(self.categories)
                else ""
            ),
            payee=(
                self.payees[selected_payee].handle
                if by_payee and selected_payee < len(self.payees)
                else None
            ),
            description=None if by_payee else self.description_entry.get_text(),
        )
        if by_payee and request.payee is None:
            self._message("Add a payee before making a payee rule.", True)
            return None
        result = add_rule(self.db, request)
        if result.value is None:
            self._message(service_error_message(result.errors[0]), True)
            return None
        self.description_entry.set_text("")
        self.refresh()
        self._message("Rule added.", False)
        return result.value

    def move(self, handle: str, position: int) -> bool:
        result = move_rule(self.db, handle, position)
        if result.value is None:
            self._message(service_error_message(result.errors[0]), True)
            return False
        self.refresh()
        return True

    def delete(self, handle: str) -> bool:
        """Delete a rule; Edit → Undo restores it."""
        result = delete_rule(self.db, handle)
        if result.value is None:
            self._message(service_error_message(result.errors[0]), True)
            return False
        self.refresh()
        self._message("Rule deleted. Edit → Undo restores it.", False)
        return True

    def accept_selected(self) -> AppliedCategories | None:
        """Accept the checked proposals as one undo step."""
        chosen = tuple(
            handle for handle, check in self.proposal_checks.items() if check.get_active()
        )
        result = apply_category_proposals(self.db, chosen)
        if result.value is None:
            self._message(service_error_message(result.errors[0]), True)
            return None
        self.refresh()
        self._message(
            f"Categorized {result.value.assigned} transaction(s); "
            f"{result.value.unchanged} left unchanged.",
            False,
        )
        return result.value

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
