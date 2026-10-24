"""Review actual transactions that have not yet been resolved against the plan."""

from __future__ import annotations

from ...api import ApiError
from ...api.local import book_server
from ...gen.lib.money import Money
from ...gen.services import ReviewClaimAttachment, ReviewOccurrence
from ...gen.services.review import ReviewDetail
from ...presentation import REVIEW_ACTION_HELP, claim_role_label
from ..gi_setup import Gtk, Pango
from ..widgets.bounded import BoundedWindow
from ..widgets.choice import bounded_dropdown
from ._base import BaseView

__all__ = ["ResolutionView"]


class ResolutionView(BaseView):
    """Queue of actuals awaiting Match / Reject / Unexpected decisions."""

    WATCHES = (
        "database-changed",
        "transaction-add",
        "transaction-update",
        "transaction-delete",
        "scheduled-add",
        "scheduled-update",
        "scheduled-delete",
    )

    def __init__(self, manager) -> None:
        super().__init__(manager)
        self._transaction_handle: str | None = None
        self._candidate_key: str | None = None
        self._detail: ReviewDetail | None = None
        self._build()

    def _build(self) -> None:
        title = Gtk.Label(label="Resolve actuals", xalign=0)
        title.add_css_class("category-title")
        title.set_margin_top(12)
        title.set_margin_start(12)
        self.append(title)

        subtitle = Gtk.Label(
            label=(
                "Match imported or entered transactions to their scheduled "
                "expectations, reject bad suggestions, or mark them unexpected."
            ),
            xalign=0,
            wrap=True,
        )
        subtitle.set_margin_start(12)
        subtitle.set_margin_end(12)
        subtitle.set_margin_bottom(8)
        self.append(subtitle)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_position(480)
        paned.set_vexpand(True)
        self.append(paned)

        self.actual_list = Gtk.ListBox()
        self.actual_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.actual_list.connect("row-selected", self._on_actual_selected)
        actual_scroll = Gtk.ScrolledWindow(child=self.actual_list)
        actual_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        paned.set_start_child(actual_scroll)

        detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        detail.set_margin_top(12)
        detail.set_margin_bottom(12)
        detail.set_margin_start(12)
        detail.set_margin_end(12)
        paned.set_end_child(detail)

        self.actual_summary = Gtk.Label(xalign=0, wrap=True)
        self.actual_summary.add_css_class("summary-value")
        detail.append(self.actual_summary)

        # What an FSA movement is and what to do with it (empty otherwise).
        self.fsa_hint = Gtk.Label(xalign=0, wrap=True)
        self.fsa_hint.add_css_class("dim")
        self.fsa_hint.set_visible(False)
        detail.append(self.fsa_hint)

        candidates_label = Gtk.Label(label="Candidate scheduled occurrences", xalign=0)
        candidates_label.add_css_class("heading")
        detail.append(candidates_label)

        self.candidate_list = Gtk.ListBox()
        self.candidate_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.candidate_list.connect("row-selected", self._on_candidate_selected)
        candidate_scroll = Gtk.ScrolledWindow(child=self.candidate_list)
        candidate_scroll.set_vexpand(True)
        detail.append(candidate_scroll)

        self.variance = Gtk.Label(xalign=0, wrap=True)
        self.variance.add_css_class("dim")
        detail.append(self.variance)

        buttons = Gtk.Box(spacing=8)
        self.match_button = Gtk.Button(label="Match")
        self.match_button.connect("clicked", self._on_match)
        buttons.append(self.match_button)
        self.reject_button = Gtk.Button(label="Reject candidate")
        self.reject_button.connect("clicked", self._on_reject)
        buttons.append(self.reject_button)
        self.skip_button = Gtk.Button(label="Skip scheduled occurrence")
        self.skip_button.connect("clicked", self._on_skip)
        buttons.append(self.skip_button)
        self.fsa_button = Gtk.Button(label="Attach to FSA claim…")
        self.fsa_button.connect("clicked", self._on_fsa_attach)
        buttons.append(self.fsa_button)
        self.unexpected_button = Gtk.Button(label="Mark unexpected")
        self.unexpected_button.connect("clicked", self._on_unexpected)
        buttons.append(self.unexpected_button)
        detail.append(buttons)
        for key, button in (
            ("match", self.match_button),
            ("reject", self.reject_button),
            ("skip", self.skip_button),
            ("fsa", self.fsa_button),
            ("unexpected", self.unexpected_button),
        ):
            button.set_tooltip_text(REVIEW_ACTION_HELP[key])
        # The selected action's explanation, so it can be read without hovering.
        self.action_help = Gtk.Label(xalign=0, wrap=True)
        self.action_help.add_css_class("dim")
        detail.append(self.action_help)
        self._set_action_sensitivity()

    def _clear_list(self, widget: Gtk.ListBox) -> None:
        while (row := widget.get_row_at_index(0)) is not None:
            widget.remove(row)

    def refresh(self) -> None:
        if self.db is None:
            return
        selected = self._transaction_handle
        self._clear_list(self.actual_list)
        transactions = book_server(self.db).review_queue()
        selected_row = None
        for transaction in transactions:
            row = Gtk.ListBoxRow()
            row.transaction_handle = transaction.handle
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.set_margin_top(6)
            box.set_margin_bottom(6)
            box.set_margin_start(8)
            box.set_margin_end(8)
            description = Gtk.Label(
                label=f"{transaction.date.isoformat()}  {transaction.description}",
                xalign=0,
            )
            description.set_ellipsize(Pango.EllipsizeMode.END)
            box.append(description)
            amount_label = Gtk.Label(label=transaction.amount.format(), xalign=0)
            amount_label.add_css_class("numeric")
            amount_label.add_css_class("dim")
            box.append(amount_label)
            row.set_child(box)
            self.actual_list.append(row)
            if transaction.handle == selected:
                selected_row = row

        if selected_row is None and transactions:
            selected_row = self.actual_list.get_row_at_index(0)
        if selected_row is not None:
            self.actual_list.select_row(selected_row)
        else:
            self._transaction_handle = None
            self._candidate_key = None
            self.actual_summary.set_text("No unresolved actual transactions.")
            self._clear_list(self.candidate_list)
            self.variance.set_text("")
            self._set_action_sensitivity()

    def _on_actual_selected(self, _listbox, row) -> None:
        self._transaction_handle = getattr(row, "transaction_handle", None) if row else None
        self._candidate_key = None
        self._refresh_candidates()

    def _refresh_candidates(self) -> None:
        self._clear_list(self.candidate_list)
        self._detail = None
        if self.db is None or self._transaction_handle is None:
            self.actual_summary.set_text("")
            self.variance.set_text("")
            self._set_action_sensitivity()
            return
        try:
            detail = book_server(self.db).review_detail(self._transaction_handle)
        except ApiError:
            return
        self._detail = detail
        actual = detail.actual
        self.actual_summary.set_text(
            f"{actual.date.isoformat()} · {actual.description}\n"
            f"Actual amount: {actual.amount.format()}"
        )
        hint = detail.fsa_hint
        self.fsa_hint.set_text(hint or "")
        self.fsa_hint.set_visible(hint is not None)
        for explained in detail.candidates:
            candidate = explained.candidate
            row = Gtk.ListBoxRow()
            row.candidate = candidate
            event = candidate.event
            text = (
                f"{event.planned_date.isoformat()}  {event.description}\n"
                f"{explained.label} · expected {event.expected_amount.format()}\n"
                + "\n".join(f"• {reason}" for reason in explained.reasons)
            )
            label = Gtk.Label(label=text, xalign=0, wrap=True)
            label.set_margin_top(6)
            label.set_margin_bottom(6)
            label.set_margin_start(8)
            label.set_margin_end(8)
            row.set_child(label)
            self.candidate_list.append(row)
        first = self.candidate_list.get_row_at_index(0)
        if first is not None:
            self.candidate_list.select_row(first)
        else:
            self.variance.set_text(detail.no_candidate_reason or "")
            self._set_action_sensitivity()

    def _on_candidate_selected(self, _listbox, row) -> None:
        candidate = getattr(row, "candidate", None) if row else None
        self._candidate_key = candidate.event.key if candidate is not None else None
        if candidate is None:
            self.variance.set_text("")
        else:
            signed_days = 0
            amount_variance = Money(0)
            if self._detail is not None:
                actual = self._detail.actual
                signed_days = (actual.date - candidate.event.planned_date).days
                amount_variance = actual.amount - candidate.event.expected_amount
            amount_text = amount_variance.format()
            if amount_variance > 0:
                amount_text = f"+{amount_text}"
            self.variance.set_text(
                f"If matched: amount variance {amount_text} · date variance {signed_days:+d} day(s)"
            )
        self._set_action_sensitivity()

    def _set_action_sensitivity(self) -> None:
        has_actual = self._transaction_handle is not None
        has_candidate = self._candidate_key is not None
        self.match_button.set_sensitive(has_actual and has_candidate)
        self.reject_button.set_sensitive(has_actual and has_candidate)
        self.skip_button.set_sensitive(has_candidate)
        has_fsa = has_actual and self._detail is not None and bool(self._detail.roles)
        self.fsa_button.set_sensitive(has_fsa)
        self.unexpected_button.set_sensitive(has_actual)
        help_keys: tuple[str, ...]
        if has_actual and has_candidate:
            help_keys = ("match", "reject", "skip", "unexpected")
        elif has_actual:
            help_keys = ("unexpected",)
        else:
            help_keys = ()
        if has_fsa:
            help_keys = (*help_keys, "fsa")
        self.action_help.set_text("\n".join(REVIEW_ACTION_HELP[key] for key in help_keys))

    def _on_match(self, _button) -> None:
        if self.db is None or self._transaction_handle is None or self._candidate_key is None:
            return
        request = ReviewOccurrence(self._transaction_handle, self._candidate_key)
        try:
            book_server(self.db).match_review(request)
        except ApiError as error:
            self.variance.set_text(str(error))
            return
        self._transaction_handle = None
        self.refresh()

    def _on_reject(self, _button) -> None:
        if self.db is None or self._transaction_handle is None or self._candidate_key is None:
            return
        request = ReviewOccurrence(self._transaction_handle, self._candidate_key)
        try:
            book_server(self.db).reject_review(request)
        except ApiError as error:
            self.variance.set_text(str(error))
            return
        self._candidate_key = None
        self._refresh_candidates()

    def _on_skip(self, _button) -> None:
        if self.db is None or self._transaction_handle is None or self._candidate_key is None:
            return
        request = ReviewOccurrence(self._transaction_handle, self._candidate_key)
        try:
            book_server(self.db).skip_review(request)
        except ApiError as error:
            self.variance.set_text(str(error))
            return
        self._candidate_key = None
        self._refresh_candidates()

    def _on_fsa_attach(self, _button) -> None:
        if self.db is None or self._detail is None:
            return
        transaction = self._detail.actual
        claims = self._detail.claims
        roles = [
            (item.role, item.split, item.account_name, list(item.years))
            for item in self._detail.roles
        ]
        if not claims or not roles:
            return
        dialog = BoundedWindow(
            title="Attach to FSA claim", transient_for=self.get_root(), modal=True
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(12)
        dialog.set_child(box)
        claim_pick = bounded_dropdown([f"{item.label} — {item.reason}" for item in claims])
        role_pick = bounded_dropdown(
            [f"{claim_role_label(role)} · {account}" for role, _split, account, _years in roles]
        )
        year_pick = bounded_dropdown(
            ["Auto funding year"]
            + sorted(
                {year.isoformat() for _role, _split, _account, years in roles for year in years}
            )
        )
        rows = (("Claim", claim_pick), ("As", role_pick), ("Funding year", year_pick))
        for label, widget in rows:
            row = Gtk.Box(spacing=8)
            row.append(Gtk.Label(label=label, xalign=0))
            row.append(widget)
            box.append(row)
        status = Gtk.Label(xalign=0, wrap=True)
        box.append(status)
        actions = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: dialog.close())
        actions.append(cancel)
        attach = Gtk.Button(label="Attach")
        attach.add_css_class("suggested-action")
        actions.append(attach)
        box.append(actions)

        def do_attach(_button) -> None:
            if self.db is None:
                return
            claim = claims[claim_pick.get_selected()].claim
            role, split, _account, _years = roles[role_pick.get_selected()]
            selected_year = year_pick.get_selected()
            year = None
            if selected_year > 0:
                model = year_pick.get_model()
                item = model.get_string(selected_year)
                from datetime import date as _date

                year = _date.fromisoformat(item)
            request = ReviewClaimAttachment(transaction.handle, claim, role, split, year)
            try:
                book_server(self.db).attach_review_claim(request)
            except ApiError as error:
                status.set_text(str(error))
                return
            dialog.close()
            self.refresh()

        def apply_suggested_role(*_args) -> None:
            selected = claim_pick.get_selected()
            if selected >= len(claims):
                return
            suggestion = claims[selected]
            for index, (role, split, _account, _years) in enumerate(roles):
                if role == suggestion.role and split == suggestion.split:
                    role_pick.set_selected(index)
                    break

        claim_pick.connect("notify::selected", apply_suggested_role)
        apply_suggested_role()
        attach.connect("clicked", do_attach)
        dialog.present()

    def _on_unexpected(self, _button) -> None:
        if self.db is None or self._transaction_handle is None:
            return
        try:
            book_server(self.db).mark_review_unexpected(self._transaction_handle)
        except ApiError as error:
            self.variance.set_text(str(error))
            return
        self._transaction_handle = None
        self.refresh()
