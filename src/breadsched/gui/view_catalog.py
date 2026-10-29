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
    "VIEW_ACTIONS",
    "ViewAction",
    "toolbar_icons",
    "view_action_name",
]

#: (label, icon, action, tooltip) for the toolbar's actions; a None action inserts
#: a separator. The view icons (from ``CATEGORIES``) follow these. Opening and
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

#: (key, label, icon) for each view. Every view has one toolbar icon and one View
#: menu item; no two views share an icon.
CATEGORIES = [
    ("dashboard", "Dashboard", "go-home-symbolic"),
    ("fsa-dashboard", "FSA Dashboard", "emoji-body-symbolic"),
    ("accounts", "Accounts", "view-list-symbolic"),
    ("register", "Register", "text-x-generic-symbolic"),
    ("scheduled", "Scheduled", "alarm-symbolic"),
    ("upcoming", "Upcoming", "x-office-calendar-symbolic"),
    ("resolution", "Review", "dialog-question-symbolic"),
    ("plan", "Plan", "x-office-spreadsheet-symbolic"),
    ("projection", "Projection", "network-cellular-signal-excellent-symbolic"),
]


@dataclass(frozen=True)
class ViewAction:
    """One view-level command, exposed as ``win.<view>-<name>`` (#156).

    Every view action appears in the menus; ``toolbar`` ones also appear as icons
    while their view is current, and ``toggle`` ones are checkable View-menu items
    whose handler receives the new boolean. ``report`` ones open a report and are
    listed on the **Reports** menu instead of under Actions (#336). The handler is
    a method of that view. ``caption`` is the toolbar icon's short caption; it
    defaults to the label.
    """

    name: str
    label: str
    handler: str
    icon: str | None = None
    toolbar: bool = False
    toggle: bool = False
    caption: str | None = None
    report: bool = False


#: Commands per view. Buttons that act on a table's selected row (a schedule, a
#: review candidate) and controls that apply a view's own settings stay beside
#: that table or setting; everything else lives here, not in stray view buttons.
VIEW_ACTIONS: dict[str, tuple[ViewAction, ...]] = {
    "dashboard": (
        ViewAction(
            "configure",
            "_Configure Dashboard…",
            "_on_configure",
            "emblem-system-symbolic",
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
        ViewAction(
            "manage-claims",
            "Manage _FSA Claims…",
            "_on_fsa_claims",
            "folder-documents-symbolic",
            toolbar=True,
        ),
    ),
    "accounts": (
        ViewAction("new-account", "_New Account…", "_on_new_account", "folder-new-symbolic", True),
        ViewAction(
            "edit-account",
            "_Edit Account…",
            "_on_edit_selected",
            "document-properties-symbolic",
            True,
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
        ViewAction("holdings", "Holdings and _Cost Basis…", "_on_holdings", report=True),
        ViewAction("realized-gains", "_Realized Gains…", "_on_realized_gains", report=True),
        ViewAction("tax-year", "_Tax Year…", "_on_tax_year", report=True),
        ViewAction("online-quotes", "Online _Quotes…", "_on_online_quotes"),
        ViewAction("hide-empty", "_Hide Empty Accounts", "set_hide_empty", toggle=True),
        ViewAction("show-hidden", "Show Hi_dden Accounts", "set_show_hidden", toggle=True),
    ),
    "register": (
        ViewAction("enter", "_Enter Transaction", "enter_transaction"),
        ViewAction("cancel", "_Cancel Transaction", "cancel_transaction"),
        ViewAction("edit-in-place", "Edit Transaction in _Place", "edit_selected_in_place"),
        ViewAction("track-reimbursable", "Track as Rei_mbursable…", "track_selected_reimbursable"),
        ViewAction("new-window", "Open in New _Window", "_on_open_window_clicked"),
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
    ),
    # A command whose button changes with the view's state (Reconcile, Save as
    # Scenario) stays in its view; the register keeps its own buttons because a
    # separate register window has no toolbar. Reconcile is also a register icon
    # in the main toolbar, where reconciling is looked for first (#361). Review
    # Due Transactions… is the Actions menu's general command, with a button on
    # the Upcoming view.
    "scheduled": (
        ViewAction(
            "new-scheduled",
            "_New Scheduled Transaction…",
            "_on_new_clicked",
            "document-new-symbolic",
            True,
            caption="New Scheduled",
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
        ViewAction(
            "explore-expenses",
            "_Explore Expenses…",
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
            "compare",
            "_Compare With…",
            "_on_compare_clicked",
            "view-dual-symbolic",
            True,
            caption="Compare",
        ),
        ViewAction("save-scenario", "_Save as Scenario…", "_on_save_clicked"),
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
}


def toolbar_icons() -> list[tuple[str, str]]:
    """(icon, label) for every icon the main window's toolbar can show."""
    icons = [(icon, label) for label, icon, action, _tip in TOOLBAR if action and icon and label]
    icons += [(icon, label) for _key, label, icon in CATEGORIES]
    icons += [
        (item.icon, item.label.replace("_", ""))
        for actions in VIEW_ACTIONS.values()
        for item in actions
        if item.toolbar and item.icon
    ]
    return icons


def view_action_name(key: str, action: ViewAction) -> str:
    return f"{key}-{action.name}"
