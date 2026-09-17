"""Add or edit an account.

Beyond the name and type, an account carries the relationships the rest of the
program needs and that a foreign book never states: which dashboard group it
belongs to, the asset a loan was borrowed against, and how a credit card is
actually used.

That last one matters more than it looks. A card cleared every month is a payment
channel and belongs nowhere in a debt forecast; a card carrying a balance is a debt
that compounds, and its usual payment is a real monthly outflow. Treating them the
same makes one household look poorer than it is and the other richer.
"""

from __future__ import annotations

from datetime import date
from decimal import InvalidOperation

from ...gen.db.sqlite import DbSQLite
from ...gen.lib import (
    Account,
    AccountClass,
    AccountType,
    FsaFundingYear,
    Money,
)
from ...gen.services import DeleteAccount, SaveAccount, delete_account, save_account
from ...gen.utils.amount_input import parse_user_amount
from ...presentation import service_error_message
from ..gi_setup import Gtk

__all__ = ["AccountDialog"]

_TYPES: list[AccountType] = [
    account_type
    for account_type in AccountType
    if account_type not in {AccountType.ROOT, AccountType.TECHNICAL}
]


class AccountDialog(Gtk.Window):
    """Create a new account, or change one that exists."""

    def __init__(
        self,
        parent: Gtk.Window | None,
        db: DbSQLite,
        account: Account | None = None,
        default_parent: str | None = None,
    ) -> None:
        editing = account is not None
        super().__init__(
            title="Edit account" if account is not None else "New account",
            transient_for=parent,
            modal=True,
        )
        self.db = db
        self.account = account
        self.editing = editing
        self.set_default_size(600, 700)

        all_accounts = list(db.iter_accounts())
        blocked_parents: set[str] = set()
        if account is not None:
            blocked_parents.add(account.handle)
            changed = True
            while changed:
                changed = False
                for candidate in all_accounts:
                    if (
                        candidate.handle not in blocked_parents
                        and candidate.parent in blocked_parents
                    ):
                        blocked_parents.add(candidate.handle)
                        changed = True
        self.parents = [a for a in all_accounts if a.handle not in blocked_parents]
        self.parents.sort(key=db.full_name)
        self.commodities = list(db.iter_commodities())
        self.commodities.sort(key=lambda item: (item.namespace, item.mnemonic))
        self.commodity_handles: list[str | None] = [None] + [
            commodity.handle for commodity in self.commodities
        ]
        self.assets = [
            a
            for a in db.iter_accounts()
            if a.account_class is AccountClass.ASSET and not a.is_root and not a.atype.is_cash_like
        ]
        self.assets.sort(key=db.full_name)
        current_payment = account.card_payment_account if account is not None else None
        self.payment_accounts = [
            a
            for a in db.iter_accounts()
            if a.atype.is_cash_like
            and not a.placeholder
            and (not a.hidden or a.handle == current_payment)
        ]
        self.payment_accounts.sort(key=db.full_name)

        # Built before anything that can emit: setting a dropdown's initial value
        # fires notify::selected, which reaches _validate long before the widgets
        # further down this method exist.
        self.status = Gtk.Label(xalign=0)
        self.save_button = Gtk.Button(label="Save")
        self._ready = False

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)

        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        box.append(grid)
        row = 0

        self.name_entry = Gtk.Entry(placeholder_text="Checking")
        self.name_entry.set_hexpand(True)
        self.name_entry.connect("changed", self._validate)
        if account is not None:
            self.name_entry.set_text(account.name)
        grid.attach(Gtk.Label(label="Name", xalign=0), 0, row, 1, 1)
        grid.attach(self.name_entry, 1, row, 1, 1)
        row += 1

        self.types = list(_TYPES)
        if account is not None and account.atype not in self.types:
            self.types.append(account.atype)
        self.type_picker = Gtk.DropDown.new_from_strings([t.value for t in self.types])
        if account is not None:
            self.type_picker.set_selected(self.types.index(account.atype))
        self.type_picker.connect("notify::selected", self._on_type_changed)
        self.type_picker.set_tooltip_text(
            "BreadSched account behavior; an imported GnuCash type is retained separately"
        )
        grid.attach(Gtk.Label(label="Account type", xalign=0), 0, row, 1, 1)
        grid.attach(self.type_picker, 1, row, 1, 1)
        row += 1

        self.source_summary = Gtk.Label(xalign=0, selectable=True)
        source_box = Gtk.Box(spacing=8)
        source_box.append(self.source_summary)
        self.source_button = Gtk.Button(label="Details…")
        self.source_button.connect("clicked", self._show_source_details)
        source_box.append(self.source_button)
        imported = account is not None and bool(account.source_guid or account.source_type)
        if imported and account is not None:
            self.source_summary.set_text(account.source_type or "Unknown type")
        source_box.set_visible(imported)
        source_heading = Gtk.Label(label="GnuCash source", xalign=0)
        source_heading.set_visible(imported)
        grid.attach(source_heading, 0, row, 1, 1)
        grid.attach(source_box, 1, row, 1, 1)
        row += 1

        commodity_labels = ["(book/default)"] + [
            f"{commodity.mnemonic} ({commodity.namespace})" for commodity in self.commodities
        ]
        if account is not None and account.commodity not in self.commodity_handles:
            commodity_labels.append(f"Imported commodity ({account.commodity})")
            self.commodity_handles.append(account.commodity)
        self.commodity_picker = Gtk.DropDown.new_from_strings(commodity_labels)
        if account is not None and account.commodity:
            self.commodity_picker.set_selected(self.commodity_handles.index(account.commodity))
        self.commodity_picker.set_tooltip_text("Currency or security associated with this account")
        grid.attach(Gtk.Label(label="Commodity", xalign=0), 0, row, 1, 1)
        grid.attach(self.commodity_picker, 1, row, 1, 1)
        row += 1

        self.commodity_scu_entry = Gtk.Entry(placeholder_text="Commodity default")
        self.commodity_scu_entry.set_tooltip_text(
            "GnuCash account-specific smallest commodity unit (SCU); "
            "leave blank for the commodity default"
        )
        if account is not None and account.commodity_scu is not None:
            self.commodity_scu_entry.set_text(str(account.commodity_scu))
        self.commodity_scu_entry.connect("changed", self._validate)
        grid.attach(Gtk.Label(label="Commodity SCU", xalign=0), 0, row, 1, 1)
        grid.attach(self.commodity_scu_entry, 1, row, 1, 1)
        row += 1

        self.parent_picker = Gtk.DropDown.new_from_strings(
            [db.full_name(a) or a.name for a in self.parents] or ["(none)"]
        )
        chosen = account.parent if account is not None else default_parent
        for index, candidate in enumerate(self.parents):
            if candidate.handle == chosen:
                self.parent_picker.set_selected(index)
                break
        grid.attach(Gtk.Label(label="Parent", xalign=0), 0, row, 1, 1)
        grid.attach(self.parent_picker, 1, row, 1, 1)
        row += 1

        self.code_entry = Gtk.Entry(placeholder_text="Optional")
        if account is not None:
            self.code_entry.set_text(account.code)
        grid.attach(Gtk.Label(label="Code", xalign=0), 0, row, 1, 1)
        grid.attach(self.code_entry, 1, row, 1, 1)
        row += 1

        self.description_entry = Gtk.Entry()
        if account is not None:
            self.description_entry.set_text(account.description)
        grid.attach(Gtk.Label(label="Description", xalign=0), 0, row, 1, 1)
        grid.attach(self.description_entry, 1, row, 1, 1)
        row += 1

        self.notes_view = Gtk.TextView()
        self.notes_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.notes_view.set_size_request(-1, 72)
        self.notes_view.set_tooltip_text(
            "BreadSched-owned notes; imported notes remain read-only under GnuCash source details"
        )
        if account is not None and account.notes:
            self.notes_view.get_buffer().set_text(account.notes)
        notes_scroll = Gtk.ScrolledWindow()
        notes_scroll.set_min_content_height(72)
        notes_scroll.set_child(self.notes_view)
        grid.attach(
            Gtk.Label(label="Notes", xalign=0, valign=Gtk.Align.START),
            0,
            row,
            1,
            1,
        )
        grid.attach(notes_scroll, 1, row, 1, 1)
        row += 1

        self.group_entry = Gtk.Entry(placeholder_text="Investments:Plan A")
        self.group_entry.set_tooltip_text(
            "Colon-separated dashboard path; parent headings total their child groups"
        )
        if account is not None:
            self.group_entry.set_text(account.group)
        grid.attach(Gtk.Label(label="Dashboard group", xalign=0), 0, row, 1, 1)
        grid.attach(self.group_entry, 1, row, 1, 1)
        row += 1

        self.fsa_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.fsa_box.add_css_class("card")
        self.fsa_rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        fsa_heading = Gtk.Label(label="FSA funding years", xalign=0)
        fsa_heading.add_css_class("summary-label")
        self.fsa_box.append(fsa_heading)
        fsa_note = Gtk.Label(
            label=(
                "Election availability is tracked separately from the ledger balance. "
                "Run-out allows explicitly assigned prior-year claims after year-end."
            ),
            xalign=0,
            wrap=True,
        )
        fsa_note.add_css_class("dim")
        self.fsa_box.append(fsa_note)
        self.fsa_box.append(self.fsa_rows)
        add_fsa = Gtk.Button(label="Add funding year")
        add_fsa.connect("clicked", lambda *_: self._add_fsa_year_row())
        self.fsa_box.append(add_fsa)
        box.append(self.fsa_box)
        if account is not None:
            for funding_year in account.fsa_years:
                self._add_fsa_year_row(funding_year)

        self.placeholder_check = Gtk.CheckButton(label="Placeholder (holds no entries)")
        if account is not None:
            self.placeholder_check.set_active(account.placeholder)
        grid.attach(self.placeholder_check, 1, row, 1, 1)
        row += 1

        self.hidden_check = Gtk.CheckButton(label="Hidden")
        self.hidden_check.set_tooltip_text(
            "Hide this account from normal account lists unless hidden accounts are shown"
        )
        if account is not None:
            self.hidden_check.set_active(account.hidden)
        grid.attach(self.hidden_check, 1, row, 1, 1)
        row += 1

        if imported:
            source_owned = (
                self.name_entry,
                self.commodity_picker,
                self.commodity_scu_entry,
                self.parent_picker,
                self.code_entry,
                self.description_entry,
                self.placeholder_check,
                self.hidden_check,
            )
            for control in source_owned:
                control.set_sensitive(False)
                control.set_tooltip_text(
                    "Controlled by the GnuCash source; change it there, then re-import"
                )
            self.source_summary.set_tooltip_text(
                "Chart fields are source-controlled; BreadSched planning fields remain editable"
            )

        self.emergency_check = Gtk.CheckButton(label="Carry in emergency fund")
        self.emergency_check.set_tooltip_text(
            "Include recurring activity against this account when sizing the "
            "no-income emergency fund"
        )
        self.emergency_check.set_active(
            account.emergency_fund_override is not False if account is not None else True
        )
        grid.attach(self.emergency_check, 1, row, 1, 1)
        row += 1

        self.opening_entry = Gtk.Entry(placeholder_text="0.00")
        self.opening_entry.set_sensitive(not editing)
        self.opening_entry.set_tooltip_text(
            "Posted against Opening Balances when the account is created"
        )
        grid.attach(Gtk.Label(label="Opening balance", xalign=0), 0, row, 1, 1)
        grid.attach(self.opening_entry, 1, row, 1, 1)
        row += 1

        # --- loan --------------------------------------------------------
        self.loan_box = Gtk.Box(spacing=8)
        self.asset_picker = Gtk.DropDown.new_from_strings(
            ["(none)"] + [db.full_name(a) for a in self.assets]
        )
        if account is not None and account.linked_asset:
            for index, asset in enumerate(self.assets, start=1):
                if asset.handle == account.linked_asset:
                    self.asset_picker.set_selected(index)
                    break
        self.loan_box.append(Gtk.Label(label="Secured on", xalign=0))
        self.loan_box.append(self.asset_picker)
        box.append(self.loan_box)

        # --- credit card -------------------------------------------------
        self.card_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.card_box.add_css_class("card")
        self.full_check = Gtk.CheckButton(label="Cleared in full every month")
        self.full_check.set_active(account.pays_in_full if account is not None else True)
        self.full_check.connect("toggled", self._on_card_changed)
        self.card_box.append(self.full_check)

        card_grid = Gtk.Grid(column_spacing=10, row_spacing=6)
        self.usual_entry = Gtk.Entry(placeholder_text="400.00")
        self.usual_entry.set_tooltip_text(
            "Carried as a scheduled estimate while a balance is outstanding"
        )
        if account is not None and account.usual_payment:
            self.usual_entry.set_text(f"{account.usual_payment.to_decimal():.2f}")
        card_grid.attach(Gtk.Label(label="Usual payment", xalign=0), 0, 0, 1, 1)
        card_grid.attach(self.usual_entry, 1, 0, 1, 1)

        self.day_spin = Gtk.SpinButton.new_with_range(1, 28, 1)
        if account is not None and account.payment_day:
            self.day_spin.set_value(account.payment_day)
        card_grid.attach(Gtk.Label(label="Payment day", xalign=0), 0, 1, 1, 1)
        card_grid.attach(self.day_spin, 1, 1, 1, 1)

        self.card_payment_picker = Gtk.DropDown.new_from_strings(
            ["(choose when recording payment)"]
            + [db.full_name(item) for item in self.payment_accounts]
        )
        if account is not None and account.card_payment_account:
            for index, payment_account in enumerate(self.payment_accounts, start=1):
                if payment_account.handle == account.card_payment_account:
                    self.card_payment_picker.set_selected(index)
                    break
        card_grid.attach(Gtk.Label(label="Paid from", xalign=0), 0, 2, 1, 1)
        card_grid.attach(self.card_payment_picker, 1, 2, 1, 1)
        self.card_box.append(card_grid)
        box.append(self.card_box)

        box.append(self.status)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        if account is not None:
            delete = Gtk.Button(label="Delete")
            delete.add_css_class("destructive-action")
            delete.connect("clicked", self._on_delete)
            buttons.append(delete)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        buttons.append(cancel)
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", self._on_save)
        buttons.append(self.save_button)
        box.append(buttons)

        self._ready = True
        self._on_type_changed()
        self._validate()

    # -------------------------------------------------------------- reactions

    def _show_source_details(self, _button) -> None:
        """Show imported provenance without making source-owned fields editable."""
        if self.account is None:
            return
        window = Gtk.Window(
            title="Imported account details",
            transient_for=self,
            modal=True,
        )
        window.set_default_size(620, 420)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(14)
        heading = Gtk.Label(
            label="Read-only GnuCash provenance",
            xalign=0,
        )
        heading.add_css_class("title-3")
        box.append(heading)
        lines = [
            f"Source GUID: {self.account.source_guid or '(not retained)'}",
            f"Source type: {self.account.source_type or '(unknown)'}",
            f"Source notes: {self.account.source_notes or '(none)'}",
        ]
        if self.account.source_fields:
            lines.append("")
            lines.extend(
                f"{field.name} [{field.value_type}]: {field.value}"
                for field in self.account.source_fields
            )
        else:
            lines.extend(("", "No additional source fields were retained."))
        details = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
        details.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        details.get_buffer().set_text("\n".join(lines))
        scroller = Gtk.ScrolledWindow(child=details)
        scroller.set_vexpand(True)
        scroller.set_hexpand(True)
        box.append(scroller)
        close = Gtk.Button(label="Close", halign=Gtk.Align.END)
        close.connect("clicked", lambda *_: window.close())
        box.append(close)
        window.set_child(box)
        window.present()

    @property
    def selected_type(self) -> AccountType:
        return self.types[self.type_picker.get_selected()]

    def _add_fsa_year_row(self, funding_year: FsaFundingYear | None = None) -> None:
        row = Gtk.Box(spacing=6)
        start = Gtk.Entry(placeholder_text="Start YYYY-MM-DD")
        through = Gtk.Entry(placeholder_text="Through YYYY-MM-DD")
        election = Gtk.Entry(placeholder_text="Election")
        runout = Gtk.Entry(placeholder_text="Run-out through (optional)")
        if funding_year is not None:
            start.set_text(funding_year.start.isoformat())
            through.set_text(funding_year.through.isoformat())
            election.set_text(f"{funding_year.election.to_decimal():.2f}")
            if funding_year.runout_through:
                runout.set_text(funding_year.runout_through.isoformat())
        for widget in (start, through, election, runout):
            widget.connect("changed", self._validate)
            row.append(widget)
        remove = Gtk.Button(label="Remove")

        def remove_row(*_args) -> None:
            self.fsa_rows.remove(row)
            self._validate()

        remove.connect("clicked", remove_row)
        row.append(remove)
        row._fsa_fields = (start, through, election, runout)
        self.fsa_rows.append(row)
        self._validate()

    def _fsa_year_values(self) -> list[FsaFundingYear]:
        from datetime import date

        years: list[FsaFundingYear] = []
        child = self.fsa_rows.get_first_child()
        while child is not None:
            start, through, election, runout = child._fsa_fields
            years.append(
                FsaFundingYear(
                    date.fromisoformat(start.get_text().strip()),
                    date.fromisoformat(through.get_text().strip()),
                    Money(parse_user_amount(election.get_text().strip())),
                    date.fromisoformat(runout.get_text().strip())
                    if runout.get_text().strip()
                    else None,
                )
            )
            child = child.get_next_sibling()
        years.sort(key=lambda year: year.start)
        for earlier, later in zip(years, years[1:], strict=False):
            if later.start <= earlier.through:
                raise ValueError("FSA funding years cannot overlap")
        return years

    def _on_type_changed(self, *_args) -> None:
        """Only show fields that mean something for the selected account type."""
        if not self._ready:
            return
        account_type = self.selected_type
        self.fsa_box.set_visible(account_type is AccountType.FSA)
        self.loan_box.set_visible(account_type is AccountType.LOAN)
        self.card_box.set_visible(account_type is AccountType.CREDIT)
        self._on_card_changed()
        self._refresh_emergency_control()
        self._validate()

    def _on_card_changed(self, *_args) -> None:
        if not self._ready:
            return
        carrying = not self.full_check.get_active()
        self.usual_entry.set_sensitive(carrying)
        self.day_spin.set_sensitive(True)
        self._refresh_emergency_control()

    def _refresh_emergency_control(self) -> None:
        if not self._ready:
            return
        eligible = self.selected_type.supports_emergency_fund and not (
            self.selected_type is AccountType.CREDIT and self.full_check.get_active()
        )
        self.emergency_check.set_visible(eligible)

    def _validate(self, *_args) -> None:
        if not self._ready:
            return
        problems = []
        if not self.name_entry.get_text().strip():
            problems.append("give it a name")
        if not self.parents:
            problems.append("this book has no parent account to hang it from")
        scu_text = self.commodity_scu_entry.get_text().strip()
        if scu_text:
            try:
                if int(scu_text) <= 0:
                    problems.append("commodity SCU must be a positive integer")
            except ValueError:
                problems.append("commodity SCU must be a positive integer")
        if self.selected_type is AccountType.FSA:
            try:
                self._fsa_year_values()
            except (ValueError, InvalidOperation, ArithmeticError) as exc:
                problems.append(str(exc))
        self.status.set_text("; ".join(problems).capitalize())
        self.save_button.set_sensitive(not problems)

    # ----------------------------------------------------------------- saving

    def build(self) -> Account:
        account = Account.from_dict(self.account.serialize()) if self.account else Account()
        account.name = self.name_entry.get_text().strip()
        account.atype = self.selected_type
        account.code = self.code_entry.get_text().strip()
        account.description = self.description_entry.get_text().strip()
        notes_buffer = self.notes_view.get_buffer()
        notes_start, notes_end = notes_buffer.get_bounds()
        account.notes = notes_buffer.get_text(notes_start, notes_end, True).strip()
        commodity_index = self.commodity_picker.get_selected()
        account.commodity = self.commodity_handles[commodity_index]
        scu_text = self.commodity_scu_entry.get_text().strip()
        account.commodity_scu = int(scu_text) if scu_text else None
        account.group = self.group_entry.get_text().strip()
        if account.atype is AccountType.FSA:
            account.fsa_years = self._fsa_year_values()
        account.placeholder = self.placeholder_check.get_active()
        account.hidden = self.hidden_check.get_active()
        if self.parents:
            account.parent = self.parents[self.parent_picker.get_selected()].handle

        index = self.asset_picker.get_selected()
        account.linked_asset = (
            self.assets[index - 1].handle
            if account.atype is AccountType.LOAN and index > 0
            else None
        )

        if account.atype is AccountType.CREDIT:
            account.pays_in_full = self.full_check.get_active()
            text = self.usual_entry.get_text().strip()
            try:
                account.usual_payment = Money(parse_user_amount(text)) if text else None
            except (ValueError, InvalidOperation, ArithmeticError):
                account.usual_payment = None
            account.payment_day = int(self.day_spin.get_value())
            payment_index = self.card_payment_picker.get_selected()
            account.card_payment_account = (
                self.payment_accounts[payment_index - 1].handle if payment_index > 0 else None
            )
        else:
            account.card_payment_account = None
        if account.emergency_fund_eligible:
            account.emergency_fund_override = self.emergency_check.get_active()
        return account

    def _on_save(self, _button) -> None:
        account = self.build()
        opening = self.opening_entry.get_text().strip()
        try:
            opening_value = Money(parse_user_amount(opening)) if opening else None
        except (ValueError, InvalidOperation, ArithmeticError):
            self.status.set_text("Enter a valid opening balance.")
            self.status.add_css_class("negative")
            return
        result = save_account(
            self.db,
            SaveAccount(
                account,
                existing_handle=account.handle if self.editing else None,
                opening_balance=opening_value if not self.editing else None,
                opening_date=date.today(),
                source=self.account,
            ),
        )
        if not result.ok:
            self.status.set_text(service_error_message(result.errors[0]))
            self.status.add_css_class("negative")
            return
        self.close()

    def _on_delete(self, _button) -> None:
        if not self.editing or self.account is None:
            return
        result = delete_account(self.db, DeleteAccount(self.account.handle))
        if not result.ok:
            self.status.set_text(service_error_message(result.errors[0]))
            self.status.add_css_class("negative")
            return
        self.close()
