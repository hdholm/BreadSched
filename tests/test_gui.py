"""Tests that build real widgets against a live GTK 4 runtime.

Skipped automatically where GTK is unavailable, so the suite still passes on a
headless build machine — but where GTK *is* present these are the only tests that
can catch the failures that matter most in this layer: a relative import that
resolves to nothing, a GTK method that was renamed, a cell factory handed a
wrapper object it did not expect. None of those are visible to a compile check.

The application is registered rather than run, so widgets are constructed and
bound without entering a main loop and no test can hang waiting for one.
"""

from __future__ import annotations

import gc
import importlib
import itertools
import threading
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

# Import through the project's own entry point rather than reaching for
# gi.repository here: that is where versions are pinned and where PyGObject's
# import-time noise is suppressed. A test that imports gi directly reintroduces
# both problems for the whole session, since the first import is the one that counts.
try:
    gi_setup = importlib.import_module("breadsched.gui.gi_setup")
except (ImportError, ValueError) as exc:
    # PyGObject can be installed while the GTK 4 typelib is absent. In that case
    # gi.require_version raises ValueError rather than ImportError; it is still an
    # unavailable GTK runtime, not a test-collection failure.
    pytest.skip(f"GTK 4 unavailable: {exc}", allow_module_level=True)
Gdk, GLib, Gtk = gi_setup.Gdk, gi_setup.GLib, gi_setup.Gtk

try:
    # Gtk.init_check() reports True even with no display, so it cannot be trusted
    # as a probe. Gtk.init() raises RuntimeError in that case, and a real display
    # connection is what these tests actually need.
    Gtk.init()
    _DISPLAY_OK = Gdk.Display.get_default() is not None
except RuntimeError:  # pragma: no cover - machine dependent
    _DISPLAY_OK = False

pytestmark = [
    pytest.mark.gui,
    pytest.mark.skipif(not _DISPLAY_OK, reason="no GTK 4 runtime or display"),
]

from breadsched import APP_ID  # noqa: E402
from breadsched.gen.db.sqlite import DbSQLite  # noqa: E402
from breadsched.gen.lib import (  # noqa: E402
    Account,
    AccountType,
    Money,
    PeriodType,
    PlanningFlowKind,
    Recurrence,
    ScheduledMonthAmount,
    ScheduledSplit,
    ScheduledTransaction,
    Transaction,
)
from breadsched.gui.app import BreadSchedApplication  # noqa: E402
from breadsched.gui.viewmanager import CATEGORIES, ViewManager  # noqa: E402
from breadsched.gui.views._base import unwrap  # noqa: E402

CATEGORY_KEYS = [key for key, _label, _icon in CATEGORIES]


_APP_IDS = itertools.count()


def _projection(window):
    """Return the Projection view after its worker has delivered the result."""
    view = window._views["projection"]
    assert view.wait_for_background()
    return view


@pytest.fixture
def app(tmp_path):
    """A registered application with an identity of its own.

    Every test builds a new application in the same process. Reusing one id makes
    the second register() fail with "an object is already exported" wherever a
    session bus exists — which is a developer's machine, not a CI runner, so the
    failure only shows up where it is least expected.
    """
    identifier = f"{APP_ID}.Test{next(_APP_IDS)}"
    application = BreadSchedApplication(
        application_id=identifier,
        unique=False,
        settings_directory=tmp_path / "config",
    )
    application.register()
    application.do_startup()
    yield application
    for window in list(application.get_windows()):
        window.destroy()
    if application.db is not None:
        application.db.close()
    gc.collect()


@pytest.fixture
def window(app):
    # Most GUI tests exercise views and book lifecycle, not the startup due
    # review. A modal due window can iterate the main context and consume idle
    # repaint sources, making those tests order-dependent. DueDialog behavior has
    # dedicated tests below; production windows still prompt by default.
    return ViewManager(app, prompt_due_on_open=False)


@pytest.fixture
def populated_book(tmp_path, gnucash_sqlite_path):
    """A real book on disk with imported data and a schedule."""
    from breadsched.cli.main import main as cli

    path = tmp_path / "gui.breadsched"
    cli(["init", str(path)])
    cli(["import", str(path), gnucash_sqlite_path.path])
    return str(path)


class TestModulesImport:
    def test_every_gui_module_imports_against_real_gtk(self):
        """The check that would have caught a relative import pointing nowhere."""
        import importlib
        import pkgutil

        import breadsched.gui

        failures = {}
        for module in pkgutil.walk_packages(breadsched.gui.__path__, "breadsched.gui."):
            try:
                importlib.import_module(module.name)
            except Exception as exc:  # noqa: BLE001 - reporting all of them at once
                failures[module.name] = f"{type(exc).__name__}: {exc}"
        assert failures == {}


class TestApplicationIdentity:
    """Two applications in one process must not fight over the session bus."""

    def test_two_applications_can_be_registered_at_once(self, app):
        second = BreadSchedApplication(application_id=f"{APP_ID}.Second", unique=False)
        second.register()
        assert second.get_is_registered()
        assert app.get_is_registered()

    def test_windows_use_the_installed_application_icon(self, app):
        assert Gtk.Window.get_default_icon_name() == APP_ID

    def test_the_default_identity_is_unchanged(self):
        application = BreadSchedApplication()
        assert application.get_application_id() == APP_ID


class TestEmptyWindow:
    def test_opens_with_a_placeholder(self, window):
        assert window.stack.get_visible_child_name() == "empty"

    def test_the_toolbar_has_an_icon_for_every_category(self, window):
        assert list(window.view_buttons) == CATEGORY_KEYS

    def test_selecting_a_category_without_a_book_does_not_crash(self, window):
        window.lookup_action("show-category").activate(GLib.Variant.new_string("dashboard"))
        assert window.stack.get_visible_child_name() == "empty"


class TestDuePromptPolicy:
    def test_production_window_prompts_when_a_book_opens(self, app, populated_book, monkeypatch):
        calls = []
        production_window = ViewManager(app)
        monkeypatch.setattr(production_window, "prompt_for_due", lambda: calls.append(None))
        try:
            app.open_book(populated_book)
            assert calls == []

            # Due review is deliberately deferred by one GLib main-loop turn so
            # the parent window is mapped before its modal child is presented.
            from breadsched.gui.gi_setup import GLib

            context = GLib.MainContext.default()
            while not calls and context.pending():
                context.iteration(False)
            assert calls == [None]
        finally:
            production_window.destroy()

    def test_prompt_can_be_suppressed_for_test_or_embedded_windows(
        self, app, populated_book, monkeypatch
    ):
        calls = []
        quiet_window = ViewManager(app, prompt_due_on_open=False)
        monkeypatch.setattr(quiet_window, "prompt_for_due", lambda: calls.append(None))
        try:
            app.open_book(populated_book)
            assert calls == []
        finally:
            quiet_window.destroy()


class TestOpeningABook:
    def test_opening_leaves_the_placeholder_behind(self, app, window, populated_book):
        app.open_book(populated_book)
        # Whatever is first in CATEGORIES is what a book opens on.
        assert window.stack.get_visible_child_name() == CATEGORIES[0][0]

    def test_the_header_reports_the_contents(self, app, window, populated_book):
        app.open_book(populated_book)
        accounts, transactions = window.status.get_text().split("\n")
        assert accounts.endswith(" accounts") and transactions.endswith(" transactions")
        # Stacked right after the view icons, not pushed to the far end.
        last_view = window.view_buttons[CATEGORIES[-1][0]]
        assert last_view.get_next_sibling() is window.status
        assert window.status.get_hexpand() is False

    def test_the_title_names_the_file(self, app, window, populated_book):
        app.open_book(populated_book)
        assert "gui.breadsched" in window.get_title()

    def test_a_new_empty_book_also_opens(self, app, window, tmp_path):
        """The reported failure: New Book wrote the file but never displayed it."""
        from breadsched.cli.main import main as cli

        path = tmp_path / "fresh.breadsched"
        cli(["init", str(path)])
        app.open_book(str(path))
        assert window.stack.get_visible_child_name() == CATEGORIES[0][0]

    def test_opening_a_second_book_switches_cleanly(self, app, window, populated_book, tmp_path):
        from breadsched.cli.main import main as cli

        app.open_book(populated_book)
        second = tmp_path / "second.breadsched"
        cli(["init", str(second)])
        app.open_book(str(second))
        assert "second.breadsched" in window.get_title()
        assert window.stack.get_visible_child_name() == CATEGORIES[0][0]

    def test_reopening_the_same_category_still_repaints(self, app, window, populated_book):
        """Row 0 is already selected the second time, so selection cannot be relied on."""
        app.open_book(populated_book)
        window.show_category("plan")
        app.open_book(populated_book)
        # Whatever is first in CATEGORIES is what a book opens on.
        assert window.stack.get_visible_child_name() == CATEGORIES[0][0]


class TestEveryViewBuilds:
    @pytest.mark.parametrize("category", CATEGORY_KEYS)
    def test_view_builds_and_shows(self, app, window, populated_book, category):
        app.open_book(populated_book)
        window.show_category(category)
        assert window.stack.get_visible_child_name() == category

    @pytest.mark.parametrize("category", CATEGORY_KEYS)
    def test_view_survives_an_empty_book(self, app, window, tmp_path, category):
        from breadsched.cli.main import main as cli

        path = tmp_path / "empty.breadsched"
        cli(["init", str(path)])
        app.open_book(str(path))
        window.show_category(category)
        assert window.stack.get_visible_child_name() == category

    @pytest.mark.parametrize("category", CATEGORY_KEYS)
    def test_switching_back_and_forth_is_safe(self, app, window, populated_book, category):
        app.open_book(populated_book)
        for _ in range(3):
            window.show_category("accounts")
            window.show_category(category)
        assert window.stack.get_visible_child_name() == category


class TestAccountTree:
    def test_the_tree_is_populated(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("accounts")
        view = window._views["accounts"]
        model = view.column_view.get_model()
        assert model.get_n_items() > 0

    def test_summary_cards_are_rendered(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("accounts")
        view = window._views["accounts"]
        assert view.summary.get_first_child() is not None

    def test_opening_a_register_from_the_tree(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("accounts")
        handle = app.db.get_account_by_name("Assets:Checking Account").handle
        window.open_register(handle)
        assert window.stack.get_visible_child_name() == "register"
        assert window._views["register"].account_handle == handle

    def test_double_clicking_an_account_opens_its_register(self, app, window, populated_book):
        """The register is the thing opened dozens of times a day, so it gets the
        gesture; editing has its own button."""
        app.open_book(populated_book)
        window.show_category("accounts")
        view = window._views["accounts"]
        model = view.column_view.get_model()

        first = unwrap(model.get_item(0))
        was_expanded = model.get_item(0).get_expanded()
        view._on_activated(view.column_view, 0)

        if first.placeholder:
            # A placeholder holds no entries of its own; it expands instead.
            assert model.get_item(0).get_expanded() is not was_expanded
            assert window.stack.get_visible_child_name() == "accounts"
        else:
            assert window.stack.get_visible_child_name() == "register"

    def test_a_leaf_account_opens_its_register(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("accounts")
        view = window._views["accounts"]
        model = view.column_view.get_model()

        for index in range(model.get_n_items()):
            model.get_item(index).set_expanded(True)
        position = next(
            (
                index
                for index in range(model.get_n_items())
                if not unwrap(model.get_item(index)).placeholder
            ),
            None,
        )
        assert position is not None, "the book has no ordinary accounts"
        account = unwrap(model.get_item(position))

        view._on_activated(view.column_view, position)
        assert window.stack.get_visible_child_name() == "register"
        assert window._views["register"].account_handle == account.handle

    def test_the_edit_button_uses_the_selection(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("accounts")
        view = window._views["accounts"]
        model = view.column_view.get_model()
        for index in range(model.get_n_items()):
            model.get_item(index).set_expanded(True)
        position = next(
            index
            for index in range(model.get_n_items())
            if not unwrap(model.get_item(index)).placeholder
        )
        model.set_selected(position)

        opened = {}
        view.edit_account = lambda account: opened.setdefault("account", account)
        view._on_edit_selected(None)
        assert opened["account"] is unwrap(model.get_item(position))

    def test_the_edit_button_does_nothing_without_a_selection(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("accounts")
        view = window._views["accounts"]
        view.selected_account = lambda: None
        opened = {}
        view.edit_account = lambda account: opened.setdefault("account", account)
        view._on_edit_selected(None)
        assert opened == {}


class TestRegister:
    def test_rows_appear_for_an_account_with_history(self, app, window, populated_book):
        app.open_book(populated_book)
        handle = app.db.get_account_by_name("Assets:Checking Account").handle
        window.open_register(handle)
        view = window._views["register"]
        # Three transactions, then the blank entry row (#158).
        assert view.column_view.get_model().get_n_items() == 4

    def test_column_headings_follow_the_account_type(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("register")
        view = window._views["register"]
        view.show_account(app.db.get_account_by_name("Assets:Checking Account").handle)
        assert view.debit_column.get_title() == "Deposit"
        view.show_account(app.db.get_account_by_name("Credit Card").handle)
        assert view.debit_column.get_title() == "Payment"

    def test_reconciliation_is_available_for_statement_accounts(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("register")
        view = window._views["register"]
        view.show_account(app.db.get_account_by_name("Assets:Checking Account").handle)
        assert view.reconcile_button.get_sensitive() is True

        view.show_account(app.db.get_account_by_name("Expenses:Rent").handle)
        assert view.reconcile_button.get_sensitive() is False

    def test_the_blank_row_posts_one_balanced_two_split_transaction(
        self, app, window, populated_book
    ):
        app.open_book(populated_book)
        checking = app.db.get_account_by_name("Assets:Checking Account")
        rent = app.db.get_account_by_name("Expenses:Rent")
        assert checking is not None and rent is not None
        window.open_register(checking.handle)
        view = window._views["register"]
        blank = view.blank
        assert blank.select_transfer(rent.handle)
        blank.date.set_text("2026-04-01")
        blank.num.set_text("101")
        blank.description.set_text("Blank-row rent")
        blank.decrease.set_text("25.50")

        assert blank.commit() is True

        posted = next(
            transaction
            for transaction in app.db.iter_transactions()
            if transaction.description == "Blank-row rent"
        )
        assert posted.imbalance() == Money(0)
        assert posted.num == "101"
        assert posted.split_for(checking.handle).value == Money("-25.50")
        assert posted.split_for(rent.handle).value == Money("25.50")
        assert blank.description.get_text() == ""
        assert blank.decrease.get_text() == ""
        assert blank.num.get_text() == ""
        # A run of same-day entries needs no retyping.
        assert blank.date.get_text() == "2026-04-01"
        assert "Posted Blank-row rent" in view.entry_status.get_text()

    def test_separate_register_windows_keep_independent_accounts(self, app, window, populated_book):
        app.open_book(populated_book)
        checking = app.db.get_account_by_name("Assets:Checking Account")
        card = app.db.get_account_by_name("Credit Card")
        assert checking is not None and card is not None
        window.open_register(checking.handle)
        main_register = window._views["register"]

        child = window.open_register_window(checking.handle)
        assert child is not None
        assert len(window._register_windows) == 1
        child_register = window._register_windows[0][1]
        child_register.show_account(card.handle)
        main_register.filter_entry.set_text("rent")
        child_register.filter_entry.set_text("payment")

        assert main_register.account_handle == checking.handle
        assert child_register.account_handle == card.handle
        assert main_register.filter_entry.get_text() == "rent"
        assert child_register.filter_entry.get_text() == "payment"
        assert "Credit Card" in child.get_title()

        child.close()
        assert window._register_windows == []


class TestRegisterSelection:
    """Repopulating the account picker must not silently change the account."""

    def test_show_account_survives_the_picker_reset(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("register")
        view = window._views["register"]
        card = app.db.get_account_by_name("Credit Card").handle
        view.show_account(card)
        assert view.account_handle == card

    def test_the_picker_reflects_the_shown_account(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("register")
        view = window._views["register"]
        card = app.db.get_account_by_name("Credit Card").handle
        view.show_account(card)
        assert view._pickable[view.account_picker.get_selected()].handle == card


class TestLiveUpdates:
    def test_posting_a_transaction_repaints_the_open_view(self, app, window, populated_book):
        app.open_book(populated_book)
        handle = app.db.get_account_by_name("Assets:Checking Account").handle
        window.open_register(handle)
        view = window._views["register"]
        before = view.column_view.get_model().get_n_items()

        other = app.db.get_account_by_name("Expenses:Rent").handle
        with app.db.transaction("New rent") as txn:
            app.db.add_transaction(
                Transaction.simple(date(2026, 4, 1), "April rent", other, handle, Money("1800.00")),
                txn,
            )
        assert view.column_view.get_model().get_n_items() == before + 1

    def test_undo_action_follows_availability(self, app, window, populated_book):
        app.open_book(populated_book)
        assert app.actions["undo"].get_enabled() is False
        handle = app.db.get_account_by_name("Assets:Checking Account").handle
        other = app.db.get_account_by_name("Expenses:Rent").handle
        with app.db.transaction("Something") as txn:
            app.db.add_transaction(
                Transaction.simple(date(2026, 4, 1), "x", other, handle, Money("1")), txn
            )
        assert app.actions["undo"].get_enabled() is True


class TestProjectionView:
    def test_projection_uses_a_read_only_worker(self, app, window, populated_book, monkeypatch):
        from breadsched.gen.engine import projection as engine

        app.open_book(populated_book)
        window.show_category("projection")
        view = _projection(window)
        observed = []
        original = engine.project

        def inspect_worker(db, scenario, progress=None):
            observed.append((threading.get_ident(), db.readonly))
            return original(db, scenario, progress)

        monkeypatch.setattr(engine, "project", inspect_worker)
        view.recompute()
        assert view.wait_for_background()

        assert len(observed) == 1
        assert observed[0][1] is True
        assert observed[0][0] != threading.get_ident()

    def test_collect_preserves_account_rates_without_sharing_the_original_map(
        self, app, window, populated_book
    ):
        from breadsched.gen.lib import Account, AccountType, Rate

        app.open_book(populated_book)
        root = app.db.root_account()
        account = Account(
            name="Generic investment", atype=AccountType.INVESTMENT, parent=root.handle
        )
        with app.db.transaction("Add generic projection account") as txn:
            app.db.add_account(account, txn)
        window.show_category("projection")
        view = _projection(window)
        original = view.scenario.assumptions
        original.per_account[account.handle] = Rate("0.0375")

        collected = view._collect().assumptions

        assert collected.per_account == {account.handle: Rate("0.0375")}
        assert collected.per_account is not original.per_account
        assert isinstance(collected.per_account[account.handle], Rate)

    def test_the_chart_receives_series(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("projection")
        view = _projection(window)
        assert len(view.chart.series) >= 3
        assert len(view.chart.series[0].values) == view.scenario.years * 12

    def test_changing_an_assumption_recomputes(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("projection")
        view = _projection(window)
        before = list(view.chart.series[2].values)
        # This fixture imports a monthly rent schedule but no scheduled income.
        # Scheduled-event projections therefore respond to expense inflation;
        # income growth is intentionally inert until an income schedule exists.
        view._scales["expense_inflation"].set_value(0.10)
        assert view.wait_for_background()
        assert view.chart.series[2].values != before

    def test_hidden_projection_is_only_invalidated_until_viewed(
        self, app, window, populated_book, monkeypatch
    ):
        app.open_book(populated_book)
        window.show_category("projection")
        view = _projection(window)
        window.show_category("register")

        recomputes = []
        monkeypatch.setattr(view, "recompute", lambda: recomputes.append(None))

        checking = app.db.get_account_by_name("Assets:Checking Account").handle
        rent = app.db.get_account_by_name("Expenses:Rent").handle
        with app.db.transaction("new actual") as txn:
            app.db.add_transaction(
                Transaction.simple(date(2026, 5, 1), "May rent", rent, checking, Money("1800")),
                txn,
            )

        assert recomputes == []
        assert view._projection_dirty is True

        window.show_category("projection")
        assert recomputes == [None]

    def test_the_chart_draws_without_error(self, app, window, populated_book):
        """Exercise the Cairo draw path, which no other test touches."""
        import cairo

        app.open_book(populated_book)
        window.show_category("projection")
        view = _projection(window)
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 800, 400)
        view.chart._draw(view.chart, cairo.Context(surface), 800, 400)

    def test_projection_month_explanation_is_available(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("projection")
        view = _projection(window)

        assert view._result is not None
        assert view.explain_button.get_sensitive() is True
        assert "events" in view.explain_button.get_tooltip_text()

    def test_many_projection_notes_are_separated_and_height_bounded(
        self, app, window, populated_book
    ):
        app.open_book(populated_book)
        window.show_category("projection")
        view = _projection(window)
        assert view._result is not None
        view._result.warnings = [f"Projection note {index}" for index in range(30)]

        view._render(view._result)

        assert view.warning_scroller.get_visible() is True
        assert view.warning_scroller.get_max_content_height() == 180
        assert view.warning_scroller.get_propagate_natural_height() is True
        assert "• Projection note 0\n\n• Projection note 1" in view.warning_label.get_text()


class TestDialogs:
    def test_book_verification_runs_read_only_in_the_background(self, app, window, populated_book):
        from breadsched.gui.dialogs.verification_dialog import VerificationDialog

        app.open_book(populated_book)
        dialog = VerificationDialog(window, populated_book)
        assert dialog.wait_for_background()
        assert dialog.heading.get_text() == "No problems found"
        assert "relationships are clean" in dialog.details.get_text()

    def test_the_transaction_dialog_starts_with_two_splits(self, app, window, populated_book):
        from breadsched.gui.dialogs.transaction_dialog import TransactionDialog

        app.open_book(populated_book)
        handle = app.db.get_account_by_name("Assets:Checking Account").handle
        dialog = TransactionDialog(window, app.db, default_account=handle)
        assert len(dialog.splits) == 2
        assert dialog.save_button.get_sensitive() is False

    def test_one_amount_is_enough_for_a_two_split_entry(self, app, window, populated_book):
        """The common case: pick two accounts, type one number."""
        from breadsched.gui.dialogs.transaction_dialog import TransactionDialog

        app.open_book(populated_book)
        dialog = TransactionDialog(window, app.db)
        dialog.splits[0].amount.set_text("125.00")
        assert dialog.save_button.get_sensitive() is True
        assert "blank split will be set to" in dialog.status.get_text()

    def test_transaction_amount_accepts_comma_decimal_input(self, app, window, populated_book):
        from breadsched.gen.lib import Money
        from breadsched.gui.dialogs.transaction_dialog import TransactionDialog

        app.open_book(populated_book)
        dialog = TransactionDialog(window, app.db)
        dialog.splits[0].amount.set_text("45,67")

        assert dialog.splits[0].value() == Money("45.67")
        assert dialog.save_button.get_sensitive() is True

    def test_the_transaction_dialog_posts(self, app, window, populated_book):
        from breadsched.gen.lib import Money
        from breadsched.gui.dialogs.transaction_dialog import TransactionDialog

        app.open_book(populated_book)
        dialog = TransactionDialog(window, app.db)
        dialog.description_entry.set_text("Test entry")
        dialog.splits[0].amount.set_text("42.00")
        before = app.db.summary()["txn"]
        dialog._on_save(None)

        assert app.db.summary()["txn"] == before + 1
        posted = next(t for t in app.db.iter_transactions() if t.description == "Test entry")
        assert posted.is_balanced()
        first_account = dialog.splits[0].account_handle
        assert posted.value_for(first_account) == Money("42.00")

    def test_the_import_dialog_preselects_the_last_successful_source(
        self, app, window, populated_book, gnucash_sqlite_path
    ):
        from breadsched.gui.dialogs.import_dialog import ImportDialog

        app.open_book(populated_book)
        dialog = ImportDialog(window, app.db)
        assert dialog.path == str(Path(gnucash_sqlite_path.path).resolve())
        assert dialog.import_button.get_sensitive() is True

    def test_hidden_accounts_are_only_offered_when_an_edited_split_uses_them(
        self, app, window, populated_book
    ):
        from breadsched.gen.lib import Account, AccountType, Split
        from breadsched.gui.dialogs.transaction_dialog import TransactionDialog

        app.open_book(populated_book)
        root = app.db.root_account()
        visible = app.db.get_account_by_name("Assets:Checking Account")
        assert root is not None and visible is not None
        hidden = Account(name="Archived category", atype=AccountType.EXPENSE, hidden=True)
        hidden.parent = root.handle
        existing = Transaction(post_date=date(2026, 3, 1), description="Archived entry")
        existing.add_split(Split(hidden.handle, Money("10.00")))
        existing.add_split(Split(visible.handle, Money("-10.00")))
        with app.db.transaction("Add hidden-account example") as txn:
            app.db.add_account(hidden, txn)
            app.db.add_transaction(existing, txn)

        fresh = TransactionDialog(window, app.db)
        assert hidden.handle not in {account.handle for account in fresh.accounts}

        editing = TransactionDialog(window, app.db, transaction=existing)
        hidden_index = next(
            index
            for index, account in enumerate(editing.accounts)
            if account.handle == hidden.handle
        )
        assert editing.account_names[hidden_index].endswith(" (hidden)")
        assert editing.splits[0].account_handle == hidden.handle

    def test_transaction_editor_preserves_split_metadata_and_edits_planning_purpose(
        self, app, window, populated_book
    ):
        from breadsched.gen.lib import (
            Account,
            AccountType,
            InvestmentActivityKind,
            PlanningFlowKind,
            ReconcileState,
            Split,
        )
        from breadsched.gui.dialogs.transaction_dialog import TransactionDialog

        app.open_book(populated_book)
        root = app.db.root_account()
        bank = app.db.get_account_by_name("Assets:Checking Account")
        assert root is not None and bank is not None
        holding = Account(
            name="Generic fund",
            atype=AccountType.INVESTMENT,
            parent=root.handle,
        )
        with app.db.transaction("Add investment fixture") as txn:
            app.db.add_account(holding, txn)

        classified = Split(
            holding.handle,
            Money("12.34"),
            quantity=Money("1.2345"),
            memo="Exact holding leg",
            action="Buy",
            reconcile=ReconcileState.CLEARED,
            planning_flow=PlanningFlowKind.RETIREMENT_SAVING,
            investment_activity=InvestmentActivityKind.CONTRIBUTION,
            fsa_year_start=date(2026, 1, 1),
        )
        classified.reconcile_date = date(2026, 2, 1)
        existing = Transaction(post_date=date(2026, 2, 1), description="Invest")
        existing.add_split(classified)
        existing.add_split(Split(bank.handle, Money("-12.34")))

        dialog = TransactionDialog(window, app.db, transaction=existing)
        assert dialog.splits[0].purpose is PlanningFlowKind.RETIREMENT_SAVING
        dialog.splits[0].planning_purpose.set_selected(2)
        rebuilt = dialog.build()
        preserved = rebuilt.split_for(holding.handle)

        assert preserved is not None
        assert preserved.quantity == Money("1.2345")
        assert preserved.action == "Buy"
        assert preserved.reconcile is ReconcileState.CLEARED
        assert preserved.reconcile_date == date(2026, 2, 1)
        assert preserved.planning_flow is PlanningFlowKind.BENEFIT_FUNDING
        assert preserved.investment_activity is InvestmentActivityKind.CONTRIBUTION
        assert preserved.fsa_year_start == date(2026, 1, 1)

    def test_transaction_editor_explains_an_escrow_balance_adjustment(
        self, app, window, populated_book
    ):
        from breadsched.gen.lib import Account, AccountType, Split
        from breadsched.gui.dialogs.transaction_dialog import TransactionDialog

        app.open_book(populated_book)
        root = app.db.root_account()
        assert root is not None
        adjustment = Account(
            name="Balance adjustment",
            atype=AccountType.EQUITY,
            parent=root.handle,
        )
        escrow = Account(name="Property escrow", atype=AccountType.ESCROW, parent=root.handle)
        correction = Transaction(post_date=date(2026, 2, 1), description="Escrow correction")
        correction.add_split(Split(escrow.handle, Money("25")))
        correction.add_split(Split(adjustment.handle, Money("-25")))
        with app.db.transaction("Escrow correction") as txn:
            app.db.add_account(adjustment, txn)
            app.db.add_account(escrow, txn)
            app.db.add_transaction(correction, txn)

        dialog = TransactionDialog(window, app.db, transaction=correction)

        assert dialog.escrow_treatment is not None
        assert "manual balance adjustment" in dialog.escrow_treatment.get_text()


class TestImportDialogState:
    """Selecting a second file must not leave the first file's outcome on screen."""

    @pytest.fixture
    def dialog(self, app, window, populated_book):
        from breadsched.gui.dialogs.import_dialog import ImportDialog

        app.open_book(populated_book)
        return ImportDialog(window, app.db)

    def test_selecting_a_file_detects_its_format(self, dialog, gnucash_sqlite_path):
        dialog.set_source(gnucash_sqlite_path.path)
        assert "SQLite" in dialog.detected_label.get_text()
        assert dialog.import_button.get_sensitive() is True

    def test_qif_exposes_number_and_date_format_choices(self, dialog, tmp_path):
        path = tmp_path / "ambiguous.qif"
        path.write_text("!Type:Bank\nD03/04/2026\nT12,50\nPExample\n^\n")

        dialog.set_source(str(path))

        assert dialog.format_box.get_visible() is True
        assert dialog.number_format.get_visible() is True
        assert dialog.date_format.get_visible() is True
        dialog.number_format.set_selected(2)
        dialog.date_format.set_selected(2)
        assert dialog.number_format.get_selected() == 2
        assert dialog.date_format.get_selected() == 2

    def test_gnucash_source_shows_the_quiet_reimport_note(
        self, dialog, tmp_path, gnucash_sqlite_path
    ):
        dialog.set_source(gnucash_sqlite_path.path)
        assert dialog.gnucash_reimport_note.get_visible() is True
        assert dialog.gnucash_reimport_note.has_css_class("dim")
        assert not dialog.gnucash_reimport_note.has_css_class("negative")

        qif = tmp_path / "statement.qif"
        qif.write_text("!Type:Bank\nD09/01/2026\nT-1.00\nPExample\n^\n")
        dialog.set_source(str(qif))
        assert dialog.gnucash_reimport_note.get_visible() is False

    def test_ofx_exposes_number_but_not_qif_date_choice(self, dialog, tmp_path):
        path = tmp_path / "statement.ofx"
        path.write_text(
            "OFXHEADER:100\nDATA:OFXSGML\n\n<OFX><BANKMSGSRSV1>"
            "<STMTTRNRS><STMTRS><BANKTRANLIST><STMTTRN>"
            "<TRNTYPE>DEBIT<DTPOSTED>20260304<TRNAMT>-12.50"
            "</STMTTRN></BANKTRANLIST></STMTRS></STMTTRNRS>"
            "</BANKMSGSRSV1></OFX>"
        )

        dialog.set_source(str(path))

        assert dialog.format_box.get_visible() is True
        assert dialog.number_format.get_visible() is True
        assert dialog.date_format.get_visible() is False

    def test_a_result_is_cleared_when_another_file_is_chosen(
        self, dialog, gnucash_sqlite_path, gnucash_xml_path
    ):
        dialog.set_source(gnucash_sqlite_path.path)
        dialog._on_import(None)
        assert dialog.wait_for_background()
        assert dialog.result_view.get_text() != ""

        dialog.set_source(gnucash_xml_path.path)
        assert dialog.result_view.get_text() == ""
        assert dialog.import_button.get_label() == "Import"

    def test_an_error_state_is_cleared_when_another_file_is_chosen(
        self, dialog, tmp_path, gnucash_sqlite_path
    ):
        junk = tmp_path / "notes.txt"
        junk.write_text("not a book")
        dialog.set_source(str(junk))
        assert dialog.import_button.get_sensitive() is False
        assert dialog.detected_label.has_css_class("negative")

        dialog.set_source(gnucash_sqlite_path.path)
        assert dialog.import_button.get_sensitive() is True
        assert not dialog.detected_label.has_css_class("negative")
        assert not dialog.result_view.has_css_class("negative")

    def test_an_unrecognised_file_cannot_be_imported(self, dialog, tmp_path):
        junk = tmp_path / "notes.txt"
        junk.write_text("not a book")
        dialog.set_source(str(junk))
        assert dialog.import_button.get_sensitive() is False

    def test_a_successful_import_reports_what_arrived(self, dialog, gnucash_sqlite_path):
        dialog.set_source(gnucash_sqlite_path.path)
        dialog._on_import(None)
        assert dialog.wait_for_background()
        text = dialog.result_view.get_text()
        assert "transactions" in text
        assert "undo step" in text

    def test_the_debug_option_writes_a_log_beside_the_source(self, dialog, gnucash_sqlite_path):
        from pathlib import Path

        dialog.debug_check.set_active(True)
        dialog.set_source(gnucash_sqlite_path.path)
        dialog._on_import(None)
        assert dialog.wait_for_background()
        log = Path(gnucash_sqlite_path.path).with_suffix(".import-log.txt")
        assert log.exists()
        assert "importing GnuCash SQLite book" in log.read_text()
        assert str(log) in dialog.result_view.get_text()

    def test_a_damaged_book_reports_rather_than_failing(self, dialog, tmp_path):
        from gnucash_fixtures import create_book, new_guid, write_transaction

        chart = [
            ("root", "Root Account", "ROOT", None, 0),
            ("bank", "Checking", "BANK", "root", 0),
            ("food", "Groceries", "EXPENSE", "root", 0),
        ]
        book = create_book(
            tmp_path / "damaged.gnucash",
            chart,
            [(date(2026, 1, 5), "Shop", [("food", 1000, 100, ""), ("bank", -1000, 100, "")])],
        )
        import sqlite3

        conn = sqlite3.connect(book.path)
        write_transaction(
            conn,
            new_guid(),
            book.currency,
            date(2026, 2, 2),
            "Lonely",
            [(book.bank, 5000, 100, "")],
        )
        conn.commit()
        conn.close()

        dialog.set_source(book.path)
        dialog._on_import(None)
        assert dialog.wait_for_background()
        text = dialog.result_view.get_text()
        assert "Import failed" not in text
        assert "Lonely" in text

    def test_the_scenario_dialog_saves(self, app, window, populated_book):
        from breadsched.gen.lib import Scenario
        from breadsched.gui.dialogs.scenario_dialog import SaveScenarioDialog

        app.open_book(populated_book)
        dialog = SaveScenarioDialog(window, app.db, Scenario(name="Trial", years=7))
        dialog.name_entry.set_text("Trial")
        dialog._on_save(None)
        assert app.db.get_scenario_by_name("Trial").years == 7


class TestImportNoise:
    """PyGObject warns during its own import; that is what gi_setup exists to absorb."""

    def test_a_fresh_interpreter_imports_the_gui_without_warnings(self):
        """Only a first import proves anything, so run one in a subprocess.

        Re-importing in-process would be worse than useless: PyGObject registers a
        GObject type per class, and importing the same module twice re-registers
        those types and corrupts every test that follows.
        """
        import json
        import subprocess
        import sys
        import textwrap

        script = textwrap.dedent(
            """
            import importlib, json, pkgutil, sys, warnings
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                import breadsched.gui
                from breadsched.gui.gi_setup import UPSTREAM_NOISE
                for module in pkgutil.walk_packages(
                    breadsched.gui.__path__, "breadsched.gui."
                ):
                    importlib.import_module(module.name)
            print(json.dumps([
                {"message": str(w.message), "file": str(w.filename)} for w in caught
            ]))
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
        )
        warnings_seen = json.loads(completed.stdout.strip().splitlines()[-1])

        import re

        from breadsched.gui.gi_setup import UPSTREAM_NOISE

        # Two things are failures: noise the guard was supposed to absorb, and any
        # warning raised from this project's own files. New upstream warnings we
        # have not seen before are left alone, so this does not become brittle
        # against every PyGObject release.
        escaped_noise = [
            w for w in warnings_seen if any(re.match(p, w["message"]) for p in UPSTREAM_NOISE)
        ]
        ours = [w for w in warnings_seen if "breadsched" in w["file"]]
        assert escaped_noise == [], "the suppression in gi_setup is not working"
        assert ours == [], "this project's own code emitted a warning on import"

    def test_the_suppression_targets_the_reported_message(self):
        """Guard the pattern itself: a typo would silently stop matching."""
        import re
        import warnings

        from breadsched.gui import gi_setup

        reported = "GLib.unix_signal_add_full is deprecated; use GLibUnix.signal_add_full instead"
        assert any(re.match(p, reported) for p in gi_setup.UPSTREAM_NOISE)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for pattern in gi_setup.UPSTREAM_NOISE:
                warnings.filterwarnings("ignore", message=pattern, category=DeprecationWarning)
            warnings.warn(reported, DeprecationWarning, stacklevel=1)
        assert caught == []

    def test_an_unrelated_gtk_deprecation_is_not_suppressed(self):
        """The filter must stay narrow enough to let real deprecations through."""
        import warnings

        from breadsched.gui import gi_setup

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for pattern in gi_setup.UPSTREAM_NOISE:
                warnings.filterwarnings("ignore", message=pattern, category=DeprecationWarning)
            warnings.warn(
                "Gtk.CssProvider.load_from_data is deprecated",
                DeprecationWarning,
                stacklevel=1,
            )
        assert len(caught) == 1

    def test_every_namespace_used_is_pinned(self):
        """An unpinned namespace loads whatever is installed, with a warning."""
        from breadsched.gui import gi_setup

        for name in ("Gtk", "Gdk", "Pango"):
            assert getattr(gi_setup, name) is not None


class TestMenuBarAndToolbar:
    """A conventional menu bar and icon toolbar, as GnuCash and Gramps present."""

    def test_the_application_supplies_a_menu_bar(self, app):
        menu = app.get_menubar()
        assert menu is not None
        labels = []
        for index in range(menu.get_n_items()):
            value = menu.get_item_attribute_value(index, "label", None)
            labels.append(value.get_string() if value else "")
        assert [label.replace("_", "") for label in labels] == [
            "File",
            "Edit",
            "View",
            "Actions",
            "Help",
        ]

    def test_the_window_shows_it(self, window):
        assert window.get_show_menubar() is True

    def test_help_menu_opens_the_packaged_user_guide(self, app):
        from breadsched.gui.user_guide import UserGuideWindow, read_user_guide

        menu = app.get_menubar()
        actions = _menu_actions(menu)
        assert "app.user-guide" in actions
        assert read_user_guide()

        app.activate_action("user-guide")
        guides = [window for window in app.get_windows() if isinstance(window, UserGuideWindow)]
        assert len(guides) == 1
        assert guides[0].text_view.get_buffer().get_char_count() > 0

        app.activate_action("user-guide")
        assert len([w for w in app.get_windows() if isinstance(w, UserGuideWindow)]) == 1

    def test_the_guide_shows_every_part_and_follows_links_between_them(self, app):
        from breadsched.gui.user_guide import UserGuideWindow

        guide = UserGuideWindow(app, None)
        try:
            buffer = guide.text_view.get_buffer()
            assert guide.part == "overview"
            assert list(guide.part_buttons) == ["overview", "desktop", "web", "cli"]
            guide.part_buttons["cli"].set_active(True)
            assert guide.part == "cli"
            assert buffer.get_text(*buffer.get_bounds(), False).startswith("Command-line guide")

            # A link to another part opens it at that heading.
            guide.part_buttons["desktop"].set_active(True)
            assert "../USER_GUIDE.md" in guide.link_targets()
            target = guide.follow("../USER_GUIDE.md#reimbursable-expenses")
            assert (target.part, target.anchor) == ("overview", "reimbursable-expenses")
            assert guide.part == "overview" and guide.part_buttons["overview"].get_active()
            assert buffer.get_mark("h:reimbursable-expenses") is not None
            guide.follow("guide/web.md#payees")
            assert guide.part == "web"
        finally:
            guide.destroy()

    def test_there_is_a_separate_icon_toolbar(self, window):
        children = []
        child = window.toolbar.get_first_child()
        while child is not None:
            children.append(child)
            child = child.get_next_sibling()
        assert len(children) >= 6

    def test_dashboard_plan_and_projection_print_their_applied_state(
        self, app, window, populated_book, monkeypatch
    ):
        from breadsched.gui import printing

        opened = []
        monkeypatch.setattr(printing, "open_print_preview", opened.append)
        app.open_book(populated_book)

        for key, heading in (
            ("dashboard", "Dashboard"),
            ("plan", "Plan"),
            ("projection", "Projection"),
        ):
            window.show_category(key)
            assert window.print_action.get_enabled() is True
            previews_before = len(opened)
            window.print_action.activate(None)
            assert len(opened) == previews_before + 1
            assert f"<h1>{heading}</h1>" in opened[-1]

        window.show_category("accounts")
        assert window.print_action.get_enabled() is False

    def test_every_menu_action_is_registered(self, app):
        """A menu item pointing at a missing action is silently dead on screen."""
        menu = app.get_menubar()
        missing = []

        def walk(model):
            for index in range(model.get_n_items()):
                target = model.get_item_attribute_value(index, "action", None)
                if target is not None:
                    name = target.get_string()
                    if name.startswith("app.") and not app.has_action(name[4:]):
                        missing.append(name)
                for link in ("submenu", "section"):
                    child = model.get_item_link(index, link)
                    if child is not None:
                        walk(child)

        walk(menu)
        assert missing == []


class TestReplacingABook:
    """Item 1: answering 'replace' in the file chooser must actually replace."""

    def test_an_existing_book_is_cleared_before_recreating(self, app, tmp_path):
        from breadsched.cli.main import main as cli
        from breadsched.gen.db.sqlite import DbSQLite
        from breadsched.gen.lib import Account, AccountType

        path = tmp_path / "book.breadsched"
        cli(["init", str(path)])
        db = DbSQLite()
        db.load(str(path))
        with db.transaction("Add") as txn:
            db.add_account(Account(name="Leftover", atype=AccountType.BANK), txn)
        db.close()

        app._remove_book(path)
        assert not path.exists()

    def test_the_sidecar_files_go_too(self, app, tmp_path):
        """A stale -wal reattaches to the new database and carries old pages in."""
        path = tmp_path / "book.breadsched"
        path.write_text("x")
        for suffix in ("-wal", "-shm", "-journal"):
            (tmp_path / f"book.breadsched{suffix}").write_text("x")

        app._remove_book(path)
        assert list(tmp_path.glob("book.breadsched*")) == []


class TestRegisterSplitDetail:
    """Item 5: splits expand into rows beneath the transaction, not a side pane."""

    @pytest.fixture
    def register(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("register")
        handle = app.db.get_account_by_name("Assets:Checking Account").handle
        view = window._views["register"]
        view.show_account(handle)
        return view

    def test_there_is_no_separate_detail_pane(self, register):
        assert not hasattr(register, "detail_frame")

    def test_a_transaction_row_has_its_splits_as_children(self, register):
        model = register.column_view.get_model().get_model()
        parent = model.get_item(0)
        parent.set_expanded(True)
        transaction = parent.get_item().payload.transaction
        children = parent.get_children()
        assert children is not None
        assert children.get_n_items() == len(transaction.splits)

    def test_expanding_inserts_rows_immediately_below(self, register):
        model = register.column_view.get_model().get_model()
        before = model.get_n_items()
        parent = model.get_item(0)
        splits = len(parent.get_item().payload.transaction.splits)
        parent.set_expanded(True)
        assert model.get_n_items() == before + splits
        # The first child sits directly after its parent.
        assert model.get_item(1).get_depth() == 1

    def test_child_rows_name_their_accounts(self, register):
        model = register.column_view.get_model().get_model()
        parent = model.get_item(0)
        parent.set_expanded(True)
        transaction = parent.get_item().payload.transaction
        names = {
            model.get_item(index + 1).get_item().payload.account_name
            for index in range(len(transaction.splits))
        }
        assert names == {register.db.full_name(s.account) for s in transaction.splits}

    def test_selecting_one_collapses_the_previous(self, register):
        selection = register.column_view.get_model()
        selection.set_selected(0)
        register._on_selection_changed(selection, None)
        model = selection.get_model()
        assert model.get_item(0).get_expanded() is True

        # Select a later top-level row; the first must close again.
        position = next(
            index
            for index in range(model.get_n_items())
            if model.get_item(index).get_depth() == 0 and index != 0
        )
        selection.set_selected(position)
        register._on_selection_changed(selection, None)
        assert model.get_item(0).get_expanded() is False


class TestColumnBehaviour:
    """Item 9 (part): every column resizes and sorts."""

    def _columns(self, view):
        columns = view.column_view.get_columns()
        return [columns.get_item(i) for i in range(columns.get_n_items())]

    def test_register_columns_resize_and_sort(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("register")
        for column in self._columns(window._views["register"]):
            assert column.get_resizable() is True

    def test_account_columns_resize_and_sort(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("accounts")
        columns = self._columns(window._views["accounts"])
        assert [column.get_title() for column in columns] == [
            "Account",
            "Type",
            "Description",
            "Balance",
            "Quote evidence",
        ]
        for column in columns:
            assert column.get_resizable() is True
            assert column.get_sorter() is not None

    def test_scheduled_columns_resize_and_sort(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("scheduled")
        window.show_category("upcoming")
        for name, attribute in (("scheduled", "definitions_view"), ("upcoming", "upcoming_view")):
            columns = getattr(window._views[name], attribute).get_columns()
            assert columns.get_n_items() > 0
            for index in range(columns.get_n_items()):
                assert columns.get_item(index).get_resizable() is True

    def test_quote_evidence_shows_date_and_source_without_age(self, app, window, populated_book):
        """Issue #151: the quote date is enough; no "N days old"."""
        from breadsched.gen.lib import (
            Account,
            AccountType,
            Commodity,
            CommodityPrice,
            Money,
            Split,
            Transaction,
        )

        app.open_book(populated_book)
        db = app.db
        usd = db.get_commodity_by_mnemonic("USD")
        euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
        cash = Account(name="Euro cash", atype=AccountType.BANK, commodity=euro.handle)
        opening = next(a for a in db.iter_accounts() if a.atype is AccountType.EQUITY)
        entry = Transaction(post_date=date(2026, 1, 2), description="Euro opening")
        entry.currency = euro.handle
        entry.splits = [Split(cash.handle, Money(10)), Split(opening.handle, Money(-10))]
        with db.transaction("Euro cash") as txn:
            db.add_commodity(euro, txn)
            db.add_account(cash, txn)
            db.add_transaction(entry, txn)
            db.add_price(
                CommodityPrice(
                    commodity=euro.handle,
                    currency=usd.handle,
                    quote_date=date(2026, 1, 3),
                    value=Money("1.10"),
                    source="bank",
                ),
                txn,
            )
        window.show_category("accounts")
        evidence = window._views["accounts"]._quote_evidence(db.get_account(cash.handle))
        assert evidence == "2026-01-03 · bank"

        # A security quoted only in euros names both quotes it was valued with.
        fund = Commodity(namespace="FUND", mnemonic="EUFND", fullname="Euro fund")
        holding = Account(name="Euro fund", atype=AccountType.INVESTMENT, commodity=fund.handle)
        dollars = next(
            item
            for item in db.iter_accounts()
            if item.atype is AccountType.BANK and item.commodity == usd.handle
        )
        purchase = Transaction(post_date=date(2026, 1, 2), description="Fund purchase")
        purchase.currency = usd.handle
        purchase.splits = [
            Split(holding.handle, Money(100), quantity=Money(2)),
            Split(dollars.handle, Money(-100)),
        ]
        with db.transaction("Euro fund") as txn:
            db.add_commodity(fund, txn)
            db.add_account(holding, txn)
            db.add_transaction(purchase, txn)
            db.add_price(
                CommodityPrice(
                    commodity=fund.handle,
                    currency=euro.handle,
                    quote_date=date(2026, 1, 4),
                    value=Money(60),
                    source="ofx",
                ),
                txn,
            )
        evidence = window._views["accounts"]._quote_evidence(db.get_account(holding.handle))
        assert evidence == "2026-01-04 · ofx; EUR→USD 2026-01-03 · bank"

    def test_amounts_sort_as_numbers_not_text(self):
        from breadsched.gui.views._base import _numeric

        assert _numeric("1,000.00") > _numeric("900.00")
        assert _numeric("(50.00)") < _numeric("0.00")


class TestProjectionRobustness:
    """Item 3: a failed projection must say so, not draw 'nothing to plot'."""

    def test_a_long_projection_is_allowed(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("projection")
        view = _projection(window)
        view.years_spin.set_value(100)
        assert view.wait_for_background()
        assert view.years_spin.get_value() == 100
        assert len(view.chart.series[0].values) == 1200

    def test_a_failure_is_reported_rather_than_left_blank(
        self, app, window, populated_book, monkeypatch
    ):
        from breadsched.gen.engine import projection as engine

        app.open_book(populated_book)
        window.show_category("projection")
        view = _projection(window)

        def explode(*args, **kwargs):
            raise RuntimeError("simulated projection failure")

        monkeypatch.setattr(engine, "project", explode)
        view.recompute()
        assert view.wait_for_background()
        assert "could not be calculated" in view.warning_label.get_text()
        assert "simulated projection failure" in view.warning_label.get_text()


class TestScheduledDetail:
    """Item 4: schedules must show their splits and categories."""

    def test_the_splits_column_names_the_accounts(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("scheduled")
        view = window._views["scheduled"]
        sched = next(iter(app.db.iter_scheduled()))
        summary = view._splits_summary(sched)
        for split in sched.splits:
            assert app.db.full_name(split.account) in summary

    def test_amounts_appear_beside_each_account(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("scheduled")
        view = window._views["scheduled"]
        sched = next(iter(app.db.iter_scheduled()))
        assert "1,800.00" in view._splits_summary(sched)

    def test_a_schedule_without_splits_says_so(self, app, window, populated_book):
        from breadsched.gen.lib import ScheduledTransaction

        app.open_book(populated_book)
        window.show_category("scheduled")
        view = window._views["scheduled"]
        assert view._splits_summary(ScheduledTransaction(name="empty")) == "(no splits)"


class TestScheduleEntry:
    """Item 6: scheduled transactions must be enterable."""

    @pytest.fixture
    def dialog(self, app, window, populated_book):
        from breadsched.gui.dialogs.schedule_dialog import ScheduleDialog

        app.open_book(populated_book)
        return ScheduleDialog(window, app.db)

    def test_it_refuses_an_incomplete_form(self, dialog):
        assert dialog.save_button.get_sensitive() is False

    def test_it_accepts_a_complete_one(self, dialog):
        dialog.name_entry.set_text("Council tax")
        dialog.amount_entry.set_text("180.00")
        assert dialog.save_button.get_sensitive() is True

    def test_saving_creates_a_two_split_schedule(self, dialog, app):
        before = len(list(app.db.iter_scheduled()))
        dialog.name_entry.set_text("Council tax")
        dialog.amount_entry.set_text("180.00")
        dialog._on_save(None)
        schedules = list(app.db.iter_scheduled())
        assert len(schedules) == before + 1
        created = next(s for s in schedules if s.name == "Council tax")
        assert len(created.splits) == 2
        assert created.instantiate(date(2026, 6, 1)).is_balanced()

    def test_an_estimate_is_never_posted(self, dialog):
        dialog.name_entry.set_text("Groceries")
        dialog.amount_entry.set_text("600.00")
        dialog.kind.set_selected(1)
        built = dialog.build()
        assert built.placeholder is True
        assert built.postable is False

    def test_a_commitment_is_postable(self, dialog):
        dialog.name_entry.set_text("Rent")
        dialog.amount_entry.set_text("1800.00")
        dialog.kind.set_selected(0)
        assert dialog.build().postable is True

    def test_fortnightly_is_offered_and_works(self, dialog):
        dialog.name_entry.set_text("Pay")
        dialog.amount_entry.set_text("1500.00")
        dialog.frequency.set_selected(1)
        dialog.start_entry.set_text("2026-01-02")
        built = dialog.build()
        assert built.recurrence.occurrences(date(2026, 2, 1))[:3] == [
            date(2026, 1, 2),
            date(2026, 1, 16),
            date(2026, 1, 30),
        ]

    def test_schedule_can_end_on_a_date(self, dialog):
        dialog.name_entry.set_text("Temporary rent")
        dialog.amount_entry.set_text("1800.00")
        dialog.start_entry.set_text("2026-01-01")
        dialog.ends.set_selected(1)
        dialog.end_entry.set_text("2026-03-31")
        built = dialog.build()
        assert built.recurrence.end == date(2026, 3, 31)
        assert built.recurrence.count is None

    def test_schedule_can_end_after_occurrences(self, dialog):
        dialog.name_entry.set_text("Six payments")
        dialog.amount_entry.set_text("1800.00")
        dialog.start_entry.set_text("2026-01-01")
        dialog.ends.set_selected(2)
        dialog.count_entry.set_text("6")
        built = dialog.build()
        assert built.recurrence.count == 6
        assert built.recurrence.end is None

    def test_existing_simple_schedule_can_be_edited_in_place(self, app, window, populated_book):
        app.open_book(populated_book)
        bank = app.db.get_account_by_name("Assets:Checking Account")
        rent = app.db.get_account_by_name("Expenses:Rent")
        assert bank is not None and rent is not None
        source = ScheduledTransaction(
            name="Rent plan",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
            splits=[
                ScheduledSplit(rent.handle, Money("1800.00")),
                ScheduledSplit(bank.handle, Money("-1800.00")),
            ],
            growth_policy="income",
        )
        with app.db.transaction("add editable schedule") as txn:
            app.db.add_scheduled(source, txn)
        from breadsched.gui.dialogs.schedule_dialog import ScheduleDialog

        dialog = ScheduleDialog(window, app.db, source=source)
        assert dialog.growth_policy.get_selected() == 2
        dialog.name_entry.set_text("Rent revised")
        dialog.amount_entry.set_text("1850.00")
        dialog.growth_policy.set_selected(1)
        dialog._on_save(None)
        edited = app.db.get_scheduled(source.handle)
        assert edited is not None
        assert edited.name == "Rent revised"
        assert edited.amount() == Money("1850.00")
        assert edited.growth_policy.value == "none"
        same_handle = [item for item in app.db.iter_scheduled() if item.handle == source.handle]
        assert len(same_handle) == 1

    def test_historical_estimate_draft_adjusts_all_review_fields(self, app, window, populated_book):
        from breadsched.gui.dialogs.schedule_dialog import ScheduleDialog

        app.open_book(populated_book)
        bank = app.db.get_account_by_name("Assets:Checking Account")
        rent = app.db.get_account_by_name("Expenses:Rent")
        assert bank is not None and rent is not None
        source = ScheduledTransaction(
            name="Estimated rent",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
            splits=[
                ScheduledSplit(rent.handle, Money("1800")),
                ScheduledSplit(bank.handle, Money("-1800")),
            ],
            seasonal_amounts=[ScheduledMonthAmount(1, "1900")],
        )
        source.placeholder = True
        source.estimate_evidence = {"history_start": "2025-01-01", "selected_months": 12}

        dialog = ScheduleDialog(window, app.db, source=source, creating=True)
        dialog.amount_entry.set_text("1850")
        dialog.frequency.set_selected(1)
        dialog.start_entry.set_text("2026-02-06")
        dialog.category_planning_flow.set_selected(3)
        dialog.seasonal_amounts_editor.set_values([(1, "2000"), (7, "1700")])
        built = dialog.build()

        assert built.amount() == Money("1850")
        assert built.recurrence.period is PeriodType.WEEK
        assert built.recurrence.interval == 2
        assert built.recurrence.start == date(2026, 2, 6)
        assert built.splits[0].planning_flow is PlanningFlowKind.DEBT_PRINCIPAL
        assert [(item.month, item.amount) for item in built.seasonal_amounts] == [
            (1, Money("2000")),
            (7, Money("1700")),
        ]
        assert built.estimate_evidence == source.estimate_evidence

    def test_duplicate_dialog_adds_an_independent_definition(self, app, window, populated_book):
        from breadsched.gen.engine import schedule
        from breadsched.gui.dialogs.schedule_dialog import ScheduleDialog

        app.open_book(populated_book)
        source = next(iter(app.db.iter_scheduled()))
        draft = schedule.duplicate_definition(source)
        dialog = ScheduleDialog(window, app.db, source=draft, creating=True)
        dialog.name_entry.set_text("Independent copy")
        dialog._on_save(None)

        assert app.db.get_scheduled(source.handle) is not None
        copied = app.db.get_scheduled(draft.handle)
        assert copied is not None
        assert copied.name == "Independent copy"

    def test_fixed_schedule_with_duplicate_expense_account_is_editable(
        self, app, window, populated_book
    ):
        from breadsched.gui.dialogs.schedule_dialog import ScheduleDialog

        app.open_book(populated_book)
        bank = app.db.get_account_by_name("Assets:Checking Account")
        expense = app.db.get_account_by_name("Expenses:Rent")
        assert bank is not None and expense is not None
        source = ScheduledTransaction(
            name="Shared insurance",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2025, 12, 3)),
            splits=[
                ScheduledSplit(expense.handle, Money("120.00"), memo="component A"),
                ScheduledSplit(bank.handle, Money("-200.00")),
                ScheduledSplit(expense.handle, Money("80.00"), memo="component B"),
            ],
        )
        window.show_category("scheduled")
        view = window._views["scheduled"]
        assert view._editability_reason(source) == ""

        dialog = ScheduleDialog(window, app.db, source=source)
        rebuilt = dialog.build()
        assert [(split.account, split.amount, split.memo) for split in rebuilt.splits] == [
            (expense.handle, Money("120.00"), "component A"),
            (expense.handle, Money("80.00"), "component B"),
            (bank.handle, Money("-200.00"), ""),
        ]

    def test_imported_fixed_custom_recurrence_round_trips(self, app, window, populated_book):
        from breadsched.gui.dialogs.schedule_dialog import ScheduleDialog

        app.open_book(populated_book)
        bank = app.db.get_account_by_name("Assets:Checking Account")
        expense = app.db.get_account_by_name("Expenses:Rent")
        assert bank is not None and expense is not None
        source = ScheduledTransaction(
            name="Periodic service",
            recurrence=Recurrence(
                PeriodType.MONTH,
                interval=2,
                start=date(2025, 1, 31),
                day_of_month=-1,
            ),
            splits=[
                ScheduledSplit(expense.handle, Money("75.00")),
                ScheduledSplit(bank.handle, Money("-75.00")),
            ],
        )
        window.show_category("scheduled")
        view = window._views["scheduled"]
        assert view._editability_reason(source) == ""

        dialog = ScheduleDialog(window, app.db, source=source)
        rebuilt = dialog.build()
        assert rebuilt.recurrence.serialize() == source.recurrence.serialize()

    def test_fixed_planning_transfer_without_income_expense_is_editable(
        self, app, window, populated_book
    ):
        from breadsched.gen.lib import Account, AccountType, PlanningFlowKind
        from breadsched.gui.dialogs.schedule_dialog import ScheduleDialog

        app.open_book(populated_book)
        bank = app.db.get_account_by_name("Assets:Checking Account")
        assert bank is not None
        with app.db.transaction("add planning account") as txn:
            planning_account = Account(
                name="Planning account",
                atype=AccountType.ASSET,
                parent=app.db.root_account().handle,
            )
            app.db.add_account(planning_account, txn)
        source = ScheduledTransaction(
            name="Planned transfer",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
            splits=[
                ScheduledSplit(
                    planning_account.handle,
                    Money("150.00"),
                    planning_flow=PlanningFlowKind.RETIREMENT_SAVING,
                ),
                ScheduledSplit(bank.handle, Money("-150.00")),
            ],
        )
        window.show_category("scheduled")
        view = window._views["scheduled"]
        assert view._editability_reason(source) == ""

        dialog = ScheduleDialog(window, app.db, source=source)
        rebuilt = dialog.build()
        assert [(split.account, split.amount, split.planning_flow) for split in rebuilt.splits] == [
            (
                planning_account.handle,
                Money("150.00"),
                PlanningFlowKind.RETIREMENT_SAVING,
            ),
            (bank.handle, Money("-150.00"), None),
        ]

    def test_fixed_investment_activity_is_editable_as_the_primary_amount(
        self, app, window, populated_book
    ):
        from breadsched.gen.lib import Account, AccountType, InvestmentActivityKind
        from breadsched.gui.dialogs.schedule_dialog import ScheduleDialog

        app.open_book(populated_book)
        bank = app.db.get_account_by_name("Assets:Checking Account")
        root = app.db.root_account()
        assert bank is not None and root is not None
        holding = Account(
            name="Long-term fund",
            atype=AccountType.INVESTMENT,
            parent=root.handle,
        )
        with app.db.transaction("add investment account") as txn:
            app.db.add_account(holding, txn)
        source = ScheduledTransaction(
            name="Invest monthly",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
            splits=[
                ScheduledSplit(
                    holding.handle,
                    Money("200.00"),
                    investment_activity=InvestmentActivityKind.CONTRIBUTION,
                ),
                ScheduledSplit(bank.handle, Money("-200.00")),
            ],
        )

        dialog = ScheduleDialog(window, app.db, source=source)
        rebuilt = dialog.build()

        classified = rebuilt.splits[0]
        assert classified.account == holding.handle
        assert classified.amount == Money("200.00")
        assert classified.investment_activity is InvestmentActivityKind.CONTRIBUTION
        assert rebuilt.instantiate(date(2026, 1, 1)).is_balanced()

    def test_fixed_multiple_planning_purpose_legs_are_editable(self, app, window, populated_book):
        from breadsched.gen.lib import Account, AccountType, PlanningFlowKind
        from breadsched.gui.dialogs.schedule_dialog import ScheduleDialog

        app.open_book(populated_book)
        bank = app.db.get_account_by_name("Assets:Checking Account")
        assert bank is not None
        with app.db.transaction("add planning accounts") as txn:
            first = Account(
                name="Planning destination A",
                atype=AccountType.ASSET,
                parent=app.db.root_account().handle,
            )
            second = Account(
                name="Planning destination B",
                atype=AccountType.ASSET,
                parent=app.db.root_account().handle,
            )
            app.db.add_account(first, txn)
            app.db.add_account(second, txn)
        source = ScheduledTransaction(
            name="Combined planned transfer",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
            splits=[
                ScheduledSplit(
                    first.handle,
                    Money("100.00"),
                    memo="component A",
                    planning_flow=PlanningFlowKind.RETIREMENT_SAVING,
                ),
                ScheduledSplit(
                    second.handle,
                    Money("50.00"),
                    memo="component B",
                    planning_flow=PlanningFlowKind.BENEFIT_FUNDING,
                ),
                ScheduledSplit(bank.handle, Money("-150.00"), memo="funding"),
            ],
        )
        window.show_category("scheduled")
        view = window._views["scheduled"]
        assert view._editability_reason(source) == ""

        dialog = ScheduleDialog(window, app.db, source=source)
        rebuilt = dialog.build()
        assert [
            (split.account, split.amount, split.memo, split.planning_flow)
            for split in rebuilt.splits
        ] == [
            (
                first.handle,
                Money("100.00"),
                "component A",
                PlanningFlowKind.RETIREMENT_SAVING,
            ),
            (
                second.handle,
                Money("50.00"),
                "component B",
                PlanningFlowKind.BENEFIT_FUNDING,
            ),
            (bank.handle, Money("-150.00"), "funding", None),
        ]

    def test_fixed_asset_transfer_without_flow_category_is_editable(
        self, app, window, populated_book
    ):
        from breadsched.gen.lib import Account, AccountType
        from breadsched.gui.dialogs.schedule_dialog import ScheduleDialog

        app.open_book(populated_book)
        bank = app.db.get_account_by_name("Assets:Checking Account")
        assert bank is not None
        with app.db.transaction("add reserve account") as txn:
            reserve = Account(
                name="Reserve",
                atype=AccountType.BANK,
                parent=bank.parent,
            )
            app.db.add_account(reserve, txn)
        source = ScheduledTransaction(
            name="Reserve transfer",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
            splits=[
                ScheduledSplit(bank.handle, Money("-125.00"), memo="source"),
                ScheduledSplit(reserve.handle, Money("125.00"), memo="destination"),
            ],
        )
        window.show_category("scheduled")
        view = window._views["scheduled"]
        assert view._editability_reason(source) == ""

        dialog = ScheduleDialog(window, app.db, source=source)
        rebuilt = dialog.build()
        assert [(split.account, split.amount, split.memo) for split in rebuilt.splits] == [
            (reserve.handle, Money("125.00"), "destination"),
            (bank.handle, Money("-125.00"), "source"),
        ]

    def test_fixed_asset_liability_transfer_is_editable(self, app, window, populated_book):
        from breadsched.gen.lib import Account, AccountType
        from breadsched.gui.dialogs.schedule_dialog import ScheduleDialog

        app.open_book(populated_book)
        bank = app.db.get_account_by_name("Assets:Checking Account")
        assert bank is not None
        with app.db.transaction("add liability account") as txn:
            liability = Account(
                name="Installment liability",
                atype=AccountType.LIABILITY,
                parent=app.db.root_account().handle,
            )
            app.db.add_account(liability, txn)
        source = ScheduledTransaction(
            name="Principal transfer",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
            splits=[
                ScheduledSplit(bank.handle, Money("-90.00"), memo="funding"),
                ScheduledSplit(liability.handle, Money("90.00"), memo="principal"),
            ],
        )
        window.show_category("scheduled")
        view = window._views["scheduled"]
        assert view._editability_reason(source) == ""

        dialog = ScheduleDialog(window, app.db, source=source)
        rebuilt = dialog.build()
        assert [(split.account, split.amount, split.memo) for split in rebuilt.splits] == [
            (liability.handle, Money("90.00"), "principal"),
            (bank.handle, Money("-90.00"), "funding"),
        ]

    def test_fixed_multi_leg_balance_sheet_transfer_is_editable(self, app, window, populated_book):
        from breadsched.gen.lib import Account, AccountType
        from breadsched.gui.dialogs.schedule_dialog import ScheduleDialog

        app.open_book(populated_book)
        bank = app.db.get_account_by_name("Assets:Checking Account")
        assert bank is not None
        with app.db.transaction("add balance-sheet accounts") as txn:
            primary = Account(
                name="Primary liability",
                atype=AccountType.LIABILITY,
                parent=app.db.root_account().handle,
            )
            secondary = Account(
                name="Secondary liability",
                atype=AccountType.LIABILITY,
                parent=app.db.root_account().handle,
            )
            app.db.add_account(primary, txn)
            app.db.add_account(secondary, txn)
        source = ScheduledTransaction(
            name="Balance-sheet transfer",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
            splits=[
                ScheduledSplit(primary.handle, Money("150.00"), memo="primary"),
                ScheduledSplit(secondary.handle, Money("-50.00"), memo="secondary"),
                ScheduledSplit(bank.handle, Money("-100.00"), memo="funding"),
            ],
        )
        window.show_category("scheduled")
        view = window._views["scheduled"]
        assert view._editability_reason(source) == ""

        dialog = ScheduleDialog(window, app.db, source=source)
        rebuilt = dialog.build()
        assert [(split.account, split.amount, split.memo) for split in rebuilt.splits] == [
            (primary.handle, Money("150.00"), "primary"),
            (secondary.handle, Money("-50.00"), "secondary"),
            (bank.handle, Money("-100.00"), "funding"),
        ]

    def test_fixed_multi_leg_transfer_preserves_opposite_direction_extra(
        self, app, window, populated_book
    ):
        from breadsched.gen.lib import Account, AccountType
        from breadsched.gui.dialogs.schedule_dialog import ScheduleDialog

        app.open_book(populated_book)
        bank = app.db.get_account_by_name("Assets:Checking Account")
        assert bank is not None
        with app.db.transaction("add transfer accounts") as txn:
            liability = Account(
                name="Synthetic liability",
                atype=AccountType.LIABILITY,
                parent=app.db.root_account().handle,
            )
            reserve = Account(
                name="Synthetic reserve",
                atype=AccountType.BANK,
                parent=bank.parent,
            )
            app.db.add_account(liability, txn)
            app.db.add_account(reserve, txn)
        source = ScheduledTransaction(
            name="Synthetic multi-leg transfer",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
            splits=[
                ScheduledSplit(liability.handle, Money("200.00"), memo="primary"),
                ScheduledSplit(reserve.handle, Money("-75.00"), memo="extra"),
                ScheduledSplit(bank.handle, Money("-125.00"), memo="funding"),
            ],
        )
        window.show_category("scheduled")
        view = window._views["scheduled"]
        assert view._editability_reason(source) == ""

        dialog = ScheduleDialog(window, app.db, source=source)
        rebuilt = dialog.build()
        assert [(split.account, split.amount, split.memo) for split in rebuilt.splits] == [
            (liability.handle, Money("200.00"), "primary"),
            (reserve.handle, Money("-75.00"), "extra"),
            (bank.handle, Money("-125.00"), "funding"),
        ]

    def test_formula_schedule_metadata_is_editable_without_rewriting_formulas(
        self, app, window, populated_book
    ):
        from breadsched.gui.dialogs.schedule_dialog import ScheduleDialog

        app.open_book(populated_book)
        bank = app.db.get_account_by_name("Assets:Checking Account")
        expense = app.db.get_account_by_name("Expenses:Rent")
        assert bank is not None and expense is not None
        source = ScheduledTransaction(
            name="Synthetic formula schedule",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
            splits=[
                ScheduledSplit(expense.handle, formula="base"),
                ScheduledSplit(bank.handle, formula="-base"),
            ],
        )
        source.variables = {"base": "250"}
        window.show_category("scheduled")
        view = window._views["scheduled"]
        assert view._editability_reason(source) == ""

        dialog = ScheduleDialog(window, app.db, source=source)
        assert dialog.save_button.get_visible() is True
        assert dialog.save_button.get_sensitive() is True
        assert dialog.amount_entry.get_sensitive() is False
        assert dialog.category.get_sensitive() is False
        assert "formula 'base'" in dialog.details.get_text()
        assert "base = 250" in dialog.details.get_text()

        dialog.name_entry.set_text("Updated formula schedule")
        dialog.start_entry.set_text("2026-02-01")
        dialog.auto_check.set_active(True)
        rebuilt = dialog.build()
        assert rebuilt.name == "Updated formula schedule"
        assert rebuilt.recurrence.start == date(2026, 2, 1)
        assert rebuilt.auto_create is True
        assert rebuilt.variables == source.variables
        assert [split.serialize() for split in rebuilt.splits] == [
            split.serialize() for split in source.splits
        ]

    def test_formula_schedule_allows_validated_formula_and_variable_edits(
        self, app, window, populated_book
    ):
        from breadsched.gui.dialogs.schedule_dialog import ScheduleDialog

        app.open_book(populated_book)
        bank = app.db.get_account_by_name("Assets:Checking Account")
        expense = app.db.get_account_by_name("Expenses:Rent")
        assert bank is not None and expense is not None
        source = ScheduledTransaction(
            name="Formula fixture",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
            splits=[
                ScheduledSplit(expense.handle, formula="base"),
                ScheduledSplit(bank.handle, formula="-base"),
            ],
        )
        source.variables = {"base": "100"}

        dialog = ScheduleDialog(window, app.db, source=source)
        assert len(dialog._formula_entries) == 2
        dialog.formula_variables_entry.set_text("base=120; factor=2")
        dialog._formula_entries[0][1].set_text("base * factor")
        dialog._formula_entries[1][1].set_text("-(base * factor)")
        assert dialog.save_button.get_sensitive() is True

        rebuilt = dialog.build()
        assert rebuilt.variables == {"base": "120", "factor": "2"}
        assert [split.formula for split in rebuilt.splits] == [
            "base * factor",
            "-(base * factor)",
        ]
        assert rebuilt.resolved_splits(when=date(2026, 1, 1)) == [
            (expense.handle, Money("240")),
            (bank.handle, Money("-240")),
        ]

        dialog._formula_entries[0][1].set_text("unknown_name + 1")
        assert dialog.save_button.get_sensitive() is False
        assert "formula" in dialog.status.get_text().lower()

    def test_imported_loan_formula_dialog_is_bounded_and_resolves_period(
        self, app, window, populated_book, caplog
    ):
        from breadsched.gui.dialogs.schedule_dialog import ScheduleDialog
        from breadsched.gui.views.scheduled import ScheduleSplitRow, _amount_of

        app.open_book(populated_book)
        bank = app.db.get_account_by_name("Assets:Checking Account")
        expense = app.db.get_account_by_name("Expenses:Rent")
        assert bank is not None and expense is not None
        source = ScheduledTransaction(
            name="Imported formula loan",
            recurrence=Recurrence(
                PeriodType.MONTH,
                start=date(2026, 1, 1),
                count=180,
            ),
            splits=[
                ScheduledSplit(
                    expense.handle,
                    formula="ppmt( .05000 / 12.00 : i : 180.00 : 200,000.00 : 0 : 0 )",
                ),
                ScheduledSplit(
                    expense.handle,
                    formula="ipmt( .05000 / 12.00 : i : 180.00 : 200,000.00 : 0 : 0 )",
                ),
                ScheduledSplit(
                    bank.handle,
                    formula="-(pmt( .05000 / 12.00 : 180.00 : 200,000.00 : 0 : 0 ))",
                ),
            ],
        )

        dialog = ScheduleDialog(window, app.db, source=source)
        assert dialog.formula_variables_entry is not None
        assert dialog.content_scroller.get_vexpand() is True
        assert dialog.button_box.get_parent() == dialog.get_child()
        assert Money(_amount_of(source).replace(",", "")) > 0
        split_row = ScheduleSplitRow(source.splits[0], source, app.db)
        assert "=ppmt(" in split_row.amount_text()
        assert "unusable formula" not in caplog.text

    def test_initial_schedule_selection_enables_view_edit(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("scheduled")
        view = window._views["scheduled"]
        selection = view.definitions_view.get_model()
        assert selection is not None
        assert selection.get_selected_item() is not None
        assert view.edit_button.get_sensitive() is True

    def test_the_scheduled_view_offers_the_dialog(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("scheduled")
        view = window._views["scheduled"]
        assert hasattr(view, "_on_new_clicked")
        assert hasattr(view, "_on_edit_clicked")


class TestScheduledIsSplitInTwo:
    """Items 6, 7, 8: definitions and upcoming are separate; splits expand inline."""

    def test_both_categories_exist(self, window):
        keys = list(window.view_buttons)
        assert "scheduled" in keys and "upcoming" in keys

    def test_the_definitions_view_has_no_due_list(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("scheduled")
        assert not hasattr(window._views["scheduled"], "upcoming_view")

    def test_commitments_and_estimates_have_separate_definition_lists(
        self, app, window, populated_book
    ):
        app.open_book(populated_book)
        window.show_category("scheduled")
        view = window._views["scheduled"]

        commitments = view.definitions_view.get_model().get_model()
        estimates = view.estimates_view.get_model().get_model()
        commitment_schedules = [
            item
            for index in range(commitments.get_n_items())
            if isinstance((item := unwrap(commitments.get_item(index))), ScheduledTransaction)
        ]
        estimate_schedules = [
            item
            for index in range(estimates.get_n_items())
            if isinstance((item := unwrap(estimates.get_item(index))), ScheduledTransaction)
        ]
        assert all(not schedule.placeholder for schedule in commitment_schedules)
        assert all(schedule.placeholder for schedule in estimate_schedules)

    def test_the_upcoming_view_has_no_definitions(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("upcoming")
        assert not hasattr(window._views["upcoming"], "definitions_view")

    def test_a_definition_expands_into_its_splits(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("scheduled")
        view = window._views["scheduled"]
        model = view.definitions_view.get_model().get_model()
        parent = model.get_item(0)
        parent.set_expanded(True)
        sched = parent.get_item().payload
        children = parent.get_children()
        assert children.get_n_items() == len(sched.splits)

    def test_split_rows_name_their_accounts(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("scheduled")
        view = window._views["scheduled"]
        model = view.definitions_view.get_model().get_model()
        parent = model.get_item(0)
        parent.set_expanded(True)
        names = {
            model.get_item(i + 1).get_item().payload.account_name
            for i in range(parent.get_children().get_n_items())
        }
        sched = parent.get_item().payload
        assert names == {app.db.full_name(s.account) for s in sched.splits}

    def test_the_kind_column_distinguishes_commitment_from_estimate(self):
        from breadsched.gen.lib import ScheduledTransaction
        from breadsched.gui.views.scheduled import _kind_of

        commitment = ScheduledTransaction(name="Rent")
        estimate = ScheduledTransaction(name="Groceries")
        estimate.placeholder = True
        assert _kind_of(commitment) == "Commitment"
        assert _kind_of(estimate) == "Estimate"


class TestColumnHiding:
    """Item 2: a menu at the end of the header row hides and shows columns."""

    def test_the_register_offers_a_column_menu(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("register")
        view = window._views["register"]
        found = _find_menu_button(view)
        assert found is not None, "no column menu in the register toolbar"

    def test_hiding_a_column_is_remembered(self, app, window, populated_book, tmp_path):
        from breadsched.gen.utils.settings import Settings
        from breadsched.gui.views._base import column_menu

        app.open_book(populated_book)
        window.show_category("register")
        view = window._views["register"]
        settings = Settings("views", directory=tmp_path)

        column_menu("register", view.column_view, settings)
        columns = view.column_view.get_columns()
        target = columns.get_item(2)
        target.set_visible(False)
        settings.set("columns:register", "hidden", [target.get_title()])
        settings.save()

        reloaded = Settings("views", directory=tmp_path)
        assert target.get_title() in reloaded.get_list("columns:register", "hidden")

    def test_a_hidden_column_is_restored_on_the_next_build(
        self, app, window, populated_book, tmp_path
    ):
        from breadsched.gen.utils.settings import Settings
        from breadsched.gui.views._base import column_menu

        app.open_book(populated_book)
        window.show_category("register")
        view = window._views["register"]
        columns = view.column_view.get_columns()
        title = columns.get_item(2).get_title()

        settings = Settings("views", directory=tmp_path)
        settings.set("columns:register", "hidden", [title])
        settings.save()

        column_menu("register", view.column_view, settings)
        assert columns.get_item(2).get_visible() is False

    def test_the_first_column_cannot_be_hidden(self, app, window, populated_book):
        """It carries the expander; hiding it leaves nothing to read or expand."""
        app.open_book(populated_book)
        window.show_category("register")
        button = _find_menu_button(window._views["register"])
        box = button.get_popover().get_child()
        checks = []
        child = box.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.CheckButton):
                checks.append(child)
            child = child.get_next_sibling()
        assert checks and checks[0].get_sensitive() is False


def _find_menu_button(view):
    stack = [view]
    while stack:
        widget = stack.pop()
        if isinstance(widget, Gtk.MenuButton):
            return widget
        child = widget.get_first_child() if hasattr(widget, "get_first_child") else None
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    return None


class TestEditingFromTheRegister:
    """Editing an existing transaction, which the interface previously could not do."""

    @pytest.fixture
    def register(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("register")
        view = window._views["register"]
        view.show_account(app.db.get_account_by_name("Assets:Checking Account").handle)
        return view

    def _dialog_for(self, register, index=0):
        from breadsched.gui.dialogs.transaction_dialog import TransactionDialog

        transaction = register._rows[index].transaction
        return TransactionDialog(
            register.get_root(), register.db, transaction=transaction
        ), transaction

    def test_the_dialog_opens_on_the_existing_values(self, register):
        dialog, transaction = self._dialog_for(register)
        assert dialog.editing is True
        assert dialog.description_entry.get_text() == transaction.description
        assert dialog.date_entry.get_text() == transaction.post_date.isoformat()
        assert len(dialog.splits) == len(transaction.splits)

    def test_it_opens_balanced_and_saveable(self, register):
        dialog, _ = self._dialog_for(register)
        assert dialog.status.get_text() == "Balanced"
        assert dialog.save_button.get_sensitive() is True

    def test_a_description_edit_is_stored(self, register, app):
        dialog, transaction = self._dialog_for(register)
        dialog.description_entry.set_text("Corrected description")
        dialog._on_save(None)
        assert app.db.get_transaction(transaction.handle).description == ("Corrected description")

    def test_breadsched_notes_are_edited_separately(self, register, app):
        dialog, transaction = self._dialog_for(register)
        dialog.notes_view.get_buffer().set_text("Local planning note")
        dialog._on_save(None)
        saved = app.db.get_transaction(transaction.handle)
        assert saved.notes == "Local planning note"

    def test_an_amount_edit_keeps_the_book_balanced(self, register, app):
        from breadsched.gen.lib import Money

        dialog, transaction = self._dialog_for(register)
        dialog.splits[0].amount.set_text("77.00")
        dialog.splits[1].amount.set_text("-77.00")
        for extra in dialog.splits[2:]:
            extra.amount.set_text("0.00")
        dialog.revalidate()
        dialog._on_save(None)

        edited = app.db.get_transaction(transaction.handle)
        assert edited.is_balanced()
        assert edited.value_for(dialog.splits[0].account_handle) == Money("77.00")

    def test_an_unbalanced_edit_cannot_be_saved(self, register):
        dialog, _ = self._dialog_for(register)
        dialog.splits[0].amount.set_text("1000000.00")
        dialog.revalidate()
        assert dialog.save_button.get_sensitive() is False
        assert "Out of balance" in dialog.status.get_text()

    def test_split_identities_survive_an_edit(self, register, app):
        """Split handles carry reconciliation state; losing them detaches it."""
        dialog, transaction = self._dialog_for(register)
        before = {s.handle for s in transaction.splits}
        dialog.description_entry.set_text("Same splits")
        dialog._on_save(None)
        after = {s.handle for s in app.db.get_transaction(transaction.handle).splits}
        assert after == before

    def test_a_split_can_be_added(self, register, app):
        dialog, transaction = self._dialog_for(register)
        before = len(dialog.splits)
        dialog.add_split()
        assert len(dialog.splits) == before + 1

    def test_the_last_two_splits_cannot_be_removed(self, register):
        dialog, _ = self._dialog_for(register)
        while len(dialog.splits) > 2:
            dialog.remove_split(dialog.splits[-1])
        dialog.remove_split(dialog.splits[-1])
        assert len(dialog.splits) == 2
        assert "at least two splits" in dialog.status.get_text()

    def test_a_transaction_can_be_deleted_from_the_dialog(self, register, app):
        dialog, transaction = self._dialog_for(register)
        before = app.db.summary()["txn"]
        dialog._on_delete(None)
        assert app.db.summary()["txn"] == before - 1
        assert app.db.get_transaction(transaction.handle) is None

    def test_transaction_service_error_uses_the_selected_gettext_catalog(self, register, app):
        from breadsched.gen.services import DeleteTransaction, delete_transaction
        from breadsched.presentation import configure_language

        dialog, transaction = self._dialog_for(register)
        assert delete_transaction(app.db, DeleteTransaction(transaction.handle)).ok
        try:
            configure_language(["es"])
            dialog._on_delete(None)
            assert dialog.status.get_text() == "La transacción ya no existe"
        finally:
            configure_language(["C"])

    def test_the_register_offers_editing_on_activation(self, register):
        assert hasattr(register, "edit_transaction")

    def test_activating_a_split_row_edits_its_parent(self, register, app):
        """The splits are one record; editing them separately would unbalance it."""
        from breadsched.gui.views.register import SplitRow

        model = register.column_view.get_model().get_model()
        parent = model.get_item(0)
        parent.set_expanded(True)
        child = model.get_item(1).get_item().payload
        assert isinstance(child, SplitRow)
        assert child.transaction is register._rows[0].transaction


class TestLastBookIsRemembered:
    """Item 1: opening a book records it for next time."""

    def test_opening_writes_the_setting(self, app, populated_book):
        from breadsched.gen.utils.settings import Settings

        app.open_book(populated_book)
        assert app.settings.get("general", "last_book_path") == str(Path(populated_book).resolve())
        assert Settings("settings", directory=app.settings.directory).get(
            "general", "last_book_path"
        ) == str(Path(populated_book).resolve())

    def test_it_is_written_immediately_not_at_shutdown(self, app, populated_book):
        """A crash should not cost the setting."""
        app.open_book(populated_book)
        assert app.settings.path.exists()

    def test_reopening_restores_it(self, app, populated_book):
        app.open_book(populated_book)
        app.db.close()
        app.db = None
        assert app.reopen_last_book() is True
        assert app.db is not None

    def test_a_book_that_has_gone_is_forgotten(self, app, tmp_path):
        app.settings.set("general", "last_book_path", str(tmp_path / "gone.breadsched"))
        app.settings.save()
        assert app.reopen_last_book() is False
        assert app.settings.get("general", "last_book_path") is None

    def test_activation_reopens_an_existing_remembered_book(self, app, tmp_path):
        from breadsched.cli.main import main as cli

        remembered = tmp_path / "remembered.breadsched"
        assert cli(["init", str(remembered)]) == 0
        app.settings.set("general", "last_book_path", str(remembered))
        app.settings.save()

        app.do_activate()

        assert app.db is not None
        assert app.book_path == str(remembered)

    def test_a_remembered_default_user_book_is_reopened(self, app, tmp_path, monkeypatch):
        from breadsched.cli.main import main as cli
        from breadsched.gui import paths

        default = tmp_path / "BreadSched.breadsched"
        assert cli(["init", str(default)]) == 0
        monkeypatch.setattr(paths, "default_book_path", lambda: default)
        app.settings.set("general", "last_book_path", str(default))
        app.settings.save()

        assert app.reopen_last_book() is True
        assert app.book_path == str(default)


class TestSortingReordersRows:
    """Item 2: clicking a header must move the rows, not just draw an arrow."""

    def test_the_register_model_is_sorted_by_the_view(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("register")
        view = window._views["register"]
        # Transactions are sorted; the blank entry row is appended after (#158).
        flat = view.column_view.get_model().get_model()
        assert isinstance(flat, Gtk.FlattenListModel)
        assert isinstance(flat.get_model().get_item(0), Gtk.SortListModel)

    def test_sorting_a_column_changes_the_order(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("register")
        view = window._views["register"]
        model = view.column_view.get_model()

        def descriptions():
            return [
                unwrap(model.get_item(i)).description
                for i in range(model.get_n_items())
                if isinstance(model.get_item(i), Gtk.TreeListRow)
                and model.get_item(i).get_depth() == 0
            ]

        before = descriptions()
        columns = view.column_view.get_columns()
        description_column = next(
            columns.get_item(i)
            for i in range(columns.get_n_items())
            if columns.get_item(i).get_title() == "Transfer"
        )
        view.column_view.sort_by_column(description_column, Gtk.SortType.ASCENDING)
        ascending = descriptions()
        view.column_view.sort_by_column(description_column, Gtk.SortType.DESCENDING)

        assert ascending == sorted(ascending) or ascending != before
        assert descriptions() == list(reversed(ascending))

    def test_a_tree_keeps_children_with_their_parents(self, app, window, populated_book):
        """Sorting the flattened list would tear splits away from their transaction."""
        app.open_book(populated_book)
        window.show_category("register")
        view = window._views["register"]
        model = view.column_view.get_model()
        model.get_item(0).set_expanded(True)

        rows = [model.get_item(i) for i in range(model.get_n_items())]
        depths = [row.get_depth() for row in rows if isinstance(row, Gtk.TreeListRow)]
        assert len(depths) == len(rows) - 1  # only the blank entry row is not a tree row
        # A depth-1 row may only follow a depth-0 or another depth-1 row.
        for index, depth in enumerate(depths[1:], start=1):
            if depth == 1:
                assert depths[index - 1] in (0, 1)


class TestNavigationHighlight:
    """However a view is reached, its toolbar icon shows as active (#155)."""

    def _active(self, window):
        return [key for key, button in window.view_buttons.items() if button.get_active()]

    def test_the_toolbar_icon_follows_the_view(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("projection")
        window.show_category("accounts")
        assert window.current_category == "accounts"
        assert self._active(window) == ["accounts"]

    def test_jumping_to_a_register_moves_the_highlight(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("accounts")
        handle = app.db.get_account_by_name("Assets:Checking Account").handle
        window.open_register(handle)
        assert self._active(window) == ["register"]

    def test_every_category_highlights_itself(self, app, window, populated_book):
        app.open_book(populated_book)
        for key, _label, _icon in CATEGORIES:
            window.show_category(key)
            assert self._active(window) == [key]

    def test_clicking_an_icon_shows_its_view(self, app, window, populated_book):
        app.open_book(populated_book)
        window.view_buttons["plan"].emit("clicked")
        assert window.stack.get_visible_child_name() == "plan"
        assert self._active(window) == ["plan"]


class TestPlanToolbarIcon:
    """Plan is reached through the sidebar; the toolbar no longer repeats it."""

    def test_plan_is_a_visible_category(self):
        assert any(key == "plan" for key, _label, _icon in CATEGORIES)


class TestDerivedPlanView:
    def test_line_chart_maps_a_click_to_the_nearest_period(self):
        from breadsched.gui.widgets.chart import LineChart, Series

        class Sized(LineChart):
            def get_width(self):
                return 494  # 400 px of plot between the 78/16 px margins

        chart = Sized()
        assert chart.index_at(200) is None
        chart.set_data([Series("Actual", [1.0, 2.0, 3.0])])
        assert [chart.index_at(x) for x in (10, 170, 290, 470, 900)] == [0, 0, 1, 2, 2]

    def test_net_worth_history_dialog_charts_complete_points(
        self, app, window, populated_book, monkeypatch, tmp_path
    ):
        from breadsched.gui import printing
        from breadsched.gui.dialogs.net_worth_history_dialog import NetWorthHistoryDialog
        from breadsched.gui.viewmanager import VIEW_ACTIONS

        assert "net-worth-history" in {item.name for item in VIEW_ACTIONS["dashboard"]}
        app.open_book(populated_book)
        dialog = NetWorthHistoryDialog(window, app.db)
        try:
            history = dialog.history
            assert history is not None and history.points
            complete = [point for point in history.points if point.net_worth is not None]
            assert [len(series.values) for series in dialog.chart.series] == [len(complete)]
            assert dialog.table.get_child_at(0, 1).get_label() == history.points[0].label
            first = history.points[0].net_worth
            assert dialog.table.get_child_at(4, 1).get_label() == (
                "Missing quote" if first is None else first.format()
            )
            last = history.points[-1]
            explain = dialog.table.get_child_at(5, len(history.points))
            assert isinstance(explain, Gtk.Button)
            explain.emit("clicked")
            change = dialog.change
            assert change is not None and change.change == last.change
            rows = len(change.postings) + 5
            assert dialog.change_table.get_child_at(1, rows).get_label() == "Change"
            printed: list[str] = []
            monkeypatch.setattr(printing, "open_print_preview", printed.append)
            dialog._print_change(None)
            assert "Market and exchange-rate changes" in printed[0]
            exported = tmp_path / "change.csv"
            dialog.export_change(str(exported))
            total = exported.read_text(encoding="utf-8").splitlines()[-1].split(",")
            shown = dialog.change_table.get_child_at(4, rows).get_label()
            assert total[1] == "Change"
            assert shown == ("Missing quote" if change.change is None else Money(total[4]).format())
            dialog.grouping.set_selected(2)
            assert dialog.history.period.value == "year"
            assert dialog.change is None
        finally:
            dialog.destroy()

    def test_expense_explorer_builds_from_applied_plan(
        self, app, window, populated_book, monkeypatch
    ):
        from breadsched.gen.services.plan import PlanQuery
        from breadsched.gui.dialogs.expense_explorer_dialog import ExpenseExplorerDialog

        app.open_book(populated_book)
        window.show_category("plan")
        view = window._views["plan"]
        dialog = ExpenseExplorerDialog(
            window,
            app.db,
            PlanQuery(start=view._start_date, end=view._end_date, period=view._grouping()),
        )
        try:
            assert dialog._report.categories
            assert dialog.content.get_first_child() is not None
            assert dialog.period.get_selected() == 0
            # Spending over time: one chart point per period, the selection shaded.
            chart = dialog.spending_chart
            assert [len(series.values) for series in chart.series] == [
                len(dialog._report.totals)
            ] * 2
            assert chart.selected_index == 0
            # Income over time: the same periods, its own chart and table.
            assert dialog._report.income
            income = dialog.income_chart
            assert [len(series.values) for series in income.series] == [
                len(dialog._report.totals)
            ] * 2
            assert income.selected_index == 0
            assert dialog.income_table.get_child_at(1, 1).get_label() == (
                dialog._report.income[0].planned.format()
            )
            # Income detail: the selected period's dated planned and received rows.
            received = [
                dialog.income_detail.get_child_at(0, row).get_label()
                for row in range(1, 40)
                if dialog.income_detail.get_child_at(0, row) is not None
            ]
            assert "Received" in received
            period = dialog._report.income[0]
            dated = [text for text in received if text[:4].isdigit()]
            assert all(period.start.isoformat() <= text <= period.end.isoformat() for text in dated)
            # Print adds the chosen income category's detail for the selected period.
            from breadsched.gui import printing

            printed: list[str] = []
            monkeypatch.setattr(printing, "open_print_preview", printed.append)
            dialog._print(None)
            assert len(printed) == 1 and "Income detail — " in printed[0]
            if len(dialog._report.totals) > 1:
                dialog.period.set_selected(1)
                assert dialog.spending_chart.selected_index == 1
                assert dialog.income_chart.selected_index == 1
            dialog.rollover.set_active(True)
            assert dialog._report.rollover
            dialog.sort.set_selected(3)
            assert dialog.content.get_first_child() is not None
        finally:
            dialog.destroy()

    def test_plan_discloses_foreign_currency_quote_or_exclusion(self, app, window, populated_book):
        from breadsched.gen.engine import valuation
        from breadsched.gen.engine.currency import reporting_currency_handle
        from breadsched.gen.lib import (
            Commodity,
            Money,
            PeriodType,
            Recurrence,
            ScheduledSplit,
            ScheduledTransaction,
        )
        from breadsched.gen.lib.account import AccountClass

        app.open_book(populated_book)
        window.show_category("plan")
        view = window._views["plan"]
        assert not view.currency_note.get_visible()
        accounts = [item for item in app.db.iter_accounts() if not item.placeholder]
        expense = next(item for item in accounts if item.account_class is AccountClass.EXPENSE)
        cash = next(item for item in accounts if item.is_spendable_cash)
        euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
        flat = ScheduledTransaction(
            name="Flat in Lyon",
            recurrence=Recurrence(PeriodType.ONCE, start=view._start_date),
            splits=[
                ScheduledSplit(expense.handle, Money(500)),
                ScheduledSplit(cash.handle, Money(-500)),
            ],
        )
        flat.currency = euro.handle
        with app.db.transaction("EUR schedule") as txn:
            app.db.add_commodity(euro, txn)
            app.db.add_scheduled(flat, txn)

        view.refresh()
        assert view.currency_note.get_visible()
        assert view.currency_note.get_text().startswith("Not included in totals")
        assert view.currency_note.has_css_class("negative")

        valuation.save_currency_quote(
            app.db,
            source_handle=euro.handle,
            target_handle=reporting_currency_handle(app.db),
            quote_date=view._start_date.replace(day=1) - timedelta(days=40),
            value=Money("1.25"),
        )
        view.refresh()
        assert "EUR amounts are converted to" in view.currency_note.get_text()
        assert not view.currency_note.has_css_class("negative")

    def test_detaching_does_not_try_to_read_book_metadata(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("plan")
        view = window._views["plan"]

        view.set_db(None)

        assert view.db is None

    def test_it_derives_periods_from_scheduled_events_and_actuals(
        self, app, window, populated_book
    ):
        app.open_book(populated_book)
        window.show_category("plan")
        view = window._views["plan"]
        assert view._report is not None
        assert view._report.activity.period.value == "month"
        assert view._report.activity.periods
        assert view._report.categories

    def test_plan_routes_unresolved_actuals_to_review(self, app, window, populated_book):
        from breadsched.gen.lib import Money, Transaction

        app.open_book(populated_book)
        checking = app.db.get_account_by_name("Assets:Checking Account")
        rent = app.db.get_account_by_name("Expenses:Rent")
        assert checking is not None and rent is not None
        with app.db.transaction("Unresolved Plan fixture") as txn:
            app.db.add_transaction(
                Transaction.simple(
                    date.today(),
                    "Unmatched rent",
                    rent.handle,
                    checking.handle,
                    Money("10"),
                ),
                txn,
            )

        window.show_category("plan")
        view = window._views["plan"]
        assert view.review_actuals_button.get_sensitive() is True
        assert "actuals to review" in view.summary.get_text()

        view.review_actuals_button.emit("clicked")

        assert window.stack.get_visible_child_name() == "resolution"

    def test_grouping_changes_display_buckets_not_source_data(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("plan")
        view = window._views["plan"]
        monthly = view._report
        monthly_planned = monthly.activity.planned_cash_change
        monthly_actual = monthly.activity.actual_cash_change

        view.period.set_selected(1)
        view.apply_button.emit("clicked")
        quarterly = view._report
        assert quarterly.activity.period.value == "quarter"
        assert quarterly.activity.planned_cash_change == monthly_planned
        assert quarterly.activity.actual_cash_change == monthly_actual
        assert len(quarterly.activity.periods) < len(monthly.activity.periods)

    def test_applied_controls_are_persisted_with_the_book(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("plan")
        view = window._views["plan"]

        view.period.set_selected(1)
        view.measure.set_selected(2)
        view.apply_button.emit("clicked")

        stored = app.db.get_metadata("plan.view")
        assert stored["start"] == view._start_date.isoformat()
        assert stored["end"] == view._end_date.isoformat()
        assert stored["period"] == "quarter"
        assert stored["measure"] == "variance"

    def test_plan_grid_includes_row_column_and_net_cash_totals(self, app, window, populated_book):
        from breadsched.gui.gi_setup import Gtk

        app.open_book(populated_book)
        window.show_category("plan")
        view = window._views["plan"]
        labels = []
        child = view.grid.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Label):
                labels.append(child.get_text())
            child = child.get_next_sibling()

        assert "Total" in labels
        assert "Income total" in labels
        assert "Expenses total" in labels
        assert "Spendable cash bridge" in labels
        assert "Net change in spendable cash" in labels
        assert "Income less expenses" in labels

    def test_plan_grid_shows_whole_mortgage_payment_as_informational(
        self, app, window, populated_book
    ):
        app.open_book(populated_book)
        accounts = {app.db.full_name(account): account for account in app.db.iter_accounts()}
        mortgage = Account(
            name="Mortgage",
            atype=AccountType.LOAN,
            parent=accounts["Liabilities"].handle,
        )
        escrow = Account(
            name="Escrow",
            atype=AccountType.ESCROW,
            parent=accounts["Assets"].handle,
        )
        when = date.today().replace(day=15)
        with app.db.transaction("GTK mortgage") as txn:
            app.db.add_account(mortgage, txn)
            app.db.add_account(escrow, txn)
            app.db.add_scheduled(
                ScheduledTransaction(
                    name="Mortgage payment",
                    recurrence=Recurrence(PeriodType.ONCE, start=when),
                    splits=[
                        ScheduledSplit(mortgage.handle, Money("800")),
                        ScheduledSplit(accounts["Expenses:Rent"].handle, Money("1150")),
                        ScheduledSplit(escrow.handle, Money("450")),
                        ScheduledSplit(
                            accounts["Assets:Checking Account"].handle,
                            Money("-2400"),
                        ),
                    ],
                ),
                txn,
            )
        window.show_category("plan")
        view = window._views["plan"]
        labels = []
        child = view.grid.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Label):
                labels.append(child.get_text())
            child = child.get_next_sibling()

        assert "Cash requirements (informational)" in labels
        assert "Mortgage payment — Liabilities:Mortgage" in labels
        assert "Mortgage cash required" in labels

    def test_the_primary_plan_view_is_read_only(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("plan")
        view = window._views["plan"]
        assert not hasattr(view, "_commit")

    def test_plan_values_are_actionable_buttons(self, app, window, populated_book):
        from breadsched.gui.gi_setup import Gtk

        app.open_book(populated_book)
        window.show_category("plan")
        view = window._views["plan"]
        buttons = []
        child = view.grid.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Button):
                buttons.append(child)
            child = child.get_next_sibling()

        assert buttons
        assert all((button.get_tooltip_text() or "").startswith("Explain ") for button in buttons)

    def test_scenario_event_actions_require_a_saved_scenario(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("plan")
        view = window._views["plan"]
        assert view.scenario_events_box.get_sensitive() is False
        assert view.add_estimate_button.get_sensitive() is False
        assert view.alter_schedule_button.get_sensitive() is False
        assert view.suppress_schedule_button.get_sensitive() is False

    def test_scenario_manager_exposes_base_inheritance_and_local_override(
        self, app, window, populated_book
    ):
        from breadsched.gen.lib import Scenario
        from breadsched.gui.dialogs.scenario_manager_dialog import ScenarioManagerDialog
        from breadsched.gui.planning_context import baseline_scenario

        app.open_book(populated_book)
        base = baseline_scenario(window, app.db)
        scenario = Scenario.derived_from_base(base.assumptions, name="Inherited future")
        with app.db.transaction("Add inheriting scenario") as txn:
            app.db.add_scenario(scenario, txn)

        dialog = ScenarioManagerDialog(window, app.db, window)
        dialog.picker.set_selected(1)
        override = dialog.inherit_controls["income_growth"]
        rate = dialog.rate_controls["income_growth"]

        assert override.get_active() is False
        assert rate.get_sensitive() is False
        override.set_active(True)
        rate.set_value(1.25)
        dialog._on_save(None)

        reloaded = app.db.get_scenario(scenario.handle)
        assert reloaded is not None
        assert reloaded.assumption_sources()["income_growth"] == "Inherited future"
        assert reloaded.effective_assumptions().income_growth == Decimal("0.0125")

    def test_scenario_manager_reparents_without_offering_a_descendant(
        self, app, window, populated_book
    ):
        from breadsched.gen.lib import Scenario
        from breadsched.gui.dialogs.scenario_manager_dialog import (
            ScenarioDeleteDialog,
            ScenarioManagerDialog,
        )
        from breadsched.gui.planning_context import baseline_scenario

        app.open_book(populated_book)
        base = baseline_scenario(window, app.db)
        parent = Scenario.derived_from_base(base.assumptions, name="A parent")
        with app.db.transaction("Add parent") as txn:
            app.db.add_scenario(parent, txn)
        child = Scenario.derived_from_base(base.assumptions, name="B child")
        with app.db.transaction("Add child") as txn:
            app.db.add_scenario(child, txn)

        dialog = ScenarioManagerDialog(window, app.db, window)
        child_index = next(
            index for index, item in enumerate(dialog._scenarios, 1) if item.handle == child.handle
        )
        dialog.picker.set_selected(child_index)
        dialog.parent_picker.set_selected(dialog._parent_handles.index(parent.handle))
        dialog._on_save(None)

        reloaded = app.db.get_scenario(child.handle)
        assert reloaded is not None
        assert reloaded.parent_handle == parent.handle

        parent_index = next(
            index for index, item in enumerate(dialog._scenarios, 1) if item.handle == parent.handle
        )
        dialog.picker.set_selected(parent_index)
        assert child.handle not in dialog._parent_handles
        delete_dialog = ScenarioDeleteDialog(dialog, app.db, parent, lambda: None)
        assert delete_dialog.children == ["B child"]
        assert delete_dialog.delete_button.get_sensitive() is False

    def test_baseline_actions_stay_disabled_when_saved_scenarios_exist(
        self, app, window, populated_book
    ):
        from breadsched.gen.lib import Scenario

        app.open_book(populated_book)
        with app.db.transaction("Add scenario") as txn:
            app.db.add_scenario(Scenario(name="Alternate future"), txn)

        window.show_category("plan")
        view = window._views["plan"]
        assert view.scenario.get_selected() == 0
        assert view._scenario_handle is None
        assert view.add_estimate_button.get_sensitive() is False
        assert view.alter_schedule_button.get_sensitive() is False
        assert view.suppress_schedule_button.get_sensitive() is False

        selected = next(
            index
            for index, scenario in enumerate(view._scenarios, 1)
            if scenario.name == "Alternate future"
        )
        view.scenario.set_selected(selected)
        assert view.add_estimate_button.get_sensitive() is True

        view.scenario.set_selected(0)
        view.flush_refresh()
        assert view._scenario_handle is None
        assert view.add_estimate_button.get_sensitive() is False
        assert view.alter_schedule_button.get_sensitive() is False
        assert view.suppress_schedule_button.get_sensitive() is False

    def test_suppressing_a_baseline_schedule_is_scenario_only(self, app, window, populated_book):
        from breadsched.gen.lib import Scenario

        app.open_book(populated_book)
        schedule = next(iter(app.db.iter_scheduled()))
        with app.db.transaction("Add scenario") as txn:
            app.db.add_scenario(Scenario(name="No recurring payment"), txn)

        window.show_category("plan")
        view = window._views["plan"]
        selected = next(
            index
            for index, scenario in enumerate(view._scenarios, 1)
            if scenario.name == "No recurring payment"
        )
        view.scenario.set_selected(selected)
        assert view.scenario_events_box.get_sensitive() is True
        assert view.add_estimate_button.get_sensitive() is True
        assert view.alter_schedule_button.get_sensitive() is True
        assert view.suppress_schedule_button.get_sensitive() is True
        view._suppress_baseline_schedule(schedule)

        saved = app.db.get_scenario_by_name("No recurring payment")
        assert saved is not None
        assert saved.schedule_overrides[0].source_schedule == schedule.handle
        assert saved.schedule_overrides[0].enabled is False
        assert app.db.get_scheduled(schedule.handle).enabled == schedule.enabled

    def test_scenario_estimate_dialog_saves_a_recurring_estimate(self, app, window, populated_book):
        from breadsched.gen.lib import PeriodType, Scenario
        from breadsched.gui.dialogs.scenario_schedule_dialog import ScenarioScheduleDialog

        app.open_book(populated_book)
        with app.db.transaction("Add scenario") as txn:
            scenario = Scenario(name="Higher food")
            app.db.add_scenario(scenario, txn)
        scenario = app.db.get_scenario_by_name("Higher food")
        assert scenario is not None

        dialog = ScenarioScheduleDialog(window, app.db, scenario)
        names = [app.db.full_name(account) for account in dialog._accounts]
        dialog.name_entry.set_text("Weekly groceries")
        dialog.category.set_selected(names.index("Income:Salary"))
        dialog.funding.set_selected(names.index("Assets:Checking Account"))
        dialog.amount_entry.set_text("300.00")
        dialog.frequency.set_selected(0)
        dialog.start_entry.set_text("2026-01-02")
        dialog._on_save(None)

        saved = app.db.get_scenario_by_name("Higher food")
        assert saved is not None
        assert len(saved.schedule_overrides) == 1
        estimate = saved.schedule_overrides[0]
        assert estimate.source_schedule is None
        assert estimate.placeholder is True
        assert estimate.recurrence.period is PeriodType.WEEK
        assert estimate.recurrence.interval == 1

    def test_scenario_dialog_can_schedule_a_direct_investment_contribution(
        self, app, window, populated_book
    ):
        from breadsched.gen.lib import (
            Account,
            AccountType,
            InvestmentActivityKind,
            Scenario,
        )
        from breadsched.gui.dialogs.scenario_schedule_dialog import ScenarioScheduleDialog

        app.open_book(populated_book)
        root = app.db.root_account()
        bank = app.db.get_account_by_name("Assets:Checking Account")
        assert root is not None and bank is not None
        holding = Account(
            name="Scenario fund",
            atype=AccountType.INVESTMENT,
            parent=root.handle,
        )
        scenario = Scenario(name="Save more")
        with app.db.transaction("Add investment scenario") as txn:
            app.db.add_account(holding, txn)
            app.db.add_scenario(scenario, txn)

        dialog = ScenarioScheduleDialog(window, app.db, scenario)
        dialog_names = [app.db.full_name(account) for account in dialog._accounts]
        dialog.name_entry.set_text("Monthly investment")
        dialog.category.set_selected(dialog_names.index(app.db.full_name(holding)))
        dialog.funding.set_selected(dialog_names.index(app.db.full_name(bank)))
        dialog.investment_activity.set_selected(1)
        dialog.amount_entry.set_text("250.00")
        dialog._on_save(None)

        saved = app.db.get_scenario(scenario.handle)
        assert saved is not None
        classified = saved.schedule_overrides[0].splits[0]
        assert classified.account == holding.handle
        assert classified.investment_activity is InvestmentActivityKind.CONTRIBUTION

    def test_alternate_schedule_changes_only_the_saved_scenario(self, app, window, populated_book):
        from breadsched.gen.lib import (
            Money,
            PeriodType,
            Recurrence,
            Scenario,
            ScheduledSplit,
            ScheduledTransaction,
        )
        from breadsched.gui.dialogs.scenario_schedule_dialog import ScenarioScheduleDialog

        app.open_book(populated_book)
        rent = app.db.get_account_by_name("Expenses:Rent")
        checking = app.db.get_account_by_name("Assets:Checking Account")
        schedule = ScheduledTransaction(
            name="Scenario rent",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
            splits=[
                ScheduledSplit(rent.handle, Money("1800.00")),
                ScheduledSplit(checking.handle, Money("-1800.00")),
            ],
            growth_policy="income",
        )
        schedule.placeholder = True
        scenario = Scenario(name="Higher rent")
        with app.db.transaction("Scenario fixture") as txn:
            app.db.add_scheduled(schedule, txn)
            app.db.add_scenario(scenario, txn)

        scenario = app.db.get_scenario_by_name("Higher rent")
        schedule = app.db.get_scheduled(schedule.handle)
        assert scenario is not None and schedule is not None
        dialog = ScenarioScheduleDialog(window, app.db, scenario, source=schedule)
        assert dialog.growth_policy.get_selected() == 2
        dialog.amount_entry.set_text("2100.00")
        dialog.growth_policy.set_selected(3)
        dialog._on_save(None)

        saved = app.db.get_scenario_by_name("Higher rent")
        baseline = app.db.get_scheduled(schedule.handle)
        assert saved is not None and baseline is not None
        assert saved.schedule_overrides[0].source_schedule == schedule.handle
        assert saved.schedule_overrides[0].splits[0].amount == Money("2100.00")
        assert saved.schedule_overrides[0].growth_policy.value == "inflation"
        assert baseline.splits[0].amount == Money("1800.00")
        assert baseline.growth_policy.value == "income"

    def test_scenario_formula_schedule_preserves_protected_fields(
        self, app, window, populated_book
    ):
        from breadsched.gen.lib import (
            Money,
            PeriodType,
            Recurrence,
            Scenario,
            ScenarioSchedule,
            ScheduledAmountChange,
            ScheduledOccurrenceAdjustment,
            ScheduledSplit,
            ScheduledTransaction,
        )
        from breadsched.gui.dialogs.scenario_schedule_dialog import ScenarioScheduleDialog

        app.open_book(populated_book)
        expense = app.db.get_account_by_name("Expenses:Rent")
        checking = app.db.get_account_by_name("Assets:Checking Account")
        source = ScheduledTransaction(
            name="Formula fixture",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
            splits=[
                ScheduledSplit(expense.handle, formula="base"),
                ScheduledSplit(checking.handle, formula="-base"),
            ],
            amount_changes=[ScheduledAmountChange(date(2027, 1, 1), Money("130"))],
            occurrence_adjustments=[ScheduledOccurrenceAdjustment(date(2026, 3, 1), Money("140"))],
        )
        source.variables = {"base": "125"}
        current = ScenarioSchedule.from_scheduled(source)
        scenario = Scenario(name="Formula scenario", schedule_overrides=[current])

        dialog = ScenarioScheduleDialog(window, app.db, scenario, source=source, current=current)
        assert dialog.save_button.get_sensitive() is True
        assert dialog.amount_entry.get_sensitive() is False
        assert "formula 'base'" in dialog.protected_details.get_text()

        dialog.name_entry.set_text("Updated formula fixture")
        dialog.start_entry.set_text("2026-02-01")
        rebuilt = dialog.build()

        assert rebuilt.name == "Updated formula fixture"
        assert rebuilt.recurrence.start == date(2026, 2, 1)
        assert [split.serialize() for split in rebuilt.splits] == [
            split.serialize() for split in current.splits
        ]
        assert rebuilt.variables == current.variables
        assert [item.serialize() for item in rebuilt.amount_changes] == [
            item.serialize() for item in current.amount_changes
        ]
        assert [item.serialize() for item in rebuilt.occurrence_adjustments] == [
            item.serialize() for item in current.occurrence_adjustments
        ]
        assert rebuilt.source_schedule == source.handle

    def test_scenario_formula_schedule_allows_validated_formula_edits(
        self, app, window, populated_book
    ):
        from breadsched.gen.lib import (
            PeriodType,
            Recurrence,
            Scenario,
            ScenarioSchedule,
            ScheduledSplit,
            ScheduledTransaction,
            evaluate,
        )
        from breadsched.gui.dialogs.scenario_schedule_dialog import ScenarioScheduleDialog

        app.open_book(populated_book)
        expense = app.db.get_account_by_name("Expenses:Rent")
        checking = app.db.get_account_by_name("Assets:Checking Account")
        source = ScheduledTransaction(
            name="Scenario formula fixture",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
            splits=[
                ScheduledSplit(expense.handle, formula="base"),
                ScheduledSplit(checking.handle, formula="-base"),
            ],
        )
        source.variables = {"base": "100"}
        current = ScenarioSchedule.from_scheduled(source)
        scenario = Scenario(name="Formula variant", schedule_overrides=[current])

        dialog = ScenarioScheduleDialog(window, app.db, scenario, source=source, current=current)
        dialog.formula_variables_entry.set_text("base=120; factor=2")
        dialog._formula_entries[0][1].set_text("base * factor")
        dialog._formula_entries[1][1].set_text("-(base * factor)")
        assert dialog.save_button.get_sensitive() is True

        rebuilt = dialog.build()
        assert rebuilt.variables == {"base": "120", "factor": "2"}
        assert [split.formula for split in rebuilt.splits] == [
            "base * factor",
            "-(base * factor)",
        ]
        context = dict(rebuilt.variables)
        context.update({"period": 1, "i": 1})
        assert evaluate(rebuilt.splits[0].formula, context) == 240
        assert rebuilt.source_schedule == source.handle

        dialog._formula_entries[0][1].set_text("unknown_name + 1")
        dialog._validate()
        assert dialog.save_button.get_sensitive() is False
        assert "formula" in dialog.status.get_text().lower()


class TestImportReview:
    """Issue #117: GnuCash changes to reconciled transactions are reviewed in a batch."""

    @pytest.fixture
    def held_book(self, app, tmp_path, gnucash_sqlite_path):
        import sqlite3

        from breadsched.cli.main import main as cli
        from breadsched.gen.engine import ledger
        from breadsched.gen.engine import reconciliation as reconcile_engine

        source = gnucash_sqlite_path
        path = tmp_path / "held.breadsched"
        cli(["init", str(path)])
        cli(["import", str(path), source.path, "--no-infer"])
        db = DbSQLite()
        db.load(str(path))
        try:
            checking = [
                item
                for item in db.iter_transactions()
                if any(split.account == source.ids.checking for split in item.splits)
            ]
            statement = max(item.post_date for item in checking)
            reconciliation = reconcile_engine.start(
                db,
                source.ids.checking,
                statement,
                ledger.balance(db, source.ids.checking, statement),
            )
            reconcile_engine.set_selection(
                db,
                reconciliation.handle,
                [
                    s.handle
                    for item in checking
                    for s in item.splits
                    if s.account == source.ids.checking
                ],
            )
            reconcile_engine.complete(db, reconciliation.handle)
            handles = {item.description: item.handle for item in checking}
        finally:
            db.close()
        with sqlite3.connect(source.path) as gnucash:
            gnucash.execute(
                "UPDATE transactions SET description='Rent (edited)' WHERE guid=?",
                (handles["Rent"],),
            )
            gnucash.execute(
                "UPDATE splits SET value_num=value_num+100, quantity_num=quantity_num+100 "
                "WHERE tx_guid=? AND account_guid=?",
                (handles["Payroll deposit"], source.ids.checking),
            )
            gnucash.execute(
                "UPDATE splits SET value_num=value_num-100, quantity_num=quantity_num-100 "
                "WHERE tx_guid=? AND account_guid=?",
                (handles["Payroll deposit"], source.ids.salary),
            )
        cli(["import", str(path), source.path, "--no-infer"])
        app.open_book(str(path))
        return app, handles

    def _dialog(self, app, window):
        from breadsched.gen.services import pending_import_changes
        from breadsched.gui.dialogs.import_review_dialog import ImportReviewDialog

        changes = pending_import_changes(app.db)
        return ImportReviewDialog(window, app.db, changes), changes

    def test_dialog_is_modal_and_defaults_to_deciding_later(self, held_book, window):
        from breadsched.gen.services import HeldImportDecision

        app, _handles = held_book
        dialog, changes = self._dialog(app, window)
        assert len(changes) == 2
        assert dialog.get_modal() is True
        assert dialog.get_transient_for() is window
        assert dialog.get_destroy_with_parent() is True
        assert all(dialog.decision(i) is HeldImportDecision.LATER for i in range(2))
        assert dialog.apply() == (0, 0)
        assert len(self._dialog(app, window)[1]) == 2

    def test_blocked_change_cannot_use_gnucash_version(self, held_book, window):
        from breadsched.gen.services import HeldImportDecision

        app, handles = held_book
        dialog, changes = self._dialog(app, window)
        dialog.set_all(HeldImportDecision.USE_SOURCE)
        chosen = {
            change.transaction: dialog.decision(index) for index, change in enumerate(changes)
        }
        assert chosen[handles["Rent"]] is HeldImportDecision.USE_SOURCE
        assert chosen[handles["Payroll deposit"]] is HeldImportDecision.LATER

        assert dialog.apply() == (0, 1)
        assert app.db.get_transaction(handles["Rent"]).description == "Rent (edited)"
        [remaining] = self._dialog(app, window)[1]
        assert remaining.transaction == handles["Payroll deposit"]
        assert remaining.blocked_by

    def test_keep_breadsched_version_is_not_asked_again(self, held_book, window):
        from breadsched.gen.services import HeldImportDecision

        app, handles = held_book
        dialog, _changes = self._dialog(app, window)
        dialog.set_all(HeldImportDecision.KEEP_LOCAL)
        assert dialog.apply() == (2, 0)
        assert app.db.get_transaction(handles["Rent"]).description == "Rent"
        assert self._dialog(app, window)[1] == []

    def test_import_dialog_offers_the_review_after_holding_changes(self, held_book, window):
        from breadsched.gui.dialogs.import_dialog import ImportDialog

        app, _handles = held_book
        review = ImportDialog(window, app.db).present_held_review()
        try:
            assert review is not None
            assert len(review.changes) == 2
        finally:
            review.destroy()

    def test_opening_the_book_presents_the_review_before_due_schedules(
        self, app, held_book, monkeypatch
    ):
        from breadsched.gui.gi_setup import GLib

        calls = []
        shown = []
        production_window = ViewManager(app)
        original = production_window.prompt_for_held_imports

        def held():
            calls.append("held")
            shown.append(original())
            return shown[-1]

        monkeypatch.setattr(production_window, "prompt_for_held_imports", held)
        monkeypatch.setattr(production_window, "prompt_for_due", lambda: calls.append("due"))
        try:
            production_window.book_opened(app.db, app.db.path)
            context = GLib.MainContext.default()
            while not calls and context.pending():
                context.iteration(False)
            assert calls == ["held"]
            [review] = shown
            assert type(review).__name__ == "ImportReviewDialog"
            review.close()
            assert calls == ["held", "due"]
        finally:
            production_window.destroy()


class TestDueReview:
    """Item 6: due occurrences are decided one at a time, not posted for you."""

    @pytest.fixture
    def due_book(self, app, window, tmp_path):
        from breadsched.cli.main import main as cli
        from breadsched.gen.db.sqlite import DbSQLite
        from breadsched.gen.lib import (
            Money,
            PeriodType,
            Recurrence,
            ScheduledSplit,
            ScheduledTransaction,
        )

        path = tmp_path / "due.breadsched"
        cli(["init", str(path)])
        db = DbSQLite()
        db.load(str(path))
        accounts = {db.full_name(a): a.handle for a in db.iter_accounts()}
        with db.transaction("Add schedule") as txn:
            db.add_scheduled(
                ScheduledTransaction(
                    name="Rent",
                    recurrence=Recurrence(PeriodType.MONTH, start=date(2020, 1, 1)),
                    splits=[
                        ScheduledSplit(accounts["Expenses"], Money("1800.00")),
                        ScheduledSplit(accounts["Assets"], Money("-1800.00")),
                    ],
                    auto_create=True,
                ),
                txn,
            )
        db.close()
        app.open_book(str(path))
        return app

    def _dialog(self, app, window, limit=4):
        from breadsched.gen.engine import schedule as engine
        from breadsched.gui.dialogs.due_dialog import DueDialog

        due = engine.due_occurrences(app.db, horizon_days=0)[:limit]
        return DueDialog(window, app.db, due), due

    def test_nothing_is_posted_without_a_decision(self, due_book, window):
        """Opening the book must not write to the ledger on its own."""
        assert due_book.db.summary()["txn"] == 0

    def test_the_dialog_lists_what_is_due(self, due_book, window):
        dialog, due = self._dialog(due_book, window)
        assert len(dialog.choosers) == len(due) > 0

    def test_due_dialog_is_modal_transient_and_destroyed_with_parent(self, due_book, window):
        dialog, _ = self._dialog(due_book, window)
        assert dialog.get_modal() is True
        assert dialog.get_transient_for() is window
        assert dialog.get_destroy_with_parent() is True

    def test_remind_me_later_is_the_default(self, due_book, window):
        from breadsched.gui.dialogs.due_dialog import LATER

        dialog, _ = self._dialog(due_book, window)
        assert all(c.get_selected() == LATER for c in dialog.choosers)

    def test_applying_with_defaults_changes_nothing(self, due_book, window):
        dialog, _ = self._dialog(due_book, window)
        assert dialog.apply() == (0, 0)
        assert due_book.db.summary()["txn"] == 0

    def test_post_now_writes_only_the_chosen_occurrence(self, due_book, window):
        from breadsched.gui.dialogs.due_dialog import POST

        dialog, due = self._dialog(due_book, window)
        dialog.choosers[0].set_selected(POST)
        posted, skipped = dialog.apply()

        assert (posted, skipped) == (1, 0)
        assert due_book.db.summary()["txn"] == 1
        recorded = next(iter(due_book.db.iter_transactions()))
        assert recorded.post_date == due[0].when

    def test_never_marks_it_done_without_posting(self, due_book, window):
        from breadsched.gui.dialogs.due_dialog import NEVER

        dialog, due = self._dialog(due_book, window)
        dialog.choosers[0].set_selected(NEVER)
        posted, skipped = dialog.apply()

        assert (posted, skipped) == (0, 1)
        assert due_book.db.summary()["txn"] == 0
        sched = next(iter(due_book.db.iter_scheduled()))
        assert due[0].when in sched.skipped

    def test_a_schedules_dates_are_grouped_with_a_set_all_chooser(self, due_book, window):
        from breadsched.gui.dialogs.due_dialog import NEVER

        dialog, due = self._dialog(due_book, window)
        [group] = dialog.group_choosers.values()
        group.set_selected(3)  # "Never, all"
        assert all(chooser.get_selected() == NEVER for chooser in dialog.choosers)
        assert dialog.apply() == (0, len(due))

    def test_a_date_posted_elsewhere_is_refused_not_duplicated(self, due_book, window):
        from breadsched.gen.engine import schedule as engine
        from breadsched.gui.dialogs.due_dialog import POST

        dialog, due = self._dialog(due_book, window)
        dialog.choosers[0].set_selected(POST)
        # The same date is posted from another window after this one opened.
        engine.post_occurrences(due_book.db, [due[0]])

        assert dialog.apply() is None
        assert "no longer due" in dialog.status.get_text()
        assert due_book.db.summary()["txn"] == 1

    def test_a_skipped_occurrence_is_not_raised_again(self, due_book, window):
        from breadsched.gen.engine import schedule as engine
        from breadsched.gui.dialogs.due_dialog import NEVER

        dialog, due = self._dialog(due_book, window)
        dialog.choosers[0].set_selected(NEVER)
        dialog.apply()

        again = engine.due_occurrences(due_book.db, horizon_days=0)
        assert due[0].when not in [o.when for o in again]

    def test_skipping_one_date_does_not_dismiss_the_others(self, due_book, window):
        from breadsched.gen.engine import schedule as engine
        from breadsched.gui.dialogs.due_dialog import NEVER

        before = len(engine.due_occurrences(due_book.db, horizon_days=0))
        dialog, due = self._dialog(due_book, window)
        dialog.choosers[0].set_selected(NEVER)
        dialog.apply()
        after = len(engine.due_occurrences(due_book.db, horizon_days=0))
        assert after == before - 1

    def test_postponing_leaves_it_due_next_time(self, due_book, window):
        from breadsched.gen.engine import schedule as engine

        before = len(engine.due_occurrences(due_book.db, horizon_days=0))
        dialog, _ = self._dialog(due_book, window)
        dialog.apply()
        assert len(engine.due_occurrences(due_book.db, horizon_days=0)) == before

    def test_mixed_decisions_are_honoured_together(self, due_book, window):
        from breadsched.gui.dialogs.due_dialog import LATER, NEVER, POST

        dialog, _ = self._dialog(due_book, window, limit=3)
        dialog.choosers[0].set_selected(POST)
        dialog.choosers[1].set_selected(NEVER)
        dialog.choosers[2].set_selected(LATER)
        assert dialog.apply() == (1, 1)

    def test_bulk_choices_set_every_row(self, due_book, window):
        from breadsched.gui.dialogs.due_dialog import POST

        dialog, _ = self._dialog(due_book, window)
        dialog.set_all(POST)
        assert all(c.get_selected() == POST for c in dialog.choosers)

    def test_a_skip_survives_reopening_the_book(self, due_book, window, tmp_path):
        from breadsched.gui.dialogs.due_dialog import NEVER

        dialog, due = self._dialog(due_book, window)
        dialog.choosers[0].set_selected(NEVER)
        dialog.apply()
        path = due_book.book_path
        due_book.open_book(path)

        sched = next(iter(due_book.db.iter_scheduled()))
        assert due[0].when in sched.skipped

    def test_posting_is_one_undo_step(self, due_book, window):
        from breadsched.gui.dialogs.due_dialog import POST

        dialog, _ = self._dialog(due_book, window, limit=3)
        dialog.set_all(POST)
        dialog.apply()
        assert due_book.db.summary()["txn"] == 3
        assert due_book.db.undo() is True
        assert due_book.db.summary()["txn"] == 0


class TestResolutionView:
    def test_imported_history_does_not_enter_review_queue(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("resolution")
        view = window._views["resolution"]
        assert view._unresolved_transactions() == []
        assert view.actual_summary.get_text() == "No unresolved actual transactions."

    def _add_matching_actual(self, populated_book):
        from datetime import timedelta

        from breadsched.gen.db.sqlite import DbSQLite
        from breadsched.gen.lib import Split, Transaction

        db = DbSQLite()
        db.load(populated_book)
        schedule = next(iter(db.iter_scheduled()))
        when = next(
            iter(schedule.recurrence.occurrences(date(2026, 12, 31), since=date(2026, 1, 1)))
        )
        actual = Transaction(
            post_date=when + timedelta(days=1),
            description=schedule.description,
        )
        for account, amount in schedule.resolved_splits(when=when):
            actual.add_split(Split(account, amount))
        with db.transaction("Unresolved actual") as txn:
            db.add_transaction(actual, txn)
        db.close()
        return actual.handle, schedule.occurrence_key(when)

    def test_match_action_persists_resolution(self, app, window, populated_book):
        from breadsched.gen.lib import PlanningResolution

        actual_handle, occurrence_key = self._add_matching_actual(populated_book)
        app.open_book(populated_book)
        window.show_category("resolution")
        view = window._views["resolution"]
        view._transaction_handle = actual_handle
        view._refresh_candidates()

        candidate_row = view.candidate_list.get_row_at_index(0)
        assert candidate_row is not None
        assert candidate_row.candidate.event.key == occurrence_key
        view.candidate_list.select_row(candidate_row)
        assert "date variance +1 day(s)" in view.variance.get_text()

        view._on_match(None)
        resolved = app.db.get_transaction(actual_handle)
        assert resolved.planning_resolution is PlanningResolution.MATCHED
        assert resolved.planned_occurrence == occurrence_key
        assert all(txn.handle != actual_handle for txn in view._unresolved_transactions())

    def test_reject_then_unexpected_is_persistent(self, app, window, populated_book):
        from breadsched.gen.lib import PlanningResolution

        actual_handle, occurrence_key = self._add_matching_actual(populated_book)
        app.open_book(populated_book)
        window.show_category("resolution")
        view = window._views["resolution"]
        view._transaction_handle = actual_handle
        view._refresh_candidates()
        candidate_row = view.candidate_list.get_row_at_index(0)
        view.candidate_list.select_row(candidate_row)

        view._on_reject(None)
        rejected = app.db.get_transaction(actual_handle)
        assert occurrence_key in rejected.rejected_plan_occurrences
        assert view.candidate_list.get_row_at_index(0) is None

        view._on_unexpected(None)
        resolved = app.db.get_transaction(actual_handle)
        assert resolved.planning_resolution is PlanningResolution.UNEXPECTED
        assert resolved.rejected_plan_occurrences == []


class TestDashboardView:
    """The dashboard is the view a book opens on."""

    @pytest.fixture
    def view(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("dashboard")
        return window._views["dashboard"]

    def test_a_book_opens_on_it(self, app, window, populated_book):
        app.open_book(populated_book)
        assert window.stack.get_visible_child_name() == "dashboard"

    def test_it_is_first_in_the_sidebar(self):
        assert CATEGORIES[0][0] == "dashboard"

    def test_the_headline_cards_are_rendered(self, view):
        assert view.cards.get_first_child() is not None

    def test_the_group_table_is_rendered(self, view):
        assert view.groups.get_first_child() is not None

    def test_fsa_information_is_not_embedded_in_the_general_dashboard(self, view):
        assert not hasattr(view, "fsa_grid")

    def test_fsa_dashboard_is_a_separate_sidebar_view(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("fsa-dashboard")

        assert window.stack.get_visible_child_name() == "fsa-dashboard"
        assert window._views["fsa-dashboard"].fsa_grid is not None

    def test_bills_and_income_have_separate_lists(self, view):
        bills = view.bills_view.get_model()
        income = view.income_view.get_model()
        # Rows are the presentation lists, with each schedule's missed dates grouped.
        assert bills.get_n_items() == len(view.board.display_bills)
        assert income.get_n_items() == len(view.board.display_incomes)

    def test_the_bills_columns_sort_and_resize(self, view):
        columns = view.bills_view.get_columns()
        assert columns.get_n_items() >= 8
        for index in range(columns.get_n_items()):
            assert columns.get_item(index).get_resizable() is True

    def test_the_horizons_are_on_the_toolbar(self, view):
        assert view.liquidity_spin.get_value() > 0
        assert view.emergency_spin.get_value() > 0

    def test_changing_a_horizon_is_saved_on_the_book(self, view, app):
        from breadsched.gen.engine.dashboard import DashboardConfig

        view.emergency_spin.set_value(9)
        assert DashboardConfig.load(app.db).emergency_months == 9

    def test_changing_a_horizon_changes_the_verdict(self, view):
        before = view.board.emergency_fund
        view.emergency_spin.set_value(12)
        assert view.board.emergency_fund > before

    def test_the_group_dialog_builds_and_saves(self, view, app):
        from breadsched.gen.engine.dashboard import DashboardConfig
        from breadsched.gui.dialogs.dashboard_dialog import DashboardDialog

        dialog = DashboardDialog(view.get_root(), app.db, view.config)
        row = dialog.add_group()
        row["name"].set_text("Property")
        row["kind"].set_selected(3)
        row["checks"][0].set_active(True)
        dialog._on_save(None)

        saved = DashboardConfig.load(app.db)
        assert any(g.name == "Property" and g.kind == "property" for g in saved.groups)

    def test_an_empty_book_still_renders(self, app, window, tmp_path):
        from breadsched.cli.main import main as cli

        path = tmp_path / "empty.breadsched"
        cli(["init", str(path)])
        app.open_book(str(path))
        window.show_category("dashboard")
        assert window.stack.get_visible_child_name() == "dashboard"

    def test_imported_security_and_unset_card_first_view(
        self, app, window, tmp_path, gnucash_household_path
    ):
        from breadsched.cli.main import main as cli

        path = tmp_path / "commitments.breadsched"
        assert cli(["init", str(path)]) == 0
        assert cli(["import", str(path), gnucash_household_path.path, "--no-infer"]) == 0
        app.open_book(str(path))
        window.show_category("dashboard")
        view = window._views["dashboard"]
        assert view.board.report_summary()["net_worth"] == Money("109950.00")
        [note] = view.board.coverage_notes
        assert note.startswith("Card payments not set up")
        shown = []
        child = view.cards.get_first_child()
        while child is not None:
            stack = [child]
            while stack:
                widget = stack.pop()
                if isinstance(widget, Gtk.Label):
                    shown.append(widget.get_text())
                inner = widget.get_first_child()
                while inner is not None:
                    stack.append(inner)
                    inner = inner.get_next_sibling()
            child = child.get_next_sibling()
        assert note in shown

    def test_dashboard_card_shows_what_savings_goals_set_aside(self, app, window, tmp_path):
        from breadsched.gen.sample_book import create_sample_book
        from breadsched.gen.services import (
            AllocateToGoal,
            SaveSavingsGoal,
            allocate_to_goal,
            goal_accounts,
            save_savings_goal,
        )

        path = tmp_path / "goals.breadsched"
        today = date.today()
        create_sample_book(path, as_of=today)
        app.open_book(str(path))
        db = app.db
        cash = next(
            handle
            for handle, _name in goal_accounts(db)
            if db.get_account(handle).atype.is_cash_like
        )
        goal = save_savings_goal(
            db,
            SaveSavingsGoal(
                "Holiday", cash, Money(1200), today.replace(year=today.year + 1), today
            ),
        ).value
        assert allocate_to_goal(db, AllocateToGoal(goal.handle, Money(250), today)).ok
        window.show_category("dashboard")
        view = window._views["dashboard"]
        view.refresh()
        assert view.board.goals_set_aside >= Money(250)
        shown = []
        child = view.cards.get_first_child()
        while child is not None:
            label = child.get_first_child()
            texts = []
            while label is not None:
                texts.append(label.get_text())
                label = label.get_next_sibling()
            shown.append(texts)
            child = child.get_next_sibling()
        [card] = [texts for texts in shown if texts[0] == "Set aside for goals"]
        assert card[1] == view.board.goals_set_aside.format()

    def test_missed_occurrences_render_as_one_row_per_schedule(self, app, window, tmp_path):
        from datetime import timedelta

        from breadsched.gen.engine.dashboard import MissedGroup
        from breadsched.gen.sample_book import create_sample_book
        from breadsched.gui.views.dashboard import _due_date

        start = (date.today().replace(day=1) - timedelta(days=100)).replace(day=15)
        path = tmp_path / "missed.breadsched"
        create_sample_book(path, as_of=start)
        app.open_book(str(path))
        window.show_category("dashboard")
        view = window._views["dashboard"]
        model = view.bills_view.get_model()
        shown = [model.get_item(index).payload for index in range(model.get_n_items())]
        groups = [item for item in shown if isinstance(item, MissedGroup)]
        assert groups, shown
        rent = next(item for item in groups if item.name == "Sample rent")
        assert sum(1 for item in shown if item.name == "Sample rent") == 1
        assert view._due_in(rent).startswith(f"{rent.count} missed, ")
        assert _due_date(rent) == f"{rent.next_due.isoformat()} to {rent.last_due.isoformat()}"
        assert rent.frequency == "every month"

    def test_imported_first_view_discloses_missing_cash_quote(
        self, app, window, tmp_path, gnucash_sqlite_path
    ):
        from breadsched.cli.main import main as cli
        from breadsched.gen.lib import Commodity, Split

        path = tmp_path / "imported.breadsched"
        assert cli(["init", str(path)]) == 0
        assert cli(["import", str(path), gnucash_sqlite_path.path]) == 0
        app.open_book(str(path))
        window.show_category("dashboard")
        view = window._views["dashboard"]
        assert view.board.groups == []
        assert view.board.report_summary()["net_worth"] == Money("2274.50")
        assert any(item.name == "Monthly rent" for item in view.board.bills)

        assets = app.db.get_account_by_name("Assets")
        income = app.db.get_account_by_name("Income")
        assert assets is not None and income is not None
        euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
        foreign_cash = Account(
            name="Foreign cash",
            atype=AccountType.BANK,
            parent=assets.handle,
            commodity=euro.handle,
        )
        entry = Transaction(post_date=date(2026, 1, 15), description="Foreign opening")
        entry.currency = euro.handle
        entry.splits = [Split(foreign_cash.handle, Money(10)), Split(income.handle, Money(-10))]
        with app.db.transaction("Foreign cash") as txn:
            app.db.add_commodity(euro, txn)
            app.db.add_account(foreign_cash, txn)
            app.db.add_transaction(entry, txn)
        view.schedule_refresh()
        view.flush_refresh()
        assert view.board.report_summary()["net_worth"] is None
        assert view.board.report_summary()["liquid"] is None
        assert view.board.missing_quotes == (foreign_cash.handle,)
        assert view.board.unavailable_reason("net_worth") == "Missing reporting-currency quote"
        assert any(item.name == "Monthly rent" for item in view.board.bills)


class TestAccountEditor:
    """Accounts are managed in the interface, not only on the command line."""

    @pytest.fixture
    def accounts_view(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("accounts")
        return window._views["accounts"]

    def _dialog(self, view, account=None):
        from breadsched.gui.dialogs.account_dialog import AccountDialog

        root = view.db.root_account()
        return AccountDialog(
            view.get_root(),
            view.db,
            account,
            default_parent=root.handle if root else None,
        )

    def test_the_view_offers_a_way_to_add_one(self, accounts_view):
        assert hasattr(accounts_view, "edit_account")

    def test_a_new_account_can_be_created(self, accounts_view, app):
        dialog = self._dialog(accounts_view)
        dialog.name_entry.set_text("Savings")
        dialog._on_save(None)
        assert app.db.get_account_by_name("Savings") is not None

    def test_an_opening_balance_can_be_posted_with_it(self, accounts_view, app):
        from breadsched.gen.engine import ledger
        from breadsched.gen.lib import Money

        dialog = self._dialog(accounts_view)
        dialog.name_entry.set_text("Savings")
        dialog.opening_entry.set_text("2500.00")
        dialog._on_save(None)

        handle = app.db.get_account_by_name("Savings").handle
        assert ledger.balance(app.db, handle) == Money("2500.00")

    def test_an_existing_account_opens_on_its_values(self, accounts_view, app):
        account = app.db.get_account_by_name("Assets:Checking Account")
        dialog = self._dialog(accounts_view, account)
        assert dialog.editing is True
        assert dialog.name_entry.get_text() == account.name

    def test_invalid_opening_amount_does_not_raise_a_secondary_exception(self, accounts_view, app):
        dialog = self._dialog(accounts_view)
        dialog.name_entry.set_text("Savings")
        dialog.opening_entry.set_text("not an amount")
        before = sorted(item.handle for item in app.db.iter_transactions())

        dialog._on_save(None)

        assert sorted(item.handle for item in app.db.iter_transactions()) == before
        assert app.db.get_account_by_name("Savings") is None
        assert dialog.status.get_text() == "Enter a valid opening balance."

    def test_dialog_close_refreshes_view_and_allows_default_close(self, accounts_view, monkeypatch):
        calls = []
        monkeypatch.setattr(accounts_view, "refresh", lambda: calls.append("refresh"))

        assert accounts_view.refresh_on_close(None) is False
        assert calls == ["refresh"]

    def test_an_edit_is_stored(self, accounts_view, app):
        account = app.db.get_account_by_name("Assets:Checking Account")
        dialog = self._dialog(accounts_view, account)
        dialog.group_entry.set_text("Cash")
        dialog._on_save(None)
        assert app.db.get_account(account.handle).group == "Cash"

    def test_imported_account_parent_hidden_and_commodity_round_trip(self, accounts_view, app):
        from breadsched.gen.lib import (
            Account,
            AccountType,
            Commodity,
            GnuCashAccountField,
        )

        root = app.db.root_account()
        with app.db.transaction("add imported-style account structure") as txn:
            commodity = Commodity(namespace="FUND", mnemonic="GENERIC", fullname="Generic security")
            app.db.add_commodity(commodity, txn)
            parent = Account(
                name="Ordinary parent",
                atype=AccountType.ASSET,
                parent=root.handle,
                placeholder=False,
            )
            app.db.add_account(parent, txn)
            child = Account(
                name="Imported child",
                atype=AccountType.INVESTMENT,
                parent=parent.handle,
                commodity=commodity.handle,
                hidden=True,
                commodity_scu=1000,
            )
            child.notes = "Local planning note"
            child.source_notes = "Generic imported account note"
            child.source_guid = "imported-account-guid"
            child.source_type = "STOCK"
            child.source_fields = [GnuCashAccountField("slot:color", "string", "#315a74")]
            app.db.add_account(child, txn)

        dialog = self._dialog(accounts_view, child)
        selected_parent = dialog.parents[dialog.parent_picker.get_selected()]
        assert selected_parent.handle == parent.handle
        assert dialog.hidden_check.get_active() is True
        assert dialog.commodity_scu_entry.get_text() == "1000"
        assert dialog.commodity_handles[dialog.commodity_picker.get_selected()] == commodity.handle
        assert dialog.source_summary.get_text() == "STOCK"
        assert dialog.source_button.get_visible() is True
        assert dialog.name_entry.get_sensitive() is False
        assert dialog.commodity_picker.get_sensitive() is False
        assert dialog.commodity_scu_entry.get_sensitive() is False
        assert dialog.parent_picker.get_sensitive() is False
        assert dialog.code_entry.get_sensitive() is False
        assert dialog.description_entry.get_sensitive() is False
        assert dialog.placeholder_check.get_sensitive() is False
        assert dialog.hidden_check.get_sensitive() is False
        assert dialog.type_picker.get_sensitive() is True
        assert dialog.notes_view.get_sensitive() is True
        assert dialog.group_entry.get_sensitive() is True
        notes_buffer = dialog.notes_view.get_buffer()
        notes_start, notes_end = notes_buffer.get_bounds()
        assert notes_buffer.get_text(notes_start, notes_end, True) == "Local planning note"

        rebuilt = dialog.build()
        assert rebuilt.parent == parent.handle
        assert rebuilt.hidden is True
        assert rebuilt.commodity == commodity.handle
        assert rebuilt.commodity_scu == 1000
        assert rebuilt.notes == "Local planning note"
        assert rebuilt.source_notes == "Generic imported account note"
        assert rebuilt.source_guid == "imported-account-guid"
        assert rebuilt.source_type == "STOCK"
        assert rebuilt.source_fields == child.source_fields

    def test_loan_fields_only_show_for_a_loan(self, accounts_view, app):
        from breadsched.gen.lib import AccountType
        from breadsched.gui.dialogs.account_dialog import _TYPES

        dialog = self._dialog(accounts_view)
        dialog.type_picker.set_selected(_TYPES.index(AccountType.BANK))
        assert dialog.loan_box.get_visible() is False
        dialog.type_picker.set_selected(_TYPES.index(AccountType.LOAN))
        assert dialog.loan_box.get_visible() is True

    def test_card_fields_only_show_for_a_card(self, accounts_view, app):
        from breadsched.gen.lib import AccountType
        from breadsched.gui.dialogs.account_dialog import _TYPES

        dialog = self._dialog(accounts_view)
        dialog.type_picker.set_selected(_TYPES.index(AccountType.CREDIT))
        assert dialog.card_box.get_visible() is True
        dialog.type_picker.set_selected(_TYPES.index(AccountType.BANK))
        assert dialog.card_box.get_visible() is False

    def test_a_card_carrying_a_balance_records_its_payment(self, accounts_view, app):
        from breadsched.gen.lib import AccountType, Money
        from breadsched.gui.dialogs.account_dialog import _TYPES

        dialog = self._dialog(accounts_view)
        dialog.name_entry.set_text("Visa")
        dialog.type_picker.set_selected(_TYPES.index(AccountType.CREDIT))
        dialog.full_check.set_active(False)
        dialog.usual_entry.set_text("400.00")
        dialog.day_spin.set_value(22)
        checking = app.db.get_account_by_name("Assets:Checking Account")
        payment_index = next(
            index
            for index, account in enumerate(dialog.payment_accounts, start=1)
            if account.handle == checking.handle
        )
        dialog.card_payment_picker.set_selected(payment_index)
        dialog._on_save(None)

        card = app.db.get_account_by_name("Visa")
        assert card.carries_balance is True
        assert card.usual_payment == Money("400.00")
        assert card.payment_day == 22
        assert card.card_payment_account == checking.handle

    def test_a_card_paid_in_full_keeps_an_editable_payment_day(self, accounts_view, app):
        from breadsched.gen.lib import AccountType
        from breadsched.gui.dialogs.account_dialog import _TYPES

        dialog = self._dialog(accounts_view)
        dialog.name_entry.set_text("Monthly card")
        dialog.type_picker.set_selected(_TYPES.index(AccountType.CREDIT))
        assert dialog.full_check.get_active() is True
        assert dialog.day_spin.get_sensitive() is True
        dialog.day_spin.set_value(17)
        dialog.full_check.set_active(False)
        dialog.full_check.set_active(True)
        assert dialog.day_spin.get_sensitive() is True
        assert dialog.usual_entry.get_sensitive() is False
        dialog._on_save(None)

        card = app.db.get_account_by_name("Monthly card")
        assert card.pays_in_full is True
        assert card.payment_day == 17
        reopened = self._dialog(accounts_view, card)
        assert reopened.day_spin.get_sensitive() is True
        assert reopened.day_spin.get_value_as_int() == 17

    def test_emergency_fund_choice_only_appears_for_eligible_accounts(self, accounts_view, app):
        from breadsched.gen.lib import AccountType
        from breadsched.gui.dialogs.account_dialog import _TYPES

        dialog = self._dialog(accounts_view)
        dialog.type_picker.set_selected(_TYPES.index(AccountType.BANK))
        assert dialog.emergency_check.get_visible() is False
        dialog.type_picker.set_selected(_TYPES.index(AccountType.EXPENSE))
        assert dialog.emergency_check.get_visible() is True
        assert dialog.emergency_check.get_active() is True
        dialog.name_entry.set_text("Optional expense")
        dialog.emergency_check.set_active(False)
        dialog._on_save(None)

        account = app.db.get_account_by_name("Optional expense")
        assert account.emergency_fund_eligible is True
        assert account.emergency_fund_included is False

    def test_a_loan_can_name_its_asset(self, accounts_view, app):
        from breadsched.gen.lib import Account, AccountType
        from breadsched.gui.dialogs.account_dialog import _TYPES

        with app.db.transaction("house") as txn:
            house = Account(
                name="House",
                atype=AccountType.ASSET,
                parent=app.db.root_account().handle,
            )
            app.db.add_account(house, txn)

        dialog = self._dialog(accounts_view)
        dialog.name_entry.set_text("Mortgage")
        dialog.type_picker.set_selected(_TYPES.index(AccountType.LOAN))
        index = next(i for i, a in enumerate(dialog.assets, start=1) if a.handle == house.handle)
        dialog.asset_picker.set_selected(index)
        dialog._on_save(None)

        assert app.db.get_account_by_name("Mortgage").linked_asset == house.handle

    def test_deleting_an_account_with_history_is_refused_with_a_reason(self, accounts_view, app):
        account = app.db.get_account_by_name("Assets:Checking Account")
        dialog = self._dialog(accounts_view, account)
        dialog._on_delete(None)
        assert app.db.get_account(account.handle) is not None
        assert dialog.status.get_text()

    def test_an_unused_account_can_be_deleted(self, accounts_view, app):
        dialog = self._dialog(accounts_view)
        dialog.name_entry.set_text("Spare")
        dialog._on_save(None)
        spare = app.db.get_account_by_name("Spare")

        editor = self._dialog(accounts_view, spare)
        editor._on_delete(None)
        assert app.db.get_account(spare.handle) is None

    def test_a_nameless_account_cannot_be_saved(self, accounts_view):
        dialog = self._dialog(accounts_view)
        dialog.name_entry.set_text("")
        assert dialog.save_button.get_sensitive() is False


class TestDashboardBillsLinkToSchedules:
    """A bill is a scheduled transaction seen from another angle."""

    def test_the_frequency_column_reads_as_the_schedule_says(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("dashboard")
        view = window._views["dashboard"]
        for bill in view.board.bills:
            assert bill.frequency.startswith("every")

    def test_activating_a_bill_opens_its_schedule(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("dashboard")
        view = window._views["dashboard"]
        if not view.board.bills:
            pytest.skip("this book has no bills to open")

        view._on_bill_activated(view.bills_view, 0)
        assert window.stack.get_visible_child_name() == "scheduled"

    def test_the_dashboard_uses_plan_schedules(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("dashboard")
        view = window._views["dashboard"]
        assert view.board is not None
        assert view.board.bills


class TestAccountDialogConstruction:
    """Building the dialog must not depend on the order its widgets are made.

    Setting a dropdown's initial value emits notify::selected, which reached a
    validator referring to widgets further down the constructor. The dialog raised
    before it could be shown — once per emission, so a single click produced five
    tracebacks and no window.
    """

    def test_it_builds_for_a_new_account(self, app, window, populated_book):
        from breadsched.gui.dialogs.account_dialog import AccountDialog

        app.open_book(populated_book)
        dialog = AccountDialog(window, app.db, None, default_parent=app.db.root_account().handle)
        assert dialog.save_button.get_sensitive() is False

    def test_it_builds_for_an_existing_account(self, app, window, populated_book):
        from breadsched.gui.dialogs.account_dialog import AccountDialog

        app.open_book(populated_book)
        account = app.db.get_account_by_name("Assets:Checking Account")
        dialog = AccountDialog(window, app.db, account)
        assert dialog.name_entry.get_text() == account.name

    def test_it_builds_for_every_account_type(self, app, window, populated_book):
        """Each type sets a different initial dropdown, and each one emits."""
        from breadsched.gui.dialogs.account_dialog import AccountDialog

        app.open_book(populated_book)
        for account in list(app.db.iter_accounts()):
            if account.is_root:
                continue
            dialog = AccountDialog(window, app.db, account)
            assert dialog.selected_type is account.atype

    def test_validation_runs_once_the_dialog_is_ready(self, app, window, populated_book):
        from breadsched.gui.dialogs.account_dialog import AccountDialog

        app.open_book(populated_book)
        dialog = AccountDialog(window, app.db, None, default_parent=app.db.root_account().handle)
        dialog.name_entry.set_text("Something")
        assert dialog.save_button.get_sensitive() is True

    def test_security_price_dialog_creates_an_exact_dated_quote(self, app, window, populated_book):
        from breadsched.gui.dialogs.security_price_dialog import SecurityPriceDialog

        app.open_book(populated_book)
        dialog = SecurityPriceDialog(window, app.db)
        dialog.mnemonic_entry.set_text("INDEX")
        dialog.fullname_entry.set_text("Generic index fund")
        dialog.date_entry.set_text("2026-03-01")
        dialog.price_entry.set_text("125,25")
        dialog._on_save(None)

        security = app.db.get_commodity_by_mnemonic("INDEX")
        assert security is not None
        price = next(app.db.iter_prices(commodity=security.handle))
        assert price.quote_date == date(2026, 3, 1)
        assert price.value == Money("125.25")


class TestExchangeRateDialog:
    """GTK manual FX entry uses the same exact quote contract as web and CLI."""

    @pytest.fixture
    def fx_book(self, app, populated_book):
        from breadsched.gen.lib import Commodity, CommodityPrice

        app.open_book(populated_book)
        euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
        with app.db.transaction("Add euro") as txn:
            app.db.add_commodity(euro, txn)
        usd = app.db.get_commodity_by_mnemonic("USD")
        imported = CommodityPrice(
            commodity=euro.handle,
            currency=usd.handle,
            quote_date=date(2026, 3, 1),
            value=Money("1.05"),
            source="user:price-editor",
        )
        with app.db.transaction("Imported quote") as txn:
            app.db.add_price(imported, txn)
        return app, euro, usd

    def _dialog(self, window, app, euro, usd):
        from breadsched.gui.dialogs.exchange_rate_dialog import ExchangeRateDialog

        dialog = ExchangeRateDialog(window, app.db)
        handles = [item.handle for item in dialog.currencies]
        dialog.source_picker.set_selected(handles.index(euro.handle))
        dialog.target_picker.set_selected(handles.index(usd.handle))
        return dialog

    def test_saves_a_directional_manual_quote_and_keeps_imported_evidence(self, fx_book, window):
        app, euro, usd = fx_book
        dialog = self._dialog(window, app, euro, usd)
        assert "Latest EUR→USD: 1.05 on 2026-03-01" in dialog.current.get_text()
        dialog.date_entry.set_text("2026-03-01")
        dialog.rate_entry.set_text("1,10")

        saved = dialog.save()

        assert saved is not None
        prices = list(app.db.iter_prices(commodity=euro.handle, currency=usd.handle))
        assert sorted((p.source, p.value) for p in prices) == [
            ("breadsched", Money("1.10")),
            ("user:price-editor", Money("1.05")),
        ]

    def test_invalid_rates_are_refused_without_writing(self, fx_book, window):
        app, euro, usd = fx_book
        before = len(list(app.db.iter_prices()))
        dialog = self._dialog(window, app, euro, usd)
        dialog.rate_entry.set_text("0")
        assert dialog.save() is None
        assert "greater than zero" in dialog.status.get_text()

        dialog.target_picker.set_selected(dialog.source_picker.get_selected())
        dialog.rate_entry.set_text("1.2")
        assert dialog.save() is None
        assert "different currencies" in dialog.status.get_text()

        dialog.target_picker.set_selected(
            [item.handle for item in dialog.currencies].index(usd.handle)
        )
        dialog.date_entry.set_text("March 1")
        assert dialog.save() is None
        assert "YYYY-MM-DD" in dialog.status.get_text()
        assert len(list(app.db.iter_prices())) == before

    def test_accounts_view_offers_the_exchange_rate_editor(self, fx_book, window):
        """#156: a toolbar icon while Accounts is shown, and a menu item."""
        window.show_category("accounts")
        names = [
            button.get_action_name()
            for button in _descendants(window.view_tools)
            if isinstance(button, Gtk.Button)
        ]
        assert "win.accounts-exchange-rate" in names
        assert window.lookup_action("accounts-exchange-rate").get_enabled()


class TestRepaintsAreDeferred:
    def test_database_open_state_is_used_as_a_property(self):
        """Deferred-refresh guards must not call ``DbSQLite.is_open``."""
        from pathlib import Path

        source = Path("src/breadsched/gui/views/_base.py").read_text()
        assert ".is_open()" not in source

    @pytest.fixture
    def plan_view(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("plan")
        return window._views["plan"]

    def test_deferred_repaint_happens(self, plan_view):
        plan_view.schedule_refresh()
        assert plan_view._refresh_pending is True
        assert plan_view._refresh_source_id is not None
        plan_view.flush_refresh()
        assert plan_view._refresh_pending is False
        assert plan_view._refresh_source_id is None

    def test_repeated_requests_collapse_into_one(self, plan_view):
        plan_view.schedule_refresh()
        plan_view.schedule_refresh()
        plan_view.schedule_refresh()
        assert plan_view._refresh_pending is True
        plan_view.flush_refresh()
        assert plan_view._refresh_pending is False

    def test_the_dashboard_defers_its_rebuild_too(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("dashboard")
        view = window._views["dashboard"]
        view._refresh_pending = False

        view.emergency_spin.set_value(9)
        assert view._refresh_pending is True
        view.flush_refresh()
        assert view.config.emergency_months == 9


#: Criticals seen since the writer was installed. Module level because GLib only
#: accepts one writer function per process.
_CRITICALS: list[str] = []
_WRITER_INSTALLED = False


def _install_log_writer() -> None:
    global _WRITER_INSTALLED
    if _WRITER_INSTALLED:
        return

    from breadsched.gui.gi_setup import GLib

    def writer(level, fields, _n, _data):
        text = ""
        for field in fields:
            if field.key == "MESSAGE":
                value = field.value
                text = value.get_string() if hasattr(value, "get_string") else str(value)
        if level & GLib.LogLevelFlags.LEVEL_CRITICAL:
            _CRITICALS.append(text)
        return GLib.LogWriterOutput.HANDLED

    GLib.log_set_writer_func(writer, None)
    _WRITER_INSTALLED = True


@pytest.mark.skipif(not _DISPLAY_OK, reason="no display")
class TestNoGtkCriticals:
    """GTK complains on stderr and carries on, so nothing fails without watching.

    A critical is a bug that has already happened; this harness turns the ones we
    can provoke into test failures.
    """

    @pytest.fixture
    def criticals(self):
        """Collect GTK criticals raised during one test.

        The writer is installed once for the whole process: GLib aborts if
        ``g_log_set_writer_func`` is called a second time, which took the test
        runner down with a trace trap the first time this fixture set it per test.
        """
        _install_log_writer()
        del _CRITICALS[:]
        yield _CRITICALS
        del _CRITICALS[:]

    def test_building_every_view_is_quiet(self, app, window, populated_book, criticals):
        app.open_book(populated_book)
        for key, _label, _icon in CATEGORIES:
            window.show_category(key)
        assert criticals == []

    def test_changing_plan_grouping_is_quiet(self, app, window, populated_book, criticals):
        app.open_book(populated_book)
        window.show_category("plan")
        view = window._views["plan"]
        view.period.set_selected(1)
        assert criticals == []

    def test_changing_the_dashboard_horizons_is_quiet(self, app, window, populated_book, criticals):
        app.open_book(populated_book)
        window.show_category("dashboard")
        view = window._views["dashboard"]
        view.emergency_spin.set_value(9)
        view.liquidity_spin.set_value(45)
        view.flush_refresh()
        assert criticals == []


class TestStartScreen:
    """A book is opened deliberately, never created behind the user's back."""

    def test_no_book_is_open_at_first(self, window):
        assert window.stack.get_visible_child_name() == "empty"

    def test_activation_without_a_remembered_book_stays_on_start_screen(self, app):
        app.do_activate()
        window = app.props.active_window
        assert app.db is None
        assert isinstance(window, ViewManager)
        assert window.stack.get_visible_child_name() == "empty"

    def test_all_four_ways_in_are_offered(self, app, window):
        actions = _action_names(window.stack.get_child_by_name("empty"))
        assert set(actions) >= {"app.new", "app.open", "app.import-new", "app.open-default"}

    def test_every_offered_action_exists(self, app, window):
        for name in _action_names(window.stack.get_child_by_name("empty")):
            assert app.has_action(name.removeprefix("app.")), f"{name} is not wired"

    def test_file_menu_offers_import_into_a_new_book(self):
        from breadsched.gui.app import build_menu_model

        menu = build_menu_model()
        actions = _menu_actions(menu)
        assert "app.import-new" in actions
        assert "app.import" in actions
        assert {"app.backup", "app.restore", "app.verify"} <= set(actions)

    def test_the_default_book_is_named_but_not_created(self, tmp_path, monkeypatch):
        from breadsched.gui import paths

        monkeypatch.setenv("XDG_DOCUMENTS_DIR", str(tmp_path))
        target = paths.default_book_path()
        assert target.parent == tmp_path
        assert not target.exists(), "the start screen must not create it"

    def test_opening_the_default_creates_it_on_request(self, app, window, tmp_path, monkeypatch):
        from breadsched.gui import paths

        monkeypatch.setenv("XDG_DOCUMENTS_DIR", str(tmp_path))
        app.on_open_default()
        assert paths.default_book_path().exists()
        assert app.db is not None
        assert window.stack.get_visible_child_name() != "empty"

    def test_use_default_never_overwrites_an_existing_user_book(
        self, app, window, tmp_path, monkeypatch
    ):
        from breadsched.cli.main import main as cli
        from breadsched.gen.lib import Account, AccountType
        from breadsched.gui import paths

        target = tmp_path / "BreadSched.breadsched"
        assert cli(["init", str(target)]) == 0
        marker_db = DbSQLite()
        marker_db.load(str(target))
        with marker_db.transaction("recognizable user data") as txn:
            marker = Account(
                name="Existing user data",
                atype=AccountType.BANK,
                parent=marker_db.root_account().handle,
            )
            marker_db.add_account(marker, txn)
        marker_db.close()

        monkeypatch.setattr(paths, "default_book_path", lambda: target)
        app.settings.remove("general", "last_book_path")
        app.settings.save()

        app.on_open_default()

        assert app.db is not None
        assert app.db.get_account(marker.handle) is not None
        assert app.settings.get("general", "last_book_path") == str(target.resolve())

    def test_starter_materialization_refuses_to_replace_an_existing_book(self, tmp_path):
        from breadsched.gui.app import BreadSchedApplication

        target = tmp_path / "existing.breadsched"
        target.write_bytes(b"do not replace")

        with pytest.raises(FileExistsError):
            BreadSchedApplication._materialize_starter_book(target)

        assert target.read_bytes() == b"do not replace"

    def test_opening_the_default_twice_reuses_it(self, app, window, tmp_path, monkeypatch):
        from breadsched.gen.lib import Account, AccountType
        from breadsched.gui import paths

        monkeypatch.setenv("XDG_DOCUMENTS_DIR", str(tmp_path))
        app.on_open_default()
        with app.db.transaction("mark") as txn:
            app.db.add_account(
                Account(name="Marker", atype=AccountType.BANK, parent=app.db.root_account().handle),
                txn,
            )
        app.on_open_default()
        assert app.db.get_account_by_name("Marker") is not None
        assert paths.default_book_path().exists()

    def test_the_documents_folder_is_used_when_there_is_one(self, tmp_path, monkeypatch):
        from breadsched.gui import paths

        monkeypatch.delenv("XDG_DOCUMENTS_DIR", raising=False)
        documents = tmp_path / "Documents"
        documents.mkdir()
        monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: tmp_path))
        assert paths.documents_directory() == documents

    def test_home_is_the_fallback(self, tmp_path, monkeypatch):
        from breadsched.gui import paths

        monkeypatch.delenv("XDG_DOCUMENTS_DIR", raising=False)
        monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: tmp_path))
        assert paths.documents_directory() == tmp_path

    def test_only_breadsched_books_are_offered(self):
        from breadsched.gui.paths import READABLE_SUFFIXES

        assert READABLE_SUFFIXES == (".breadsched",)


def _action_names(widget, found=None):
    found = [] if found is None else found
    name = widget.get_action_name() if hasattr(widget, "get_action_name") else None
    if name:
        found.append(name)
    child = widget.get_first_child() if hasattr(widget, "get_first_child") else None
    while child is not None:
        _action_names(child, found)
        child = child.get_next_sibling()
    return found


def _menu_actions(menu):
    actions = []
    for index in range(menu.get_n_items()):
        action = menu.get_item_attribute_value(index, "action", None)
        if action is not None:
            actions.append(action.get_string())
        for link_name in ("section", "submenu"):
            linked = menu.get_item_link(index, link_name)
            if linked is not None:
                actions.extend(_menu_actions(linked))
    return actions


class TestImportDialogProgress:
    """A slow import must not look like a hung application."""

    @pytest.fixture
    def dialog(self, app, window, populated_book):
        from breadsched.gui.dialogs.import_dialog import ImportDialog

        app.open_book(populated_book)
        return ImportDialog(window, app.db)

    def test_the_dialog_is_big_enough_to_read_warnings_in(self, dialog):
        width, height = dialog.get_default_size()
        assert width >= 800 and height >= 600

    def test_the_meter_is_hidden_until_an_import_starts(self, dialog):
        assert dialog.progress.get_visible() is False

    def test_it_shows_progress_while_importing(self, dialog, gnucash_sqlite_path):
        seen = []
        original = dialog._on_progress

        def watching(stage, done, total):
            seen.append((stage, done, total))
            original(stage, done, total)

        dialog._on_progress = watching
        dialog.set_source(gnucash_sqlite_path.path)
        dialog._on_import(None)
        assert dialog.wait_for_background()

        assert seen, "the importer reported no progress at all"
        assert any(stage for stage, _d, _t in seen)

    def test_import_runs_outside_the_gtk_thread(self, dialog, gnucash_sqlite_path, monkeypatch):
        dialog.set_source(gnucash_sqlite_path.path)
        observed = []
        original = dialog._plugin.run

        def inspect_worker(*args, **kwargs):
            observed.append(threading.get_ident())
            return original(*args, **kwargs)

        monkeypatch.setattr(dialog._plugin, "run", inspect_worker)
        dialog._on_import(None)
        assert dialog.wait_for_background()

        assert observed and observed[0] != threading.get_ident()

    def test_reimport_notifies_views_once_on_the_gtk_thread(self, dialog, gnucash_sqlite_path):
        """A worker callback must never rebuild GTK list models directly."""
        gtk_thread = threading.get_ident()
        notifications = []
        dialog.db.connect(
            "database-changed", lambda *_: notifications.append(threading.get_ident())
        )
        dialog.set_source(gnucash_sqlite_path.path)

        dialog._on_import(None)
        assert dialog.wait_for_background()

        assert notifications == [gtk_thread]

    def test_it_finishes_at_full(self, dialog, gnucash_sqlite_path):
        dialog.set_source(gnucash_sqlite_path.path)
        dialog._on_import(None)
        assert dialog.wait_for_background()
        assert dialog.progress.get_fraction() == 1.0
        assert "Finished" in dialog.progress.get_text()

    def test_an_xml_book_pulses_rather_than_lying(self, dialog, gnucash_xml_path):
        """An XML book gives no count in advance, so a fraction would be invented."""
        dialog.set_source(gnucash_xml_path.path)
        dialog._on_import(None)
        assert dialog.wait_for_background()
        assert "transactions" in dialog.result_view.get_text()

    def test_fifty_warnings_are_shown(self, dialog):
        from breadsched.gui.dialogs.import_dialog import WARNING_LIMIT
        from breadsched.plugins.importer.gnucash_common import ImportResult

        assert WARNING_LIMIT == 50
        result = ImportResult()
        for index in range(120):
            result.warn(f"warning number {index}")
        detail = result.detail(limit=WARNING_LIMIT)
        assert "warning number 49" in detail
        assert "warning number 50" not in detail
        assert "70 more" in detail

    def test_the_meter_is_cleared_when_another_file_is_chosen(
        self, dialog, gnucash_sqlite_path, gnucash_xml_path
    ):
        dialog.set_source(gnucash_sqlite_path.path)
        dialog._on_import(None)
        assert dialog.wait_for_background()
        dialog.set_source(gnucash_xml_path.path)
        assert dialog.progress.get_visible() is False
        assert dialog.progress.get_fraction() == 0.0


class TestCsvImportDialog:
    """Map, preview, and import a CSV statement through the shared service."""

    STATEMENT = (
        "Date,Description,Amount,Memo\n"
        "2026-09-01,Corner Grocer,-42.10,card\n"
        "2026-09-02,Refund,5.00,\n"
    )

    @pytest.fixture
    def dialog(self, app, window, populated_book):
        from breadsched.gui.dialogs.csv_import_dialog import CsvImportDialog

        app.open_book(populated_book)
        dialog = CsvImportDialog(window, app.db)
        yield dialog
        dialog.destroy()

    def _load(self, dialog, tmp_path, text=None):
        path = tmp_path / "statement.csv"
        path.write_text(text or self.STATEMENT, encoding="utf-8")
        dialog.set_source(str(path))
        return path

    def test_reading_columns_suggests_a_mapping(self, dialog, tmp_path):
        self._load(dialog, tmp_path)

        assert dialog.columns == ("Date", "Description", "Amount", "Memo")
        assert dialog.mapping().date == "Date"
        assert dialog.mapping().amount == "Amount"
        assert dialog.mapping().description == "Description"
        assert dialog.mapping().memo == "Memo"
        assert dialog.mapping().debit is None
        assert "utf-8" in dialog.layout_label.get_text()

    def test_preview_lists_rows_without_writing(self, dialog, tmp_path, app):
        self._load(dialog, tmp_path)
        before = len(list(app.db.iter_transactions()))

        preview = dialog.preview()

        assert preview is not None
        assert [row.status for row in preview.rows] == ["new", "new"]
        assert dialog.rows_box.get_first_child() is not None
        assert "New: 2" in dialog.layout_label.get_text()
        assert dialog.import_button.get_sensitive() is True
        assert len(list(app.db.iter_transactions())) == before

    def test_import_is_one_undo_step_and_reimport_adds_nothing(self, dialog, tmp_path, app):
        self._load(dialog, tmp_path)
        before = len(list(app.db.iter_transactions()))

        result = dialog.import_rows()

        assert result is not None and result.transactions_new == 2
        assert len(list(app.db.iter_transactions())) == before + 2
        assert "Imported 2 new" in dialog.status.get_text()
        again = dialog.preview()
        assert again is not None and [row.status for row in again.rows] == [
            "imported",
            "imported",
        ]
        assert app.db.undo() is True
        assert len(list(app.db.iter_transactions())) == before

    def test_category_column_is_suggested_and_shown_in_the_preview(self, dialog, tmp_path, app):
        expense = next(
            item
            for item in app.db.iter_accounts()
            if item.atype.value == "EXPENSE" and not item.placeholder
        )
        full = app.db.full_name(expense)
        self._load(
            dialog,
            tmp_path,
            f"Date,Description,Amount,Category\n2026-09-01,Shop,-4.00,{full}\n"
            "2026-09-02,Other,-1.00,Nowhere\n",
        )

        assert dialog.mapping().category == "Category"
        preview = dialog.preview()

        assert preview is not None
        assert preview.rows[0].category == expense.handle
        assert preview.rows[1].status == "invalid"
        texts = []
        child = dialog.rows_box.get_first_child()
        while child is not None:
            texts.append(child.get_label() if hasattr(child, "get_label") else "")
            child = child.get_next_sibling()
        assert full in texts

    def test_refused_mapping_explains_and_writes_nothing(self, dialog, tmp_path, app):
        self._load(dialog, tmp_path, "Date,Amount\n01/02/2026,-1.00\n")
        before = len(list(app.db.iter_transactions()))

        assert dialog.preview() is None
        assert "day-first or month-first" in dialog.status.get_text()
        assert dialog.import_rows() is None
        assert len(list(app.db.iter_transactions())) == before

        dialog.date_format.set_selected(dialog.DATE_FORMATS.index("day-first"))
        preview = dialog.preview()
        assert preview is not None and preview.rows[0].when.isoformat() == "2026-02-01"

    def test_possible_transfer_is_linked_only_when_checked(self, dialog, tmp_path, app):
        # Money leaves the first account and arrives in the second a day later.
        assert len(dialog.accounts) >= 2
        self._load(dialog, tmp_path, "Date,Description,Amount\n2026-09-10,Transfer out,-75.00\n")
        dialog.account.set_selected(0)
        assert dialog.import_rows() is not None
        self._load(dialog, tmp_path, "Date,Description,Amount\n2026-09-11,Transfer in,75.00\n")
        dialog.account.set_selected(1)
        before = len(list(app.db.iter_transactions()))

        preview = dialog.preview()
        assert preview is not None and preview.rows[0].status == "possible_transfer"
        assert "Possible transfer: 1" in dialog.layout_label.get_text()
        dialog.link_transfers.set_active(True)
        result = dialog.import_rows()

        assert result is not None and result.transactions_linked == 1
        assert "1 transfer(s) linked" in dialog.status.get_text()
        assert len(list(app.db.iter_transactions())) == before

    def test_app_action_opens_the_dialog(self, app, window, populated_book):
        from breadsched.gui.dialogs.csv_import_dialog import CsvImportDialog

        app.open_book(populated_book)
        assert app.actions["import-csv"].get_enabled() is True
        opened = app.on_import_csv()
        try:
            assert isinstance(opened, CsvImportDialog)
        finally:
            opened.destroy()


class TestPayeesDialog:
    """Add, edit, delete payees and accept proposals through the shared service."""

    @pytest.fixture
    def dialog(self, app, window, populated_book):
        from breadsched.gui.dialogs.payee_dialog import PayeesDialog

        app.open_book(populated_book)
        dialog = PayeesDialog(window, app.db)
        yield dialog
        dialog.destroy()

    @staticmethod
    def _describe(dialog, description):
        dialog.edit(None)
        dialog.name_entry.set_text("Known")
        dialog.matches_view.get_buffer().set_text(description)

    def test_add_then_accept_selected_proposals(self, dialog, app):
        transaction = next(iter(app.db.iter_transactions()))
        self._describe(dialog, transaction.description)

        payee = dialog.save()

        assert payee is not None and dialog.editing is None
        assert transaction.handle in dialog.proposal_checks
        assert app.db.get_transaction(transaction.handle).payee is None
        for handle, check in dialog.proposal_checks.items():
            check.set_active(handle == transaction.handle)
        result = dialog.accept_selected()
        assert result is not None and result.assigned == 1
        stored = app.db.get_transaction(transaction.handle)
        assert (stored.payee, stored.description) == (payee.handle, transaction.description)
        assert transaction.handle not in dialog.proposal_checks
        assert "Assigned 1 payee(s)" in dialog.status.get_text()

    def test_refused_save_explains_and_writes_nothing(self, dialog, app):
        dialog.name_entry.set_text("  ")
        assert dialog.save() is None
        assert "Enter a payee name" in dialog.status.get_text()
        assert list(app.db.iter_payees()) == []

    def test_edit_renames_and_delete_clears(self, dialog, app):
        transaction = next(iter(app.db.iter_transactions()))
        self._describe(dialog, transaction.description)
        payee = dialog.save()
        dialog.accept_selected()

        dialog.edit(payee.handle)
        assert dialog.save_button.get_label() == "Save changes"
        dialog.name_entry.set_text("Renamed")
        assert dialog.save() is not None
        assert app.db.get_payee(payee.handle).name == "Renamed"
        assert app.db.get_transaction(transaction.handle).payee == payee.handle

        cleared = dialog.delete(payee.handle)
        assert cleared is not None and cleared >= 1
        assert app.db.get_transaction(transaction.handle).payee is None
        assert "Edit → Undo restores it" in dialog.status.get_text()

    def test_app_action_opens_the_dialog(self, app, window, populated_book):
        from breadsched.gui.dialogs.payee_dialog import PayeesDialog

        assert app.actions["payees"].get_enabled() is False
        app.open_book(populated_book)
        assert app.actions["payees"].get_enabled() is True
        opened = app.on_payees()
        try:
            assert isinstance(opened, PayeesDialog)
        finally:
            opened.destroy()


class TestPayeeInRegisterAndEditor:
    """The register shows the accepted payee; the editor sets, keeps, or clears it."""

    def _payee(self, app, name="Landlord"):
        from breadsched.gen.services.payees import SavePayee, save_payee

        return save_payee(app.db, SavePayee(name)).value

    def test_register_column_shows_the_payee_name(self, app, window, populated_book):
        from breadsched.gen.services.payees import assign_payee

        app.open_book(populated_book)
        window.show_category("register")
        view = window._views["register"]
        columns = view.column_view.get_columns()
        titles = [columns.get_item(i).get_title() for i in range(columns.get_n_items())]
        assert titles.index("Payee") == titles.index("Description") + 1
        payee = self._payee(app)
        row = view._rows[0]
        assert view._payee_name(row.transaction) == ""
        assert assign_payee(app.db, row.transaction.handle, payee.handle).ok
        view.refresh()
        assert view._payee_name(app.db.get_transaction(row.transaction.handle)) == "Landlord"

    def test_editor_picker_sets_keeps_and_clears(self, app, window, populated_book):
        from breadsched.gui.dialogs.transaction_dialog import TransactionDialog

        app.open_book(populated_book)
        payee = self._payee(app)
        existing = next(iter(app.db.iter_transactions()))
        description = existing.description

        dialog = TransactionDialog(window, app.db, transaction=existing)
        assert dialog.payee_picker.get_selected() == 0
        dialog.payee_picker.set_selected(1)
        dialog._on_save(None)
        stored = app.db.get_transaction(existing.handle)
        assert (stored.payee, stored.description) == (payee.handle, description)

        reopened = TransactionDialog(window, app.db, transaction=stored)
        assert reopened.payee_picker.get_selected() == 1
        reopened._on_save(None)
        assert app.db.get_transaction(existing.handle).payee == payee.handle

        cleared = TransactionDialog(
            window, app.db, transaction=app.db.get_transaction(existing.handle)
        )
        cleared.payee_picker.set_selected(0)
        cleared._on_save(None)
        assert app.db.get_transaction(existing.handle).payee is None


class TestDashboardWidth:
    """Issue #140: a combined group's long note must not widen the window."""

    def test_long_group_and_coverage_notes_do_not_widen_the_dashboard(
        self, app, window, populated_book
    ):
        from breadsched.gen.engine.dashboard import GroupResult
        from breadsched.gen.lib import Money

        app.open_book(populated_book)
        window.show_category("dashboard")
        view = window._views["dashboard"]
        view._render_groups()
        _minimum, baseline = view.measure(Gtk.Orientation.HORIZONTAL, -1)[:2]

        combined = "; ".join(
            f"Assets:Investments:Brokerage account {index}: 2026-09-01 · Manual quote"
            for index in range(30)
        )
        view.board.groups.append(
            GroupResult(name="Everything", kind="asset", total=Money(0), note=combined)
        )
        view.board.coverage_notes = (*view.board.coverage_notes, combined)
        view._render_groups()
        view._render_cards()
        _minimum, natural = view.measure(Gtk.Orientation.HORIZONTAL, -1)[:2]

        # Before the fix the 30-account note alone asked for ~29,000 pixels.
        assert natural - baseline < 1000
        name = next(
            child
            for child in _children(view.groups)
            if isinstance(child, Gtk.Label) and child.get_text().startswith("Everything")
        )
        assert combined in (name.get_tooltip_text() or "")

    def test_inferred_members_are_in_the_tooltip_one_per_line(self, app, window, populated_book):
        """Issue #150: the row names the group; the tooltip lists its accounts."""
        from breadsched.gen.engine.dashboard import GroupResult
        from breadsched.gen.lib import Money

        app.open_book(populated_book)
        window.show_category("dashboard")
        view = window._views["dashboard"]
        members = ("Assets", "  Assets:Checking", "  Assets:Savings: 2026-09-01 · Manual")
        view.board.groups.append(
            GroupResult(
                name="Everything",
                path="Everything",
                kind="asset",
                total=Money(0),
                members=members,
            )
        )
        view._render_groups()
        name = next(
            child
            for child in _children(view.groups)
            if isinstance(child, Gtk.Label) and child.get_text().startswith("Everything")
        )
        assert name.get_text() == "Everything"
        assert name.get_tooltip_text().split("\n") == ["Everything", *members]


def _children(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        child = child.get_next_sibling()


class TestRulesDialog:
    """Add, reorder, and delete rules and accept their proposals in GTK."""

    @pytest.fixture
    def dialog(self, app, window, populated_book, tmp_path):
        from breadsched.gen.lib.account import AccountClass
        from breadsched.gen.services.csv_import import CsvImportRequest, CsvMapping, import_csv
        from breadsched.gui.dialogs.rules_dialog import RulesDialog

        app.open_book(populated_book)
        bank = next(
            account
            for account in app.db.iter_accounts()
            if not account.placeholder and account.account_class is AccountClass.ASSET
        )
        path = tmp_path / "statement.csv"
        path.write_text(
            "Date,Description,Amount\n2026-09-01,CORNER GROCER #1,-42.10\n"
            "2026-09-02,City Power,-60.00\n",
            encoding="utf-8",
        )
        mapping = CsvMapping(date="Date", description="Description", amount="Amount")
        assert import_csv(app.db, CsvImportRequest(str(path), bank.handle, mapping)).ok
        dialog = RulesDialog(window, app.db)
        yield dialog
        dialog.destroy()

    def _add(self, dialog, description, category=0):
        dialog.kind_picker.set_selected(0)
        dialog.description_entry.set_text(description)
        dialog.category_picker.set_selected(category)
        return dialog.add_rule()

    def test_add_rule_then_accept_selected(self, dialog, app):
        rule = self._add(dialog, "city power")
        assert rule is not None and rule.category == dialog.categories[0].handle
        [handle] = list(dialog.proposal_checks)
        assert app.db.get_transaction(handle).description == "City Power"

        result = dialog.accept_selected()

        assert result is not None and result.assigned == 1
        stored = app.db.get_transaction(handle)
        assert rule.category in {split.account for split in stored.splits}
        assert dialog.proposal_checks == {}
        assert "Categorized 1 transaction(s)" in dialog.status.get_text()

    def test_reorder_changes_the_deciding_rule_and_delete_works(self, dialog, app):
        from breadsched.gen.services.categorization import list_rules

        assert len(dialog.categories) >= 2
        first = self._add(dialog, "city power", 0)
        second = self._add(dialog, "city power 2", 1)
        assert second is None  # same key as the first rule: a duplicate match
        assert "already matches" in dialog.status.get_text()
        second = self._add(dialog, "corner grocer", 1)
        assert [rule.handle for rule in list_rules(app.db)] == [first.handle, second.handle]

        assert dialog.move(second.handle, 1)
        assert [rule.handle for rule in list_rules(app.db)] == [second.handle, first.handle]
        assert dialog.delete(first.handle)
        assert [rule.handle for rule in list_rules(app.db)] == [second.handle]
        assert "Edit → Undo restores it" in dialog.status.get_text()

    def test_refused_rule_explains_and_writes_nothing(self, dialog, app):
        from breadsched.gen.services.categorization import list_rules

        assert self._add(dialog, "#1234") is None
        assert "at least one word without digits" in dialog.status.get_text()
        assert list_rules(app.db) == []

    def test_app_action_opens_the_dialog(self, app, window, populated_book):
        from breadsched.gui.dialogs.rules_dialog import RulesDialog

        assert app.actions["rules"].get_enabled() is False
        app.open_book(populated_book)
        assert app.actions["rules"].get_enabled() is True
        opened = app.on_rules()
        try:
            assert isinstance(opened, RulesDialog)
        finally:
            opened.destroy()


class TestBlankRowAutocomplete:
    """Leaving the description proposes the latest matching entry; nothing posts."""

    def _register(self, app, window, populated_book):
        app.open_book(populated_book)
        handle = app.db.get_account_by_name("Assets:Checking Account").handle
        window.open_register(handle)
        return window._views["register"]

    def _post(self, view, description, amount, transfer_index=1):
        blank = view.blank
        blank.transfer.set_selected(transfer_index)
        blank.description.set_text(description)
        blank.decrease.set_text(amount)
        assert blank.commit() is True
        return blank.transfers[transfer_index].handle

    def test_a_matching_description_fills_transfer_and_amount(self, app, window, populated_book):
        view = self._register(app, window, populated_book)
        expected = self._post(view, "Corner Grocer #12", "42.10")
        blank = view.blank
        assert blank.transfer.get_selected() == 0  # reset after posting
        before = len(list(app.db.iter_transactions()))

        blank.description.set_text("CORNER GROCER #99")
        suggestion = blank.propose()

        assert suggestion is not None
        assert blank.transfer_handle() == expected
        assert blank.decrease.get_text() == "42.10"
        assert blank.increase.get_text() == ""
        assert "Proposed from" in view.entry_status.get_text()
        assert len(list(app.db.iter_transactions())) == before

    def test_typed_input_is_never_overwritten(self, app, window, populated_book):
        view = self._register(app, window, populated_book)
        self._post(view, "Corner Grocer", "42.10")
        blank = view.blank
        blank.description.set_text("Corner Grocer")
        blank.transfer.set_selected(2)
        chosen = blank.transfer_handle()
        blank.increase.set_text("7.00")

        blank.propose()

        assert blank.increase.get_text() == "7.00"
        assert blank.decrease.get_text() == ""
        assert blank.transfer_handle() == chosen

    def test_no_match_leaves_the_row_alone(self, app, window, populated_book):
        view = self._register(app, window, populated_book)
        view.blank.description.set_text("Something never entered before")

        assert view.blank.propose() is None
        assert view.blank.increase.get_text() == ""
        assert view.blank.decrease.get_text() == ""


class TestEditorAutocomplete:
    """A new transaction's untouched splits are proposed from the latest match."""

    def _grocer(self, app, amount="42.10"):
        from breadsched.gen.lib import Transaction

        checking = app.db.get_account_by_name("Assets:Checking Account").handle
        other = next(
            account.handle
            for account in app.db.iter_accounts()
            if not account.is_root
            and not account.placeholder
            and not account.hidden
            and account.handle != checking
        )
        transaction = Transaction.simple(
            date(2026, 9, 1), "Corner Grocer #12", other, checking, amount
        )
        transaction.splits[0].memo = "weekly"
        with app.db.transaction("Grocer") as txn:
            app.db.add_transaction(transaction, txn)
        return checking, transaction

    def test_leaving_the_description_fills_every_split(self, app, window, populated_book):
        from breadsched.gui.dialogs.transaction_dialog import TransactionDialog

        app.open_book(populated_book)
        checking, source = self._grocer(app)
        before = len(list(app.db.iter_transactions()))
        dialog = TransactionDialog(window, app.db, default_account=checking)
        dialog.description_entry.set_text("CORNER GROCER #99")

        suggestion = dialog.propose_from_entry()

        assert suggestion is not None and suggestion.source == source.handle
        filled = [(e.account_handle, e.value(), e.memo.get_text()) for e in dialog.splits]
        assert filled == [(s.account, s.value, s.memo) for s in source.splits]
        assert "Proposed from 2026-09-01" in dialog.proposal_note.get_text()
        assert dialog.proposal_note.get_visible() is True
        assert dialog.save_button.get_sensitive() is True
        assert len(list(app.db.iter_transactions())) == before

    def test_choosing_a_payee_proposes_and_a_proposed_payee_is_selected(
        self, app, window, populated_book
    ):
        from breadsched.gen.services.payees import SavePayee, assign_payee, save_payee
        from breadsched.gui.dialogs.transaction_dialog import TransactionDialog

        app.open_book(populated_book)
        checking, source = self._grocer(app)
        payee = save_payee(app.db, SavePayee("Corner Grocer")).value
        assert assign_payee(app.db, source.handle, payee.handle).ok

        by_payee = TransactionDialog(window, app.db, default_account=checking)
        by_payee.payee_picker.set_selected(1)
        assert [e.value() for e in by_payee.splits] == [s.value for s in source.splits]

        by_description = TransactionDialog(window, app.db, default_account=checking)
        by_description.description_entry.set_text("Corner Grocer")
        assert by_description.propose_from_entry() is not None
        assert by_description.payee_picker.get_selected() == 1

    def test_typed_splits_are_never_overwritten(self, app, window, populated_book):
        from breadsched.gui.dialogs.transaction_dialog import TransactionDialog

        app.open_book(populated_book)
        checking, _source = self._grocer(app)
        dialog = TransactionDialog(window, app.db, default_account=checking)
        dialog.splits[0].amount.set_text("7.00")
        dialog.description_entry.set_text("Corner Grocer")

        assert dialog.propose_from_entry() is None
        assert dialog.splits[0].amount.get_text() == "7.00"
        assert dialog.proposal_note.get_visible() is False

        memo_only = TransactionDialog(window, app.db, default_account=checking)
        memo_only.splits[1].memo.set_text("typed")
        memo_only.description_entry.set_text("Corner Grocer")
        assert memo_only.propose_from_entry() is None

    def test_an_existing_transaction_is_never_rewritten(self, app, window, populated_book):
        from breadsched.gui.dialogs.transaction_dialog import TransactionDialog

        app.open_book(populated_book)
        checking, _source = self._grocer(app, "42.10")
        _checking, older = self._grocer(app, "5.00")
        dialog = TransactionDialog(window, app.db, transaction=older)
        values = [e.value() for e in dialog.splits]

        assert dialog.propose_from_entry() is None
        assert [e.value() for e in dialog.splits] == values

    def test_no_match_leaves_the_form_alone(self, app, window, populated_book):
        from breadsched.gui.dialogs.transaction_dialog import TransactionDialog

        app.open_book(populated_book)
        dialog = TransactionDialog(window, app.db)
        accounts = [e.account_handle for e in dialog.splits]
        dialog.description_entry.set_text("Something never entered before")

        assert dialog.propose_from_entry() is None
        assert [e.account_handle for e in dialog.splits] == accounts
        assert all(e.is_blank for e in dialog.splits)


class TestChromeCleanup:
    """Issue: sidebar/toolbar overlap, cramped panel, no color, numbers at the edge.

    Category navigation lives in exactly one place (the sidebar); the toolbar
    keeps only actions that are not also categories. Reclaiming that duplication
    lets the sidebar start narrower, freeing width for the views. Numeric values
    keep clear of the pane edge, and ColumnView-based tables can be told apart at
    rest, not only on hover.
    """

    def test_the_toolbar_does_not_duplicate_sidebar_categories(self):
        from breadsched.gui.viewmanager import CATEGORIES, TOOLBAR

        category_keys = {key for key, _label, _icon in CATEGORIES}
        for _label, _icon, action, _tooltip in TOOLBAR:
            if action and action.startswith("win.show-category::"):
                assert action.split("::", 1)[1] not in category_keys

    def test_there_is_no_category_sidebar(self, window):
        """#155: view icons in the toolbar replace the sidebar."""
        assert not hasattr(window, "navigator")
        assert not hasattr(window, "paned")
        assert window.stack.get_parent() is window.get_child()

    def test_every_view_has_one_icon_and_one_menu_item_with_distinct_icons(self, app, window):
        from breadsched.gui.viewmanager import CATEGORIES

        icons = [icon for _key, _label, icon in CATEGORIES]
        assert len(set(icons)) == len(icons)
        for key, button in window.view_buttons.items():
            assert button.get_action_name() == "win.show-category"
            assert button.get_action_target_value().get_string() == key
        menu = app.get_menubar()
        targets = []

        def walk(model):
            for index in range(model.get_n_items()):
                action = model.get_item_attribute_value(index, "action", None)
                target = model.get_item_attribute_value(index, "target", None)
                if action is not None and action.get_string() == "win.show-category":
                    targets.append(target.get_string())
                for link in ("submenu", "section"):
                    child = model.get_item_link(index, link)
                    if child is not None:
                        walk(child)

        walk(menu)
        assert targets == [key for key, _label, _icon in CATEGORIES]

    def test_data_tables_carry_the_shared_zebra_striping_class(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("register")
        window.show_category("dashboard")
        window.show_category("scheduled")
        window.show_category("upcoming")
        window.show_category("accounts")
        assert window._views["register"].column_view.has_css_class("data-table")
        assert window._views["dashboard"].bills_view.has_css_class("data-table")
        assert window._views["dashboard"].income_view.has_css_class("data-table")
        assert window._views["scheduled"].definitions_view.has_css_class("data-table")
        assert window._views["scheduled"].estimates_view.has_css_class("data-table")
        assert window._views["upcoming"].upcoming_view.has_css_class("data-table")
        assert window._views["accounts"].column_view.has_css_class("data-table")

    def test_the_stylesheet_gives_numeric_values_and_tables_visual_distinction(self):
        from breadsched.gui.app import STYLE_RESOURCE

        stylesheet = STYLE_RESOURCE.read_text(encoding="utf-8")
        assert "padding-right" in stylesheet
        assert "data-table" in stylesheet and "nth-child" in stylesheet

    def test_plan_grid_numbers_carry_the_numeric_class(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("plan")
        view = window._views["plan"]
        child = view.grid.get_first_child()
        found_numeric = False
        while child is not None:
            if child.has_css_class("numeric"):
                found_numeric = True
                break
            child = child.get_next_sibling()
        assert found_numeric

    def test_the_register_add_button_is_distinguished_from_the_blank_row(
        self, app, window, populated_book
    ):
        """The dialog is for extra splits/notes/reconciliation; the blank row is
        the direct, no-dialog way to add an ordinary two-split transaction."""
        app.open_book(populated_book)
        window.show_category("register")
        view = window._views["register"]
        assert view.blank.description is not None  # direct entry needs no dialog
        # Walk to the "+" button in the toolbar row and check its tooltip text
        # distinguishes it from quick entry rather than saying only "Add".
        bar = view.get_first_child()
        found = []

        def walk(widget):
            child = widget.get_first_child()
            while child is not None:
                tooltip = child.get_tooltip_text()
                if tooltip:
                    found.append(tooltip)
                walk(child)
                child = child.get_next_sibling()

        walk(bar)
        assert any("full transaction editor" in text for text in found)


def _descendants(widget):
    for child in _children(widget):
        yield child
        yield from _descendants(child)


class TestTableSections:
    """#152/#153: each table owns its column chooser; Dashboard sections are sized."""

    VIEWS = ("dashboard", "register", "accounts", "scheduled", "upcoming")

    def test_every_chooser_sits_with_its_own_table(self, app, window, populated_book):
        app.open_book(populated_book)
        for key in self.VIEWS:
            window.show_category(key)
            view = window._views[key]
            toolbar = view.get_first_child()
            choosers = [
                widget
                for widget in _descendants(view)
                if isinstance(widget, Gtk.MenuButton) and hasattr(widget, "column_view")
            ]
            assert choosers, key
            tooltips = [chooser.get_tooltip_text() for chooser in choosers]
            assert len(set(tooltips)) == len(tooltips), key
            for chooser in choosers:
                assert chooser not in set(_descendants(toolbar)), key
                assert chooser.get_tooltip_text().startswith("Choose "), key
                section = chooser.get_parent().get_parent()
                assert chooser.column_view in set(_descendants(section)), key

    def test_dashboard_sections_are_separate_and_not_stretched(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("dashboard")
        view = window._views["dashboard"]
        sections = (view.groups_section, view.bills_section, view.income_section)
        for section in sections:
            assert section.get_hexpand() is False
            assert section.get_halign() == Gtk.Align.START
        assert view.bills_section.heading.get_text() == "Pending bills"
        assert view.income_section.heading.get_text() == "Expected income"
        assert view.bills_view in set(_descendants(view.bills_section))
        assert view.income_view in set(_descendants(view.income_section))
        assert view.groups in set(_descendants(view.groups_section))


class TestTablesShrink:
    """#154: tables keep every column on screen as the window narrows."""

    def test_tables_shrink_instead_of_scrolling_sideways(self, app, window, populated_book):
        app.open_book(populated_book)
        tables = []
        for key in ("dashboard", "register", "accounts", "scheduled", "upcoming"):
            window.show_category(key)
            view = window._views[key]
            tables.extend(
                widget for widget in _descendants(view) if isinstance(widget, Gtk.ColumnView)
            )
        assert len(tables) >= 6
        for table in tables:
            scroller = table.get_parent()
            while not isinstance(scroller, Gtk.ScrolledWindow):
                scroller = scroller.get_parent()
            assert scroller.get_policy()[0] == Gtk.PolicyType.NEVER

    def test_text_cells_ellipsize_and_amount_cells_stay_whole(self):
        from breadsched.gui.gi_setup import Pango
        from breadsched.gui.views._base import column

        for numeric, expected in (
            (False, Pango.EllipsizeMode.END),
            (True, Pango.EllipsizeMode.NONE),
        ):
            factory = column("Heading", lambda row: row, numeric=numeric).get_factory()
            item = Gtk.ListItem()
            factory.emit("setup", item)
            assert item.get_child().get_ellipsize() == expected

    def test_register_minimum_width_is_below_its_natural_width(self, app, window, populated_book):
        app.open_book(populated_book)
        handle = app.db.get_account_by_name("Assets:Checking Account").handle
        window.open_register(handle)
        table = window._views["register"].column_view
        minimum, natural = table.measure(Gtk.Orientation.HORIZONTAL, -1)[:2]
        assert minimum < natural


class TestViewActions:
    """#156: view commands live in menus and toolbar icons, not stray buttons."""

    def _labels(self, view):
        return [
            widget.get_label()
            for widget in _descendants(view)
            if isinstance(widget, Gtk.Button) and widget.get_label()
        ]

    def test_accounts_and_fsa_have_no_stray_action_buttons(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("accounts")
        assert self._labels(window._views["accounts"]) == []
        window.show_category("fsa-dashboard")
        assert "Manage FSA claims…" not in self._labels(window._views["fsa-dashboard"])

    def test_view_icons_follow_the_current_view(self, app, window, populated_book):
        app.open_book(populated_book)

        def tools():
            return [
                button.get_action_name()
                for button in _descendants(window.view_tools)
                if isinstance(button, Gtk.Button)
            ]

        window.show_category("fsa-dashboard")
        assert tools() == ["win.fsa-dashboard-manage-claims"]
        window.show_category("register")
        assert tools() == []
        window.show_category("dashboard")
        assert tools() == ["win.dashboard-configure-groups", "win.dashboard-net-worth-history"]

    @pytest.mark.parametrize(
        ("key", "icons", "gone"),
        (
            (
                "scheduled",
                ["new-scheduled", "suggest", "new-loan"],
                ["New scheduled…", "Suggest from history…", "New loan…"],
            ),
            (
                "plan",
                ["new-scenario", "manage-scenarios", "explore-expenses"],
                ["New scenario…", "Manage scenarios…", "Explore expenses…"],
            ),
            ("projection", ["compare", "export"], ["Compare with…"]),
        ),
    )
    def test_stateless_view_commands_are_toolbar_icons_not_buttons(
        self, app, window, populated_book, key, icons, gone
    ):
        """#156 follow-up: commands that never change with the view's state."""
        app.open_book(populated_book)
        window.show_category(key)
        actions = [
            button.get_action_name()
            for button in _descendants(window.view_tools)
            if isinstance(button, Gtk.Button)
        ]
        assert actions == [f"win.{key}-{name}" for name in icons]
        labels = self._labels(window._views[key])
        assert not set(gone) & set(labels)

    def test_state_dependent_buttons_stay_in_their_views(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("projection")
        assert window._views["projection"].save_button.get_parent() is not None
        window.show_category("upcoming")
        assert window._views["upcoming"].post_button.get_parent() is not None
        window.show_category("register")
        assert window._views["register"].reconcile_button.get_parent() is not None

    def test_every_view_action_is_in_a_menu_and_registered(self, app, window):
        from breadsched.gui.viewmanager import VIEW_ACTIONS, view_action_name

        expected = {
            f"win.{view_action_name(key, item)}"
            for key, actions in VIEW_ACTIONS.items()
            for item in actions
        }
        found = set()

        def walk(model):
            for index in range(model.get_n_items()):
                action = model.get_item_attribute_value(index, "action", None)
                if action is not None:
                    found.add(action.get_string())
                for link in ("submenu", "section"):
                    child = model.get_item_link(index, link)
                    if child is not None:
                        walk(child)

        walk(app.get_menubar())
        assert expected <= found
        for name in expected:
            assert window.lookup_action(name[4:]) is not None

    def test_view_actions_are_disabled_without_a_book(self, window):
        assert window.lookup_action("accounts-new-account").get_enabled() is False

    def test_hide_empty_toggle_filters_accounts(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("accounts")
        view = window._views["accounts"]
        window.lookup_action("accounts-hide-empty").activate(None)
        assert window.lookup_action("accounts-hide-empty").get_state().get_boolean()
        assert view._show_zero is False
        window.lookup_action("accounts-hide-empty").activate(None)
        assert view._show_zero is True

    def test_manage_claims_action_opens_the_claims_dialog(
        self, app, window, populated_book, monkeypatch
    ):
        app.open_book(populated_book)
        window.show_category("fsa-dashboard")
        opened = []
        monkeypatch.setattr(
            window._views["fsa-dashboard"], "_open_claim", lambda *a: opened.append(a)
        )
        window.lookup_action("fsa-dashboard-manage-claims").activate(None)
        assert opened == [()]


class TestRegisterOpensAtTheEnd:
    """#157: like a check register, open on the most recent entry."""

    def _register(self, app, window, populated_book, monkeypatch):
        app.open_book(populated_book)
        window.show_category("register")
        view = window._views["register"]
        calls = []
        original = view.column_view.scroll_to

        def record(position, *args):
            calls.append(position)
            return original(position, *args)

        monkeypatch.setattr(view.column_view, "scroll_to", record)
        return view, calls

    def test_showing_an_account_scrolls_to_its_last_entry(
        self, app, window, populated_book, monkeypatch
    ):
        view, calls = self._register(app, window, populated_book, monkeypatch)
        handle = app.db.get_account_by_name("Assets:Checking Account").handle
        view.show_account(handle)
        count = view.column_view.get_model().get_n_items()
        assert count > 1
        assert calls == [count - 1]
        # Rows stay in date order, oldest first.
        dates = [row.transaction.post_date for row in view._rows]
        assert dates == sorted(dates)

    def test_an_unrelated_repaint_keeps_the_readers_place(
        self, app, window, populated_book, monkeypatch
    ):
        view, calls = self._register(app, window, populated_book, monkeypatch)
        handle = app.db.get_account_by_name("Assets:Checking Account").handle
        view.show_account(handle)
        calls.clear()
        view.refresh()
        assert calls == []

    def test_a_blank_row_entry_scrolls_to_the_new_end(
        self, app, window, populated_book, monkeypatch
    ):
        view, calls = self._register(app, window, populated_book, monkeypatch)
        handle = app.db.get_account_by_name("Assets:Checking Account").handle
        view.show_account(handle)
        calls.clear()
        view.blank.description.set_text("New entry")
        view.blank.transfer.set_selected(1)
        view.blank.decrease.set_text("5.00")
        assert view.blank.commit() is True
        view.flush_refresh()
        assert calls == [view.column_view.get_model().get_n_items() - 1]


class TestBlankEntryRow:
    """#158: a GnuCash-style blank row at the bottom replaces quick entry."""

    def _register(self, app, window, populated_book, name="Assets:Checking Account"):
        app.open_book(populated_book)
        window.open_register(app.db.get_account_by_name(name).handle)
        return window._views["register"]

    @staticmethod
    def _last_payload(view):
        from breadsched.gui.views._base import unwrap

        model = view.column_view.get_model()
        return unwrap(model.get_item(model.get_n_items() - 1))

    def test_the_blank_row_is_last_under_any_sort_or_filter(self, app, window, populated_book):
        from breadsched.gui.views.blank_entry import BLANK

        view = self._register(app, window, populated_book)
        assert self._last_payload(view) is BLANK
        date_column = view.column_view.get_columns().get_item(0)
        view.column_view.sort_by_column(date_column, Gtk.SortType.DESCENDING)
        view.refresh()
        assert self._last_payload(view) is BLANK
        view.filter_entry.set_text("no transaction matches this")
        view.refresh()
        model = view.column_view.get_model()
        assert model.get_n_items() == 1
        assert self._last_payload(view) is BLANK

    def test_the_blank_cells_host_the_persistent_entry_widgets(self):
        from breadsched.gui.views._base import host_widget

        entry = Gtk.Entry(text="typed")
        first, second = Gtk.Box(), Gtk.Box()
        for host in (first, second):
            host.label = Gtk.Label()
            host.append(host.label)
        assert host_widget(first, entry) is True
        assert entry.get_parent() is first
        assert not first.label.get_visible()
        # A repaint binds the blank row to another cell: the widget moves, text intact.
        assert host_widget(second, entry) is True
        assert entry.get_parent() is second
        assert host_widget(first, None) is False
        assert first.label.get_visible()
        assert entry.get_text() == "typed"

    def test_typing_one_amount_clears_the_other(self, app, window, populated_book):
        blank = self._register(app, window, populated_book).blank
        blank.increase.set_text("5.00")
        blank.decrease.set_text("3.00")
        assert blank.increase.get_text() == ""
        blank.increase.set_text("4.00")
        assert blank.decrease.get_text() == ""

    def test_an_increase_moves_the_register_account_up(self, app, window, populated_book):
        view = self._register(app, window, populated_book)
        checking = app.db.get_account_by_name("Assets:Checking Account")
        blank = view.blank
        blank.description.set_text("Paycheck")
        blank.increase.set_text("100")
        assert blank.commit() is True
        posted = next(t for t in app.db.iter_transactions() if t.description == "Paycheck")
        assert posted.split_for(checking.handle).value == Money("100")

    def test_a_chosen_payee_is_saved(self, app, window, populated_book):
        from breadsched.gen.services.payees import SavePayee, save_payee

        app.open_book(populated_book)
        payee = save_payee(app.db, SavePayee(name="Corner Grocer")).value
        view = self._register(app, window, populated_book)
        blank = view.blank
        blank.payee.set_selected([p.handle for p in blank.payees].index(payee.handle) + 1)
        blank.description.set_text("Groceries")
        blank.decrease.set_text("9.99")
        assert blank.commit() is True
        posted = next(t for t in app.db.iter_transactions() if t.description == "Groceries")
        assert posted.payee == payee.handle

    def test_an_error_keeps_what_was_typed_and_names_it(self, app, window, populated_book):
        view = self._register(app, window, populated_book)
        blank = view.blank
        before = len(list(app.db.iter_transactions()))
        blank.num.set_text("7")
        blank.decrease.set_text("12.00")
        assert blank.commit() is False
        assert "description" in view.entry_status.get_text()
        assert view.entry_status.has_css_class("negative")
        assert blank.num.get_text() == "7"
        assert blank.decrease.get_text() == "12.00"
        blank.description.set_text("Coffee")
        blank.decrease.set_text("-2")
        assert blank.commit() is False
        assert "greater than zero" in view.entry_status.get_text()
        assert len(list(app.db.iter_transactions())) == before

    def test_enter_commits_escape_clears_and_tab_moves_in_column_order(
        self, app, window, populated_book, monkeypatch
    ):
        view = self._register(app, window, populated_book)
        blank = view.blank
        focused = []
        for widget in blank.fields:
            monkeypatch.setattr(widget, "grab_focus", lambda w=widget: focused.append(w) or True)
        assert blank.handle_key(blank.date, Gdk.KEY_Tab) is True
        assert focused[-1] is blank.num
        assert blank.handle_key(blank.num, Gdk.KEY_ISO_Left_Tab) is True
        assert focused[-1] is blank.date
        assert blank.handle_key(blank.editor_button, Gdk.KEY_Tab) is True
        assert focused[-1] is blank.date  # wraps around

        blank.description.set_text("Typed then abandoned")
        blank.decrease.set_text("1.00")
        assert blank.handle_key(blank.decrease, Gdk.KEY_Escape) is True
        assert not blank.has_input()

        blank.description.set_text("Entered by keyboard")
        blank.decrease.set_text("3.00")
        assert blank.handle_key(blank.decrease, Gdk.KEY_Return) is True
        assert any(t.description == "Entered by keyboard" for t in app.db.iter_transactions())

    def test_hidden_accounts_show_the_row_insensitive_with_a_reason(
        self, app, window, populated_book
    ):
        from breadsched.gen.lib import Account, AccountType

        app.open_book(populated_book)
        archived = Account(name="Archived savings", atype=AccountType.BANK, hidden=True)
        archived.parent = app.db.root_account().handle
        with app.db.transaction("Add hidden-account example") as txn:
            app.db.add_account(archived, txn)
        window.open_register(archived.handle)
        blank = window._views["register"].blank
        assert blank.enabled is False
        assert not blank.description.get_sensitive()
        assert "Hidden" in blank.description.get_placeholder_text()
        assert blank.commit() is False

    @pytest.mark.parametrize(
        ("answer", "switched", "posted"),
        (("cancel", False, False), ("discard", True, False), ("save", True, True)),
    )
    def test_switching_accounts_with_unsaved_input_asks_first(
        self, app, window, populated_book, monkeypatch, answer, switched, posted
    ):
        from breadsched.gui.views import blank_entry

        view = self._register(app, window, populated_book)
        blank = view.blank
        start = view.account_handle
        other = app.db.get_account_by_name("Expenses:Rent").handle
        blank.description.set_text("Half typed")
        blank.decrease.set_text("4.00")
        asked = []
        choice = {"cancel": blank_entry.CANCEL, "discard": blank_entry.DISCARD}.get(
            answer, blank_entry.SAVE
        )
        monkeypatch.setattr(blank, "ask_unsaved", lambda done: (asked.append(1), done(choice)))

        view.show_account(other)

        assert asked == [1]
        assert view.account_handle == (other if switched else start)
        assert blank.has_input() is (not switched)
        assert any(t.description == "Half typed" for t in app.db.iter_transactions()) is posted

    def test_an_untouched_row_switches_without_asking(
        self, app, window, populated_book, monkeypatch
    ):
        view = self._register(app, window, populated_book)
        monkeypatch.setattr(view.blank, "ask_unsaved", lambda done: pytest.fail("asked"))
        other = app.db.get_account_by_name("Expenses:Rent").handle
        view.show_account(other)
        assert view.account_handle == other

    def test_split_opens_the_editor_prefilled_and_cancel_keeps_the_row(
        self, app, window, populated_book
    ):
        view = self._register(app, window, populated_book)
        checking = app.db.get_account_by_name("Assets:Checking Account").handle
        blank = view.blank
        transfer = blank.transfers[1].handle
        blank.transfer.set_selected(1)
        blank.num.set_text("55")
        blank.description.set_text("Needs three splits")
        blank.decrease.set_text("30.00")

        dialog = blank.open_split_editor()
        try:
            assert dialog.description_entry.get_text() == "Needs three splits"
            assert dialog.num_entry.get_text() == "55"
            by_account = {editor.account_handle: editor.value() for editor in dialog.splits}
            assert by_account == {checking: Money("-30.00"), transfer: Money("30.00")}
        finally:
            dialog.close()
        assert blank.description.get_text() == "Needs three splits"
        assert blank.decrease.get_text() == "30.00"

    def test_saving_in_the_split_editor_clears_the_row(self, app, window, populated_book):
        view = self._register(app, window, populated_book)
        blank = view.blank
        blank.transfer.set_selected(1)
        blank.description.set_text("Saved from the editor")
        blank.decrease.set_text("8.00")
        dialog = blank.open_split_editor()
        dialog._on_save(None)
        assert dialog.saved is True
        assert any(t.description == "Saved from the editor" for t in app.db.iter_transactions())
        assert not blank.has_input()

    def test_activating_the_blank_row_opens_no_editor(
        self, app, window, populated_book, monkeypatch
    ):
        view = self._register(app, window, populated_book)
        monkeypatch.setattr(view, "_open_editor", lambda *_: pytest.fail("opened"))
        last = view.column_view.get_model().get_n_items() - 1
        view._on_activated(view.column_view, last)

    def test_a_shown_register_binds_the_blank_widgets_into_its_last_row(
        self, app, window, populated_book
    ):
        """Realized for real: the persistent widgets land inside the column view."""
        view = self._register(app, window, populated_book)
        window.set_default_size(1200, 700)
        window.present()
        context = GLib.MainContext.default()
        try:
            for _ in range(200):
                context.iteration(False)
                if view.blank.description.get_ancestor(Gtk.ColumnView) is view.column_view:
                    break
            for widget in view.blank.cells.values():
                assert widget.get_ancestor(Gtk.ColumnView) is view.column_view
            view.blank.description.set_text("Survives a repaint")
            view.refresh()
            for _ in range(200):
                context.iteration(False)
            assert view.blank.description.get_ancestor(Gtk.ColumnView) is view.column_view
            assert view.blank.description.get_text() == "Survives a repaint"
            # Tab skips a column the user has hidden.
            payee_column = next(
                column for column in view.column_view.get_columns() if column.get_title() == "Payee"
            )
            payee_column.set_visible(False)
            for _ in range(200):
                context.iteration(False)
            focused = []
            view.blank.transfer.grab_focus = lambda: focused.append("transfer") or True
            view.blank.handle_key(view.blank.description, Gdk.KEY_Tab)
            assert focused == ["transfer"]
        finally:
            window.set_visible(False)


class TestBlankRowSplits:
    """#158 slice 2: "Split" expands the blank row into editable split lines."""

    def _register(self, app, window, populated_book):
        app.open_book(populated_book)
        window.open_register(app.db.get_account_by_name("Assets:Checking Account").handle)
        return window._views["register"]

    @staticmethod
    def _handle(app, name):
        return app.db.get_account_by_name(name).handle

    def _fill(self, blank, line, account, increase="", decrease="", memo=""):
        handles = [account.handle for account in blank.accounts]
        line.account.set_selected(handles.index(account) + 1)
        line.memo.set_text(memo)
        if increase:
            line.increase.set_text(increase)
        if decrease:
            line.decrease.set_text(decrease)

    def test_split_expands_the_row_into_lines_with_an_imbalance_line(
        self, app, window, populated_book
    ):
        from breadsched.gui.views.blank_entry import BLANK, ImbalanceLine, SplitLine

        view = self._register(app, window, populated_book)
        blank = view.blank
        rent = self._handle(app, "Expenses:Rent")
        blank.select_transfer(rent)
        blank.decrease.set_text("100.00")
        before = view.column_view.get_model().get_n_items()

        blank.split_toggle.set_active(True)

        assert blank.split_mode is True
        rows = blank.rows()
        assert rows[0] is BLANK and isinstance(rows[-1], ImbalanceLine)
        assert all(isinstance(row, SplitLine) for row in rows[1:-1])
        # The row's two sides carry over, plus a trailing empty line.
        assert len(blank.lines) == 3
        assert blank.line_account(blank.lines[0]) == view.account_handle
        assert blank.lines[0].decrease.get_text() == "100.00"
        assert blank.line_account(blank.lines[1]) == rent
        assert blank.lines[1].increase.get_text() == "100.00"
        assert blank.imbalance.amount.get_text() == "Balanced"
        assert view.column_view.get_model().get_n_items() == before + 4
        assert not blank.transfer.get_visible()

    def test_typing_in_the_last_line_adds_another(self, app, window, populated_book):
        view = self._register(app, window, populated_book)
        blank = view.blank
        blank.split_toggle.set_active(True)
        count = len(blank.lines)
        blank.lines[-1].memo.set_text("more")
        assert len(blank.lines) == count + 1
        assert view.column_view.get_model().get_n_items() > count

    def test_an_unbalanced_entry_is_refused_and_kept(self, app, window, populated_book):
        view = self._register(app, window, populated_book)
        blank = view.blank
        blank.description.set_text("Unbalanced")
        blank.split_toggle.set_active(True)
        self._fill(blank, blank.lines[0], view.account_handle, decrease="100.00")
        self._fill(blank, blank.lines[1], self._handle(app, "Expenses:Rent"), increase="60.00")
        assert "(40.00)" in blank.imbalance.amount.get_text()
        before = len(list(app.db.iter_transactions()))

        assert blank.commit() is False

        assert "out of balance by (40.00)" in view.entry_status.get_text()
        assert blank.lines[1].increase.get_text() == "60.00"
        assert len(list(app.db.iter_transactions())) == before

    def test_three_balanced_splits_post_with_memos(self, app, window, populated_book):
        view = self._register(app, window, populated_book)
        blank = view.blank
        rent = self._handle(app, "Expenses:Rent")
        groceries = self._handle(app, "Income:Salary")
        blank.description.set_text("Shared bill")
        blank.split_toggle.set_active(True)
        self._fill(blank, blank.lines[0], view.account_handle, decrease="100.00")
        self._fill(blank, blank.lines[1], rent, increase="60.00", memo="rent part")
        self._fill(blank, blank.lines[2], groceries, increase="40.00", memo="food part")
        assert blank.imbalance.amount.get_text() == "Balanced"

        assert blank.commit() is True

        posted = next(t for t in app.db.iter_transactions() if t.description == "Shared bill")
        assert posted.imbalance() == Money(0)
        values = {split.account: (split.value, split.memo) for split in posted.splits}
        assert values == {
            view.account_handle: (Money("-100.00"), ""),
            rent: (Money("60.00"), "rent part"),
            groceries: (Money("40.00"), "food part"),
        }
        assert blank.split_mode is False
        assert blank.lines == []
        assert not blank.has_input()

    def test_splits_must_touch_this_register(self, app, window, populated_book):
        view = self._register(app, window, populated_book)
        blank = view.blank
        blank.description.set_text("Elsewhere")
        blank.split_toggle.set_active(True)
        self._fill(blank, blank.lines[0], self._handle(app, "Expenses:Rent"), decrease="5")
        self._fill(blank, blank.lines[1], self._handle(app, "Income:Salary"), increase="5")
        assert blank.commit() is False
        assert "this register's account" in view.entry_status.get_text()

    def test_collapsing_keeps_two_splits_and_refuses_more(self, app, window, populated_book):
        view = self._register(app, window, populated_book)
        blank = view.blank
        rent = self._handle(app, "Expenses:Rent")
        blank.split_toggle.set_active(True)
        self._fill(blank, blank.lines[0], view.account_handle, decrease="30.00")
        self._fill(blank, blank.lines[1], rent, increase="30.00")

        blank.split_toggle.set_active(False)

        assert blank.split_mode is False
        assert blank.decrease.get_text() == "30.00"
        assert blank.transfer_handle() == rent

        blank.split_toggle.set_active(True)
        self._fill(blank, blank.lines[2], self._handle(app, "Income:Salary"), increase="1")
        blank.split_toggle.set_active(False)
        assert blank.split_mode is True
        assert blank.split_toggle.get_active() is True
        assert "two remain" in view.entry_status.get_text()

    def test_a_multi_split_match_is_proposed_as_lines(self, app, window, populated_book):
        view = self._register(app, window, populated_book)
        blank = view.blank
        rent = self._handle(app, "Expenses:Rent")
        groceries = self._handle(app, "Income:Salary")
        blank.description.set_text("Warehouse club")
        blank.split_toggle.set_active(True)
        self._fill(blank, blank.lines[0], view.account_handle, decrease="90.00")
        self._fill(blank, blank.lines[1], rent, increase="50.00")
        self._fill(blank, blank.lines[2], groceries, increase="40.00")
        assert blank.commit() is True

        blank.description.set_text("Warehouse club")
        suggestion = blank.propose()

        assert suggestion is not None
        assert blank.split_mode is True
        accounts = [blank.line_account(line) for line in blank.lines if line.has_input()]
        assert sorted(accounts) == sorted([view.account_handle, rent, groceries])
        assert blank.imbalance.amount.get_text() == "Balanced"
        assert "3 splits" in view.entry_status.get_text()

    def test_tab_walks_the_split_lines_in_order(self, app, window, populated_book, monkeypatch):
        view = self._register(app, window, populated_book)
        blank = view.blank
        blank.split_toggle.set_active(True)
        first = blank.lines[0]
        focused = []
        monkeypatch.setattr(first.account, "grab_focus", lambda: focused.append(1) or True)
        assert blank.handle_key(first.memo, Gdk.KEY_Tab) is True
        assert focused == [1]
        assert first.memo in blank.fields and blank.transfer not in blank.fields

    def test_the_editor_receives_every_split_line(self, app, window, populated_book):
        view = self._register(app, window, populated_book)
        blank = view.blank
        rent = self._handle(app, "Expenses:Rent")
        groceries = self._handle(app, "Income:Salary")
        blank.description.set_text("Into the editor")
        blank.split_toggle.set_active(True)
        self._fill(blank, blank.lines[0], view.account_handle, decrease="10.00")
        self._fill(blank, blank.lines[1], rent, increase="6.00", memo="six")
        self._fill(blank, blank.lines[2], groceries, increase="4.00")
        dialog = blank.open_split_editor()
        try:
            got = {
                editor.account_handle: (editor.value(), editor.memo.get_text())
                for editor in dialog.splits
            }
            assert got == {
                view.account_handle: (Money("-10.00"), ""),
                rent: (Money("6.00"), "six"),
                groceries: (Money("4.00"), ""),
            }
        finally:
            dialog.close()

    def test_escape_clears_split_mode(self, app, window, populated_book):
        view = self._register(app, window, populated_book)
        blank = view.blank
        blank.split_toggle.set_active(True)
        blank.lines[0].memo.set_text("typed")
        assert blank.handle_key(blank.lines[0].memo, Gdk.KEY_Escape) is True
        assert blank.split_mode is False
        assert not blank.has_input()
        assert view.blank.rows() == [view.blank.rows()[0]]

    def test_shown_split_lines_are_bound_into_the_register(self, app, window, populated_book):
        view = self._register(app, window, populated_book)
        window.set_default_size(1200, 700)
        window.present()
        context = GLib.MainContext.default()
        try:
            view.blank.split_toggle.set_active(True)
            for _ in range(300):
                context.iteration(False)
            for line in view.blank.lines:
                for widget in line.fields:
                    assert widget.get_ancestor(Gtk.ColumnView) is view.column_view
            assert view.blank.imbalance.amount.get_ancestor(Gtk.ColumnView) is view.column_view
        finally:
            window.set_visible(False)


class TestEditorKeepsQuantity:
    """#166: changing an amount in the editor keeps quantity equal to value."""

    def test_an_edited_amount_updates_same_currency_quantities(self, app, window, populated_book):
        from breadsched.gui.dialogs.transaction_dialog import TransactionDialog

        app.open_book(populated_book)
        rent = next(t for t in app.db.iter_transactions() if t.description == "Rent")
        dialog = TransactionDialog(window, app.db, transaction=rent)
        try:
            for editor in dialog.splits:
                value = editor.value()
                editor.amount.set_text("-1900.00" if value < 0 else "1900.00")
            dialog._on_save(None)
        finally:
            dialog.destroy()
        stored = app.db.get_transaction(rent.handle)
        assert {split.value for split in stored.splits} == {Money("1900"), Money("-1900")}
        for split in stored.splits:
            assert split.quantity == split.value


class TestEditInPlace:
    """#158 slice 3: edit an existing transaction in its own register row."""

    def _register(self, app, window, populated_book):
        app.open_book(populated_book)
        window.open_register(app.db.get_account_by_name("Assets:Checking Account").handle)
        return window._views["register"]

    @staticmethod
    def _select(view, description):
        from breadsched.gui.views._base import unwrap

        selection = view.column_view.get_model()
        for index in range(selection.get_n_items()):
            item = selection.get_item(index)
            if isinstance(item, Gtk.TreeListRow) and item.get_depth() == 0:
                if unwrap(item).transaction.description == description:
                    selection.set_selected(index)
                    return unwrap(item)
        raise AssertionError(description)

    def _edit(self, view, description):
        row = self._select(view, description)
        assert view.edit_selected_in_place() is True
        return row

    def test_f2_loads_a_two_split_transaction_into_its_row(self, app, window, populated_book):
        view = self._register(app, window, populated_book)
        row = self._edit(view, "Rent")
        editor = view.editor
        assert editor is not None and editor.split_mode is False
        assert editor.description.get_text() == "Rent"
        assert editor.date.get_text() == row.transaction.post_date.isoformat()
        assert editor.decrease.get_text() == "1,800.00" or editor.decrease.get_text() == "1800.00"
        assert editor.transfer_handle() == app.db.get_account_by_name("Expenses:Rent").handle
        assert view._blank_widget(row, "Description") is editor.description
        assert editor.has_input() is False  # nothing changed yet

    def test_saving_keeps_handles_notes_memos_and_purposes(self, app, window, populated_book):
        from breadsched.gen.lib.transaction import PlanningFlowKind

        app.open_book(populated_book)
        rent = next(t for t in app.db.iter_transactions() if t.description == "Rent")
        rent.notes = "Lease 12B"
        expense = app.db.get_account_by_name("Expenses:Rent").handle
        for split in rent.splits:
            split.memo = "kept memo" if split.account == expense else split.memo
            if split.account == expense:
                split.planning_flow = PlanningFlowKind.RETIREMENT_SAVING
        with app.db.transaction("Fixture") as txn:
            app.db.commit_transaction(rent, txn)
        view = self._register(app, window, populated_book)
        self._edit(view, "Rent")
        editor = view.editor
        editor.description.set_text("Rent, corrected")
        editor.decrease.set_text("1850.00")

        assert editor.commit() is True

        stored = app.db.get_transaction(rent.handle)
        assert stored.description == "Rent, corrected"
        assert stored.notes == "Lease 12B"
        assert {s.handle for s in stored.splits} == {s.handle for s in rent.splits}
        mine = next(s for s in stored.splits if s.account == expense)
        assert mine.value == Money("1850")
        assert mine.quantity == Money("1850")
        assert mine.memo == "kept memo"
        assert mine.planning_flow is PlanningFlowKind.RETIREMENT_SAVING
        assert view.editor is None
        assert "Saved changes" in view.entry_status.get_text()

    def test_escape_cancels_without_writing(self, app, window, populated_book):
        view = self._register(app, window, populated_book)
        row = self._edit(view, "Rent")
        editor = view.editor
        editor.description.set_text("Never saved")
        assert editor.handle_key(editor.description, Gdk.KEY_Escape) is True
        assert view.editor is None
        assert app.db.get_transaction(row.transaction.handle).description == "Rent"

    def test_a_split_transaction_edits_as_lines_and_keeps_split_handles(
        self, app, window, populated_book
    ):
        view = self._register(app, window, populated_book)
        row = self._edit(view, "Supermarket")
        editor = view.editor
        assert editor.split_mode is True
        stored = app.db.get_transaction(row.transaction.handle)
        filled = [line for line in editor.lines if line.handle is not None]
        assert {line.handle for line in filled} == {s.handle for s in stored.splits}
        assert filled[0].handle == row.split.handle  # this register's split first
        view.flush_refresh()
        assert list(view._edit_children)  # the lines sit under the row
        # Move 5.00 from one expense line to the other.
        others = filled[1:]
        first, second = others[0], others[1]
        first_value = editor.line_value(first)
        second_value = editor.line_value(second)
        target = first.increase if first_value > 0 else first.decrease
        target.set_text(abs(first_value + Money("5")).format())
        target = second.increase if second_value > 0 else second.decrease
        target.set_text(abs(second_value - Money("5")).format())
        assert editor.imbalance.amount.get_text() == "Balanced"

        assert editor.commit() is True

        after = app.db.get_transaction(row.transaction.handle)
        assert {s.handle for s in after.splits} == {s.handle for s in stored.splits}
        values = {s.handle: s.value for s in after.splits}
        assert values[first.handle] == first_value + Money("5")
        assert values[second.handle] == second_value - Money("5")

    def test_switching_accounts_with_an_unsaved_edit_asks_first(
        self, app, window, populated_book, monkeypatch
    ):
        from breadsched.gui.views import blank_entry

        view = self._register(app, window, populated_book)
        self._edit(view, "Rent")
        editor = view.editor
        editor.description.set_text("Changed")
        asked = []
        monkeypatch.setattr(
            editor, "ask_unsaved", lambda done: (asked.append(1), done(blank_entry.CANCEL))
        )
        start = view.account_handle
        view.show_account(app.db.get_account_by_name("Expenses:Rent").handle)
        assert asked == [1]
        assert view.account_handle == start
        assert view.editor is editor

    def test_the_pencil_hands_the_edit_to_the_full_editor(
        self, app, window, populated_book, monkeypatch
    ):
        view = self._register(app, window, populated_book)
        row = self._edit(view, "Rent")
        opened = []
        monkeypatch.setattr(view, "_open_editor", lambda transaction: opened.append(transaction))
        view.editor.open_split_editor()
        assert [t.handle for t in opened] == [row.transaction.handle]
        assert view.editor is None

    def test_edit_in_place_is_a_register_action(self, app, window, populated_book):
        app.open_book(populated_book)
        assert window.lookup_action("register-edit-in-place") is not None

    def test_a_shown_edit_hosts_its_widgets_in_the_row(self, app, window, populated_book):
        view = self._register(app, window, populated_book)
        window.set_default_size(1200, 700)
        window.present()
        context = GLib.MainContext.default()
        try:
            self._edit(view, "Supermarket")
            for _ in range(300):
                context.iteration(False)
            editor = view.editor
            assert editor.description.get_ancestor(Gtk.ColumnView) is view.column_view
            for line in editor.lines:
                assert line.memo.get_ancestor(Gtk.ColumnView) is view.column_view
            # The blank row is still its own, separate row.
            assert view.blank.description.get_ancestor(Gtk.ColumnView) is view.column_view
        finally:
            window.set_visible(False)


class TestGnuCashWritebackDialog:
    """#174: preview, tick, and write simple changes back to the GnuCash book."""

    def test_the_dialog_writes_only_the_ticked_changes(self, app, tmp_path, gnucash_sqlite_path):
        import sqlite3

        from breadsched.gen.db.sqlite import DbSQLite
        from breadsched.gen.services.imports import ImportBook, import_book

        path = tmp_path / "linked.breadsched"
        setup = DbSQLite()
        setup.load(str(path))
        assert import_book(setup, ImportBook(source=gnucash_sqlite_path.path, notify=False)).ok
        setup.close()
        app.open_book(str(path))
        db = app.db
        edited = []
        for description in ("Rent", "Payroll deposit"):
            transaction = next(t for t in db.iter_transactions() if t.description == description)
            transaction.description = f"{description} (edited)"
            with db.transaction("Edit") as txn:
                db.commit_transaction(transaction, txn)
            edited.append(transaction.handle)

        dialog = app.on_gnucash_writeback()
        try:
            assert [handle for handle, _check in dialog.checks] == sorted(
                edited, key=lambda h: db.get_transaction(h).post_date
            )
            assert dialog.write() is None  # nothing ticked
            rent_check = next(check for handle, check in dialog.checks if handle == edited[0])
            rent_check.set_active(True)
            assert dialog.write() == 1
            assert "Wrote 1 transaction" in dialog.status.get_text()
            [handle] = [h for h, _c in dialog.checks]
            assert handle == edited[1]  # the unticked edit is still pending
            dialog.keep.set_value(3)
            assert db.get_metadata("gnucash.writeback.keep_backups") == 3
        finally:
            dialog.destroy()
        conn = sqlite3.connect(gnucash_sqlite_path.path)
        rows = dict(conn.execute("SELECT guid, description FROM transactions").fetchall())
        conn.close()
        assert rows[edited[0]] == "Rent (edited)"
        assert rows[edited[1]] == "Payroll deposit"

    def test_the_dialog_explains_a_book_that_was_not_imported(self, app, window, tmp_path):
        from breadsched.gen.db.sqlite import DbSQLite

        path = tmp_path / "native.breadsched"
        native = DbSQLite()
        native.load(str(path))
        native.close()
        app.open_book(str(path))
        dialog = app.on_gnucash_writeback()
        try:
            assert "Import the GnuCash SQLite book" in dialog.summary.get_text()
            assert dialog.write_button.get_sensitive() is False
        finally:
            dialog.destroy()


class TestDialogsFitTheScreen:
    """Dialog audit (#148, like #140): no dialog demands more than a laptop screen.

    A book with very long account names, descriptions, and memos, plus many
    payees and a many-split transaction, must not raise any dialog's minimum
    size past what a 1366x768 screen shows with its title bar and panel.
    """

    MAX_WIDTH = 800
    MAX_HEIGHT = 600

    @pytest.fixture
    def stressed(self, app, populated_book):
        from breadsched.gen.lib import Account, AccountType, Split, Transaction
        from breadsched.gen.services.payees import SavePayee, save_payee

        app.open_book(populated_book)
        db = app.db
        long_account = Account(name="Very long account name " * 12, atype=AccountType.EXPENSE)
        long_account.parent = db.root_account().handle
        checking = db.get_account_by_name("Assets:Checking Account")
        long_entry = Transaction.simple(
            date(2026, 2, 1),
            "Long description " * 20,
            long_account.handle,
            checking.handle,
            Money("5"),
        )
        long_entry.splits[0].memo = "long memo " * 30
        many = Transaction(post_date=date(2026, 3, 1), description="Many splits")
        for _ in range(40):
            many.add_split(Split(long_account.handle, Money("1")))
        many.add_split(Split(checking.handle, Money("-40")))
        with db.transaction("Stress fixture") as txn:
            db.add_account(long_account, txn)
            db.add_transaction(long_entry, txn)
            db.add_transaction(many, txn)
        for index in range(150):
            save_payee(db, SavePayee(name=f"Payee {index}"))
        return db, long_account, checking, many

    DIALOGS = (
        ("account_dialog", "AccountDialog", "account"),
        ("csv_import_dialog", "CsvImportDialog", None),
        ("dashboard_dialog", "DashboardDialog", None),
        ("exchange_rate_dialog", "ExchangeRateDialog", None),
        ("fsa_claims_dialog", "FsaClaimsDialog", None),
        ("gnucash_writeback_dialog", "GnuCashWritebackDialog", None),
        ("historical_estimates_dialog", "HistoricalEstimatesDialog", None),
        ("import_dialog", "ImportDialog", None),
        ("loan_dialog", "LoanDialog", None),
        ("payee_dialog", "PayeesDialog", None),
        ("receivables_dialog", "ReceivablesDialog", None),
        ("reconciliation_dialog", "ReconciliationDialog", "checking"),
        ("rules_dialog", "RulesDialog", None),
        ("scenario_dialog", "SaveScenarioDialog", "scenario"),
        ("scenario_manager_dialog", "ScenarioManagerDialog", "manager"),
        ("scenario_schedule_dialog", "ScenarioScheduleDialog", "scenario"),
        ("schedule_dialog", "ScheduleDialog", None),
        ("security_price_dialog", "SecurityPriceDialog", None),
        ("transaction_dialog", "TransactionDialog", "transaction"),
    )

    @pytest.mark.parametrize(("module", "name", "extra"), DIALOGS)
    def test_the_minimum_size_stays_on_a_laptop_screen(self, window, stressed, module, name, extra):
        import importlib

        from breadsched.gen.lib.scenario import Scenario

        db, long_account, checking, many = stressed
        args = {
            None: (),
            "account": (long_account,),
            "checking": (checking,),
            "scenario": (Scenario(name="Stress"),),
            "manager": (window,),
        }.get(extra, ())
        cls = getattr(importlib.import_module(f"breadsched.gui.dialogs.{module}"), name)
        dialog = (
            cls(window, db, transaction=many) if extra == "transaction" else cls(window, db, *args)
        )
        try:
            child = dialog.get_child()
            width = child.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
            height = child.measure(Gtk.Orientation.VERTICAL, -1)[0]
            assert width <= self.MAX_WIDTH, f"{name} needs {width}px of width"
            assert height <= self.MAX_HEIGHT, f"{name} needs {height}px of height"
        finally:
            dialog.destroy()

    def test_a_scrolled_dialog_keeps_its_buttons_outside_the_scroller(self, window, stressed):
        from breadsched.gen.lib.scenario import Scenario
        from breadsched.gui.dialogs.scenario_schedule_dialog import ScenarioScheduleDialog

        db = stressed[0]
        dialog = ScenarioScheduleDialog(window, db, Scenario(name="Stress"))
        try:
            outer = dialog.get_child()
            assert isinstance(outer.get_first_child(), Gtk.ScrolledWindow)
            footer = outer.get_last_child()
            assert not isinstance(footer, Gtk.ScrolledWindow)
            assert any(isinstance(w, Gtk.Button) for w in _descendants(footer))
        finally:
            dialog.destroy()


def summary_owed(db):
    from breadsched.gen.services.receivables import list_receivables

    [summary] = list_receivables(db).value
    return summary.remaining


class TestReceivablesDialog:
    """Reimbursable expenses in GTK, entirely through the shared service."""

    def _book(self, app, populated_book):
        app.open_book(populated_book)
        db = app.db
        rent_txn = next(t for t in db.iter_transactions() if t.description == "Rent")
        rent = db.get_account_by_name("Expenses:Rent")
        cost = next(s for s in rent_txn.splits if s.account == rent.handle)
        return db, rent_txn, rent, cost

    def _refund(self, db, rent, amount="300"):
        from breadsched.gen.lib import Transaction

        checking = db.get_account_by_name("Assets:Checking Account")
        refund = Transaction.simple(
            date(2026, 2, 10), "Insurer refund", checking.handle, rent.handle, Money(amount)
        )
        with db.transaction("Refund fixture") as txn:
            db.add_transaction(refund, txn)
        return refund

    def test_tracking_a_register_transaction_links_its_expense(self, app, window, populated_book):
        from breadsched.gui.views._base import unwrap

        db, rent_txn, _rent, cost = self._book(app, populated_book)
        window.open_register(db.get_account_by_name("Assets:Checking Account").handle)
        view = window._views["register"]
        selection = view.column_view.get_model()
        for index in range(selection.get_n_items()):
            item = selection.get_item(index)
            if isinstance(item, Gtk.TreeListRow) and item.get_depth() == 0:
                if unwrap(item).transaction.description == "Rent":
                    selection.set_selected(index)
        dialog = view.track_selected_reimbursable()
        try:
            assert dialog.description_entry.get_text() == "Rent"
            assert dialog.incurred_entry.get_text() == rent_txn.post_date.isoformat()
            dialog.payer_entry.set_text("Employer relocation")
            receivable = dialog.save()
            assert receivable is not None
            stored = db.get_receivable(receivable.handle)
            assert [(link.transaction, link.split) for link in stored.expenses] == [
                (rent_txn.handle, cost.handle)
            ]
            assert "expense is linked" in dialog.status.get_text()
        finally:
            dialog.destroy()

    def test_an_fsa_claim_is_linked_to_cover_the_rest_of_a_receivable(
        self, app, window, populated_book
    ):
        # Issue #192: linked only from the claim; the FSA share waits for the EOB.
        from breadsched.gen.lib.fsa_claim import FsaClaimSplitLink
        from breadsched.gen.services.receivables import (
            SaveReceivable,
            attach_expense_split,
            save_receivable,
        )
        from breadsched.gui.dialogs.fsa_claims_dialog import FsaClaimsDialog
        from breadsched.gui.dialogs.receivables_dialog import ReceivablesDialog

        db, rent_txn, _rent, cost = self._book(app, populated_book)
        receivable = save_receivable(
            db,
            SaveReceivable(
                rent_txn.post_date, "Acme Insurance", "Visit", expected_amount=Money("500")
            ),
        ).value
        assert attach_expense_split(db, receivable.handle, rent_txn.handle, cost.handle).ok
        claims = FsaClaimsDialog(window, db)
        try:
            assert claims.shared.get_visible() is False
            claims.service.set_text(rent_txn.post_date.isoformat())
            claims.provider.set_text("Clinic")
            claims.payments.set_links([FsaClaimSplitLink(rent_txn.handle, cost.handle)])
            index = [item.handle for item in claims.receivables].index(receivable.handle)
            claims.payer.set_selected(index + 1)
            claims._save(None)
            assert claims.status.get_text() == "Claim saved."
            assert db.get_fsa_claim(claims.current.handle).receivable == receivable.handle
            assert claims.shared.get_visible()
            assert "Acme Insurance pays 500.00" in claims.shared.get_text()
            assert "until the EOB is entered" in claims.shared.get_text()
        finally:
            claims.destroy()
        dialog = ReceivablesDialog(window, db)
        try:
            dialog.edit(receivable.handle)
            assert dialog.warning.get_visible() is False
            assert dialog.shared.get_visible()
            assert "Acme Insurance pays 500.00" in dialog.shared.get_text()
        finally:
            dialog.destroy()

    def test_link_dispute_write_off_and_unlink_leave_linked_transactions_alone(
        self, app, window, populated_book
    ):
        from breadsched.gen.engine.ledger import balance
        from breadsched.gen.engine.receivables import owned_postings
        from breadsched.gen.services.receivables import ReceivableStatus, list_receivables
        from breadsched.gui.dialogs.receivables_dialog import ReceivablesDialog

        db, rent_txn, rent, cost = self._book(app, populated_book)
        refund = self._refund(db, rent)
        ledger_before = [t.serialize() for t in db.iter_transactions()]
        dialog = ReceivablesDialog(window, db, expense=(rent_txn.handle, cost.handle))
        try:
            dialog.payer_entry.set_text("Acme Insurance")
            receivable = dialog.save()
            handle = receivable.handle
            assert dialog.editing == handle and dialog.detail.get_visible()
            # What is owed now sits in the default Receivable account (#170).
            held = db.get_receivable(handle).account
            assert dialog._accounts == [None, held]
            assert dialog.account_picker.get_selected() == 1
            assert balance(db, held) == summary_owed(db)
            assert dialog.warning.get_visible() is False
            credit = [c for c in dialog._credits].index((refund.handle, refund.splits[1].handle))
            dialog.credit_picker.set_selected(credit)
            assert dialog.link("reimbursement") is True
            [summary] = list_receivables(db).value
            assert summary.reimbursed == Money("300")
            assert summary.status is ReceivableStatus.PARTIAL

            dialog.dispute_date.set_text("2026-03-01")
            dialog.dispute_note.set_text("Payer says out of network")
            assert dialog.dispute() is True
            assert list_receivables(db).value[0].status is ReceivableStatus.DISPUTED
            assert dialog.clear_dispute() is True

            dialog.write_off_amount.set_text("100")
            dialog.write_off_reason.set_text("Deductible")
            assert dialog.write_off() is True
            assert list_receivables(db).value[0].written_off == Money("100")

            link = db.get_receivable(handle).reimbursements[0]
            assert dialog.unlink(link) is True
            assert db.get_receivable(handle).reimbursements == []
            owned = owned_postings(db)
            assert [
                t.serialize() for t in db.iter_transactions() if t.handle not in owned
            ] == ledger_before
        finally:
            dialog.destroy()

    def test_rejected_input_saves_nothing_and_says_why(self, app, window, populated_book):
        from breadsched.gui.dialogs.receivables_dialog import ReceivablesDialog

        db, *_ = self._book(app, populated_book)
        dialog = ReceivablesDialog(window, db)
        try:
            assert dialog.detail.get_visible() is False
            assert dialog.save() is None
            assert "who owes" in dialog.status.get_text()
            dialog.payer_entry.set_text("Acme")
            dialog.incurred_entry.set_text("yesterday")
            assert dialog.save() is None
            assert "YYYY-MM-DD" in dialog.status.get_text()
            dialog.incurred_entry.set_text("2026-01-02")
            dialog.expected_entry.set_text("-5")
            assert dialog.save() is None
            assert list(db.iter_receivables()) == []
        finally:
            dialog.destroy()

    def test_deleting_keeps_the_transactions(self, app, window, populated_book):
        from breadsched.gui.dialogs.receivables_dialog import ReceivablesDialog

        db, rent_txn, _rent, cost = self._book(app, populated_book)
        dialog = ReceivablesDialog(window, db, expense=(rent_txn.handle, cost.handle))
        try:
            dialog.payer_entry.set_text("Acme")
            dialog.save()
            assert dialog.delete() is True
            assert list(db.iter_receivables()) == []
            assert db.get_transaction(rent_txn.handle) is not None
        finally:
            dialog.destroy()

    def test_the_menu_and_register_actions_exist(self, app, window, populated_book):
        app.open_book(populated_book)
        assert app.lookup_action("receivables").get_enabled() is True
        assert window.lookup_action("register-track-reimbursable") is not None

    def test_proposed_reimbursements_link_only_when_accepted(self, app, window, populated_book):
        from breadsched.gen.services.receivables import ReceivableStatus, list_receivables
        from breadsched.gui.dialogs.receivables_dialog import ReceivablesDialog

        db, rent_txn, rent, cost = self._book(app, populated_book)
        dialog = ReceivablesDialog(window, db, expense=(rent_txn.handle, cost.handle))
        try:
            dialog.payer_entry.set_text("Acme Insurance")
            receivable = dialog.save()
            refund = self._refund(db, rent)
            dialog.refresh()
            credit = next(s for s in refund.splits if s.account == rent.handle)
            key = (receivable.handle, refund.handle, credit.handle)
            assert list(dialog.proposal_checks) == [key]
            assert db.get_receivable(receivable.handle).reimbursements == []

            dialog.proposal_checks[key].set_active(False)
            assert dialog.accept_selected().linked == 0
            assert db.get_receivable(receivable.handle).reimbursements == []

            dialog.proposal_checks[key].set_active(True)
            assert dialog.accept_selected().linked == 1
            [summary] = list_receivables(db).value
            assert summary.status is ReceivableStatus.PARTIAL
            assert dialog.proposal_checks == {}
        finally:
            dialog.destroy()

    def test_reconciling_the_deposit_account_points_at_proposals(self, app, window, populated_book):
        from breadsched.gui.dialogs.receivables_dialog import ReceivablesDialog
        from breadsched.gui.dialogs.reconciliation_dialog import ReconciliationDialog

        db, rent_txn, rent, cost = self._book(app, populated_book)
        dialog = ReceivablesDialog(window, db, expense=(rent_txn.handle, cost.handle))
        dialog.payer_entry.set_text("Acme Insurance")
        dialog.save()
        dialog.destroy()
        checking = db.get_account_by_name("Assets:Checking Account")
        quiet = ReconciliationDialog(window, db, checking)
        assert quiet.reimbursement_note is None
        quiet.destroy()

        self._refund(db, rent)
        reconcile = ReconciliationDialog(window, db, checking)
        try:
            assert reconcile.reimbursement_note.get_text().startswith(
                "1 credit looks like money back"
            )
        finally:
            reconcile.destroy()


class TestViewSpecificChrome:
    """#182: the toolbar and Actions menu are arranged around the current view."""

    def _visible_icons(self, window):
        return [key for key, button in window.view_buttons.items() if button.get_visible()]

    def test_the_current_view_has_no_icon_and_is_named_beside_its_commands(
        self, app, window, populated_book
    ):
        from breadsched.gui.viewmanager import CATEGORIES

        app.open_book(populated_book)
        keys = [key for key, _label, _icon in CATEGORIES]
        for key, label, _icon in CATEGORIES:
            window.show_category(key)
            assert self._visible_icons(window) == [k for k in keys if k != key]
            assert window.view_title.get_label() == label
            assert window.view_heading.get_visible()

    def test_the_view_commands_come_before_the_other_view_icons(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("accounts")
        order = []
        child = window.toolbar.get_first_child()
        while child is not None:
            order.append(child)
            child = child.get_next_sibling()
        assert order.index(window.view_tools) < order.index(window.view_buttons["dashboard"])
        assert order.index(window.view_heading) < order.index(window.view_tools)

    def _sections(self, app):
        menubar = app.get_menubar()
        actions = next(
            menubar.get_item_link(index, "submenu")
            for index in range(menubar.get_n_items())
            if menubar.get_item_attribute_value(index, "label", None).get_string() == "_Actions"
        )
        sections = []
        for index in range(actions.get_n_items()):
            label = actions.get_item_attribute_value(index, "label", None)
            sections.append(
                (label.get_string() if label else None, actions.get_item_link(index, "section"))
            )
        return sections

    def test_the_actions_menu_lists_the_current_views_commands_first(
        self, app, window, populated_book
    ):
        from breadsched.gui.viewmanager import VIEW_ACTIONS, view_action_name

        app.open_book(populated_book)
        window.show_category("scheduled")
        sections = self._sections(app)
        label, first = sections[0]
        assert label == "Scheduled"
        assert _menu_actions(first) == [
            f"win.{view_action_name('scheduled', item)}"
            for item in VIEW_ACTIONS["scheduled"]
            if not item.toggle
        ]
        others = _menu_actions(sections[-1][1])
        assert not any(action.startswith("win.scheduled-") for action in others)
        assert "win.plan-new-scenario" in others

        window.show_category("plan")
        label, first = self._sections(app)[0]
        assert label == "Plan" and "win.plan-new-scenario" in _menu_actions(first)
        assert "win.scheduled-new-scheduled" in _menu_actions(self._sections(app)[-1][1])


class TestTabs:
    """#183: opened views and each open register account are tabs below the toolbar."""

    def _accounts(self, app):
        checking = app.db.get_account_by_name("Assets:Checking Account")
        card = app.db.get_account_by_name("Credit Card")
        return checking.handle, card.handle

    def test_views_and_registers_open_as_tabs(self, app, window, populated_book):
        app.open_book(populated_book)
        checking, card = self._accounts(app)
        assert window.tabs == [("dashboard", "Dashboard")]
        window.show_category("accounts")
        window.open_register(checking)
        first = window._views["register"]
        window.open_register(card)
        second = window._views["register"]
        assert first is not second
        assert (first.account_handle, second.account_handle) == (checking, card)
        assert window.tabs == [
            ("dashboard", "Dashboard"),
            ("accounts", "Accounts"),
            ("register", "Checking Account"),
            ("register", "Credit Card"),
        ]
        # Reopening an account's register returns to its tab.
        window.open_register(checking)
        assert window._views["register"] is first
        assert len(window.tabs) == 4
        assert window.stack.get_visible_child_name() == "register"
        assert [tab.current for tab in window._tabs] == [False, False, True, False]

    def test_each_register_tab_keeps_its_own_place(self, app, window, populated_book):
        app.open_book(populated_book)
        checking, card = self._accounts(app)
        window.open_register(checking)
        first = window._views["register"]
        first.blank.description.set_text("Half typed")
        window.open_register(card)
        # Switching tabs never discards another register's typing.
        window._on_tab_clicked(window._tabs[-2])
        assert window._views["register"] is first
        assert first.blank.description.get_text() == "Half typed"

    def test_closing_tabs_selects_a_neighbour_and_asks_about_typing(
        self, app, window, populated_book, monkeypatch
    ):
        app.open_book(populated_book)
        checking, card = self._accounts(app)
        window.open_register(checking)
        window.open_register(card)
        card_tab = window._tabs[-1]
        asked = []
        register = card_tab.register
        register.blank.description.set_text("Unsaved")
        monkeypatch.setattr(register, "confirm_leave", lambda proceed: asked.append(proceed))
        window.close_tab(card_tab)
        assert len(asked) == 1 and card_tab in window._tabs
        asked[0]()
        assert card_tab not in window._tabs and register not in window._registers
        assert window._views["register"].account_handle == checking
        assert window.stack.get_visible_child_name() == "register"

        for tab in list(window._tabs):
            window.close_tab(tab)
        # Closing the last tab returns to the Dashboard.
        assert window.tabs == [("dashboard", "Dashboard")]
        assert window.stack.get_visible_child_name() == "dashboard"

    def test_a_new_book_starts_with_fresh_tabs(self, app, window, populated_book):
        app.open_book(populated_book)
        checking, _card = self._accounts(app)
        window.open_register(checking)
        app.open_book(populated_book)
        assert window.tabs == [("dashboard", "Dashboard")]
        assert window._registers == []
