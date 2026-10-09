# User interfaces

Part of the [BreadSched design](../../DESIGN.md).

## GTK window, toolbar, and tabs

The main window has no sidebar, which duplicated the View menu and took width from
every view. The toolbar is arranged around the current view: actions that work
anywhere (`view_catalog.TOOLBAR`: open or import a book, undo, redo, new
transaction, print), the current view's name and its own command icons
(`view_tools`), one toggle per other view (`view_catalog.CATEGORIES`), and the book's
account and transaction counts. The toolbar scrolls horizontally rather than setting
the window's minimum width. The View menu and the view icons both target the
stateful `win.show-category` action, and `show_category()` is the single navigation
entry point, so the active icon and menu item always agree however a view was
reached.

`gui/view_catalog` holds that toolbar, the views, and every view's commands as
plain data with no GTK import (an architecture test checks it), so the window and
the application build from one catalog that can be read without a display;
`viewmanager` re-exports it.

View commands are declared once in `view_catalog.VIEW_ACTIONS`. Each becomes a
`win.<view>-<name>` action, is listed in **Actions**, and appears in the toolbar
only while its view is current. **Actions** lists the current view's commands
first, then the commands that work anywhere, then every other view's commands under
**Other Views**, so each command has exactly one item. The menu model is rebuilt only
when the view changes: opening a submenu moves focus into its popover, and
rebuilding the same view's model then destroyed the open submenu and crashed GTK.
Buttons stay in a view only where their state depends on it (Review due, Save as
scenario, Reconcile) or where they act on a table's selected row.

Below the toolbar, a tab bar lists each open view, one tab per open register, and
one per pinned scenario Projection (`ViewManager._tabs`). Register tabs are
`RegisterView`s in their own stack, and `_views["register"]` always names the one
shown, so callers that address "the register" keep working. A pinned
`ProjectionView` keeps its scenario and only recalculates when inherited Base
assumptions change, while the unpinned Projection and Plan follow the selected
scenario. Closing a register with unsaved typing asks first; opening another book
closes every tab, because tabs name the old book's accounts. Each book's tabs are
remembered in `views.ini` (section `open-tabs`, keyed by a digest of the book path)
and restored after the Dashboard opens, skipping deleted accounts and scenarios. The
browser has no tab bar: `openInNewTab` opens a browser tab with the view and account
or scenario in its query string.

## Tables, dialogs, and bounded sizes

Every table except the register grid (below) is built with `_base.table_section()`: a heading row with the table's
own column chooser (never in the view toolbar, where identical icons could not be
told apart) above a scrolled `Gtk.ColumnView` carrying the `data-table` class, which
stripes rows so they are distinguishable at rest. Numeric cells carry `numeric` for
tabular figures and a small right padding. Tables never scroll sideways: a narrowing
window gives each column between its minimum and natural width, text cells ellipsize,
and amount cells never do, so figures are never truncated. The appearance corrects
concrete overlap and legibility problems; outside the register, reproducing
GnuCash's look is not a goal.

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
presented window, then migrates. A remembered book that needs no upgrade still
opens before the window appears, so the start screen never flashes.

Windows must stay usable on small screens whatever the book holds:

- Dialogs keep minimum sizes within 800 × 600 even with very long names
  and 40-split transactions (`TestDialogsFitTheScreen`); long forms move their body
  into a scroller (`widgets.bounded.scroll_body`) and keep the button row outside.
- Views keep within a 1024 × 700 work area (`TestBoundedSizes`). Drop-downs come
  from `widgets.choice.bounded_dropdown`, whose button ellipsizes in the middle at 36
  characters (GTK's default made the longest account path the minimum width), view
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
them after a commit. The main window detaches secondary windows before the book
closes, so no callback reaches a replaced database. Register column headings are a
shared engine mapping, so GTK and web name the same ledger directions.

Entry autocomplete comes from one service, `services/autocomplete.suggest_entry`,
so GTK and web never infer different templates. A description matches on its
normalized key (`description_keys.match_key`); candidates
must use the entry's account and currency and only visible, postable accounts, and
the latest wins. The proposal carries accounts, values, and memos only (never
reconcile state, source identity, notes, planning purpose, investment activity, or
claims), fills only fields the user has not touched, names its source, and never
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
- **Cells and keys.** Date (defaulting to the last date entered), Num, Description,
  Transfer (visible, postable accounts), R, and Increase/Decrease titled with the
  account's headings. Tab and Shift+Tab move in column order, Enter saves and moves
  down, Escape restores the transaction, Up/Down/Page Up/Page Down move the cursor
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
  in a `Gtk.ListBox` under the cell. `quickfill_description` completes a new
  entry's description from the register's earlier descriptions, most recent first;
  leaving the description then asks `suggest_entry` for the rest.
- **Commit.** Two balancing `TransactionSplitInput`s with a positive exact amount
  (direction from the cell typed in), or one per split line, go through
  `save_transaction` as one atomic change and one undo step. Fewer than two splits,
  an imbalance, an incomplete line, or no split in the register's account is refused
  with the typed values kept and the cursor on the offending cell.
- **Split lines.** A transaction of more than two splits opens with its split lines
  (memo, account, and amount each) and a trailing empty line; the Split button
  toggles them for a two-split one. Viewing the lines is not an edit. Collapsing is
  refused while more than two lines hold a split.
- **Editing in place.** Moving the cursor onto a transaction loads it as the draft:
  split lines record their stored split handles, and saving passes
  `existing_handle` and `source`, so handles, planning purposes, investment
  activity, and notes the row does not show are kept; an emptied line removes its
  split. Autocomplete never runs over a stored transaction. The **R** column calls
  `services.toggle_cleared`, which flips only `n`↔`c`, refuses `y`/frozen/void,
  and in the same database change adds a newly cleared candidate to (or removes
  it from) the account's open statement.
- **Leaving.** Switching accounts, opening another transaction, or closing a
  register window with unsaved typing asks Save / Discard / Cancel first.
- **Web.** The browser register has the same row, split lines, and in-place editing
  through `POST /api/register/entry`, started by clicking a row. Up/Down call
  `editRow`, which saves a changed row through its registered `commit` before
  editing the neighbor in `data.rows` order; the blank row's typing is kept. Account
  selects list the matches `GET /api/entry/accounts` returns, and the R column posts
  to `POST /api/register/cleared` (`toggle_cleared`). It keeps amounts as BigInt micro-units and
  posts exact `[numerator, denominator]` pairs, so a comma-decimal browser cannot be
  misread, and keeps drafts across re-renders.

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
  their top; its button calls the application's `show_guide(topic)`, which reuses
  the guide window. Browser views (`VIEW_HELP` in `app.js`) and sections
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

