"""Architecture tests.

The value of the layering is only real if it is enforced. These tests fail the
build if the core starts depending on GTK, if the GUI starts doing arithmetic, or
if a GUI module stops parsing -- the last one matters especially here, because the
GTK layer cannot be imported on a machine without a GTK runtime and would otherwise
go unchecked entirely.
"""

from __future__ import annotations

import ast
import py_compile
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "breadsched"


def resolve_relative(module: Path, level: int, name: str | None) -> list[str] | None:
    """Resolve a relative import to a dotted path under ``breadsched``.

    Mirrors Python's own rule: a module's package is its parent directory, and each
    extra leading dot climbs one level above that.
    """
    package = list(module.relative_to(SRC).parts[:-1])
    climb = level - 1
    if climb > len(package):
        return None
    base = package[: len(package) - climb] if climb else package
    return ["breadsched", *base, *(name.split(".") if name else [])]


def module_exists(dotted: list[str]) -> bool:
    """True if the dotted path names a module or package on disk."""
    relative = Path(*dotted[1:])  # drop the leading 'breadsched'
    candidate = SRC / relative
    return (candidate / "__init__.py").exists() or candidate.with_suffix(".py").exists()


CORE_DIRS = ["gen", "cli", "plugins"]
GUI_DIR = SRC / "gui"


def modules_under(*parts: str) -> list[Path]:
    root = SRC.joinpath(*parts)
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def calls_in_method(path: Path, class_name: str | None, method_name: str) -> set[str]:
    """Return the simple and qualified call names made below one adapter method.

    ``class_name`` None names a module-level function (a web resource adapter).
    """
    if class_name is None:
        return calls_in_function(path, method_name)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    class_node = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    method = next(
        node
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == method_name
    )
    calls: set[str] = set()
    for node in ast.walk(method):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            calls.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            calls.add(node.func.attr)
            if isinstance(node.func.value, ast.Attribute):
                calls.add(f"{node.func.value.attr}.{node.func.attr}")
    return calls


def calls_in_function(path: Path, function_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
    )
    calls: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            calls.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            calls.add(node.func.attr)
    return calls


class TestLayering:
    @pytest.mark.parametrize("layer", CORE_DIRS)
    def test_the_core_never_imports_gtk(self, layer):
        """The engine must run headless: on a server, in CI, under cron."""
        offenders = [
            module.relative_to(SRC).as_posix()
            for module in modules_under(layer)
            if {"gi", "gtk"} & imported_names(module)
        ]
        assert offenders == []

    def test_the_core_has_no_third_party_dependencies(self):
        """Everything below the GUI runs on the standard library alone."""
        allowed = {
            "__future__",
            "abc",
            "argparse",
            "ast",
            "bisect",
            "calendar",
            "configparser",
            "csv",
            "ctypes",
            "dataclasses",
            "datetime",
            "decimal",
            "enum",
            "fractions",
            "gzip",
            "hashlib",
            "html",
            "io",
            "json",
            "logging",
            "math",
            "numbers",
            "operator",
            "pathlib",
            "sqlite3",
            "statistics",
            "subprocess",
            "sys",
            "time",
            "typing",
            "unicodedata",
            "urllib",
            "uuid",
            "weakref",
            "xml",
            "breadsched",
            "IO",
            "collections",
            "functools",
            "inspect",
            "itertools",
            "locale",
            "os",
            "re",
            "shutil",
            "socket",
            "tempfile",
            "textwrap",
            "threading",
        }
        offenders: dict[str, set[str]] = {}
        for layer in CORE_DIRS:
            for module in modules_under(layer):
                extra = imported_names(module) - allowed
                if extra:
                    offenders[module.relative_to(SRC).as_posix()] = extra
        assert offenders == {}

    def test_importing_the_gui_package_does_not_pull_in_gtk(self):
        """A user with no GTK installed must still be able to import the package."""
        import breadsched.gui  # noqa: F401

        assert "gi" not in imported_names(GUI_DIR / "__init__.py")


class TestGuiIsCheckable:
    @pytest.mark.parametrize("module", modules_under("gui"), ids=lambda p: p.name)
    def test_every_gui_module_compiles(self, module, tmp_path):
        py_compile.compile(str(module), cfile=str(tmp_path / f"{module.stem}.pyc"), doraise=True)

    @pytest.mark.parametrize("module", modules_under("gui"), ids=lambda p: p.name)
    def test_every_relative_import_resolves_to_something_real(self, module):
        """Checking depth is not enough: a shallow import can still point nowhere.

        ``from ..gen.db import x`` inside ``gui/views/_base.py`` is well within the
        package and resolves to ``breadsched.gui.gen``, which does not exist. Only
        resolving the target against the filesystem catches that, and it is a
        run-time-only failure otherwise, reached the moment a user clicks the view.
        """
        unresolved = []
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level == 0:
                continue
            target = resolve_relative(module, node.level, node.module)
            if target is None or not module_exists(target):
                unresolved.append(
                    f"{'.' * node.level}{node.module or ''} -> "
                    f"{'.'.join(target) if target else 'above the package root'}"
                )
        assert unresolved == []

    def test_the_gui_does_no_money_arithmetic_of_its_own(self):
        """Every figure on screen comes from the engine, so both agree by construction."""
        offenders = []
        for module in modules_under("gui"):
            source = module.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                # A Money literal built from arithmetic inside a view means the view
                # is computing a figure rather than displaying one.
                if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div,)):
                    segment = ast.get_source_segment(source, node) or ""
                    if "Money" in segment:
                        offenders.append(f"{module.name}: {segment}")
        assert offenders == []


class TestServiceBoundaries:
    """Presentation adapters may translate fields, but services own use cases."""

    _schedule_adapters = (
        ("gui/dialogs/schedule_dialog.py", "ScheduleDialog", "build"),
        ("gui/dialogs/schedule_dialog.py", "ScheduleDialog", "_on_save"),
        ("gui/dialogs/scenario_schedule_dialog.py", "ScenarioScheduleDialog", "build"),
        ("gui/dialogs/scenario_schedule_dialog.py", "ScenarioScheduleDialog", "_on_save"),
        ("web/scenario_resource.py", None, "scenario_event_save"),
        ("web/schedule_resource.py", None, "scheduled_save"),
        ("web/schedule_resource.py", None, "scheduled_formula_save"),
    )

    @pytest.mark.parametrize(
        ("relative", "class_name", "method_name"),
        _schedule_adapters,
        ids=lambda value: str(value).rsplit("/", 1)[-1],
    )
    def test_schedule_adapters_do_not_construct_or_persist_domain_objects(
        self, relative, class_name, method_name
    ):
        calls = calls_in_method(SRC / relative, class_name, method_name)
        forbidden = {
            "ScheduledSplit",
            "ScheduledTransaction",
            "ScenarioSchedule",
            "transaction",
            "db.transaction",
            "add_scheduled",
            "commit_scheduled",
            "add_scenario",
            "commit_scenario",
        }
        assert calls.isdisjoint(forbidden), sorted(calls & forbidden)

    @pytest.mark.parametrize(
        ("relative", "class_name", "method_name", "expected"),
        (
            (
                "gui/dialogs/schedule_dialog.py",
                "ScheduleDialog",
                "_on_save",
                {"save_fixed_schedule", "save_formula_schedule"},
            ),
            (
                "gui/dialogs/scenario_schedule_dialog.py",
                "ScenarioScheduleDialog",
                "_on_save",
                {"save_fixed_scenario_schedule", "save_formula_scenario_schedule"},
            ),
            ("web/schedule_resource.py", None, "scheduled_formula_save", {"save_formula_schedule"}),
        ),
    )
    def test_schedule_writes_call_the_typed_service(
        self, relative, class_name, method_name, expected
    ):
        calls = calls_in_method(SRC / relative, class_name, method_name)
        assert expected <= calls

    def test_web_schedule_writes_live_in_service_backed_resources(self):
        """Neither write remains on ``Api``; each resource calls its typed service."""
        server = ast.parse((SRC / "web/context.py").read_text(encoding="utf-8"))
        api_methods = {
            node.name
            for item in server.body
            if isinstance(item, ast.ClassDef) and item.name == "Api"
            for node in item.body
            if isinstance(node, ast.FunctionDef)
        }
        assert {"scheduled_save", "scenario_event_save"}.isdisjoint(api_methods)
        fixed = calls_in_function(SRC / "web/schedule_resource.py", "scheduled_save")
        scenario = calls_in_function(SRC / "web/scenario_resource.py", "scenario_event_save")
        assert "save_fixed_schedule" in fixed
        assert "save_fixed_scenario_schedule" in scenario

    def test_gui_plan_consumes_the_typed_query_service(self):
        calls = calls_in_method(SRC / "gui/views/plan.py", "PlanView", "refresh")
        assert "query_plan" in calls
        assert "build_category_report" not in calls

    def test_web_plan_delegates_to_a_service_backed_resource(self):
        route_calls = calls_in_function(SRC / "web/resources.py", "_plan")
        resource_calls = calls_in_function(SRC / "web/plan_resource.py", "plan_report")

        assert "plan_report" in route_calls
        assert "query_plan" not in route_calls
        assert "query_plan" in resource_calls
        assert "build_category_report" not in resource_calls

    @pytest.mark.parametrize(
        ("relative", "class_name", "method_name"),
        (
            ("gui/dialogs/transaction_dialog.py", "TransactionDialog", "_on_save"),
            ("web/register_resource.py", None, "transaction_add"),
        ),
    )
    def test_transaction_adapters_do_not_construct_or_persist_domain_objects(
        self, relative, class_name, method_name
    ):
        calls = (
            calls_in_function(SRC / relative, method_name)
            if class_name is None
            else calls_in_method(SRC / relative, class_name, method_name)
        )
        forbidden = {
            "Transaction",
            "Split",
            "transaction",
            "db.transaction",
            "add_transaction",
            "commit_transaction",
        }
        assert calls.isdisjoint(forbidden), sorted(calls & forbidden)
        assert "save_transaction" in calls

    @pytest.mark.parametrize(
        ("relative", "class_name", "method_name", "service_call"),
        (
            (
                "gui/dialogs/reconciliation_dialog.py",
                "ReconciliationDialog",
                "_on_start",
                "start_reconciliation",
            ),
            (
                "gui/dialogs/reconciliation_dialog.py",
                "ReconciliationDialog",
                "_on_selection",
                "update_reconciliation",
            ),
            (
                "gui/dialogs/reconciliation_dialog.py",
                "ReconciliationDialog",
                "_on_finish",
                "complete_reconciliation",
            ),
            (
                "web/reconciliation_resource.py",
                None,
                "reconciliation_start",
                "start_reconciliation",
            ),
            (
                "web/reconciliation_resource.py",
                None,
                "reconciliation_update",
                "update_reconciliation",
            ),
            (
                "web/reconciliation_resource.py",
                None,
                "reconciliation_complete",
                "complete_reconciliation",
            ),
            (
                "web/reconciliation_resource.py",
                None,
                "reconciliation_cancel",
                "cancel_reconciliation",
            ),
            (
                "web/reconciliation_resource.py",
                None,
                "reconciliation_reopen",
                "reopen_reconciliation",
            ),
        ),
    )
    def test_reconciliation_mutations_use_typed_services(
        self, relative, class_name, method_name, service_call
    ):
        calls = (
            calls_in_function(SRC / relative, method_name)
            if class_name is None
            else calls_in_method(SRC / relative, class_name, method_name)
        )
        assert service_call in calls
        assert calls.isdisjoint(
            {"start", "update", "set_selection", "complete", "cancel", "reopen"}
        )

    @pytest.mark.parametrize(
        ("relative", "class_name", "method_name", "service_call"),
        (
            ("gui/dialogs/fsa_claims_dialog.py", "FsaClaimsDialog", "_save", "save_claim"),
            ("gui/dialogs/fsa_claims_dialog.py", "FsaClaimsDialog", "_delete", "delete_claim"),
            ("gui/dialogs/fsa_claims_dialog.py", "FsaClaimsDialog", "_close_claim", "close_claim"),
            (
                "gui/dialogs/fsa_claims_dialog.py",
                "FsaClaimsDialog",
                "_reopen_claim",
                "reopen_claim",
            ),
            ("web/fsa_claim_resource.py", None, "fsa_claim_save", "save_claim"),
            ("web/fsa_claim_resource.py", None, "fsa_claim_delete", "delete_claim"),
            ("web/fsa_claim_resource.py", None, "fsa_claim_close", "close_claim"),
            ("web/fsa_claim_resource.py", None, "fsa_claim_reopen", "reopen_claim"),
        ),
    )
    def test_claim_mutations_use_typed_services(
        self, relative, class_name, method_name, service_call
    ):
        calls = (
            calls_in_function(SRC / relative, method_name)
            if class_name is None
            else calls_in_method(SRC / relative, class_name, method_name)
        )
        assert service_call in calls
        assert calls.isdisjoint(
            {
                "FsaClaim",
                "FsaClaimAllocation",
                "FsaClaimRejection",
                "db.transaction",
                "add_fsa_claim",
                "commit_fsa_claim",
                "remove_fsa_claim",
            }
        )

    @pytest.mark.parametrize(
        ("relative", "class_name", "method_name"),
        (
            ("gui/dialogs/loan_dialog.py", "LoanDialog", "_on_save"),
            ("web/loan_resource.py", None, "loan_save"),
        ),
    )
    def test_loan_mutations_use_the_typed_service(self, relative, class_name, method_name):
        calls = calls_in_method(SRC / relative, class_name, method_name)
        assert "save_loan" in calls
        assert calls.isdisjoint(
            {"create_loan", "add_scheduled", "add_transaction", "db.transaction"}
        )

    @pytest.mark.parametrize(
        ("relative", "class_name", "method_name"),
        (
            ("gui/dialogs/import_dialog.py", "ImportDialog", "_on_import"),
            ("web/import_resource.py", None, "import_local"),
        ),
    )
    def test_import_adapters_use_the_typed_service(self, relative, class_name, method_name):
        calls = calls_in_method(SRC / relative, class_name, method_name)
        assert "import_book" in calls
        assert "run" not in calls
        assert "remember_import_source" not in calls

    def test_cli_import_uses_the_typed_service(self):
        calls = calls_in_function(SRC / "cli/import_commands.py", "cmd_import")
        assert "import_book" in calls
        assert "run" not in calls
        assert "remember_import_source" not in calls

    @pytest.mark.parametrize(
        ("relative", "class_name", "method_name", "service_call"),
        (
            ("gui/views/resolution.py", "ResolutionView", "_on_match", "match_review"),
            ("gui/views/resolution.py", "ResolutionView", "_on_reject", "reject_review"),
            ("gui/views/resolution.py", "ResolutionView", "_on_skip", "skip_review"),
            (
                "gui/views/resolution.py",
                "ResolutionView",
                "_on_unexpected",
                "mark_review_unexpected",
            ),
            (
                "gui/views/resolution.py",
                "ResolutionView",
                "_on_fsa_attach",
                "attach_review_claim",
            ),
            ("web/review_resource.py", None, "review_match", "match_review"),
            ("web/review_resource.py", None, "review_reject", "reject_review"),
            ("web/review_resource.py", None, "review_skip", "skip_review"),
            ("web/review_resource.py", None, "review_unexpected", "mark_review_unexpected"),
            ("web/review_resource.py", None, "review_fsa_attach", "attach_review_claim"),
        ),
    )
    def test_review_adapters_use_typed_services(
        self, relative, class_name, method_name, service_call
    ):
        calls = calls_in_method(SRC / relative, class_name, method_name)
        assert service_call in calls
        assert calls.isdisjoint(
            {
                "actualize_transaction",
                "reject_candidate",
                "skip_occurrence",
                "mark_unexpected",
                "attach_transaction_to_claim",
                "db.transaction",
                "commit_transaction",
            }
        )

    @pytest.mark.parametrize(
        ("function_name", "service_call"),
        (
            ("cmd_plan_resolve", "match_review"),
            ("cmd_plan_reject", "reject_review"),
            ("cmd_plan_unexpected", "mark_review_unexpected"),
        ),
    )
    def test_cli_review_mutations_use_typed_services(self, function_name, service_call):
        calls = calls_in_function(SRC / "cli/plan_commands.py", function_name)
        assert service_call in calls
        assert calls.isdisjoint(
            {"actualize_transaction", "reject_candidate", "mark_unexpected", "db.transaction"}
        )

    @pytest.mark.parametrize(
        ("relative", "class_name", "method_name", "service_call"),
        (
            (
                "gui/dialogs/scenario_dialog.py",
                "SaveScenarioDialog",
                "_on_save",
                "save_scenario",
            ),
            (
                "gui/dialogs/scenario_manager_dialog.py",
                "ScenarioManagerDialog",
                "_on_save",
                "save_scenario_assumptions",
            ),
            (
                "gui/dialogs/scenario_manager_dialog.py",
                "ScenarioManagerDialog",
                "_on_duplicate",
                "duplicate_scenario",
            ),
            (
                "gui/dialogs/scenario_manager_dialog.py",
                "ScenarioDeleteDialog",
                "_confirm",
                "delete_scenario",
            ),
            (
                "gui/views/plan.py",
                "PlanView",
                "_suppress_baseline_schedule",
                "suppress_scenario_schedule",
            ),
            ("web/scenario_resource.py", None, "scenario_save", "save_scenario_assumptions"),
            ("web/scenario_resource.py", None, "scenario_duplicate", "duplicate_scenario"),
            ("web/scenario_resource.py", None, "scenario_delete", "delete_scenario"),
            (
                "web/scenario_resource.py",
                None,
                "scenario_event_suppress",
                "suppress_scenario_schedule",
            ),
        ),
    )
    def test_scenario_lifecycle_adapters_use_typed_services(
        self, relative, class_name, method_name, service_call
    ):
        calls = calls_in_method(SRC / relative, class_name, method_name)
        assert service_call in calls
        assert calls.isdisjoint(
            {"add_scenario", "commit_scenario", "remove_scenario", "db.transaction"}
        )

    def test_cli_scenario_lifecycle_uses_typed_services(self):
        calls = calls_in_function(SRC / "cli/projection_commands.py", "cmd_scenario")
        assert {"save_scenario", "delete_scenario"} <= calls
        assert calls.isdisjoint(
            {"add_scenario", "commit_scenario", "remove_scenario", "db.transaction"}
        )

    @pytest.mark.parametrize(
        ("relative", "class_name", "method_name", "service_call"),
        (
            (
                "gui/dialogs/scenario_manager_dialog.py",
                "AssumptionTimelineDialog",
                "_save_new",
                "save_assumption_period",
            ),
            (
                "gui/dialogs/scenario_manager_dialog.py",
                "AssumptionTimelineDialog",
                "_save_edit",
                "save_assumption_period",
            ),
            (
                "gui/dialogs/scenario_manager_dialog.py",
                "AssumptionTimelineDialog",
                "_on_delete",
                "delete_assumption_period",
            ),
            (
                "gui/views/projection.py",
                "ProjectionView",
                "_on_save_clicked",
                "save_scenario_assumptions",
            ),
            ("web/scenario_resource.py", None, "scenario_period_save", "save_assumption_period"),
            (
                "web/scenario_resource.py",
                None,
                "scenario_period_delete",
                "delete_assumption_period",
            ),
        ),
    )
    def test_projection_assumption_adapters_use_typed_services(
        self, relative, class_name, method_name, service_call
    ):
        calls = calls_in_method(SRC / relative, class_name, method_name)
        assert service_call in calls
        assert calls.isdisjoint({"set_metadata", "commit_scenario", "db.transaction"})

    def test_base_assumption_writes_use_typed_service(self):
        helper_calls = calls_in_function(
            SRC / "gui/planning_context.py", "persist_baseline_assumptions"
        )
        assert "save_base_assumptions" in helper_calls
        assert "set_metadata" not in helper_calls
        for relative, class_name, method_name in (
            ("web/scenario_resource.py", None, "scenario_save"),
            ("web/projection_resource.py", None, "projection_save"),
        ):
            calls = calls_in_method(SRC / relative, class_name, method_name)
            assert "save_base_assumptions" in calls
            assert "save_scenario_assumptions" in calls
            assert "set_metadata" not in calls

    @pytest.mark.parametrize(
        ("function_name", "service_call"),
        (
            ("cmd_add", "save_transaction"),
            ("cmd_edit", "save_transaction"),
            ("cmd_delete", "delete_transaction"),
        ),
    )
    def test_cli_transaction_mutations_use_typed_services(self, function_name, service_call):
        calls = calls_in_function(SRC / "cli/ledger_commands.py", function_name)
        assert service_call in calls
        assert calls.isdisjoint(
            {"add_transaction", "commit_transaction", "remove_transaction", "db.transaction"}
        )

    @pytest.mark.parametrize(
        ("relative", "class_name", "method_name", "service_call"),
        (
            ("gui/views/blank_entry.py", "BlankEntryRow", "_save", "save_transaction"),
            (
                "gui/dialogs/transaction_dialog.py",
                "TransactionDialog",
                "_on_delete",
                "delete_transaction",
            ),
        ),
    )
    def test_gtk_transaction_mutations_use_typed_services(
        self, relative, class_name, method_name, service_call
    ):
        calls = calls_in_method(SRC / relative, class_name, method_name)
        assert service_call in calls
        assert calls.isdisjoint(
            {"Transaction", "add_transaction", "remove_transaction", "db.transaction"}
        )

    @pytest.mark.parametrize(
        ("relative", "class_name", "method_name", "service_call"),
        (
            ("gui/dialogs/account_dialog.py", "AccountDialog", "_on_save", "save_account"),
            ("gui/dialogs/account_dialog.py", "AccountDialog", "_on_delete", "delete_account"),
            ("web/account_resource.py", None, "account_type_save", "save_account"),
            ("web/account_resource.py", None, "account_emergency_fund_save", "save_account"),
            ("web/account_resource.py", None, "account_card_save", "save_account"),
            ("web/account_resource.py", None, "account_fsa_years_save", "save_account"),
        ),
    )
    def test_account_adapters_use_typed_services(
        self, relative, class_name, method_name, service_call
    ):
        calls = calls_in_method(SRC / relative, class_name, method_name)
        assert service_call in calls
        assert calls.isdisjoint(
            {"add_account", "commit_account", "remove_account", "db.transaction"}
        )

    def test_cli_account_mutations_use_typed_services(self):
        calls = calls_in_function(SRC / "cli/ledger_commands.py", "cmd_account")
        assert {"save_account", "delete_account"} <= calls
        assert calls.isdisjoint(
            {
                "add_account",
                "commit_account",
                "remove_account",
                "add_transaction",
                "db.transaction",
            }
        )

    @pytest.mark.parametrize(
        ("relative", "class_name", "method_name", "service_call"),
        (
            (
                "gui/views/scheduled.py",
                "ScheduleDuplicateDialog",
                "_confirm",
                "duplicate_schedule",
            ),
            (
                "gui/views/scheduled.py",
                "ScheduleDeleteDialog",
                "_confirm",
                "delete_schedule",
            ),
            ("web/schedule_resource.py", None, "scheduled_duplicate", "duplicate_schedule"),
            ("web/schedule_resource.py", None, "scheduled_delete", "delete_schedule"),
        ),
    )
    def test_schedule_lifecycle_adapters_use_typed_services(
        self, relative, class_name, method_name, service_call
    ):
        calls = calls_in_method(SRC / relative, class_name, method_name)
        assert service_call in calls
        assert calls.isdisjoint(
            {"duplicate_saved_definition", "delete_definition", "db.transaction"}
        )

    def test_cli_estimate_writes_use_schedule_services(self):
        calls = calls_in_function(SRC / "cli/plan_commands.py", "cmd_estimate")
        assert {"save_schedule", "delete_schedule"} <= calls
        assert calls.isdisjoint({"add_scheduled", "remove_scheduled", "db.transaction"})


class TestEngineBoundaries:
    def test_plan_cell_explanations_build_on_the_activity_report(self):
        """Plan drill-down lives beside the Plan grid it explains, never inside it.

        ``engine/plan_detail`` rebuilds one cell from ``activity``'s own report, so
        the explanation cannot disagree with the grid; ``activity`` never needs the
        explanations, and no private helper crosses the module boundary.
        """
        engine = SRC / "gen" / "engine"
        activity_tree = ast.parse((engine / "activity.py").read_text(encoding="utf-8"))
        detail_tree = ast.parse((engine / "plan_detail.py").read_text(encoding="utf-8"))
        activity_imports = {
            alias.name
            for node in ast.walk(activity_tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        } | {
            node.module or ""
            for node in ast.walk(activity_tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert "plan_detail" not in activity_imports
        assert not any(name.startswith("explain_") for name in _defined(activity_tree))
        from_activity = [
            alias.name
            for node in ast.walk(detail_tree)
            if isinstance(node, ast.ImportFrom) and node.module == "activity"
            for alias in node.names
        ]
        assert "build_activity_report" in from_activity
        assert [name for name in from_activity if name.startswith("_")] == []

    def test_the_plan_category_report_builds_on_the_activity_report(self):
        """The Plan's rows live in ``category_report``; ``activity`` only dates activity.

        ``category_report`` turns ``activity``'s dated report into the Plan's rows and
        cash position; ``activity`` never imports it or defines its report types, and
        no private helper crosses the boundary.
        """
        engine = SRC / "gen" / "engine"
        activity_tree = ast.parse((engine / "activity.py").read_text(encoding="utf-8"))
        report_tree = ast.parse((engine / "category_report.py").read_text(encoding="utf-8"))
        activity_modules = {
            node.module or ""
            for node in ast.walk(activity_tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert "category_report" not in activity_modules
        assert {"CategoryReport", "build_category_report", "CashPosition"}.isdisjoint(
            _defined(activity_tree)
        )
        assert {"CategoryReport", "build_category_report", "CashPosition"} <= _defined(report_tree)
        from_activity = [
            alias.name
            for node in ast.walk(report_tree)
            if isinstance(node, ast.ImportFrom) and node.module == "activity"
            for alias in node.names
        ]
        assert "build_activity_report" in from_activity
        assert [name for name in from_activity if name.startswith("_")] == []

    def test_projection_results_are_separate_from_the_engine(self):
        """``projection_result`` holds results; only ``projection`` calculates them.

        Print, export, the bridge, and presentation read result types without
        importing the engine, and ``projection`` re-exports the same classes.
        """
        from breadsched.gen.engine import projection, projection_result

        engine = SRC / "gen" / "engine"
        result_tree = ast.parse((engine / "projection_result.py").read_text(encoding="utf-8"))
        modules = {
            node.module or "" for node in ast.walk(result_tree) if isinstance(node, ast.ImportFrom)
        }
        assert "projection" not in modules
        for name in projection_result.__all__:
            assert getattr(projection, name) is getattr(projection_result, name)
        for path in (
            engine / "projection_bridge.py",
            SRC / "plugins" / "export" / "report_layout.py",
            SRC / "plugins" / "export" / "csv_export.py",
            SRC / "plugins" / "export" / "html_report.py",
        ):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported = {
                node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
            }
            assert not any(module.endswith("engine.projection") for module in imported), path
            assert "projection" not in imported, path

    def test_gnucash_writeback_layers_only_depend_downward(self):
        """Reading, planning, writing, and applying a write-back are separate layers.

        ``gnucash_source`` reads, ``gnucash_writeback_plan`` plans from it,
        ``gnucash_book_writers`` writes a plan, and ``gnucash_writeback`` applies,
        verifies, and re-exports; each imports only the layers below it, and no
        private name crosses a module.
        """
        export = SRC / "plugins" / "export"
        layers = [
            "gnucash_source",
            "gnucash_writeback_plan",
            "gnucash_book_writers",
            "gnucash_writeback",
        ]
        for index, name in enumerate(layers):
            tree = ast.parse((export / f"{name}.py").read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in layers:
                    assert layers.index(node.module) < index, (name, node.module)
                    assert not any(alias.name.startswith("_") for alias in node.names), name

    def test_book_files_lock_and_storage_checks_are_separate_from_storage(self):
        """``DbSQLite`` stores objects; the lock, copies, and index checks live apart.

        ``book_lock`` owns the single-writer lock, ``backups`` the backup, restore,
        and read-snapshot copies, and ``storage_verification`` the derived-index
        checks; none of them imports the backend.
        """
        db_dir = SRC / "gen" / "db"
        tree = ast.parse((db_dir / "sqlite.py").read_text(encoding="utf-8"))
        storage = next(
            node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DbSQLite"
        )
        methods = {node.name for node in storage.body if isinstance(node, ast.FunctionDef)}
        assert methods.isdisjoint({"_writer_lock_path", "_pid_is_alive", "_acquire_book_lock"})
        assert "_snapshot_of" not in _defined(tree)
        for name in ("book_lock", "backups", "storage_verification"):
            module = ast.parse((db_dir / f"{name}.py").read_text(encoding="utf-8"))
            imported = {
                node.module or "" for node in ast.walk(module) if isinstance(node, ast.ImportFrom)
            }
            assert "sqlite" not in imported, name

    def test_commit_verification_is_separate_from_storage(self):
        """``DbSQLite`` stores rows; ``ChangeVerification`` checks what a batch changed."""
        from breadsched.gen.db.change_verification import ChangeVerification
        from breadsched.gen.db.sqlite import DbSQLite

        assert issubclass(DbSQLite, ChangeVerification)
        tree = ast.parse((SRC / "gen" / "db" / "sqlite.py").read_text(encoding="utf-8"))
        storage = next(
            node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DbSQLite"
        )
        assert [
            node.name
            for node in storage.body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("_verify_")
        ] == []

    def test_estimate_statistics_never_read_the_book(self):
        """History statistics are pure; ``estimates`` alone gathers the book's activity."""
        path = SRC / "gen" / "engine" / "estimate_history.py"
        modules = {
            node.module or ""
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.ImportFrom)
        }
        assert not {"db.sqlite", "planning", "activity", "ledger"} & modules
        estimates = (SRC / "gen" / "engine" / "estimates.py").read_text(encoding="utf-8")
        assert "from . import estimate_history as estimate_stats" in estimates

    def test_dashboard_bills_stand_apart_from_the_dashboard(self):
        """Bill rows and reserves never reach back into the view that assembles them."""
        path = SRC / "gen" / "engine" / "dashboard_bills.py"
        modules = {
            node.module or ""
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.ImportFrom)
        }
        assert "dashboard" not in modules
        from breadsched.gen.engine import dashboard, dashboard_bills

        assert dashboard.BillRow is dashboard_bills.BillRow
        assert dashboard.MissedGroup is dashboard_bills.MissedGroup


def _defined(tree: ast.Module) -> set[str]:
    return {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}


class TestGuiBoundaries:
    def test_the_view_catalog_needs_no_display(self):
        """The toolbar, views, and view commands are plain data the window builds from."""
        tree = ast.parse((SRC / "gui" / "view_catalog.py").read_text(encoding="utf-8"))
        imported = {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, (ast.ImportFrom, ast.Import))
            for node in [node]
        }
        assert not any("gi" in module.split(".") or "gi_setup" in module for module in imported)
        from breadsched.gui import view_catalog

        assert {key for key, _label, _icon in view_catalog.CATEGORIES} >= set(
            view_catalog.VIEW_ACTIONS
        )


class TestPresentationPackage:
    def test_wording_is_split_by_area_and_re_exported(self):
        """Each area's wording is its own module; the package re-exports every name."""
        import importlib

        import breadsched.presentation as presentation

        package = SRC / "presentation"
        areas = sorted(p.stem for p in package.glob("*.py") if p.stem != "__init__")
        assert areas == ["benefits", "charts", "investments", "messages", "notices", "planning"]
        for area in areas:
            path = package / f"{area}.py"
            tree = ast.parse(path.read_text(encoding="utf-8"))
            relative = {
                node.module or ""
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.level
            }
            # Wording reads engine and service results; it never reaches an interface.
            assert not any(m.split(".")[0] in {"gui", "web", "cli"} for m in relative), area
            module = importlib.import_module(f"breadsched.presentation.{area}")
            defined = {
                node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))
            } | {
                target.id
                for node in tree.body
                if isinstance(node, ast.Assign)
                for target in node.targets
                if isinstance(target, ast.Name)
            }
            public = {name for name in defined if not name.startswith("_")}
            assert public <= set(presentation.__all__), (area, public - set(presentation.__all__))
            for name in public:
                assert getattr(presentation, name) is getattr(module, name)


class TestWebBoundaries:
    def test_the_request_context_holds_only_the_book(self):
        """Every handler is a resource adapter; ``Api`` carries the open book alone."""
        tree = ast.parse((SRC / "web/context.py").read_text(encoding="utf-8"))
        [api] = [node for node in tree.body if isinstance(node, ast.ClassDef)]
        methods = [node.name for node in api.body if isinstance(node, ast.FunctionDef)]
        assert methods == ["__init__"]
        resources = (SRC / "web/resources.py").read_text(encoding="utf-8")
        assert "getattr(api" not in resources

    def test_financial_api_does_not_own_http_transport_or_route_tables(self):
        source = (SRC / "web/server.py").read_text(encoding="utf-8")
        assert "class Handler(" not in source
        assert "BaseHTTPRequestHandler" not in source
        assert "ROUTES =" not in source
        assert "POST_ROUTES =" not in source

    def test_resource_and_transport_ownership_is_explicit(self):
        resources = (SRC / "web/resources.py").read_text(encoding="utf-8")
        transport = (SRC / "web/transport.py").read_text(encoding="utf-8")
        assert "GET_ROUTES:" in resources
        assert "POST_ROUTES:" in resources
        assert "class Handler(BaseHTTPRequestHandler):" in transport
        assert "class BreadSchedHTTPServer(ThreadingHTTPServer):" in transport

    def test_transport_does_not_import_financial_engines(self):
        source = (SRC / "web/transport.py").read_text(encoding="utf-8")
        assert "gen.engine" not in source
        assert "gen.services" not in source

    def test_transport_keeps_one_writer_and_opens_file_reads_read_only(self):
        source = (SRC / "web/transport.py").read_text(encoding="utf-8")
        assert 'reader.load(path, mode="r")' in source
        assert '"writer_db": db' in source
        assert "with self.lock:" in source


class TestCliBoundaries:
    """The CLI entry point only parses and dispatches; each area owns its commands."""

    COMMAND_MODULES = sorted((SRC / "cli").glob("*_commands.py"))

    def test_the_entry_point_defines_no_command_handlers(self):
        tree = ast.parse((SRC / "cli" / "main.py").read_text(encoding="utf-8"))
        functions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        assert functions == {"build_parser", "main"}

    @pytest.mark.parametrize("path", COMMAND_MODULES, ids=lambda path: path.stem)
    def test_each_command_is_registered_beside_its_handler(self, path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defined = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        assert "register" in defined
        handlers = {
            keyword.value.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "set_defaults"
            for keyword in node.keywords
            if keyword.arg == "func" and isinstance(keyword.value, ast.Name)
        }
        assert handlers and handlers <= defined

    def test_rules_tags_and_attachments_have_their_own_module(self):
        tree = ast.parse((SRC / "cli" / "categorization_commands.py").read_text(encoding="utf-8"))
        defined = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        assert {"cmd_rules", "cmd_tags", "cmd_attachments"} <= defined
        ledger = ast.parse((SRC / "cli" / "ledger_commands.py").read_text(encoding="utf-8"))
        ledger_defined = {node.name for node in ledger.body if isinstance(node, ast.FunctionDef)}
        assert ledger_defined.isdisjoint({"cmd_rules", "cmd_tags", "cmd_attachments"})

    def test_shared_helpers_depend_on_no_command_module(self):
        tree = ast.parse((SRC / "cli" / "common.py").read_text(encoding="utf-8"))
        local = [
            node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level == 1
        ]
        assert local == []

    def test_every_subcommand_has_one_handler(self):
        from breadsched.cli.main import build_parser

        parser = build_parser()
        actions = [a for a in parser._actions if a.dest == "command"]
        choices = actions[0].choices
        assert {"init", "import", "add", "scheduled", "claims", "project"} <= set(choices)
        assert all(callable(sub.get_default("func")) for sub in choices.values())


class TestSuiteIsLocationIndependent:
    """The suite must pass however pytest was invoked, from wherever.

    ``pytest`` and ``python -m pytest`` do not build the same ``sys.path``: only
    the latter adds the working directory. A test importing ``tests.something``
    therefore passes under one and fails under the other, which is how this rule
    came to exist.
    """

    def test_no_test_imports_the_tests_package_by_name(self):
        test_dir = Path(__file__).resolve().parent
        offenders = []
        for module in sorted(test_dir.glob("*.py")):
            tree = ast.parse(module.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("tests"):
                    offenders.append(f"{module.name}: from {node.module} import …")
                elif isinstance(node, ast.Import):
                    offenders.extend(
                        f"{module.name}: import {a.name}"
                        for a in node.names
                        if a.name.split(".")[0] == "tests"
                    )
        assert offenders == []

    def test_shared_fixtures_are_importable_as_a_top_level_module(self):
        import gnucash_fixtures

        assert hasattr(gnucash_fixtures, "create_book")


class TestWarningPolicy:
    """Third-party noise is filtered; our own warnings are defects."""

    def test_importing_the_package_warns_about_nothing_of_ours(self):
        """A deprecation raised from our own files is a bug, not background noise.

        Checked here rather than with a ``filterwarnings`` entry because the module
        field of a warnings filter is matched against a *file path*, so a pattern
        like ``breadsched\\..*`` silently never matches and the filter does nothing.
        """
        import importlib
        import pkgutil
        import warnings

        import breadsched

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for module in pkgutil.walk_packages(breadsched.__path__, "breadsched."):
                if module.name.startswith("breadsched.gui"):
                    continue  # needs GTK; covered by the gui tests
                importlib.import_module(module.name)

        ours = [
            f"{w.filename}:{w.lineno} {w.message}" for w in caught if str(SRC) in str(w.filename)
        ]
        assert ours == []

    def test_no_blanket_warning_suppression_in_the_config(self):
        """PyGObject's noise is handled at the import, not hidden across the suite.

        A ``filterwarnings`` entry would silence the message for the tests only,
        leaving it in place for anyone embedding this code, and the blunt version
        of it would also have hidden the Gtk.CssProvider deprecation this project
        did need to act on.
        """
        import tomllib

        root = Path(__file__).resolve().parent.parent
        config = tomllib.loads((root / "pyproject.toml").read_text())
        filters = config["tool"]["pytest"]["ini_options"].get("filterwarnings", [])
        assert not any(entry.split(":")[0] == "ignore" and "::" in entry for entry in filters), (
            "suppress warnings at their source, not by category across the suite"
        )

    def test_gi_is_imported_in_exactly_one_place(self):
        """Version pinning only works if it happens before the first import."""
        offenders = []
        for module in modules_under("gui"):
            if module.name == "gi_setup.py":
                continue
            tree = ast.parse(module.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    offenders.extend(
                        f"{module.name}: import {alias.name}"
                        for alias in node.names
                        if alias.name == "gi" or alias.name.startswith("gi.")
                    )
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    # Absolute only: `from .gi_setup import Gtk` is the correct
                    # form, and its module name also begins with "gi".
                    name = node.module or ""
                    if name == "gi" or name.startswith("gi."):
                        offenders.append(f"{module.name}: from {name} import ...")
        assert offenders == []

    def test_the_pinned_versions_are_declared_before_the_import(self):
        """require_version after the first import of a namespace does nothing."""
        source = (SRC / "gui" / "gi_setup.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        pin_lines, import_lines = [], []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "require_version":
                pin_lines.append(node.lineno)
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("gi.repository"):
                import_lines.append(node.lineno)
        assert pin_lines and import_lines
        assert max(pin_lines) < min(import_lines)

    def test_no_magic_enum_numbers_in_the_gui(self):
        """set_ellipsize(3) is a latent bug: the constant is not ours to assume."""
        offenders = []
        for module in modules_under("gui"):
            source = module.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "attr", "")
                if not name.startswith("set_"):
                    continue
                for argument in node.args:
                    if (
                        isinstance(argument, ast.Constant)
                        and isinstance(argument.value, int)
                        and name in _ENUM_SETTERS
                    ):
                        offenders.append(f"{module.name}: {name}({argument.value})")
        assert offenders == []


#: Setters whose argument is an enum, where a bare integer would be a magic number.
_ENUM_SETTERS = {
    "set_ellipsize",
    "set_wrap_mode",
    "set_justify",
    "set_orientation",
    "set_align",
    "set_halign",
    "set_valign",
    "set_policy",
    "set_line_join",
}


class TestPluginRegistry:
    def test_every_importer_declares_how_to_recognise_its_files(self):
        from breadsched.gen.plug import IMPORTER, PluginManager

        for plugin in PluginManager.instance().by_category(IMPORTER):
            assert plugin.extensions or plugin.sniff is not None
            assert plugin.description

    def test_registering_a_new_importer_needs_no_edits_elsewhere(self, tmp_path):
        """The registry is the extension point; nothing else hard-codes formats."""
        from breadsched.gen.plug import IMPORTER, Plugin, PluginManager

        manager = PluginManager()
        marker = tmp_path / "ledger.qif"
        marker.write_text("!Type:Bank")
        manager.register(
            Plugin(
                id="qif",
                name="QIF",
                category=IMPORTER,
                run=lambda *a, **k: None,
                description="Quicken interchange",
                extensions=[".qif"],
            )
        )
        assert manager.for_file(str(marker), IMPORTER).id == "qif"


class TestTheResolverItself:
    """The import checker is only worth having if it would catch the real bug."""

    def test_it_rejects_the_bug_that_shipped(self):
        # gui/views/_base.py once said `from ..gen.db.sqlite import DbSQLite`.
        target = resolve_relative(SRC / "gui" / "views" / "_base.py", 2, "gen.db.sqlite")
        assert target == ["breadsched", "gui", "gen", "db", "sqlite"]
        assert not module_exists(target)

    def test_it_accepts_the_corrected_form(self):
        target = resolve_relative(SRC / "gui" / "views" / "_base.py", 3, "gen.db.sqlite")
        assert target == ["breadsched", "gen", "db", "sqlite"]
        assert module_exists(target)

    def test_it_resolves_a_sibling_import(self):
        target = resolve_relative(SRC / "gui" / "views" / "accounts.py", 1, "_base")
        assert module_exists(target)

    def test_it_resolves_a_package_import(self):
        target = resolve_relative(SRC / "gui" / "views" / "projection.py", 3, "gen.lib")
        assert module_exists(target)

    def test_it_reports_climbing_above_the_package_root(self):
        assert resolve_relative(SRC / "gui" / "app.py", 5, "gen") is None


class TestPackagedResources:
    def test_declared_gui_stylesheet_exists(self):
        root = Path(__file__).resolve().parent.parent
        assert (root / "src/breadsched/gui/resources/style.css").is_file()

    def test_application_loads_css_from_the_packaged_resource(self):
        source = (SRC / "gui" / "app.py").read_text(encoding="utf-8")
        assert 'joinpath("resources/style.css")' in source
        assert 'STYLE = """' not in source

    def test_all_web_assets_are_declared_as_package_data(self):
        import tomllib

        root = Path(__file__).resolve().parent.parent
        config = tomllib.loads((root / "pyproject.toml").read_text())
        declared = config["tool"]["setuptools"]["package-data"]["breadsched"]
        assert "web/static/*.html" in declared
        assert "web/static/*.css" in declared
        assert "web/static/*.js" in declared


class TestCommunityHealth:
    """Reports never invite real financial data, and vulnerabilities go privately."""

    ROOT = Path(__file__).resolve().parent.parent

    def test_security_policy_routes_reports_privately(self):
        policy = (self.ROOT / "SECURITY.md").read_text(encoding="utf-8")
        assert "Report a vulnerability" in policy
        assert "Never attach an unsanitized financial book" in policy
        config = (self.ROOT / ".github/ISSUE_TEMPLATE/config.yml").read_text(encoding="utf-8")
        assert "blank_issues_enabled: false" in config
        assert "/security/advisories/new" in config

    @pytest.mark.parametrize("form", ["bug_report.yml", "feature_request.yml"])
    def test_issue_forms_require_the_privacy_confirmation(self, form):
        text = (self.ROOT / ".github/ISSUE_TEMPLATE" / form).read_text(encoding="utf-8")
        privacy = text.split("id: privacy", 1)[1]
        assert "no unsanitized financial data" in privacy
        assert "required: true" in privacy
        assert "\t" not in text

    def test_every_path_has_a_code_owner(self):
        owners = (self.ROOT / ".github/CODEOWNERS").read_text(encoding="utf-8")
        assert any(line.startswith("* @") for line in owners.splitlines())
