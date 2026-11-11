# Breadlets

A *breadlet* is BreadSched's plug-in, after Gramps' gramplets: a small, registered
piece that shows or moves a book's information. There are four kinds, all held by
one registry (`gen/plug`, `PluginManager`):

| Kind | Does | Runs as |
| --- | --- | --- |
| `importer` | reads a file into a book | the import service calls `run`; sniffing and extensions choose it |
| `exporter` | writes a file from a book | the export commands call `run` |
| `report` | builds a printable report | `run(book, options, today)` returns a `ReportDocument` |
| `dashboard` | builds a small report the Dashboard shows below its own sections | the same signature as a report |

Every interface lists and reaches them the same way: Reports and the Dashboard in
GTK (a tab of the Reports window, `gui/dialogs/breadlets_dialog.py` with
`fixed=True`, and `gui/views/dashboard.py`), the browser
(`web/breadlet_resource.py`, `/api/breadlets` and `/api/breadlet`, drawn by
`web/static/reports.js`), and the command line (`breadsched breadlets`,
`breadsched report`).

## The book server is the only way in

A report or dashboard breadlet is given the open book's server (`api.BookApi`: in
process, or the HTTP client of a shared book) and nothing else. It reads with the
server's operations, each of which already declares its permission in
`api.permissions`, so a breadlet can do no more than the person running it may, and
running a breadlet is not itself a book operation. A breadlet never writes; the
writing operations are not part of what any shipped breadlet calls, and an
add-on that called one would be refused by the role exactly as a person would.

## One document, every renderer

A breadlet describes its output once, as the renderer-neutral
`plugins/export/report_layout.ReportDocument` the printed reports already use:
headings, paragraphs, summary cards, tables, and charts (`ModelChart` over a
`ChartModel`, followed by the table of its exact values). GTK draws it with
`gui/widgets/document_view.DocumentView` and prints it with the native report
printer; the HTML export renders it with `html_report.render_html`; the browser
and `breadsched report --json` receive the one JSON form
`api.views.report_document_json` (each block tagged with its `type`; a chart block
carries only the model, since its table is the next block). So a chart and its
numbers cannot disagree between interfaces, and a chart is never the only place a
number appears.

## Options are declared, not drawn

A breadlet lists its settings as `BreadletOption`s: a key, a label, a kind (`date`,
`integer`, `choice`, `flag`), a default (text, or a function of today), and for a
choice its values. Each interface builds its own form from these, and every
interface hands the text it collected to `resolve_options`, which types it, fills
defaults, and refuses text that does not fit with a `BreadletOptionError` naming
the option (also a key the breadlet does not take, and an end date before the start
date). The web answers that as `400 breadlet.option.invalid` with the option's key
as the field; the command line as a usage error; GTK as a message beside the form,
keeping the report already shown.

## The shipped breadlets

`plugins/breadlets` registers the reports and Dashboard items BreadSched ships.

- **Spending by category** (`spending-by-category`): a `PIE` chart of each
  top-level expense category's actual spending in the range, from the Expense
  Explorer's spending points. The seven largest are named and the rest are one
  *Other* slice. A category whose refunds exceed its spending is not a slice (a
  ring cannot draw a negative share); the ring's whole is the spending it shows, and
  the report notes the net total when it differs.
- **Income and spending** (`income-and-spending`): `BARS` of each period's actual
  income beside its actual spending, with a table adding what was left over.
- **Net worth over time**, **Budget jars**, **Tax year**, **Realized gains**: the
  existing layouts over the existing operations, so every report is reached the
  same way; their own windows remain for their interactive controls.
- Dashboard: **Spending this month** (the ring for the current month) and **Income
  and spending** for the last six months, shown until the household hides them.

A range starting before the book's first month starts with that month
(`plan_bounds`), since the Plan refuses a range before the book's history. Periods
with foreign activity that has no rate are left out of the figures and the report
says so, as the Expense Explorer does.

## Dashboard items

Everything on the Dashboard is a dashboard breadlet. Its own sections are
registered by `plugins/breadlets/dashboard.py`, one per `dashboard.SECTIONS` entry
and with the same id: the summary figures, the cash-flow chart, account groups,
pending bills, expected income, pending reimbursables, FSA claims needing attention,
and savings goals. Each draws its section from `report_layout.dashboard_section_blocks`,
the same blocks the printed Dashboard (`dashboard_layout`) is made of, so screen and
print cannot disagree. Other dashboard breadlets (the shipped spending ring and
income-and-spending bars, and any add-on's) follow them.

Whether each is shown is kept with the Dashboard's configuration: a section under
its own name, any other breadlet under `breadlet:<id>`
(`dashboard_config.breadlet_key`); one the household has not chosen for takes its
`shown_by_default` (every section but pending reimbursables). Saving an unknown key
is refused. `DashboardSettings.breadlets` lists every dashboard breadlet in the order
shown, which is the one list Configure Dashboard offers in GTK and the browser.
Running them together, an interface wraps the book in `breadlets.dashboard.sharing`,
so they share one `book.dashboard()` (and the browser's `/api/dashboard` sends every
shown breadlet's document at once, calculated with the horizons asked for).

## What a document can do on screen

The Dashboard's sections needed more than a printable document, and every report
gets the same abilities:

- **Links.** A `Card` or `TableRow` may carry a `link`: `schedule:<handle>`,
  `register:<account>`, `account:<account>` (a card's payment behaviour),
  `due-review`, `goals`, `receivables`, `fsa-claims`, or `report:<key>`
  (`report_layout.LINK_KINDS`). GTK follows one in `DashboardView.follow` (a row is
  activated by double-click or Enter, a card by a click or Enter), the browser in
  `followDashboardLink` (a row's first cell is a link button). Printing ignores them.
- **Actions.** An `Actions` block is a row of commands, such as **Review due
  transactions…** above the bills when something is due; printing leaves it out.
- **Interactive tables.** A `Table` with a `key` is sortable by every column (a
  cell's `sort` value, else its number or text) and lets the person choose its
  columns, remembered under that key: in GTK a `Gtk.ColumnView` in a `table_section`
  with the same column settings as every other table, in the browser a sortable
  table whose hidden columns are kept per browser. The Dashboard's bills, income,
  reimbursables and goals use the keys their tables always had (`dashboard`,
  `dashboard-income`, ...), so Configure Dashboard's Columns page still lists them.
- A missed schedule's dates are its due cell's note, below the range, on every
  screen and on paper.

## Charts drawn as a ring

`PIE` is a ring (a donut): one series, each category a slice sized by its share of
`totals[0]`, the whole the engine reported (`ChartModel.slice_share`; a slice is
drawn only for a positive value of a positive whole). Slice *n* takes colour slot
*n + 1*, so a ring has at most eight slices. `presentation.charts.chart_pie_layout`
holds the geometry for GTK and print (clockwise from twelve o'clock, the ring 42% of
its radius, the legend beside it with each share, and `slice_at` for hover); the
browser mirrors it in `pieChart`. Adjacent slices are separated by a 2-unit line of
the surface colour, and `chart_table` gives a ring's table as amount and share with
the whole as its total row.

## Add-ons

An add-on is an installed Python package naming a `register(manager)` function under
the `breadsched.breadlets` entry-point group. `load_installed` calls each into a
scratch registry first, so a package that raises registers nothing and is reported
(`PluginManager.problems`, listed by every interface) rather than stopping the
application; what a package registers is marked with its source. Nothing is loaded
from a data folder or a file found on disk: an application whose job is reading
other people's financial documents does not execute code found beside them, and a
package is something the person chose to install.

Tests: `tests/test_breadlets.py` (registry, options, add-on loading, the ring, the
shipped reports, Dashboard settings, the web routes and refusals, the command line),
`TestBreadletReports` and `TestDashboardIsHome` in `tests/test_gui.py`, and the
Reports and Dashboard tests in `tests/test_web_browser.py`.
