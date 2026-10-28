# User interfaces

Part of the [BreadSched design](../../DESIGN.md).

## GTK window, toolbar, and tabs

The main window has no sidebar, which duplicated the View menu and took width from
every view. The toolbar is arranged around the current view (#182, #361): actions
that work anywhere (`view_catalog.TOOLBAR`: undo, redo, new transaction, print;
opening and importing a book are File-menu and start-screen commands), one toggle
per other view (`view_catalog.CATEGORIES`), the current view's name in bold
(`view_heading`) as the divider, its own command icons (`view_tools`, including the
register's Reconcile), and, only on `viewmanager.SUMMARY_VIEWS` (Dashboard and
Accounts), the book's account and transaction counts. The toolbar scrolls horizontally rather than setting
the window's minimum width. The View menu and the view icons both target the
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
has exactly one item; **Reports** lists every `report` action whichever view
computes it (#336). An item that asks for more input ends in "…", and the one New
Transaction command (`app.new-transaction`, Ctrl+T) starts in the shown register's
account (`ViewManager.shown_register`). Toolbar icons are unique per command:
`view_catalog.toolbar_icons()` lists them and only `SHARED_ICONS`
(`document-new` for New…, `tab-new` for …in New Tab) may repeat, each with that one
meaning; a test enforces it. The menu model is rebuilt only
when the view changes: opening a submenu moves focus into its popover, and
rebuilding the same view's model then destroyed the open submenu and crashed GTK.
Buttons stay in a view only where their state depends on it (Review Due
Transactions…, Save Base as Scenario…, Reconcile…) or where they act on a table's
selected row.

The reconcile window (`dialogs/reconciliation_dialog`, #362) is the one statement
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
A double-click puts the check back and edits (#374, #375). Keyboard selection in
one list empties the other, so the edit and check commands act on the row in view.
Each list has its own sort buttons (✓, Date, Description, Amount; the same one again
reverses), and both GTK and the browser order rows with the shared
`reconciliation.sort_candidates` (`CANDIDATE_SORTS`): size for amounts, checked
first, ties in date order, and `after_statement` rows always last. The browser asks
`GET /api/reconciliation?sort=…&descending=…` after saving its checks (#377).
The browser's reconcile table has an **Edit** per row that saves the checks and
opens the row in the register with **Back to reconciliation**.

GTK's **Track as Reimbursable…** saves the register's unsaved typing first and
opens the dialog over the register's own window, never over another one that was
active, such as the guide. Each refusal is an alert as well as the register's status
line (#371).

The FSA claims window (`dialogs/fsa_claims_dialog`, #366) is likewise not modal and
one per book (`FsaClaimsDialog.present_for`): it is the application action
`app.fsa-claims`, listed in every view's Actions menu (on the FSA Dashboard, as that
view's own command and toolbar icon instead), and it refreshes its candidate
transactions on `database-changed`. Its chooser's first entry is the new claim. Each
link list pins the linked splits above a candidate list filtered by search text and
by range (60 days around the service date, the claim's benefit year, or all) and
capped at `SHOWN_LIMIT` rows.

Below the toolbar, a tab bar lists each open view, one tab per open register, and
one per pinned scenario Projection (`ViewManager._tabs`). Register tabs are
`RegisterView`s in their own stack, and `_views["register"]` always names the one
shown, so callers that address "the register" keep working. A pinned
`ProjectionView` keeps its scenario and only recalculates when inherited Base
assumptions change, while the unpinned Projection and Plan follow the selected
scenario. Choosing a tab, a view icon, or a menu item leaves the keyboard in the
shown view (`ViewManager._focus_view`, a register's cursor cell, #331); tab buttons
do not take focus on click. Ctrl+Page Up and Ctrl+Page Down step through the tabs.
A tab can be dragged onto another to take its place, or moved one place with
Ctrl+Shift+Page Up/Down (**View → Move Tab Left/Right**); `ViewManager.move_tab`
reorders the bar and the remembered tabs keep the new order (#390).
Ctrl+W closes the current one, and Ctrl+F (**Edit → Find in Register**) goes to the
register's filter, which matches with `ledger.transaction_matches`, as the browser
register's `filter` does: date, number, description, notes, tags, memos,
accounts, and amounts as shown or typed (#382). Ctrl+Shift+F (**Edit → Find
Transactions…**, `app.find-transactions`) opens `dialogs.find_dialog.FindDialog`,
which searches every register through `engine.transaction_query` (see valuation and
reporting) and opens a found split's register when a row is activated. Closing a register with unsaved typing asks first; opening another book
closes every tab, because tabs name the old book's accounts. Each book's tabs are
remembered in `views.ini` (section `open-tabs`, keyed by a digest of the book path)
and restored after the Dashboard opens, skipping deleted accounts and scenarios. The
browser has no tab bar: `openInNewTab` opens a browser tab with the view and account
or scenario in its query string.

The browser's **Quick entry** page (`/quick`, experimental branch) is the small-screen
entry point: a separate `quick.html` that loads only `core.js` and `quick.js`
(`web.transport.QUICK_SCRIPTS`), one column at most 480 px wide with 44 px touch
targets and 16 px fields (so iOS does not zoom), a decimal keyboard for the amount,
and Spent/Received choosing which account is debited. It saves through
`/api/transaction` and suggests from `/api/entry/suggest`, so it adds no write path;
the account used last is kept in `localStorage`. The token travels in the URL's
fragment as on the full page, and **Full view** and **Quick entry** link the two.

## Tables, dialogs, and bounded sizes

Editors disclose progressively (#383): the fields most entries need come first, and
what is set once (defaults, planning classifications) or rarely (future changes) is
a tab or a fold away, never removed. The desktop uses `widgets.form_pages`
(`FormPages`, a notebook whose tab can carry a count, and `FormGrid` label/field
rows); the browser uses `details.form-section`. A page or fold that holds something
says so in its title (`Future changes (2)`, `Tags & documents (1)`) or opens by
itself, so nothing set is hidden from view. A field that applies only to some
accounts (investment activity) shows only for those, or when it already holds a
choice. The schedule and scenario schedule editors share `update_schedule_pages`;
the transaction editor's per-split planning purpose and investment activity follow
its **Planning detail** toggle (`SplitEditor.show_detail`).

Every table except the register grid (below) is built with `_base.table_section()`: a heading row with the table's
own column chooser (never in the view toolbar, where identical icons could not be
told apart; the Dashboard hides its tables' choosers and lists the same columns,
under the same `columns:<view id>` settings, on the Columns page of
`dialogs.dashboard_dialog.DashboardDialog`, #381) above a scrolled `Gtk.ColumnView` carrying the `data-table` class, which
stripes rows so they are distinguishable at rest. Numeric cells carry `numeric` for
tabular figures and a small right padding. Tables never scroll sideways: a narrowing
window gives each column between its minimum and natural width, text cells ellipsize,
and amount cells never do, so figures are never truncated. The appearance corrects
concrete overlap and legibility problems; outside the register, reproducing
GnuCash's look is not a goal.

Scrollbars are always shown: `app.show_scrollbars()` turns GTK's
`gtk-overlay-scrolling` setting off at start-up, where GTK has it, so a list longer
than its window (the due review, for example) shows a permanent scrollbar rather
than a thin indicator that appears only while scrolling (#318); the stylesheet
widens the slider to 10 pixels.

Every dialog follows one set of conventions (#335). `widgets.bounded.BoundedWindow`
closes on Escape and makes its main button (the `suggested-action` button in its
last row, or one named with `set_primary`) the default, so Enter in a one-line
field activates it; a field whose Enter does its own job is marked with
`keeps_enter`. Editors close with **Cancel** and save with **Save** (or a named
save); viewers and reports close with **Close**. Every dialog has a **Help** row
naming a `user_guide.HELP_TOPICS` topic (`TestDialogConventions` checks both). Every
Delete is a `widgets.confirm.delete_button`: styled `destructive-action`, it asks
"Delete …?" with Cancel as the default before deleting, even where Ctrl+Z could
restore it. The browser confirms every delete the same way.

Window sizes are the user's, not a book's (#349): `widgets.window_sizes` keeps them
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
`dialogs.preferences_dialog.PreferencesDialog`, #333) edits user settings that are
not part of a book: `general.last_book_path`, `register.double_line` and
`register.banding` in the view settings, and the Alpha Vantage key through
`plugins.quotes.save_alphavantage_key` (stored only if changed, so a key from the
environment is not copied into the file). **Configure Dashboard…** there
activates the window's existing `win.dashboard-configure`, because the Dashboard's
horizons, sections, and groups belong to the book. The browser has no equivalent window; its guide's Preferences
section says where each setting lives there.

A modal dialog is never shown behind the window it blocks (#295), and never two at
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
large book's start-up is visibly under way and the start screen never flashes (#389).
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
  is as wide as the longest choice and the open list's rows are too (#327; GTK 4.22
  had shrunk such buttons to "Sc…oss" and wrapped "Keep" as "Ke-ep"). A list of
  `SEARCH_FROM` (8) or more choices can be searched: its popup has a search field
  matching anywhere in a choice (`StringFilterMatchMode.SUBSTRING`), so every
  account choice, including the register's account picker, filters as the
  register's account cell does (#334). Such a list opens at least `LIST_MIN_HEIGHT`
  tall (the list's scrolled window inside the drop-down's popover), so it never shows
  only two or three rows (#381). View
  toolbars scroll, Dashboard cards wrap in a `Gtk.FlowBox`, and the main stacks are
  not homogeneous, so the window's minimum is the shown page's.
- Every secondary window is a `widgets.bounded.BoundedWindow`, which opens at its
  natural size capped to 90% of the monitor, because a wrapping label's natural width
  is its whole text on one line.
- Dashboard sections are separate cards sized to their content up to 900 × 420
  pixels, beyond which the table scrolls inside its card.

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
by its own save (`RegisterSheet.saving`). Before #319 one save read the account's
ledger four times and rebuilt every built view, seconds on a long book. The main window detaches secondary windows before the book
closes, so no callback reaches a replaced database. Register column headings are a
shared engine mapping, so GTK and web name the same ledger directions.

Entry autocomplete comes from one service, `services/autocomplete.suggest_entry`,
so GTK and web never infer different templates. A description matches on its
normalized key (`description_keys.match_key`); candidates
must use the entry's account and currency and only visible, postable accounts, and
the latest wins. The proposal carries accounts, values, memos, and the earlier
transaction's notes (never reconcile state, source identity, planning purpose,
investment activity, or claims), fills only fields the user has not touched (GTK's
register and transaction editor take the notes only into empty notes, #363; the
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
  sorting: a register is always in ledger order.
- **Cells and keys.** Arriving in a cell selects all of its text, so typing or
  Backspace replaces a proposed value and Left or Right goes to its start or end
  (#332). Date (defaulting to the last date entered), Num, Description,
  Transfer (visible, postable accounts), R, and Increase/Decrease titled with the
  account's headings. Tab and Shift+Tab move in column order, Enter saves and moves
  to the next transaction (past the saved one's notes and split lines, #325), Escape restores the transaction, Up/Down/Page Up/Page Down move the cursor
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
  as Up and Down move through it (#334). `quickfill_description` completes a new
  entry's description from the register's earlier descriptions, most recent first;
  leaving the description then asks `suggest_entry` for the rest. The grid keeps the
  letters as typed, so a completion that stops matching gives back the typed case
  rather than the earlier description's (#363). Tab from the transfer of a two-split
  entry whose transfer is an expense goes to the decrease (money-out) column, for
  that one step only (`RegisterSheet._expense_hop`, #369; the browser does the same).
- **Commit.** Two balancing `TransactionSplitInput`s with a positive exact amount
  (direction from the cell typed in), or one per split line, go through
  `save_transaction` as one atomic change and one undo step. Fewer than two splits,
  an imbalance, an incomplete line, or a new entry with no split in the register's
  account is refused with the typed values kept and the cursor on the offending
  cell. An existing transaction may move out of the register's account entirely
  (#364), in GTK and the browser alike; the message names where it went.
- **Split lines.** A transaction of more than two splits opens with its split lines
  (memo, account, and amount each) and a trailing empty line; while they do not
  balance, that line shows the rest in the balancing column
  (`RegisterSheet.balancing_line`, display only) and takes it as its amount when
  given an account or memo, as GnuCash's blank split does (#369), in GTK and the
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
- **Securities (#323).** An account whose commodity is not a currency
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
  splits show a Shares entry for a security's split.
- **Transaction editor splits (#369).** Each `SplitEditor` row is a small register
  line: account, memo, and increase/decrease entries named by
  `ledger.register_headings` for the row's own account, so no amount is typed
  signed (either entry may be arithmetic). A single blank row shows the balancing
  amount as its placeholder and takes it on save; when every row has an amount and
  they do not balance, the dialog adds a row (`SplitEditor.auto`) with no account,
  which blocks saving until one is chosen, and removes it again, untouched, once
  the entry balances.
- **Double Line.** The window's stateful `win.double-line` action (**View → Double
  Line**, also in Preferences; remembered in view settings as
  `register.double_line`) adds a `NOTES` row under every transaction
  and the blank one. Its editable cell is the description column, drawn and edited
  across to the balance column; Tab reaches it after the transaction's last cell,
  and `Draft.notes` is saved with the transaction. With it off, notes are kept.
- **Shading.** `RegisterSheet.ordinals` numbers each row's transaction, so
  `RegisterGrid.banded` can alternate by transaction (the default; a notes or split
  line shares its transaction's band), by line, or not at all. Preferences stores it
  as `register.banding`. Before #325 the band followed the row index, so
  Double Line gave every transaction the same shade.
- **Enter and Cancel.** `RegisterGrid.enter` and `RegisterGrid.cancel` are the one
  path for the Enter and Escape keys, the register toolbar's **Enter** and **Cancel**
  buttons (which do not take focus), and the `register-enter` and `register-cancel`
  view actions (#344, as GnuCash's register toolbar has them).
- **Shared display settings.** Both are settings for every register, not toolbar
  controls (#333): `views.register.apply_register_display()` redraws each live
  `RegisterView` (a `WeakSet`) after View → Double Line or Preferences → Save, and
  `BreadSchedApp.preferences_changed` updates each window's Double Line check.
- **Focus.** Split does not take focus on click, and a display change redraws with
  focus back in the cursor's cell, so typing continues (#325: after Double Line the
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
  dialog closed (#314); it scrolls to the topic's heading once it is mapped, since
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

