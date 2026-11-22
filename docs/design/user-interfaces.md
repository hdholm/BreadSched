# User interfaces

Part of the [BreadSched design](../../DESIGN.md).

## GTK window, toolbar, and tabs

The main window has no sidebar, which duplicated the View menu and took width from
every view. The toolbar is arranged around the current view: actions
that work anywhere (`view_catalog.TOOLBAR`: undo, redo, new transaction, print;
opening and importing a book are File-menu and start-screen commands), one toggle
per other main work area (`view_catalog.TOOLBAR_VIEWS`: Dashboard, Accounts,
Register, Scheduled, Plan, Projection; the FSA Dashboard and Review are on the View
menu and reached from the commands that lead to them, which leaves the toolbar room
for the current view's commands), the current view's name in bold
(`view_heading`) as the divider, its own command icons (`view_tools`, including the
register's Reconcile), and, only on `viewmanager.SUMMARY_VIEWS` (Dashboard and
Accounts), the book's account and transaction counts. The toolbar and the tab row scroll rather than setting
the window's minimum width, by arrows at their ends rather than a scrollbar
(`widgets/scrolling_bar.ScrollingBar`, as GnuCash's tabs do): a scroller with an
`EXTERNAL` policy and no scrollbar, between a left and a right arrow shown only while
the row overflows and greyed out at its ends; a wheel scrolls it too, and choosing a
tab brings it into view (`reveal`). The arrows are shown or hidden at idle, since the
adjustment changes during layout. The View menu and the view icons both target the
stateful `win.show-category` action, and `show_category()` is the single navigation
entry point, so the active icon and menu item always agree however a view was
reached.

`gui/view_catalog` holds that toolbar, the views, and every view's commands as
plain data with no GTK import (an architecture test checks it), so the window and
the application build from one catalog that can be read without a display;
`viewmanager` re-exports it.

View commands are declared once in `view_catalog.VIEW_ACTIONS`. Each becomes a
`win.<view>-<name>` action, is listed in **Actions** (a `toggle` on **View**, a
`report` on **Reports**), and appears in the toolbar only while its view is current.
**Actions** lists the current view's commands first, then the commands that work
anywhere, then every other view's commands under **Other Views**, so each command
has exactly one item.

Where a command goes follows one rule, the GnuCash and Gramps arrangement, which
replaced a mix in which a view's row commands (Edit, Delete, Enter Next, Match...)
were buttons beside its table while its larger tools (Suggest, New Loan) were
toolbar icons:

- **Menus list every command.** A `row` command (one acting on the selected row) is
  listed only under the current view, never under **Other Views**: the row it acts
  on is one you can see. An `app_action` item (Scheduled's **Review Due**, which is
  `app.post-scheduled`) is the application's own action shown among a view's toolbar
  icons; **Actions** lists it once, under the view that shows it or else with the
  commands that work anywhere.
- **The toolbar shows the current view's everyday commands**, its row commands first
  (New, Edit, Delete, Duplicate, then the view's own), then its tools. A view says
  which commands it can use now with `BaseView.enable_commands(name=bool)`; a `row`
  command is greyed out until it does, and `ViewManager.apply_view_commands` applies
  the displayed view's states to the shared window actions, again whenever the
  displayed view (or register tab) changes.
- **A row's right-click menu** (`_base.attach_row_menu`, also Shift+F10 and the Menu
  key) is a `Gtk.PopoverMenu` of the view's `row` commands, narrowed per table when a
  view has two (Review's actuals and candidates). The view (a `Gtk.Box`, which lets
  go of every child as it goes) holds the menu, not the table: a `Gtk.ListBox` being
  disposed loops forever trying to remove a popover that is not one of its rows,
  which would hang closing the window. A right-click first selects the
  row under the pointer: a `Gtk.ColumnView` cell carries its row's item
  (`mark_row`), whose position `column_view_row_at` looks up when clicked (rows
  opening above a row move it without its cells being bound again, so a position
  noted at bind time goes stale), a `Gtk.ListBox` row is found by height, and the
  register moves its cursor (`RegisterGrid.point_at`). The register's
  menu takes the keys in the capture phase, ahead of the cell editor's own text menu.
- **Buttons inside a view only change what it shows**: pickers, filters, ranges and
  their Apply, and the Dashboard's arranging mode (Add Item, Reset Layout, Cancel,
  Done), which edits the layout shown. The register's Enter, Cancel, and Split act on
  the transaction being typed, as GnuCash's do. A register in a window of its own
  has no toolbar or menus: it shows Edit, New, New Window, and Reconcile as buttons
  (`RegisterView.use_own_window`) and holds its own `win` action group, so its
  right-click menu acts on that register.

Upcoming is no longer a view: its by-date occurrences, look-ahead, and overdue note
are the first table of Scheduled, which keeps one selection across its three tables,
as the browser's Scheduled page already showed them. A remembered `upcoming` tab
reopens Scheduled. A `report` action is not on **Actions**: the **Reports** menu
lists every report once from `gui/reports_window.report_specs()` (the interactive
reports, then each report breadlet that is not one of them) as `app.open-report`
with the report's key, and a view's `report` action opens the same key.

**The Reports window** (`gui/reports_window.py`, one per main window, closed when
the book is) shows each open report in its own closable, reorderable notebook tab,
so reports can be compared. A report is still built as its own window (its class,
tests and printing are unchanged); `ReportsWindow.adopt` moves that window's content
into a tab and keeps the window as the tab's controller. A window never shown is not
sent `close-request`, so closing a tab, or the report's own **Close**, sends it before
the tab goes, letting a report stop its background work. The Expense Explorer and
Budget Jars take the Plan view's range, grouping and scenario when the Plan is open
(`_plan_context`), else the Plan's defaults. An item that asks for more input ends in "…", and the one New
Transaction command (`app.new-transaction`, Ctrl+T) starts in the shown register's
account (`ViewManager.shown_register`). Toolbar icons are unique per command:
`view_catalog.toolbar_icons()` lists them and only `SHARED_ICONS`
(`document-new` for New…, `tab-new` for …in New Tab, `document-edit` for Edit,
`edit-delete` for Delete, `insert-link` for Match, `media-skip-forward` for Skip) may
repeat, each with that one meaning; a test enforces it. The menu model is rebuilt only
when the view changes: opening a submenu moves focus into its popover, and
rebuilding the same view's model then destroyed the open submenu and crashed GTK.

The reconcile window (`dialogs/reconciliation_dialog`) is the one statement
dialog that is not modal: `ReconciliationDialog.present_for` keeps one per book and
account and brings it forward, and the window follows `database-changed` (coalesced
on idle) so a transaction entered or edited elsewhere appears in its Funds In and
Funds Out lists while it stays open. Each check is saved through
`update_reconciliation` at once, so Postpone is a plain close and Abandon is
`cancel_reconciliation`; multi-selection checks or unchecks several rows in one
update. Rows for `after_statement` candidates sit below an "After" heading with the
`after-statement` style. The mouse is the dialog's own: a capture-phase click
gesture on each list claims a row click for `ReconciliationDialog.click_row` (a
click on the check is left to the check). A plain click selects only that row,
across both lists, and toggles its check. Ctrl and Shift select without checking.
A double-click puts the check back and edits. Keyboard selection in
one list empties the other, so the edit and check commands act on the row in view.
Each list has its own sort buttons (✓, Date, Description, Amount; the same one again
reverses), and both GTK and the browser order rows with the shared
`reconciliation.sort_candidates` (`CANDIDATE_SORTS`): size for amounts, checked
first, ties in date order, and `after_statement` rows always last. The browser asks
`GET /api/reconciliation?sort=…&descending=…` after saving its checks.
The browser's reconcile table has an **Edit** per row that saves the checks and
opens the row in the register with **Back to reconciliation**.

GTK's **Track as Reimbursable…** saves the register's unsaved typing first and
opens the dialog over the register's own window, never over another one that was
active, such as the guide. Each refusal is an alert as well as the register's status
line.

The FSA claims window (`dialogs/fsa_claims_dialog`) is likewise not modal and
one per book (`FsaClaimsDialog.present_for`): it is the application action
`app.fsa-claims`, listed in every view's Actions menu (on the FSA Dashboard, as that
view's own command and toolbar icon instead, beside **Edit FSA Claim**, which opens
the claim selected in the dashboard's claims table, and **Link Chosen Statement
Lines**), and it refreshes its candidate
transactions on `database-changed`. Its chooser's first entry is the new claim. Each
link list pins the linked splits above a candidate list filtered by search text and
by range (60 days around the service date, the claim's benefit year, or all) and
capped at `SHOWN_LIMIT` rows.

The Dashboard is where a book opens and its tab is pinned first
(`viewmanager.HOME_VIEW`, `_Tab.pinned`): it has no close button, cannot be moved,
and nothing can be put before it. It is the one place that answers "where do I
stand" without choosing anything, so every other view is a step away from it.

Below the toolbar, a tab bar lists each open view, one tab per open register, and
one per pinned scenario Projection (`ViewManager._tabs`). Register tabs are
`RegisterView`s in their own stack, and `_views["register"]` always names the one
shown, so callers that address "the register" keep working. A pinned
`ProjectionView` keeps its scenario and only recalculates when inherited Base
assumptions change, while the unpinned Projection and Plan follow the selected
scenario. Choosing a tab, a view icon, or a menu item leaves the keyboard in the
shown view (`ViewManager._focus_view`, a register's cursor cell); tab buttons
do not take focus on click. Ctrl+Page Up and Ctrl+Page Down step through the tabs.
A tab can be dragged onto another to take its place, or moved one place with
Ctrl+Shift+Page Up/Down (**View → Move Tab Left/Right**); `ViewManager.move_tab`
reorders the bar and the remembered tabs keep the new order.
Ctrl+W closes the current one, and Ctrl+F (**Edit → Find in Register**) goes to the
register's filter, which matches with `ledger.transaction_matches`, as the browser
register's `filter` does: date, number, description, notes, tags, memos,
accounts, and amounts as shown or typed. Ctrl+Shift+F (**Edit → Find
Transactions…**, `app.find-transactions`) opens `dialogs.find_dialog.FindDialog`,
which searches every register through `engine.transaction_query` (see valuation and
reporting) and opens a found split's register when a row is activated. Closing a register with unsaved typing asks first; opening another book
closes every tab, because tabs name the old book's accounts. Each book's tabs are
remembered in `views.ini` (section `open-tabs`, keyed by a digest of the book path)
and restored behind the Dashboard, skipping deleted accounts and scenarios. Only the
Dashboard is drawn on opening: a view the window holds but does not show is attached
to the book without being drawn (`BaseView.set_db` asks
`ViewManager.defers_refresh`), and a register is created with its account set so it
reads that account once. Opening a book therefore takes as long as drawing one view,
however many tabs it remembers.

Slow views are calculated off GTK's loop. `gui.background.
SnapshotReader` is one worker thread per open book that runs book-server reads against
a read-only snapshot (`DbSQLite.load(path, "r")`, an in-memory copy, so a long read
neither blocks a save nor sees part of one), reusing the snapshot until the book's
revision changes. A view that can split its refresh into a read and a draw overrides
`BaseView.background_refresh` (the Plan does, through `book.plan`, and the FSA
Dashboard, reading its claims and benefit years once); its redraw after a
change, or when shown out of date, then keeps the old figures under an **Updating…**
label and draws the new ones when they arrive, rereading if the book changed meanwhile.
`ViewManager._prefill` redraws hidden tabs of such views (`BACKGROUND_VIEWS`) shortly
after the book opens and after each change, so switching to them is immediate.
Registers read the same way: a full read is `book.register_view(account, None)` (every
row, with headings, the security held, and which accounts hold securities), which
`RegisterSheet.apply_view` lays out, so restored and hidden register tabs are filled
in the background and switching to one is immediate. A register a few saved
transactions behind still rereads only those in place. Account names come from
`book.accounts()`. Accounts reads one `book.chart(include_hidden=True)` (each account's
value and quote evidence) and `book.balance_summary()` (cash, assets, liabilities, and
net worth), off GTK's loop as well; its filters re-lay the tree from the chart already
read. The other views still draw when shown; moving them onto the book
server brings them to the same path. An in-memory book has no snapshot and draws in
place. The
browser has no tab bar: `openInNewTab` opens a browser tab with the view and account
or scenario in its query string.

The browser's **Quick entry** page (`/quick`) is the small-screen
entry point: a separate `quick.html` that loads only `core.js` and `quick.js`
(`web.transport.QUICK_SCRIPTS`), one column at most 480 px wide with 44 px touch
targets and 16 px fields (so iOS does not zoom), a decimal keyboard for the amount,
and Spent/Received choosing which account is debited. It saves through
`/api/transaction` and suggests from `/api/entry/suggest`, so it adds no write path;
the account used last is kept in `localStorage`. The token travels in the URL's
fragment as on the full page, and **Full view** and **Quick entry** link the two.

## Freezes: the watchdog and hidden dialogs

GTK draws and handles input on one thread, so a stuck main loop (a loop that
never ends, a call that never returns) leaves every window, the window manager's
close button included, unanswered, and nothing in the process can ask anything.
`gui/watchdog.Watchdog`, started only by the launcher (tests drive GTK without
running its loop between steps), has the loop tick a heartbeat every second and
watches it from a thread. After `STALL_SECONDS` (20, past the seconds a large
book's upgrade blocks the loop by design) without a tick it writes every thread's
stack with `faulthandler` to `hang-<time>.log` in the data folder's
`hang-reports`, naming the main thread, and keeps the latest five. It then starts
`gui/stall_prompt` as a process of its own, which asks **Wait** (the default, and
closing it) or **Quit BreadSched**, and is withdrawn when the heartbeat resumes.
Quit writes `last-hang.json` and ends the process with `os._exit(EXIT_HUNG)`: each
save is a complete SQLite transaction and a lock whose process is gone is replaced
as stale, so nothing saved is lost. On the next activation
`BreadSchedApplication.offer_hang_report` asks once whether to report it;
**Report Problem…** opens the bug form (`versioning.issue_url` with `what` and
`log`) holding the start of the log, the home directory masked as `~`. On POSIX,
`SIGUSR1` writes every thread's stack to `stacks-on-request.log`.

Work that blocks the loop by design says so with `watchdog.busy(doing, allow)`, a
context manager that is a no-op without a running watchdog. Inside it the watchdog
waits `max(STALL_SECONDS, allow)` (`BUSY_SECONDS`, 180, by default) before asking,
writes `while <doing>` in the log's first line, and passes `doing` to the prompt,
which then says what BreadSched has been doing rather than that it stopped. Opening
a book (`open_book_announced`) is wrapped as "upgrading your book" or "opening
your book". Work run off the main thread, such as imports and reports behind a
progress dialog, keeps the loop ticking and needs no announcement.

A modal dialog behind another window looks like a freeze rather than being one.
When the main window becomes active it presents the visible modal windows that
stand over it (`_belongs_to` follows their transient-for chain), and
`app.show-all-windows` (**View → Bring All Windows Forward**) presents every
window of the application, modal ones last so they end on top.

## Tables, dialogs, and bounded sizes

Editors disclose progressively: the fields most entries need come first, and
what is set once (defaults, planning classifications) or rarely (future changes) is
a tab or a fold away, never removed. The desktop uses `widgets.form_pages`
(`FormPages`, a notebook whose tab can carry a count, and `FormGrid` label/field
rows); the browser uses `details.form-section`. A page or fold that holds something
says so in its title (`Future changes (2)`, `Tags & documents (1)`) or opens by
itself, so nothing set is hidden from view. A field that applies only to some
accounts (investment activity) shows only for those, or when it already holds a
choice. The schedule and scenario schedule editors share `update_schedule_pages`;
the transaction editor's per-split planning purpose and investment activity are
columns of its split grid that its **Planning detail** toggle adds
(`SplitSheet.set_detail`).

Every table except the register grid (below) is built with `_base.table_section()`: a heading row with the table's
own column chooser (`column_menu`, which also moves columns, and
`arrange_columns`, which keeps one expanding column, the last marked to expand,
and remembers order and widths by title under `columns:<view id>`, the first column
pinned first) (never in the view toolbar, where identical icons could not be
told apart; the Dashboard hides its tables' choosers and lists the same columns,
under the same `columns:<view id>` settings, on the Columns page of
`dialogs.dashboard_dialog.DashboardDialog`) above a scrolled `Gtk.ColumnView` carrying the `data-table` class, which
stripes rows so they are distinguishable at rest. Numeric cells carry `numeric` for
tabular figures and a small right padding. Tables never scroll sideways: a narrowing
window gives each column between its minimum and natural width, text cells ellipsize,
and amount cells never do, so figures are never truncated. The appearance corrects
concrete overlap and legibility problems; outside the register, reproducing
GnuCash's look is not a goal.

Scrollbars are always shown: `app.show_scrollbars()` turns GTK's
`gtk-overlay-scrolling` setting off at start-up, where GTK has it, so a list longer
than its window (the due review, for example) shows a permanent scrollbar rather
than a thin indicator that appears only while scrolling; the stylesheet
widens the slider to 10 pixels.

Every dialog follows one set of conventions. `widgets.bounded.BoundedWindow`
closes on Escape and makes its main button (the `suggested-action` button in its
last row, or one named with `set_primary`) the default, so Enter in a one-line
field activates it; a field whose Enter does its own job is marked with
`keeps_enter`. Editors close with **Cancel** and save with **Save** (or a named
save); viewers and reports close with **Close**. Every dialog has a **Help** row
naming a `user_guide.HELP_TOPICS` topic (`TestDialogConventions` checks both). Every
Delete is a `widgets.confirm.delete_button`: styled `destructive-action`, it asks
"Delete …?" with Cancel as the default before deleting, even where Ctrl+Z could
restore it. The browser confirms every delete the same way.

Window sizes are the user's, not a book's: `widgets.window_sizes` keeps them
in the view settings section `window-sizes` as `width,height[,maximized]`. The main
window (`main`) restores its size and maximized state when created and records them
on `close-request` and on `app.quit` (which closes windows without that signal); an
independent register window (`register-window`) records on every close. Any other
`BoundedWindow` records under its class name only when it closes at a size other
than the one GTK settled on after mapping it, so a dialog whose size follows its
content keeps doing so until the user resizes it. `present` restores before
`fit_to_screen`, so a remembered size is still capped to the monitor. Preferences
can forget them all (`forget_sizes`).

**Edit → Preferences…** (`app.preferences`, Ctrl+comma;
`dialogs.preferences_dialog.PreferencesDialog`) edits user settings that are
not part of a book: `general.last_book_path`, `register.double_line` and
`register.banding` in the view settings, and the Alpha Vantage key through
`plugins.quotes.save_alphavantage_key` (stored only if changed, so a key from the
environment is not copied into the file). **Configure Dashboard…** there
activates the window's existing `win.dashboard-configure`, because the Dashboard's
horizons, sections, and groups belong to the book. The browser has no equivalent window; its guide's Preferences
section says where each setting lives there.

A modal dialog is never shown behind the window it blocks, and never two at
once. The main window's own modal dialogs (alerts from `_report`, the GnuCash change
review, the due review) go through `ViewManager.queue_modal`: each waits until the
window is mapped and has drawn (`widgets.presented.when_presented`) and until the one
before it has closed (`ViewManager.when_closed`, on `unrealize`, since GTK 4 emits no
`hide` when a window closes). Opening a book queues its notice first, then the
reviews. Two modal dialogs open together let the later one take every input while
the earlier one covered it, so neither answered and the window stayed greyed out. Opening a book that needs a
schema upgrade first shows a busy page naming the book and its schema
(`ViewManager.show_busy`, text from `presentation.book_upgrade_progress`) on a
presented window, then migrates. A remembered book that needs no upgrade opens the
same way behind an **Opening** page (`presentation.book_opening_progress`), so a
large book's start-up is visibly under way and the start screen never flashes.
That page is the start-up splash: it leads with the BreadSched logo, as the start
screen (no book) and the About dialog do (`gui/widgets/brand.py`, which draws the
packaged logo at a fixed width and adds the packaged icons to GTK's search path).
The due review builds the rows of a schedule with several dates only when its
expander opens (`DueDialog._fill_dates`, answers held by `_Choice` until then), and
balances and "already posted" are answered in SQL (`DbSQLite.split_totals`, summed
exactly per currency and denominator, and `DbSQLite.has_scheduled_posting`) rather
than by decoding every split or the day's transactions.

Windows must stay usable on small screens whatever the book holds:

- Dialogs keep minimum sizes within 800 × 600 even with very long names
  and 40-split transactions (`TestDialogsFitTheScreen`); long forms move their body
  into a scroller (`widgets.bounded.scroll_body`) and keep the button row outside.
- Views keep within a 1024 × 700 work area (`TestBoundedSizes`). Drop-downs come
  from `widgets.choice.bounded_dropdown`, whose button ellipsizes in the middle only
  when a choice is longer than 36 characters (GTK's default made the longest account
  path the minimum width). A list whose choices all fit shows them whole: its button
  is as wide as the longest choice and the open list's rows are too (GTK 4.22
  had shrunk such buttons to "Sc…oss" and wrapped "Keep" as "Ke-ep"). A list of
  `SEARCH_FROM` (8) or more choices can be searched: its popup has a search field
  matching anywhere in a choice (`StringFilterMatchMode.SUBSTRING`), so every
  account choice, including the register's account picker, filters as the
  register's account cell does. Such a list opens at least `LIST_MIN_HEIGHT`
  tall (the list's scrolled window inside the drop-down's popover), so it never shows
  only two or three rows. GTK 4.16 and later cannot unselect a drop-down, so a
  choice that may be left empty is a real item. Split accounts are typed, not
  chosen from a drop-down (see **One transaction editor**). Toolbar icons are ones current Adwaita still ships
  (`TestMenusAndIcons`). View
  toolbars scroll, Dashboard cards wrap in a `Gtk.FlowBox`, and the main stacks are
  not homogeneous, so the window's minimum is the shown page's. The Dashboard's
  items sit in a `Gtk.Grid` with homogeneous columns and rows (see
  [The Dashboard's grid](#the-dashboards-grid)): each tile scrolls in both
  directions inside itself, so no item's width or height can widen the window.
- Every secondary window is a `widgets.bounded.BoundedWindow`, which opens at its
  natural size capped to 90% of the monitor, because a wrapping label's natural width
  is its whole text on one line.
- Dashboard items are tiles of whole grid columns and rows; a table keeps its
  columns' natural widths and scrolls inside its tile, and a chart fills its tile.

## The Dashboard's grid

The Dashboard's items are tiles on a grid (`gen/engine/dashboard_grid`): `COLUMNS`
(4) across, rows `ROW_HEIGHT` (120) pixels tall with a 12-pixel gap, each item 1 to
`COLUMNS` wide and 1 to `MAX_ROWS` (8) tall. An item's place is its position in one
ordered list; `pack` puts each tile, in order, at the first place from the top left
where it fits, which is what a CSS grid with `grid-auto-flow: row dense` does. The
browser lets its CSS pack the tiles (`.dashboard-tile.w<n>.h<n>`) and GTK packs them
with `pack` into a `Gtk.Grid`, so both draw one arrangement alike; a placement is
the order and sizes, never coordinates, so the arrangement survives a different
window width. A narrower window has fewer columns and packs the same list with each
tile no wider than the grid: GTK takes a column away below 240 pixels a column
(`DashboardView._on_width`, from the scrolled window's page size), the browser at
1100, 820 and 560 pixels, where a phone stacks the tiles at their own heights.

The order and sizes are `DashboardConfig.layout`, saved with the rest of the
Dashboard's settings (`SaveDashboardConfig.layout`, validated by
`save_dashboard_config`: a key that is not a dashboard breadlet, a key twice, or a
size off the grid is refused and nothing is stored). An item not in the layout
follows the arranged ones at its breadlet's own `size` (`Plugin.size`, else
`DEFAULT_SIZE`), so a book saved before layouts, or a breadlet installed later,
needs no migration; `DashboardSettings.breadlets` lists every item arranged, with
its size, and `default_layout` is every item in its own place for **Reset Layout**.

Arranging is a mode of the Dashboard rather than a dialog, because a place only
means something where it is seen. Each tile gets a title to drag it by (a
`Gtk.DragSource` and `Gtk.DropTarget`; HTML drag and drop), arrows that move it one
place among the items shown, a size control, a hide button, and a corner that
resizes it by whole columns and rows (a `Gtk.GestureDrag`; pointer events). GTK moves
and resizes by setting each tile's `Gtk.GridLayoutChild` without drawing the
documents again, so a drag in progress is never interrupted. **Done** saves the
layout and every item's shown state together; **Cancel** draws the saved one again.
Links do nothing while arranging, so a click meant to pick a tile opens nothing.
A table keeps its columns' natural widths and scrolls inside its tile; a chart takes
the height the rest of its item leaves, drawn natively at that size in GTK and, in
the browser, drawn again at its tile's pixel size (`fitCharts`, on a
`ResizeObserver`) so its text stays the page's size in a narrow or a wide tile.
Printing follows the order arranged. Tests: `tests/test_dashboard_layout.py` (packing,
storage, refusals), the layout tests in `tests/test_web.py` and
`tests/test_book_server.py`, `TestArrangeDashboard` in `tests/test_gui.py`, and the
arranging tests in `tests/test_web_browser.py`.

## Registers and entry

The register lists `ledger.register` rows in date order and opens scrolled to the
end like a check register, without selecting or expanding a row. Register windows
are independent consumers of the one open `DbSQLite` connection: each owns its
account, filter, selection, and expansion, while database signals refresh all of
them after a commit. A view on screen repaints once after the current event
(`schedule_refresh`), however many signals a burst of commits raised; a view that is
not on screen only marks itself stale and refreshes when it is next mapped
(`BaseView._on_change`, `_on_map`). Showing a view redraws it only when it is out of
date (`ensure_current`): the book changed since it was drawn (every view also
listens to `database-changed` for this, whatever it watches), it was drawn on an
earlier day, or (Projection) its calculation is pending. Switching back to an
unchanged view is therefore instant instead of recalculating it. A setting change
(`ViewManager.refresh_views`) redraws the shown view and invalidates the others. A register reloads only
the transactions a commit names (`transaction-*` signals,
`ledger.update_register`), which keeps rows and recomputes running balances in
memory, and ignores the `database-changed` that follows them and any signal raised
by its own save (`RegisterSheet.saving`). One save therefore reads the
account's ledger once and rebuilds only the shown view. The main window detaches secondary windows before the book
closes, so no callback reaches a replaced database. Register column headings are a
shared engine mapping, so GTK and web name the same ledger directions.

Entry autocomplete comes from one service, `services/autocomplete.suggest_entry`,
so GTK and web never infer different templates. A description matches on its
normalized key (`description_keys.match_key`); candidates
must use the entry's account and currency and only visible, postable accounts, and
the latest wins. The proposal carries accounts, values, memos, and the earlier
transaction's notes (never reconcile state, source identity, planning purpose,
investment activity, or claims), fills only fields the user has not touched (GTK's
register and transaction editor take the notes only into empty notes; the
browser's register has no notes cell for a new entry), names its source, and never
writes. The web route is `GET /api/entry/suggest`.

### Register grid and blank entry row

GTK and web registers take new entries in a blank row at the bottom, modelled on
GnuCash's interaction. The GTK register is a custom grid closer to GnuCash's
appearance too; both are narrow adapters to the shared transaction service, not
parallel transaction models:

- **Model.** `gui/register_sheet.py` is a toolkit-free sheet: the ledger rows, the
  expanded split lines of the cursor's transaction, and a blank row that is not a
  database object, so it never enters balances or exports. It owns the cursor (row
  and column), the one `Draft` being edited, Tab order, Enter/Escape, saving, and
  the split toggle, and is tested without GTK (`tests/test_register_sheet.py`).
  Opening a register puts the cursor on the blank row, scrolled to the bottom.
- **Drawing.** `gui/widgets/register_grid.py` draws only the visible rows on a
  `Gtk.DrawingArea` with Cairo and PangoCairo (plain ruled rows, a tinted cursor
  row, split lines, a rule before future-dated rows) and moves one frameless
  `Gtk.Entry` over the cursor's cell, so no other cell looks like an input. A
  vertical scrollbar drives the first drawn row; nothing is recycled, so the
  cursor cannot jump when a split transaction expands (the regression that a
  `Gtk.ColumnView` with hosted editors showed). There is no column chooser or
  sorting: a register is always in ledger order. The account list under the
  transfer cell opens above the row when it would not fit below
  (`match_list_placement`). A cash register that a security moved through
  shows `TRADE_COLUMNS` (Shares and Price for the security leg); a security's split
  line takes them, and with shares, price, and amount all filled a change waits on
  `RegisterSheet.trade_question` for which other one to work out.
- **Cells and keys.** Arriving in a cell selects all of its text, so typing or
  Backspace replaces a proposed value and Left or Right goes to its start or end.
  Date (defaulting to the last date entered), Num, Description,
  Transfer (visible, postable accounts), R, and Increase/Decrease titled with the
  account's headings. Tab and Shift+Tab move in column order, Enter saves and moves
  to the next transaction (past the saved one's notes and split lines), Escape restores the transaction, Up/Down/Page Up/Page Down move the cursor
  (saving the row being left; a refused save keeps it), and Ctrl+End/Home go to the
  blank row and the first row.
- **Typing.** `engine/entry_input` holds the pure rules for dates (GnuCash's
  `+`/`-`, `[`/`]`, `t`, `m`/`h`, `y`/`r`, and short forms relative to the date
  shown), amount arithmetic (each number read in the entry's decimal convention,
  then evaluated by the safe formula language and rounded half up to the
  currency's unit; a single number stays exact), segment-by-segment account
  completion, and Num stepping. `services/entry_input` adds what only the book
  knows: postable visible accounts, the register's last number, and the
  currency's fraction. GTK calls them directly (a shortcut applies only while the
  date field holds a whole date, so typing an ISO date is never intercepted); the
  browser sends the text
  to `GET /api/entry/date`, `/amount`, `/accounts`, and `/num`, so neither
  register has its own parser.
- **Quickfill.** `quickfill_account` completes the segment being typed from the
  best match, and the grid selects the added text so the next key replaces it; `:`
  accepts the segments shown and prefers a deeper account. The matches are listed
  in a `Gtk.ListBox` under the cell, which scrolls to keep the chosen one in view
  as Up and Down move through it. `quickfill_description` completes a new
  entry's description from the register's earlier descriptions, most recent first;
  leaving the description then asks `suggest_entry` for the rest. The grid keeps the
  letters as typed, so a completion that stops matching gives back the typed case
  rather than the earlier description's. Tab from the transfer of a two-split
  entry whose transfer is an expense goes to the decrease (money-out) column, for
  that one step only (`RegisterSheet._expense_hop`; the browser does the same).
- **Commit.** Two balancing `TransactionSplitInput`s with a positive exact amount
  (direction from the cell typed in), or one per split line, go through
  `save_transaction` as one atomic change and one undo step. Fewer than two splits,
  an imbalance, an incomplete line, or a new entry with no split in the register's
  account is refused with the typed values kept and the cursor on the offending
  cell. An existing transaction may move out of the register's account entirely
 , in GTK and the browser alike; the message names where it went.
- **Split lines.** A transaction of more than two splits opens with its split lines
  (memo, account, and amount each) and a trailing empty line; while they do not
  balance, that line shows the rest in the balancing column
  (`RegisterSheet.balancing_line`, display only) and takes it as its amount when
  given an account or memo, as GnuCash's blank split does, in GTK and the
  browser alike; there is no separate imbalance row any more; the Split button
  toggles them for a two-split one. Viewing the lines is not an edit. Collapsing is
  refused while more than two lines hold a split.
- **Editing in place.** Moving the cursor onto a transaction loads it as the draft:
  split lines record their stored split handles, and saving passes
  `existing_handle` and `source`, so handles, planning purposes, and investment
  activity the row does not show are kept; an emptied line removes its
  split. Autocomplete never runs over a stored transaction. The **R** column calls
  `services.toggle_cleared`, which flips only `n`↔`c`, refuses `y`/frozen/void,
  and in the same database change adds a newly cleared candidate to (or removes
  it from) the account's open statement.
- **Securities.** An account whose commodity is not a currency
  (`ledger.security_of`) has GnuCash's stock-register columns: `SECURITY_COLUMNS`
  adds Shares and Price before the amounts and puts Shares Held
  (`RegisterRow.share_balance`, kept by `register` and `update_register`) in place
  of the amount balance, whose sum of trade amounts is neither shares nor value.
  Leaving a Shares, Price, or amount cell works out the third
  (`services/entry_input.read_entry_trade` over `engine/entry_input.fill_trade`):
  shares or a price recompute the amount, an amount recomputes the price, or the
  shares when none are typed. Computed shares round to the account's SCU
  (`ledger.share_fraction`) and prices to six places. The shares are saved as the
  split's `quantity`, signed by the amount column; shares typed without an amount
  (a share split) keep their own sign, and shares left as loaded keep the stored
  quantity exactly. Split lines in a security take shares the same way. A buy or
  sell without shares is refused at the Shares cell before the service, which
  also requires them. The footer is `presentation.register_share_summary`: shares
  held and their market value with the quote's date and source, or "no quote" and
  the ledger amount, never an unquoted conversion. The transaction editor's
  split grid takes shares and price on a security's line the same way.
- **One transaction editor.** Every GTK window that enters or changes a
  transaction holds `widgets/transaction_editor.TransactionEditor`:
  `dialogs/transaction_dialog.TransactionDialog` (the register's Edit and New,
  reconciliation, File → New Transaction) and
  `dialogs/posted_dialog.PostedTransactionsDialog`, whose list opens the chosen
  posted transaction in it. The editor has the **Entry** page (date, number,
  description, notes), the split grid beneath its pages, and **Tags &
  documents**; the window adds only its buttons. The splits are typed on the
  register's own grid (`widgets/register_grid.RegisterGrid`) over a
  `split_sheet.SplitSheet`, one transaction's split lines. It and the register's
  `register_sheet.RegisterSheet` are both a `split_entry.SplitEntry`, which holds
  everything a split line does once: memo completion, account completion
  (`selectable_names`, then `entry_input.quickfill_account`), arithmetic amounts,
  the rest of an unbalanced entry on the empty last line (#369), shares and price
  in a security (#323, #407), the planning purpose and investment activity cells
  (`CHOICES`, completed by `choose`; saved only by a sheet that shows them,
  `edits_detail`, so the register keeps the stored ones), and the split inputs for
  the shared save service (`_line_splits`, `_split_input`). The split sheet's
  amount columns are named by `ledger.register_headings` for the account the
  editor was opened from (Debit and Credit with none). Unlike the register, it
  saves nothing itself: Enter asks the editor to save (`on_enter`), Escape to close
  (`on_escape`), and Tab past the last cell sets `leaving`, which the grid answers
  by moving the keyboard on to the window's next control. An account outside the
  tree, a placeholder, or a hidden account (while hidden accounts are not shown) is
  offered only where the transaction already uses it.
- **Double Line.** The window's stateful `win.double-line` action (**View → Double
  Line**, also in Preferences; remembered in view settings as
  `register.double_line`) adds a `NOTES` row under every transaction
  and the blank one. Its editable cell is the description column, drawn and edited
  across to the balance column; Tab reaches it after the transaction's last cell,
  and `Draft.notes` is saved with the transaction. With it off, notes are kept.
- **Shading.** `RegisterSheet.ordinals` numbers each row's transaction, so
  `RegisterGrid.banded` can alternate by transaction (the default; a notes or split
  line shares its transaction's band), by line, or not at all. Preferences stores it
  as `register.banding`. The band follows the transaction, not the row index, so
  Double Line still alternates.
- **Enter and Cancel.** `RegisterGrid.enter` and `RegisterGrid.cancel` are the one
  path for the Enter and Escape keys, the register toolbar's **Enter** and **Cancel**
  buttons (which do not take focus), and the `register-enter` and `register-cancel`
  view actions (as GnuCash's register toolbar has them).
- **Shared display settings.** Both are settings for every register, not toolbar
  controls: `views.register.apply_register_display()` redraws each live
  `RegisterView` (a `WeakSet`) after View → Double Line or Preferences → Save, and
  `BreadSchedApp.preferences_changed` updates each window's Double Line check.
- **Focus.** Split does not take focus on click, and a display change redraws with
  focus back in the cursor's cell, so typing continues (after Double Line the
  keys went to the button).
- **Accounts anywhere.** `complete_account` lists, after the segment matches, every
  account whose full name holds text typed without a `:` (`groc` lists
  Expenses:Groceries); such a match is listed but never filled into the cell. With
  a `:`, matching stays segment by segment. The browser's `/api/entry/accounts`
  uses the same rule.
- **Leaving.** Switching accounts, opening another transaction, or closing a
  register window with unsaved typing asks Save / Discard / Cancel first.
- **Web.** The browser register has the same row, split lines, and in-place editing
  through `POST /api/register/entry`, started by clicking a row. Up/Down call
  `editRow`, which saves a changed row through its registered `commit` before
  editing the neighbor in `data.rows` order; the blank row's typing is kept. Account
  selects list the matches `GET /api/entry/accounts` returns, and the R column posts
  to `POST /api/register/cleared` (`toggle_cleared`). It keeps amounts as BigInt micro-units and
  posts exact `[numerator, denominator]` pairs, so a comma-decimal browser cannot be
  misread, and keeps drafts across re-renders. A security's register (`security` in
  `GET /api/register`, with `shares`, `price`, and `share_balance` per row, each
  split's exact `quantity`, `security_accounts`, and the footer `summary`) adds the
  same Shares and Price cells; leaving one asks `GET /api/entry/trade` for the third
  value, a fill ignores a cell typed while it ran, and Enter reads any cell not yet
  read before saving. Shares are posted as decimal `quantity` text, which
  `register_entry_resource` gives the service as the account's commodity.

The full editor remains the place for complex metadata; the row's editor icon hands
its contents to `TransactionDialog.prefill`.

## Web navigation

The browser's bar is `NAV_GROUPS` in `core.js`: **Dashboard** and **Reports** on
their own, and menus for the rest (**Ledger**, **Schedule**, **Planning**,
**Benefits**, **Book**), so nineteen pages take seven places. A group's button
names the page shown (**Ledger: Register**) and has `aria-expanded`; its menu
closes on a choice, **Escape**, or a click elsewhere. `VIEWS` is derived from the
groups, so a page is added in one place. The **Reports** page lists the interactive
reports (Expense Explorer, Budget Jars, Net Worth History, Holdings, Realized Gains,
Tax Year) and then every other report breadlet; the Plan's **Expense Explorer…**
opens it there with the Plan's range.

## Web tables

The web `table()` helper accepts `<tr>` elements or arrays of cell values; an array
row becomes one `<tr>` whose cells follow the header's numeric alignment. Views
therefore cannot leak loose text into a `<tbody>`. `tests/test_web_browser.py`
renders the Dashboard in headless Chromium, where available, to check real rows and
formatted group totals.

## Packaged guide

The guide is split by interface so that desktop menus, browser pages, and
command options are not mixed in one text, while every interface still shows all
four parts. `breadsched.user_guide` (no GTK import) owns the part list, reads each
part from the installed package, forms GitHub-style heading anchors
(`heading_slug`), and resolves relative Markdown links by file name
(`resolve_link`), so the links between files work the same in the repository, the
desktop, and the browser. Interface parts carry steps and link to the overview for
rules; the overview links to each interface's steps. `tests/test_user_guide.py`
fails on any link to a missing part or heading.

- GTK **Help → User Guide** (`gui/user_guide.py`) presents the parts in a bounded,
  scrollable native window with a linked toggle per part. Links are text tags; a
  click follows an internal link to its part and heading mark, or opens an external
  one in the default browser.
- The browser's **Guide** page reads `GET /api/guide?part=` (`web/guide_resource.py`)
  and renders the guide's small Markdown subset as DOM nodes, never as HTML text.
- Contextual help: `user_guide.HELP_TOPICS` maps each workflow topic to a heading
  present in both the desktop and browser parts, and `help_target(topic, interface)`
  gives the part and anchor. GTK dialogs place `widgets.help.help_row(topic)` at
  their top; its button calls the application's `show_guide(topic, dialog)`, which
  reuses the guide window and keeps it transient for the asking dialog. The guide
  window has its own `Gtk.WindowGroup`, because a modal dialog's grab covers every
  window in its group and would otherwise leave the guide unresponsive until the
  dialog closed; it scrolls to the topic's heading once it is mapped, since
  a text view not yet laid out cannot scroll. Browser views (`VIEW_HELP` in `app.js`) and sections
  (`helpHeading` in `core.js`) open `?view=Guide&help=<topic>` in a new tab, which
  reads `GET /api/guide?topic=` for the browser part and its `anchor`, so an open
  dialog or form keeps its contents. Tests require every heading to exist in both
  parts and every topic used by either interface to be in the table.
- `breadsched guide [overview|desktop|web|cli]` prints a part; `--list` names them.

The Markdown files remain the only content source: each surface performs a
deliberately conservative presentation transform instead of maintaining a second
embedded copy or requiring network access or a Markdown-rendering runtime
dependency.

## Synthetic sample book

The synthetic learning book is generated on request by `gen.sample_book` into a
new path; CLI is only its entry point. Its reference date anchors the previous
month's balanced ledger transactions and next month's recurring Plan examples.
It uses the normal schema, account, schedule, scenario, and Dashboard configuration
APIs, so it does not require a special database format or contaminate a real book.
Random object handles do not affect the reproducible account names, dates, amounts,
or financial results. Existing paths are refused before opening.

