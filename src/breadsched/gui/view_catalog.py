"""What the main window offers: toolbar actions, views, and each view's commands.

Plain data, with no GTK: the window (``viewmanager``) builds its toolbar, View and
Actions menus, and keyboard actions from it, and the application registers one
action per view command. Keeping it apart lets the catalog be read and tested
without a display.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "CATEGORIES",
    "SHARED_ICONS",
    "TOOLBAR",
    "TOOLBAR_VIEWS",
    "VIEW_ACTIONS",
    "ViewAction",
    "row_commands",
    "toolbar_icons",
    "view_action_name",
]

#: (label, icon, action, tooltip) for the toolbar's actions; a None action inserts
#: a separator. The work-area icons (``TOOLBAR_VIEWS``) follow these. Opening and
#: importing a book are File menu and start-screen commands, not toolbar icons on
#: every view (#361).
TOOLBAR = [
    ("Undo", "edit-undo-symbolic", "app.undo", "Undo the last change"),
    ("Redo", "edit-redo-symbolic", "app.redo", "Redo the last undone change"),
    (None, None, None, None),
    ("Transaction", "list-add-symbolic", "app.new-transaction", "Enter a new transaction"),
    (None, None, None, None),
    ("Print", "document-print-symbolic", "win.print-view", "Print the current report"),
]

#: (key, label, icon) for each view. Every view has one View menu item; no two
#: views share an icon.
CATEGORIES = [
    ("dashboard", "Dashboard", "go-home-symbolic"),
    ("fsa-dashboard", "FSA Dashboard", "emoji-body-symbolic"),
    ("accounts", "Accounts", "view-list-symbolic"),
    ("register", "Register", "text-x-generic-symbolic"),
    ("scheduled", "Scheduled", "alarm-symbolic"),
    ("resolution", "Review", "dialog-question-symbolic"),
    ("plan", "Plan", "x-office-spreadsheet-symbolic"),
    ("projection", "Projection", "network-cellular-signal-excellent-symbolic"),
]

#: The work areas with a toolbar icon of their own. The FSA Dashboard and Review are
#: reached from the View menu and from the commands that lead to them (Manage FSA
#: Claims, Resolve Actuals), which leaves the toolbar room for the current view's
#: commands.
TOOLBAR_VIEWS = ("dashboard", "accounts", "register", "scheduled", "plan", "projection")


@dataclass(frozen=True)
class ViewAction:
    """One view-level command, exposed as ``win.<view>-<name>`` (#156).

    Every view action appears in the menus; ``toolbar`` ones also appear as icons
    while their view is current, and ``toggle`` ones are checkable View-menu items
    whose handler receives the new boolean. ``report`` ones open a report and are
    listed on the **Reports** menu instead of under Actions (#336). The handler is
    a method of that view. ``caption`` is the toolbar icon's short caption; it
    defaults to the label.

    A ``row`` command acts on the row selected in the view: it is greyed out until
    the view says a row is selected (``BaseView.enable_commands``), and it is also
    in that row's right-click menu. An ``app_action`` is a command that works
    anywhere (such as ``app.post-scheduled``) shown among this view's toolbar icons;
    the menus list it once, with the other commands that work anywhere.
    """

    name: str
    label: str
    handler: str
    icon: str | None = None
    toolbar: bool = False
    toggle: bool = False
    caption: str | None = None
    report: bool = False
    row: bool = False
    app_action: str | None = None


#: Every command of every view, in the order its toolbar and menus show them.
#:
#: Where a command goes follows one rule, the GnuCash and Gramps arrangement:
#:
#: - The menus list every command: the current view's first under Actions (or on
#:   the Reports or View menu), so nothing is reachable only by a button.
#: - The toolbar shows the current view's everyday commands: first those on the
#:   selected row (New, Edit, Delete, Duplicate, then the view's own, such as Enter
#:   Next or Match), greyed out with nothing selected; then the view's tools
#:   (Suggest, New Loan, Reconcile...).
#: - A table row's right-click menu (or Shift+F10, or the Menu key) lists the
#:   commands on that row.
#: - Buttons inside a view only change what it shows: pickers, filters, ranges and
#:   their Apply. The register's Enter, Cancel, and Split act on the transaction
#:   being typed, as GnuCash's do; a register in a window of its own, which has no
#:   toolbar, keeps its commands as buttons too.
VIEW_ACTIONS: dict[str, tuple[ViewAction, ...]] = {
    "dashboard": (
        ViewAction(
            "arrange",
            "_Arrange Dashboard",
            "_on_arrange",
            "view-app-grid-symbolic",
            toolbar=True,
        ),
        ViewAction(
            "net-worth-history",
            "Net Worth _History…",
            "_open_net_worth_history",
            "document-open-recent-symbolic",
            True,
            caption="History",
            report=True,
        ),
    ),
    "fsa-dashboard": (
        # One command for claims: the selected claim if there is one, otherwise the
        # claims window as it opens anywhere (Edit FSA Claim and Manage FSA Claims
        # did the same thing twice).
        ViewAction(
            "edit-claims",
            "Edit _FSA Claims…",
            "edit_claims",
            "document-edit-symbolic",
            True,
            caption="Edit Claims",
            row=True,
        ),
        ViewAction(
            "link-proposals",
            "_Link Chosen Statement Lines",
            "_on_link_selected",
            "insert-object-symbolic",
            True,
            caption="Link Lines",
        ),
    ),
    "accounts": (
        ViewAction("new-account", "_New Account…", "_on_new_account", "folder-new-symbolic", True),
        ViewAction(
            "open-register",
            "_Open Register",
            "open_selected_register",
            "document-open-symbolic",
            True,
            caption="Open",
            row=True,
        ),
        ViewAction(
            "edit-account",
            "_Edit Account…",
            "_on_edit_selected",
            "document-edit-symbolic",
            True,
            caption="Edit",
            row=True,
        ),
        ViewAction(
            "delete-account",
            "_Delete Account…",
            "delete_selected_account",
            "edit-delete-symbolic",
            True,
            caption="Delete",
            row=True,
        ),
        ViewAction(
            "security-price",
            "_Security Price…",
            "_on_security_price",
            "accessories-calculator-symbolic",
            True,
        ),
        ViewAction(
            "exchange-rate",
            "E_xchange Rate…",
            "_on_exchange_rate",
            "object-flip-horizontal-symbolic",
            True,
        ),
        ViewAction("online-quotes", "Online _Quotes…", "_on_online_quotes"),
        ViewAction("holdings", "Holdings and _Cost Basis…", "_on_holdings", report=True),
        ViewAction("realized-gains", "_Realized Gains…", "_on_realized_gains", report=True),
        ViewAction("tax-year", "_Tax Year…", "_on_tax_year", report=True),
        ViewAction("hide-empty", "_Hide Empty Accounts", "set_hide_empty", toggle=True),
    ),
    "register": (
        ViewAction(
            "edit-transaction",
            "_Edit Transaction…",
            "open_full_editor",
            "document-edit-symbolic",
            True,
            caption="Edit",
            row=True,
        ),
        ViewAction(
            "delete-transaction",
            "_Delete Transaction…",
            "delete_selected_transaction",
            "edit-delete-symbolic",
            True,
            caption="Delete",
            row=True,
        ),
        ViewAction(
            "make-scheduled",
            "Make _Scheduled…",
            "make_selected_scheduled",
            "appointment-new-symbolic",
            True,
            caption="Schedule",
            row=True,
        ),
        ViewAction(
            "match-to-plan",
            "Match to _Plan…",
            "match_selected_to_plan",
            "insert-link-symbolic",
            True,
            caption="Match",
            row=True,
        ),
        ViewAction(
            "split",
            "Show Transaction Sp_lits",
            "toggle_split",
            row=True,
        ),
        ViewAction(
            "track-reimbursable",
            "Track as Rei_mbursable…",
            "track_selected_reimbursable",
            row=True,
        ),
        ViewAction(
            "track-fsa-claim",
            "Track as _FSA Claim…",
            "track_selected_fsa_claim",
            row=True,
        ),
        # Enter and Edit in Place are the register's own keys and Enter button, not
        # menu commands; Cancel stays, as Escape is easy to miss.
        ViewAction("cancel", "_Cancel Transaction", "cancel_transaction"),
        # GnuCash's Blank: to the new transaction at the end (Ctrl+End, Shift+Page Down).
        ViewAction(
            "blank-transaction",
            "_Blank Transaction",
            "to_blank_transaction",
            "go-bottom-symbolic",
            True,
            caption="Blank",
        ),
        ViewAction(
            "reconcile",
            "_Reconcile…",
            "_on_reconcile_clicked",
            "object-select-symbolic",
            True,
            caption="Reconcile",
        ),
        ViewAction(
            "new-tab",
            "Open Register in New _Tab",
            "open_in_new_tab",
            "tab-new-symbolic",
            True,
            caption="New tab",
        ),
        ViewAction("new-window", "Open in New _Window", "_on_open_window_clicked"),
    ),
    # Scheduled lists the definitions and, above them, their occurrences by date
    # (what the Upcoming view used to show). A command acts on the selected
    # schedule, or on the schedule of the selected occurrence; Match acts on the
    # selected occurrence, or on the selected schedule's current one.
    "scheduled": (
        ViewAction(
            "new-scheduled",
            "_New Scheduled Transaction…",
            "_on_new_clicked",
            "document-new-symbolic",
            True,
            caption="New",
        ),
        ViewAction(
            "edit",
            "_Edit Scheduled Transaction…",
            "_on_edit_clicked",
            "document-edit-symbolic",
            True,
            caption="Edit",
            row=True,
        ),
        ViewAction(
            "delete",
            "Dele_te Scheduled Transaction…",
            "_on_delete_clicked",
            "edit-delete-symbolic",
            True,
            caption="Delete",
            row=True,
        ),
        ViewAction(
            "duplicate",
            "D_uplicate Scheduled Transaction…",
            "_on_duplicate_clicked",
            "edit-copy-symbolic",
            True,
            caption="Duplicate",
            row=True,
        ),
        ViewAction(
            "enter-next",
            "Enter Ne_xt Occurrence",
            "_on_enter_next_clicked",
            "document-send-symbolic",
            True,
            caption="Enter Next",
            row=True,
        ),
        ViewAction(
            "skip-next",
            "S_kip Next Occurrence",
            "_on_skip_next_clicked",
            "media-skip-forward-symbolic",
            True,
            caption="Skip Next",
            row=True,
        ),
        ViewAction(
            "match",
            "_Match Occurrence…",
            "match_selected",
            "insert-link-symbolic",
            True,
            caption="Match",
            row=True,
        ),
        ViewAction(
            "review-due",
            "Review _Due Transactions…",
            "",
            "view-list-ordered-symbolic",
            True,
            caption="Review Due",
            app_action="app.post-scheduled",
        ),
        ViewAction(
            "suggest",
            "_Suggest Estimates from History…",
            "_on_suggest_clicked",
            "edit-find-replace-symbolic",
            True,
            caption="Suggest",
        ),
        ViewAction(
            "new-loan",
            "New _Loan…",
            "_on_loan_clicked",
            "x-office-document-symbolic",
            True,
            caption="New Loan",
        ),
        ViewAction(
            "payroll",
            "_Payroll…",
            "_on_payroll_clicked",
            "x-office-address-book-symbolic",
            True,
            caption="Payroll",
        ),
    ),
    # Review: the selected actual, and the selected candidate occurrence for it.
    "resolution": (
        ViewAction(
            "match",
            "_Match to Candidate",
            "_on_match",
            "insert-link-symbolic",
            True,
            caption="Match",
            row=True,
        ),
        ViewAction(
            "reject",
            "_Reject Candidate",
            "_on_reject",
            "list-remove-symbolic",
            True,
            caption="Reject",
            row=True,
        ),
        ViewAction(
            "skip",
            "_Skip Scheduled Occurrence",
            "_on_skip",
            "media-skip-forward-symbolic",
            True,
            caption="Skip",
            row=True,
        ),
        ViewAction(
            "match-split",
            "Match by S_plit…",
            "open_split_matching",
            "edit-select-all-symbolic",
            True,
            caption="By Split",
            row=True,
        ),
        ViewAction(
            "attach-fsa",
            "Attach to _FSA Claim…",
            "_on_fsa_attach",
            "emoji-body-symbolic",
            True,
            caption="FSA Claim",
            row=True,
        ),
        ViewAction(
            "unexpected",
            "Mark _Unexpected",
            "_on_unexpected",
            "mail-mark-important-symbolic",
            True,
            caption="Unexpected",
            row=True,
        ),
    ),
    "plan": (
        ViewAction(
            "new-scenario",
            "_New Scenario…",
            "_on_new_scenario",
            "document-new-symbolic",
            True,
            caption="New Scenario",
        ),
        ViewAction(
            "manage-scenarios",
            "_Manage Scenarios…",
            "_on_manage_scenarios",
            "view-grid-symbolic",
            True,
            caption="Scenarios",
        ),
        # Scenario events change the scenario chosen, so a saved one: greyed out
        # for the Base scenario.
        ViewAction(
            "add-estimate",
            "_Add Scenario Estimate…",
            "_on_add_estimate",
            "bookmark-new-symbolic",
            True,
            caption="Add Estimate",
        ),
        ViewAction(
            "alter-baseline",
            "A_lter Baseline Schedule…",
            "_on_alter_schedule",
            "document-revert-symbolic",
            True,
            caption="Alter",
        ),
        ViewAction(
            "suppress-baseline",
            "_Suppress Baseline Schedule…",
            "_on_suppress_schedule",
            "view-conceal-symbolic",
            True,
            caption="Suppress",
        ),
        ViewAction(
            "resolve-actuals",
            "_Resolve Actuals…",
            "_on_resolve_actuals",
            "dialog-question-symbolic",
            True,
            caption="Resolve Actuals",
        ),
        ViewAction(
            "explore-expenses",
            "_Expense Explorer…",
            "_open_expense_explorer",
            "system-search-symbolic",
            True,
            caption="Explore",
            report=True,
        ),
        ViewAction("budget-jars", "_Budget Jars…", "_open_budget_jars", report=True),
    ),
    "projection": (
        ViewAction(
            "explain-month",
            "Ex_plain Month…",
            "_on_explain_clicked",
            "help-about-symbolic",
            True,
            caption="Explain",
        ),
        ViewAction(
            "compare",
            "_Compare With…",
            "_on_compare_clicked",
            "view-dual-symbolic",
            True,
            caption="Compare",
        ),
        ViewAction(
            "save-scenario",
            "_Save Scenario",
            "_on_save_clicked",
            "document-save-as-symbolic",
            True,
            caption="Save Scenario",
        ),
        ViewAction(
            "export",
            "E_xport Projection…",
            "_on_export_clicked",
            "document-save-symbolic",
            True,
            caption="Export",
        ),
        ViewAction(
            "open-tab",
            "Open Scenario in New _Tab",
            "open_in_new_tab",
            "tab-new-symbolic",
            True,
            caption="New tab",
        ),
    ),
}


#: The only icons more than one toolbar command may show, each with the one meaning
#: it has wherever it appears (#336). Every other toolbar icon means one command.
SHARED_ICONS = {
    "document-new-symbolic": "New",
    "tab-new-symbolic": "in New Tab",
    "document-edit-symbolic": "Edit",
    "edit-delete-symbolic": "Delete",
    "insert-link-symbolic": "Match",
    "media-skip-forward-symbolic": "Skip",
}


def toolbar_icons() -> list[tuple[str, str]]:
    """(icon, label) for every icon the main window's toolbar can show."""
    icons = [(icon, label) for label, icon, action, _tip in TOOLBAR if action and icon and label]
    icons += [(icon, label) for key, label, icon in CATEGORIES if key in TOOLBAR_VIEWS]
    icons += [
        (item.icon, item.label.replace("_", ""))
        for actions in VIEW_ACTIONS.values()
        for item in actions
        if item.toolbar and item.icon
    ]
    return icons


def row_commands(key: str) -> tuple[ViewAction, ...]:
    """The commands on a row of ``key``'s tables, as its right-click menu lists them."""
    return tuple(item for item in VIEW_ACTIONS.get(key, ()) if item.row)


def view_action_name(key: str, action: ViewAction) -> str:
    return f"{key}-{action.name}"
