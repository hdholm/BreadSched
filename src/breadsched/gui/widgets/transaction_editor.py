"""The one transaction editor: every window that enters or changes a transaction.

A transaction is typed the same way wherever it is entered: the transaction
dialog (the register's Edit and New, reconciliation, File → New Transaction) and
the window that shows what the due review posted both hold a
:class:`TransactionEditor`, and the schedule editors use its pages too. It has:

- **Entry**: date, number, description, and notes;
- the **splits**, always shown beneath the pages, on the register's own grid
  (``split_sheet.SplitSheet`` drawn by ``widgets/register_grid``): memo and
  account completion, arithmetic amounts, the two amount columns named as the
  register names them, the rest of an unbalanced entry on the empty last line,
  shares and price in an account that holds a security, and, with **Planning
  detail**, each split's planning purpose and investment activity;
- **Tags & documents**: tags, linked documents, and how escrow is treated.

Rules the editor enforces rather than explains: Save stays unavailable until the
splits balance, so an impossible transaction cannot be recorded and repaired
later; a new entry whose splits are untouched is proposed from the latest earlier
one with the same description (``services.autocomplete``) when the description
is left, filling only the form, with a note saying so; and a change to a split
already reconciled against a statement asks first (#403).

Tags are saved with the transaction. Linked documents are changed at once through
``services.attachments`` (each change is its own undoable step) and only on a
transaction that has been saved; a missing file is marked, never removed.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path

from ...api import ApiError
from ...api.local import book_server
from ...gen.engine import escrow, fsa_claims
from ...gen.lib import Money, Transaction
from ...gen.services import ClaimAttachment, SaveTransaction, TransactionInput
from ...gen.services.autocomplete import EntrySuggestion, SuggestEntry
from ...gen.services.transactions import reconciled_changes
from ...presentation import claim_role_label
from ..gi_setup import GLib, Gtk
from ..remote_book import OpenBook
from ..split_sheet import SplitSheet
from .bounded import keeps_enter
from .choice import bounded_dropdown
from .date_entry import attach_date_keys, parse_date_text
from .editor_parts import DocumentsPage, SplitsPanel
from .form_pages import FormPages

__all__ = ["ENTRY", "MORE", "TransactionEditor"]

#: The first page: what the transaction is.
ENTRY = "Entry"
#: The second page: tags, documents, and escrow detail; its tab counts them (#383).
MORE = "Tags & documents"
#: Split lines the grid shows at its smallest; it grows with the window.
_GRID_LINES = 4


class TransactionEditor(Gtk.Box):
    """Enter a transaction, or change one that already exists, inside any window."""

    def __init__(
        self,
        db: OpenBook,
        *,
        default_account: str | None = None,
        default_date: date | None = None,
        transaction: Transaction | None = None,
        on_saved: Callable[[str], None] | None = None,
        on_close: Callable[[], None] | None = None,
        save_with: Callable[[SaveTransaction], str] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.db = db
        self.transaction = transaction
        self.editing = transaction is not None
        self.default_account = default_account
        #: Called with the saved transaction's handle after Save, and to close the window
        #: (Escape in the splits).
        self.on_saved = on_saved
        self.on_close = on_close
        #: How Save stores the entry, returning its handle: the shared save service
        #: unless the window adds the entry another way (a statement line, which is
        #: also cleared and checked on the statement).
        self.save_with = save_with
        #: True once Save has written the transaction.
        self.saved = False
        #: The accounts of reconciled splits the last Save asked about (#403).
        self.reconciled_question: tuple[str, ...] | None = None

        # What the editor shows besides the transaction, through the book server (#358).
        self.book = book_server(db)
        stored = transaction.handle if transaction is not None else None
        try:
            self.context = self.book.transaction_editor(stored)
        except ApiError:
            self.context = self.book.transaction_editor(None)
        self._by_handle = {
            record.account.handle: record.account for record in self.context.accounts
        }

        self.pages = FormPages()
        self.append(self.pages)
        header = self.pages.page(ENTRY)
        more = self.pages.page(MORE)

        when = (transaction.post_date if transaction is not None else default_date) or date.today()
        self.date_entry = attach_date_keys(Gtk.Entry(text=when.isoformat()))
        self.date_entry.connect("changed", self.revalidate)
        header.add("Date", self.date_entry)

        self.num_entry = Gtk.Entry(placeholder_text="Cheque or reference")
        if transaction is not None:
            self.num_entry.set_text(transaction.num)
        header.add("Number", self.num_entry)

        self.description_entry = Gtk.Entry(placeholder_text="What was it for")
        if transaction is not None:
            self.description_entry.set_text(transaction.description)
        self.description_entry.connect("changed", self._description_changed)
        header.add("Description", self.description_entry)
        if transaction is None:
            # Leaving the description proposes the latest matching entry's splits.
            keeps_enter(self.description_entry)
            self.description_entry.connect("activate", lambda *_: self.propose_from_entry())
            leave = Gtk.EventControllerFocus()
            leave.connect("leave", lambda *_: self.propose_from_entry())
            self.description_entry.add_controller(leave)

        self.notes_view = Gtk.TextView()
        self.notes_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        if transaction is not None and transaction.notes:
            self.notes_view.get_buffer().set_text(transaction.notes)
        notes_scroll = Gtk.ScrolledWindow(child=self.notes_view)
        notes_scroll.set_min_content_height(56)
        # One note, imported ones included (#385).
        header.add("Notes", notes_scroll)

        self.escrow_treatment: Gtk.Label | None = None
        if transaction is not None:
            explanation = list(self.context.escrow_explanations) or escrow.recognition(
                ((split.account, split.value) for split in transaction.splits), self._by_handle
            ).explanations(self._by_handle)
            if explanation:
                self.escrow_treatment = Gtk.Label(
                    label="\n".join(explanation), xalign=0, wrap=True, selectable=True
                )
                more.add("Escrow treatment", self.escrow_treatment)

        self.docs = DocumentsPage(
            more,
            self.book,
            "transaction",
            transaction,
            folder=self.context.folder,
            on_count=self._count_more,
            on_error=self._fail,
            on_changed=self._document_changed,
        )
        self.tags_entry = self.docs.tags_entry
        self.documents = self.docs.documents
        self._count_more()

        # The splits, on the register's grid, beneath every page.
        self.sheet = SplitSheet(
            db,
            self.context.accounts,
            security_accounts=self.context.security_accounts,
            transaction=transaction,
            account=default_account
            or (transaction.splits[0].account if transaction and transaction.splits else None),
            currency=transaction.currency if transaction is not None else None,
        )
        self.sheet.on_enter = self._on_grid_enter
        self.sheet.on_escape = self._on_grid_escape
        self.splits_panel = SplitsPanel(self.sheet, self.revalidate)
        self.detail_toggle = self.splits_panel.detail_toggle
        self.grid = self.splits_panel.grid
        self.append(self.splits_panel)

        self.proposal_note = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.proposal_note.add_css_class("dim")
        self.append(self.proposal_note)

        self.fsa_claims = list(self.context.open_claims)
        self.fsa_claim = None
        self.fsa_role = None
        if self.fsa_claims and not self.editing:
            fsa_row = Gtk.Box(spacing=8)
            fsa_row.append(Gtk.Label(label="FSA claim (optional)", xalign=0))
            self.fsa_claim = bounded_dropdown(
                ["Do not attach"]
                + [
                    f"{claim.service_date} {claim.provider or claim.description or 'FSA claim'}"
                    for claim in self.fsa_claims
                ]
            )
            fsa_row.append(self.fsa_claim)
            self.fsa_role = bounded_dropdown(
                [claim_role_label(role) for role in fsa_claims.ATTACHMENT_ROLES]
            )
            fsa_row.append(self.fsa_role)
            self.append(fsa_row)

        self.status = Gtk.Label(xalign=0, wrap=True)
        self.append(self.status)

        #: Whether Save may be chosen now; the window's Save button follows it.
        self.can_save = False
        self.on_can_save: Callable[[bool], None] | None = None
        self.revalidate()

    # ----------------------------------------------------------------- splits

    def _description_changed(self, *_args) -> None:
        # Memos complete from those written before for this description (#407).
        self.sheet.draft.description = self.description_entry.get_text()
        self.revalidate()

    def _on_grid_enter(self) -> bool:
        self.revalidate()
        if self.can_save:
            return self.save()
        return False

    def _on_grid_escape(self) -> None:
        if self.on_close is not None:
            self.on_close()

    def lines(self) -> list[tuple[str, str, str, str]]:
        """Each split line with input, as (account, memo, increase, decrease) text."""
        return [
            (line.account, line.memo, line.increase, line.decrease)
            for line in self.sheet.lines
            if line.has_input()
        ]

    def split_values(self) -> list[tuple[str | None, Money | None, str]]:
        """Each line with input as (account handle, signed amount, memo)."""
        return self.sheet.line_values()

    def type_line(self, index: int, **cells: str) -> None:
        """Type into split line ``index`` as the keyboard would (``SplitsPanel``)."""
        self.splits_panel.type_line(index, **cells)
        self.revalidate()

    # ----------------------------------------------------------------- filling

    def prefill(
        self,
        *,
        description: str = "",
        num: str = "",
        transfer: str | None = None,
        amount: Money | None = None,
        splits: list[tuple[str | None, Money | None, str]] | None = None,
    ) -> None:
        """Start a new transaction from a register's blank entry row (#158).

        ``amount`` is signed as it moves the register account; the transfer split
        balances it. With no amount the lines stay blank, so the ordinary proposal
        from the description can still fill them. ``splits`` instead gives
        (account, value, memo) for each split.
        """
        if self.transaction is not None:
            return
        self.description_entry.set_text(description)
        self.num_entry.set_text(num)
        here = self.default_account
        if splits:
            self.sheet.set_lines(splits)
        elif transfer is not None or amount is not None:
            self.sheet.set_lines(
                [
                    (here, amount, ""),
                    (transfer, -amount if amount is not None else None, ""),
                ]
            )
        elif description:
            self.propose_from_entry()
        self.grid.sync(focus=False)
        self.revalidate()

    def propose_from_entry(self) -> EntrySuggestion | None:
        """Fill untouched lines of a new transaction from the latest matching entry."""
        if self.transaction is not None:
            return None  # an existing transaction is never rewritten
        if not self.sheet.untouched():
            return None  # never overwrite what the user typed
        try:
            proposal = self.book.suggest_entry(
                SuggestEntry(
                    description=self.description_entry.get_text(),
                    account=self.default_account,
                    currency=self.book.transaction_currency(),
                )
            )
        except ApiError:
            return None
        suggestion = proposal.suggestion
        if suggestion is None or len(suggestion.splits) < 2:
            return None
        if any(split.account not in self.sheet.full_names for split in suggestion.splits):
            return None
        self.sheet.set_lines(
            [(split.account, split.value, split.memo) for split in suggestion.splits]
        )
        notes = self.notes_view.get_buffer()
        if (
            suggestion.notes.strip()
            and not notes.get_text(notes.get_start_iter(), notes.get_end_iter(), False).strip()
        ):
            notes.set_text(suggestion.notes)  # #363: only notes left empty
        self.proposal_note.set_text(
            f"Proposed from {suggestion.when.isoformat()} “{suggestion.description}”. "
            "Edit anything before saving."
        )
        self.proposal_note.set_visible(True)
        self.grid.sync(focus=False)
        self.revalidate()
        return suggestion

    # -------------------------------------------------------------- validation

    def revalidate(self, *_args) -> None:
        """Say whether the entry can be saved, and why not; Save follows it."""
        if not hasattr(self, "status"):
            return  # still being built
        problem = None
        sheet = self.sheet
        typed = [line for line in sheet.lines if line.has_input()]
        try:
            parse_date_text(self.date_entry.get_text().strip())
        except ValueError:
            problem = "Check the date (YYYY-MM-DD)."
        residual = sheet.residual()
        if sheet.error and sheet.message:
            text, negative = sheet.message, True
        elif problem is not None:
            text, negative = problem, True
        elif len(typed) < 2:
            text, negative = "Choose at least two accounts and their amounts.", False
        elif residual:
            text = (
                f"Out of balance by {residual.format(parens_negative=True)}: the last line "
                "holds the rest; choose its account."
            )
            negative = True
        else:
            text, negative = "Balanced.", False
        self.status.set_text(text)
        if negative:
            self.status.add_css_class("negative")
        else:
            self.status.remove_css_class("negative")
        ready = problem is None and len(typed) >= 2 and not residual
        if ready != self.can_save:
            self.can_save = ready
        if self.on_can_save is not None:
            self.on_can_save(ready)

    def _fail(self, text: str) -> bool:
        # Redrawn first: the grid's redraw restates the balance on the status line.
        self.grid.sync(focus=False)
        self.status.set_text(text)
        self.status.add_css_class("negative")
        return False

    # ------------------------------------------------------------------ saving

    def request(self) -> SaveTransaction:
        """Translate the form into the shared typed service contract;
        ``ValueError`` with the reason when it cannot be saved."""
        try:
            when = parse_date_text(self.date_entry.get_text().strip())
        except ValueError:
            raise ValueError("Check the date (YYYY-MM-DD).") from None
        currency = (
            self.transaction.currency if self.transaction is not None else None
        ) or self.book.transaction_currency()
        self.sheet.message, self.sheet.error = "", False
        splits = self.sheet.splits(currency)
        if splits is None:
            raise ValueError(self.sheet.message or "Check the splits.")
        attachment = None
        if (
            self.fsa_claim is not None
            and self.fsa_role is not None
            and self.fsa_claim.get_selected() > 0
        ):
            claim = self.fsa_claims[self.fsa_claim.get_selected() - 1]
            role = fsa_claims.ATTACHMENT_ROLES[self.fsa_role.get_selected()]
            attachment = ClaimAttachment(claim.handle, role)
        notes_start, notes_end = self.notes_view.get_buffer().get_bounds()
        return SaveTransaction(
            TransactionInput(
                post_date=when,
                description=self.description_entry.get_text().strip() or "(no description)",
                num=self.num_entry.get_text().strip(),
                notes=self.notes_view.get_buffer().get_text(notes_start, notes_end, True).strip(),
                currency=currency,
                splits=splits,
                tags=self.docs.tags(),
            ),
            existing_handle=self.transaction.handle if self.transaction is not None else None,
            claim_attachment=attachment,
            source=self.transaction,
        )

    def build(self) -> Transaction:
        """The service-built preview of what Save would store."""
        try:
            return self.book.draft_transaction(self.request())
        except ApiError as error:
            raise ValueError(str(error)) from error

    def save(self, *, confirmed: bool = False) -> bool:
        """Save the transaction; False (with the reason shown) when it cannot be.

        A change to a split already reconciled against a statement asks first
        (#403); its answer saves again with ``confirmed``.
        """
        if self.sheet.settle() is False:
            return self._fail(self.sheet.message)
        try:
            request = self.request()
        except ValueError as exc:
            return self._fail(str(exc))
        changed = (
            reconciled_changes(self.transaction, request.definition.splits)
            if self.transaction is not None
            and not confirmed
            # Allowed already, when the first change to it was typed.
            and not self.sheet.reconciled_allowed()
            else ()
        )
        if changed:
            names = ", ".join(self.book.account(account).full_name for account in changed)
            alert = Gtk.AlertDialog(
                message="Change a reconciled split?",
                detail=(
                    f"This changes a split already reconciled against a statement ({names}). "
                    "The statement's reconciled balance will no longer match it."
                ),
                buttons=["Cancel", "Save anyway"],
                cancel_button=0,
                default_button=0,
            )

            def answered(dialog, result) -> None:
                try:
                    choice = dialog.choose_finish(result)
                except GLib.Error:
                    choice = 0
                if choice == 1:
                    self.save(confirmed=True)

            alert.choose(self.get_root(), None, answered)
            self.reconciled_question = changed
            return False
        try:
            handle = (
                self.save_with(request)
                if self.save_with is not None
                else self.book.save_transaction(request).handle
            )
        except ApiError as error:
            return self._fail(str(error))
        self.saved = True
        if self.on_saved is not None:
            self.on_saved(handle)
        return True

    def delete(self) -> bool:
        """Delete the transaction being edited; False with the reason if refused."""
        if self.transaction is None:
            return False
        try:
            self.book.delete_transaction(self.transaction.handle)
        except ApiError as error:
            return self._fail(str(error))
        return True

    def make_scheduled(self, parent: Gtk.Window | None):
        """Open a new scheduled transaction drafted from every split."""
        if self.transaction is None:
            return None
        from ...gen.engine import schedule
        from ..dialogs.schedule_dialog import ScheduleDialog

        draft = schedule.from_transaction(self.transaction)
        dialog = ScheduleDialog(parent, self.db, source=draft, creating=True)
        dialog.present()
        return dialog

    # --------------------------------------------------------------- documents

    def _count_more(self) -> None:
        """Count what the second page holds on its tab."""
        if hasattr(self, "docs"):
            self.pages.set_count(MORE, self.docs.count())

    def _document_changed(self, changed: Transaction) -> None:
        # Later saves build on the stored transaction, including its documents.
        self.transaction = changed
        self.sheet.draft.source = changed

    @property
    def document_rows(self):
        return self.docs.document_rows

    @property
    def link_entry(self) -> Gtk.Entry | None:
        return self.docs.link_entry

    def _show_documents(self) -> None:
        self.docs.show()

    def attach_path(self, path: Path) -> bool:
        return self.docs.attach_path(path)

    def link_location(self, location: str) -> bool:
        return self.docs.link_location(location)

    def remove_document(self, location: str) -> bool:
        return self.docs.remove_document(location)

    def relink_document(self, location: str, path: Path) -> bool:
        return self.docs.relink_document(location, path)

    def open_document(self, item) -> str | None:
        return self.docs.open_document(item)
