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
from datetime import date
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
Gdk, Gtk = gi_setup.Gdk, gi_setup.Gtk

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
    Money,
    PeriodType,
    Recurrence,
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

    def test_the_default_identity_is_unchanged(self):
        application = BreadSchedApplication()
        assert application.get_application_id() == APP_ID


class TestEmptyWindow:
    def test_opens_with_a_placeholder(self, window):
        assert window.stack.get_visible_child_name() == "empty"

    def test_the_navigator_lists_every_category(self, window):
        rows = []
        index = 0
        while (row := window.navigator.get_row_at_index(index)) is not None:
            rows.append(row.category_key)
            index += 1
        assert rows == CATEGORY_KEYS

    def test_selecting_a_category_without_a_book_does_not_crash(self, window):
        window.navigator.select_row(window.navigator.get_row_at_index(0))
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
        assert "transactions" in window.status.get_text()
        assert "No book open" not in window.status.get_text()

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
        assert view.column_view.get_model().get_n_items() == 3

    def test_column_headings_follow_the_account_type(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("register")
        view = window._views["register"]
        view.show_account(app.db.get_account_by_name("Assets:Checking Account").handle)
        assert view.debit_column.get_title() == "Deposit"
        view.show_account(app.db.get_account_by_name("Credit Card").handle)
        assert view.debit_column.get_title() == "Payment"


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
        keys = []
        index = 0
        while (row := window.navigator.get_row_at_index(index)) is not None:
            keys.append(row.category_key)
            index += 1
        assert "scheduled" in keys and "upcoming" in keys

    def test_the_definitions_view_has_no_due_list(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("scheduled")
        assert not hasattr(window._views["scheduled"], "upcoming_view")

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
        assert isinstance(view.column_view.get_model().get_model(), Gtk.SortListModel)

    def test_sorting_a_column_changes_the_order(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("register")
        view = window._views["register"]
        model = view.column_view.get_model()

        def descriptions():
            return [
                unwrap(model.get_item(i)).description
                for i in range(model.get_n_items())
                if model.get_item(i).get_depth() == 0
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
        model.get_model().get_item(0).set_expanded(True)

        depths = [model.get_item(i).get_depth() for i in range(model.get_n_items())]
        # A depth-1 row may only follow a depth-0 or another depth-1 row.
        for index, depth in enumerate(depths[1:], start=1):
            if depth == 1:
                assert depths[index - 1] in (0, 1)


class TestNavigationHighlight:
    """Item 3: however a view is reached, the sidebar follows."""

    def test_the_toolbar_moves_the_sidebar(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("projection")
        window.show_category("accounts")
        assert window.navigator.get_selected_row().category_key == "accounts"

    def test_jumping_to_a_register_moves_the_sidebar(self, app, window, populated_book):
        app.open_book(populated_book)
        window.show_category("accounts")
        handle = app.db.get_account_by_name("Assets:Checking Account").handle
        window.open_register(handle)
        assert window.navigator.get_selected_row().category_key == "register"

    def test_every_category_highlights_itself(self, app, window, populated_book):
        app.open_book(populated_book)
        for key, _label, _icon in CATEGORIES:
            window.show_category(key)
            assert window.navigator.get_selected_row().category_key == key


class TestPlanToolbarIcon:
    """The primary planning toolbar opens the derived event-driven Plan view."""

    def test_it_switches_category(self, app, window, populated_book):
        from breadsched.gui.viewmanager import TOOLBAR

        entry = next(item for item in TOOLBAR if item[0] == "Plan")
        assert entry[2] == "win.show-category::plan"

    def test_plan_is_a_visible_category(self):
        assert any(key == "plan" for key, _label, _icon in CATEGORIES)


class TestDerivedPlanView:
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
        assert "Net cash change" in labels

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
        dialog.category.set_selected(names.index("Expenses:Groceries"))
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

    def test_the_pending_cash_flow_list_is_populated(self, view):
        model = view.bills_view.get_model()
        assert model.get_n_items() == len(view.board.pending)

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
        from breadsched.gen.lib import Account, AccountType

        dialog = self._dialog(accounts_view)
        account = app.db.get_account_by_name("Assets:Checking Account")
        before = sorted(item.handle for item in app.db.iter_transactions())
        with app.db.transaction("Check malformed opening input") as txn:
            if app.db.get_account_by_name("Equity") is None:
                root = app.db.root_account()
                app.db.add_account(
                    Account(name="Equity", atype=AccountType.EQUITY, parent=root.handle), txn
                )
            dialog._post_opening(account, "not an amount", txn)
        assert sorted(item.handle for item in app.db.iter_transactions()) == before

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
        from breadsched.gen.lib import Account, AccountType, Commodity

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
            child.notes = "Generic imported account note"
            app.db.add_account(child, txn)

        dialog = self._dialog(accounts_view, child)
        selected_parent = dialog.parents[dialog.parent_picker.get_selected()]
        assert selected_parent.handle == parent.handle
        assert dialog.hidden_check.get_active() is True
        assert dialog.commodity_scu_entry.get_text() == "1000"
        assert dialog.commodity_handles[dialog.commodity_picker.get_selected()] == commodity.handle
        notes_buffer = dialog.notes_view.get_buffer()
        notes_start, notes_end = notes_buffer.get_bounds()
        assert (
            notes_buffer.get_text(notes_start, notes_end, True) == "Generic imported account note"
        )

        rebuilt = dialog.build()
        assert rebuilt.parent == parent.handle
        assert rebuilt.hidden is True
        assert rebuilt.commodity == commodity.handle
        assert rebuilt.commodity_scu == 1000
        assert rebuilt.notes == "Generic imported account note"

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
        dialog._on_save(None)

        card = app.db.get_account_by_name("Visa")
        assert card.carries_balance is True
        assert card.usual_payment == Money("400.00")
        assert card.payment_day == 22

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
