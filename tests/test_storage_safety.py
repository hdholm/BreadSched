"""The storage safety net: the writer lock, backups and restores, and commit checks.

These guard every book-format migration, so each refusal is tested for what it
leaves behind: no half-installed file, no stray temporary copy, and no change to
the stored book.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from breadsched.gen.db import backups
from breadsched.gen.db.base import DbError
from breadsched.gen.db.book_lock import BookWriterLock
from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.db.verification import BookIssue, BookVerification
from breadsched.gen.lib import (
    Account,
    AccountType,
    Commodity,
    CommodityPrice,
    Money,
    PeriodType,
    Recurrence,
    Scenario,
    ScheduledSplit,
    ScheduledTransaction,
    Split,
    Transaction,
)
from breadsched.gen.lib.fsa_claim import FsaClaim, FsaClaimAllocation, FsaClaimSplitLink
from breadsched.gen.lib.receivable import Receivable, ReceivableSplitLink
from breadsched.gen.lib.reconciliation import Reconciliation
from breadsched.gen.lib.savings_goal import SavingsGoal

MISSING = "0" * 32


# --------------------------------------------------------------------- writer lock


class TestWriterLock:
    def test_a_live_writer_on_this_host_keeps_the_book(self, tmp_path):
        book = str(tmp_path / "book.breadsched")
        held = BookWriterLock.acquire(book)
        try:
            with pytest.raises(DbError, match=f"PID {os.getpid()} on "):
                BookWriterLock.acquire(book)
        finally:
            held.release()
        assert not BookWriterLock.lock_path(book).exists()

    def test_an_unreadable_lock_names_no_owner_and_is_never_replaced(self, tmp_path):
        book = str(tmp_path / "book.breadsched")
        lock = BookWriterLock.lock_path(book)
        lock.write_text("not json", encoding="utf-8")
        with pytest.raises(DbError, match="by another process"):
            BookWriterLock.acquire(book)
        assert lock.read_text(encoding="utf-8") == "not json"

    def test_a_lock_from_another_host_is_never_taken_over(self, tmp_path):
        book = str(tmp_path / "book.breadsched")
        lock = BookWriterLock.lock_path(book)
        lock.write_text(json.dumps({"pid": 1, "host": "elsewhere", "token": "x"}))
        with pytest.raises(DbError, match="PID 1 on elsewhere"):
            BookWriterLock.acquire(book)

    def test_a_stale_lock_is_replaced_even_if_it_vanishes_first(self, tmp_path, monkeypatch):
        import socket

        book = str(tmp_path / "book.breadsched")
        lock = BookWriterLock.lock_path(book)
        lock.write_text(json.dumps({"pid": 999999, "host": socket.gethostname(), "token": "x"}))

        def dead_and_cleaned_up(pid):
            # The other writer's own cleanup removes the file between our read and
            # our removal; acquiring must still succeed.
            lock.unlink()
            return False

        monkeypatch.setattr(BookWriterLock, "pid_is_alive", staticmethod(dead_and_cleaned_up))
        held = BookWriterLock.acquire(book)
        assert json.loads(lock.read_text())["token"] == held.token
        held.release()

    def test_release_leaves_a_lock_another_writer_now_holds(self, tmp_path):
        book = str(tmp_path / "book.breadsched")
        held = BookWriterLock.acquire(book)
        held.path.write_text(json.dumps({"token": "someone else"}), encoding="utf-8")
        held.release()
        assert held.path.exists()
        held.path.write_text("damaged", encoding="utf-8")
        held.release()
        assert held.path.read_text(encoding="utf-8") == "damaged"

    def test_release_tolerates_a_lock_removed_underneath_it(self, tmp_path, monkeypatch):
        held = BookWriterLock.acquire(str(tmp_path / "book.breadsched"))

        def gone(self, missing_ok=False):
            raise FileNotFoundError(str(self))

        monkeypatch.setattr(Path, "unlink", gone)
        held.release()  # no error
        monkeypatch.undo()
        held.release()
        assert not held.path.exists()

    def test_in_memory_books_take_no_lock(self):
        assert BookWriterLock.acquire(":memory:") is None

    @pytest.mark.parametrize(
        ("raised", "alive"),
        [(ProcessLookupError, False), (PermissionError, True), (OSError, True), (None, True)],
    )
    def test_posix_liveness_is_conservative(self, monkeypatch, raised, alive):
        def probe(pid, signal):
            if raised is not None:
                raise raised()

        monkeypatch.setattr(os, "name", "posix")
        monkeypatch.setattr(os, "kill", probe)
        assert BookWriterLock.pid_is_alive(1234) is alive
        assert BookWriterLock.pid_is_alive(0) is False
        assert BookWriterLock.pid_is_alive(-5) is False

    @pytest.mark.parametrize(
        ("handle", "last_error", "alive"),
        [(7, 0, True), (0, 87, False), (0, 5, True)],
    )
    def test_windows_liveness_never_signals_the_process(
        self, monkeypatch, handle, last_error, alive
    ):
        """On Windows signal 0 is Ctrl+C; liveness opens a process handle instead."""
        import ctypes

        closed = []

        class Kernel32:
            def OpenProcess(self, access, inherit, pid):  # noqa: N802 - Win32 name
                return handle

            def CloseHandle(self, opened):  # noqa: N802 - Win32 name
                closed.append(opened)

        monkeypatch.setattr(os, "name", "nt")
        monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: Kernel32(), raising=False)
        monkeypatch.setattr(ctypes, "get_last_error", lambda: last_error, raising=False)
        monkeypatch.setattr(os, "kill", lambda *a: pytest.fail("signalled the process"))
        assert BookWriterLock.pid_is_alive(1234) is alive
        assert closed == ([handle] if handle else [])


# -------------------------------------------------------------- backup and restore


def _book(tmp_path, name="book.breadsched") -> Path:
    path = tmp_path / name
    db = DbSQLite()
    db.load(str(path))
    db.close()
    return path


class _Connection:
    """A real connection with one behavior overridden, for failure injection."""

    def __init__(self, inner, *, integrity=None, backup_error=None):
        self._inner = inner
        self._integrity = integrity
        self._backup_error = backup_error

    def execute(self, sql, *args):
        if self._integrity is not None and sql.startswith("PRAGMA integrity_check"):
            return _Rows([(problem,) for problem in self._integrity])
        return self._inner.execute(sql, *args)

    def backup(self, target):
        if self._backup_error is not None:
            raise self._backup_error
        return self._inner.backup(target._inner if isinstance(target, _Connection) else target)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


def _clean(tmp_path) -> set[str]:
    return {p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")}


class TestBackup:
    def test_a_backup_onto_the_open_book_is_refused(self, tmp_path):
        book = _book(tmp_path)
        source = sqlite3.connect(book)
        try:
            with pytest.raises(DbError, match="must differ from the open book"):
                backups.backup_connection(source, str(book), str(book), overwrite=True)
            # A destination whose folder does not exist yet is not the open book.
            written = backups.backup_connection(
                source, str(tmp_path / "new" / "copy.breadsched"), str(book)
            )
        finally:
            source.close()
        assert Path(written).is_file()

    def test_a_copy_failing_its_integrity_check_is_never_installed(self, tmp_path, monkeypatch):
        book = _book(tmp_path)
        target = tmp_path / "copy.breadsched"
        target.write_bytes(b"previous backup")
        real_connect = sqlite3.connect
        monkeypatch.setattr(
            backups.sqlite3,
            "connect",
            lambda path, *a, **k: _Connection(
                real_connect(path, *a, **k), integrity=["page 4 is never used"]
            ),
        )
        source = _Connection(real_connect(book))
        try:
            with pytest.raises(DbError, match="page 4 is never used"):
                backups.backup_connection(source, str(target), str(book), overwrite=True)
        finally:
            source.close()
        assert target.read_bytes() == b"previous backup"
        assert _clean(tmp_path) == set()


class TestRestore:
    @staticmethod
    def _verify_ok(path):
        return BookVerification()

    def test_refusals_before_anything_is_touched(self, tmp_path):
        book = _book(tmp_path)
        with pytest.raises(DbError, match="backup does not exist"):
            backups.restore_backup(
                str(tmp_path / "absent"),
                str(book),
                overwrite=True,
                verify=self._verify_ok,
                preserve=lambda *_: None,
            )
        with pytest.raises(DbError, match="must differ"):
            backups.restore_backup(
                str(book),
                str(book),
                overwrite=True,
                verify=self._verify_ok,
                preserve=lambda *_: None,
            )

    def test_a_damaged_or_inconsistent_backup_is_refused(self, tmp_path):
        backup = _book(tmp_path, "backup.breadsched")
        target = tmp_path / "restored.breadsched"
        with pytest.raises(DbError, match="SQLite integrity check: page 2"):
            backups.restore_backup(
                str(backup),
                str(target),
                overwrite=False,
                verify=lambda p: BookVerification(sqlite=("page 2",)),
                preserve=lambda *_: None,
            )
        issue = BookIssue("transaction.unbalanced", "out of balance by 1.00", "h")
        with pytest.raises(DbError, match="logical verification: transaction.unbalanced"):
            backups.restore_backup(
                str(backup),
                str(target),
                overwrite=False,
                verify=lambda p: BookVerification(issues=(issue,)),
                preserve=lambda *_: None,
            )
        assert not target.exists()

    def test_a_damaged_destination_is_neither_preserved_nor_replaced(self, tmp_path, monkeypatch):
        backup = _book(tmp_path, "backup.breadsched")
        target = _book(tmp_path, "book.breadsched")
        before = target.read_bytes()
        real_connect = sqlite3.connect
        monkeypatch.setattr(
            backups.sqlite3,
            "connect",
            lambda path, *a, **k: _Connection(
                real_connect(path, *a, **k),
                integrity=["row 7 missing"] if Path(path) == target else None,
            ),
        )
        preserved = []
        with pytest.raises(DbError, match="existing destination failed SQLite recovery"):
            backups.restore_backup(
                str(backup),
                str(target),
                overwrite=True,
                verify=self._verify_ok,
                preserve=lambda *a: preserved.append(a),
            )
        assert preserved == [] and target.read_bytes() == before
        assert not BookWriterLock.lock_path(str(target)).exists()

    @pytest.mark.parametrize("failure", ["copy", "verify raises", "verify fails"])
    def test_a_failed_copy_leaves_the_destination_and_no_temporary(
        self, tmp_path, monkeypatch, failure
    ):
        backup = _book(tmp_path, "backup.breadsched")
        target = _book(tmp_path, "book.breadsched")
        before = target.read_bytes()
        real_connect = sqlite3.connect
        if failure == "copy":
            monkeypatch.setattr(
                backups.sqlite3,
                "connect",
                lambda path, *a, **k: _Connection(
                    real_connect(path, *a, **k),
                    backup_error=sqlite3.OperationalError("disk I/O error")
                    if Path(path) == backup
                    else None,
                ),
            )

        def verify(path):
            if path.endswith(".restore.tmp"):
                if failure == "verify raises":
                    raise sqlite3.DatabaseError("file is not a database")
                if failure == "verify fails":
                    issue = BookIssue("split_index.missing", "missing", "h")
                    return BookVerification(issues=(issue,))
            return self._verify_ok(path)

        preserved = []
        with pytest.raises((DbError, sqlite3.Error)):
            backups.restore_backup(
                str(backup),
                str(target),
                overwrite=True,
                verify=verify,
                preserve=lambda *a: preserved.append(a),
            )
        assert target.read_bytes() == before
        assert len(preserved) == 1  # the old book was kept before the attempt
        assert not (tmp_path / "book.breadsched.restore.tmp").exists()
        assert not BookWriterLock.lock_path(str(target)).exists()


# ----------------------------------------------------------- commit-time checks


def _refused(db, code: str, write) -> None:
    """``write`` inside one transaction is refused for ``code`` and changes nothing."""
    before = db.verify_book()
    with pytest.raises(DbError, match=f"would leave an invalid book: {code}:"):
        with db.transaction("Refused") as txn:
            write(txn)
    assert db.verify_book() == before


def _checking_split(db, book):
    transaction = Transaction.simple(
        date(2026, 1, 5), "Paycheck", book.checking, book.salary, "100.00"
    )
    with db.transaction("Paycheck") as txn:
        db.add_transaction(transaction, txn)
    split = next(s for s in transaction.splits if s.account == book.checking)
    return transaction, split


class TestCommitChecks:
    def test_accounts_must_name_existing_relatives(self, db, book):
        for field_name, code in (
            ("parent", "account.missing_parent"),
            ("commodity", "account.missing_commodity"),
            ("linked_asset", "account.missing_linked_asset"),
            ("card_payment_account", "account.missing_card_payment_account"),
        ):
            account = Account(name=f"Odd {field_name}", atype=AccountType.BANK, parent=book.assets)
            setattr(account, field_name, MISSING)
            _refused(db, code, lambda txn, a=account: db.add_account(a, txn))

    def test_prices_need_a_known_security_and_a_currency_quote(self, db, book):
        usd = next(c for c in db.iter_commodities() if c.mnemonic == "USD")
        fund = Commodity(namespace="FUND", mnemonic="IDX", fullname="Index")
        with db.transaction("Fund") as txn:
            db.add_commodity(fund, txn)
        for commodity, currency, code in (
            (MISSING, usd.handle, "price.missing_commodity"),
            (fund.handle, MISSING, "price.missing_currency"),
            (usd.handle, fund.handle, "price.non_currency_quote"),
        ):
            price = CommodityPrice(
                commodity=commodity,
                currency=currency,
                quote_date=date(2026, 1, 1),
                value=Money(10),
            )
            _refused(db, code, lambda txn, p=price: db.add_price(p, txn))

    def test_transactions_need_their_currency_and_accounts(self, db, book):
        no_currency = Transaction.simple(
            date(2026, 1, 1), "Odd", book.checking, book.salary, "1.00"
        )
        no_currency.currency = MISSING
        _refused(
            db, "transaction.missing_currency", lambda txn: db.add_transaction(no_currency, txn)
        )
        no_account = Transaction(post_date=date(2026, 1, 1), description="Odd")
        no_account.splits = [Split(book.checking, Money(1)), Split(MISSING, Money(-1))]
        _refused(db, "transaction.missing_account", lambda txn: db.add_transaction(no_account, txn))

    def test_planning_objects_need_their_accounts(self, db, book):
        scheduled = ScheduledTransaction(
            name="Odd",
            recurrence=Recurrence(PeriodType.MONTH, interval=1, start=date(2026, 1, 1)),
            splits=[ScheduledSplit(book.checking, Money(1)), ScheduledSplit(MISSING, Money(-1))],
        )
        _refused(db, "scheduled.missing_account", lambda txn: db.add_scheduled(scheduled, txn))
        priced = ScheduledTransaction(
            name="Odd currency",
            recurrence=Recurrence(PeriodType.MONTH, interval=1, start=date(2026, 1, 1)),
            splits=[ScheduledSplit(book.checking, Money(1)), ScheduledSplit(book.rent, Money(-1))],
        )
        priced.currency = MISSING
        _refused(db, "scheduled.missing_currency", lambda txn: db.add_scheduled(priced, txn))
        scenario = Scenario(name="Odd", start=date(2026, 1, 1), years=1)
        scenario.opening_overrides = {MISSING: Money(1)}
        _refused(db, "scenario.missing_account", lambda txn: db.add_scenario(scenario, txn))
        goal = SavingsGoal(name="Odd", account=MISSING, target_amount=Money(10))
        _refused(db, "savings_goal.missing_account", lambda txn: db.add_savings_goal(goal, txn))

    def test_reconciliations_hold_only_their_own_accounts_splits(self, db, book):
        transaction, split = _checking_split(db, book)
        other = next(s for s in transaction.splits if s.account == book.salary)
        for account, splits, code in (
            (MISSING, [], "reconciliation.missing_account"),
            (book.checking, [MISSING], "reconciliation.missing_split"),
            (book.checking, [other.handle], "reconciliation.wrong_account"),
        ):
            record = Reconciliation(account=account, selected_splits=splits)
            _refused(db, code, lambda txn, r=record: db.add_reconciliation(r, txn))

    def test_claims_and_receivables_link_only_to_existing_splits(self, db, book):
        transaction, split = _checking_split(db, book)
        for link, code in (
            (FsaClaimSplitLink(MISSING, split.handle), "fsa_claim.missing_transaction"),
            (FsaClaimSplitLink(transaction.handle, MISSING), "fsa_claim.missing_split"),
        ):
            claim = FsaClaim(provider="Clinic", payments=[link])
            _refused(db, code, lambda txn, c=claim: db.add_fsa_claim(c, txn))
        allocation = FsaClaimAllocation(account=MISSING, funding_year_start=date(2026, 1, 1))
        claim = FsaClaim(provider="Clinic", allocations=[allocation])
        _refused(db, "fsa_claim.missing_account", lambda txn: db.add_fsa_claim(claim, txn))
        claim = FsaClaim(provider="Clinic", receivable=MISSING)
        _refused(db, "fsa_claim.missing_receivable", lambda txn: db.add_fsa_claim(claim, txn))

        for receivable, code in (
            (Receivable(payer="Insurer", account=MISSING), "receivable.missing_account"),
            (
                Receivable(payer="Insurer", expenses=[ReceivableSplitLink(MISSING, split.handle)]),
                "receivable.missing_transaction",
            ),
            (
                Receivable(
                    payer="Insurer",
                    reimbursements=[ReceivableSplitLink(transaction.handle, MISSING)],
                ),
                "receivable.missing_split",
            ),
        ):
            _refused(db, code, lambda txn, r=receivable: db.add_receivable(r, txn))

    def test_deleting_what_others_refer_to_is_refused(self, db, book):
        transaction, split = _checking_split(db, book)
        reconciliation = Reconciliation(account=book.checking, selected_splits=[split.handle])
        receivable = Receivable(
            payer="Insurer",
            account=book.groceries,
            expenses=[ReceivableSplitLink(transaction.handle, split.handle)],
        )
        claim = FsaClaim(
            provider="Clinic",
            payments=[FsaClaimSplitLink(transaction.handle, split.handle)],
            receivable=receivable.handle,
        )
        goal = SavingsGoal(name="Trip", account=book.savings, target_amount=Money(100))
        bill = ScheduledTransaction(
            name="Power",
            recurrence=Recurrence(PeriodType.MONTH, interval=1, start=date(2026, 1, 1)),
            splits=[
                ScheduledSplit(book.utilities, Money(50)),
                ScheduledSplit(book.card, Money(-50)),
            ],
        )
        fund = Commodity(namespace="FUND", mnemonic="IDX", fullname="Index")
        holding = Account(
            name="Index", atype=AccountType.INVESTMENT, parent=book.assets, commodity=fund.handle
        )
        with db.transaction("References") as txn:
            db.add_reconciliation(reconciliation, txn)
            db.add_receivable(receivable, txn)
            db.add_fsa_claim(claim, txn)
            db.add_savings_goal(goal, txn)
            db.add_scheduled(bill, txn)
            db.add_commodity(fund, txn)
            db.add_account(holding, txn)

        for code, remove in (
            ("savings_goal.missing_account", lambda txn: db.remove_account(book.savings, txn)),
            ("scheduled.missing_account", lambda txn: db.remove_account(book.utilities, txn)),
            ("receivable.missing_account", lambda txn: db.remove_account(book.groceries, txn)),
            (
                "reconciliation.missing_split",
                lambda txn: db.remove_transaction(transaction.handle, txn),
            ),
            (
                "fsa_claim.missing_receivable",
                lambda txn: db.remove_receivable(receivable.handle, txn),
            ),
            ("account.missing_commodity", lambda txn: db.remove_commodity(fund.handle, txn)),
        ):
            _refused(db, code, remove)
        # Accounts with children or transactions are refused before the commit check.
        with pytest.raises(DbError, match="still has children"):
            with db.transaction("Refused") as txn:
                db.remove_account(book.expenses, txn)

    def test_derived_index_rows_must_match_their_blob(self, db, book):
        transaction, split = _checking_split(db, book)
        conn = db._require()
        data = transaction.serialize()
        assert db._verify_changed_object("txn", transaction.handle, data) == []

        conn.execute("UPDATE txn SET description='Tampered' WHERE handle=?", (transaction.handle,))
        conn.execute("UPDATE split_index SET value_num=value_num+1 WHERE handle=?", (split.handle,))
        codes = [i.code for i in db._verify_changed_object("txn", transaction.handle, data)]
        assert codes == ["txn.index_mismatch", "split_index.mismatch"]

        conn.execute("DELETE FROM split_index WHERE handle=?", (split.handle,))
        conn.execute(
            "INSERT INTO split_index(handle, txn, account, post_date, value_num, value_den, "
            "quantity_num, quantity_den) VALUES (?,?,?,?,?,?,?,?)",
            (MISSING, transaction.handle, book.checking, "2026-01-05", 1, 1, 1, 1),
        )
        codes = [i.code for i in db._verify_transaction_index(transaction)]
        assert codes == ["split_index.missing", "split_index.orphan"]

        wrong_handle = dict(data, handle=MISSING)
        assert "txn.handle_mismatch" in [
            i.code for i in db._verify_changed_object("txn", transaction.handle, wrong_handle)
        ]
        assert "txn.missing_row" in [
            i.code for i in db._verify_changed_object("txn", MISSING, dict(data, handle=MISSING))
        ]
        conn.rollback()
