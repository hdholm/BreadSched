"""Breadlets: the registry, typed options, add-on loading, the shipped reports, the
ring chart, and the same document through the web and the command line."""

from __future__ import annotations

import json
import math
import threading
import urllib.error
import urllib.request
from datetime import date
from types import SimpleNamespace

import pytest

from breadsched.api.local import book_server
from breadsched.api.views import report_document_json
from breadsched.cli.main import main as cli
from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.engine.chart_model import PIE, ChartModel, ChartSeries
from breadsched.gen.engine.dashboard import BREADLET_PREFIX
from breadsched.gen.lib.money import Money
from breadsched.gen.plug import (
    DASHBOARD,
    EXPORTER,
    IMPORTER,
    REPORT,
    BreadletOption,
    BreadletOptionError,
    Plugin,
    PluginManager,
)
from breadsched.gen.plug._breadlets import load_installed, resolve_options
from breadsched.gen.sample_book import create_sample_book
from breadsched.gen.services import SaveDashboardConfig
from breadsched.plugins.breadlets import income_and_spending, spending_by_category
from breadsched.plugins.export.html_report import model_chart_svg, render_html
from breadsched.plugins.export.report_layout import ModelChart, chart_table
from breadsched.presentation import chart_pie_layout
from breadsched.web.server import serve

TODAY = date(2026, 3, 15)
FEBRUARY = {"start": "2026-02-01", "end": "2026-02-28"}


@pytest.fixture(scope="module")
def sample_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("breadlets") / "sample.breadsched"
    create_sample_book(path, as_of=TODAY)
    return path


@pytest.fixture
def book(sample_path):
    db = DbSQLite()
    db.load(str(sample_path))
    try:
        yield book_server(db)
    finally:
        db.close()


def _manager() -> PluginManager:
    return PluginManager.instance()


def _money(text: str) -> Money:
    return Money(text)


# ------------------------------------------------------------------ registry


class TestRegistry:
    def test_every_kind_is_one_registry(self):
        manager = _manager()
        assert {"gnucash-sqlite", "gnucash-xml", "ofx", "qif"} <= {
            plugin.id for plugin in manager.by_category(IMPORTER)
        }
        assert {"csv-transactions", "csv-projection"} <= {
            plugin.id for plugin in manager.by_category(EXPORTER)
        }
        assert {
            "spending-by-category",
            "income-and-spending",
            "net-worth",
            "budget-jars",
            "tax-year",
            "realized-gains",
        } == {plugin.id for plugin in manager.by_category(REPORT)}
        assert {"spending-this-month", "recent-income-and-spending"} == {
            plugin.id for plugin in manager.by_category(DASHBOARD)
        }
        assert all(plugin.source == "built in" for plugin in manager.all())

    def test_a_breadlet_describes_its_options_with_todays_defaults(self):
        described = _manager().get(REPORT, "income-and-spending").describe(TODAY)
        assert described["kind"] == REPORT
        options = {option["key"]: option for option in described["options"]}
        assert options["start"]["default"] == "2025-04-01"
        assert options["end"]["default"] == "2026-03-31"
        assert options["period"]["choices"] == [
            ["month", "Month"],
            ["quarter", "Quarter"],
            ["year", "Year"],
        ]


class TestOptions:
    OPTIONS = (
        BreadletOption("start", "From", "date", "2026-01-01"),
        BreadletOption("end", "Through", "date", lambda today: today.isoformat()),
        BreadletOption("count", "Count", "integer", "3", minimum=1, maximum=12),
        BreadletOption("period", "Group by", "choice", "month", (("month", "Month"),)),
        BreadletOption("detail", "Detail", "flag", "no"),
    )

    def test_defaults_fill_what_is_not_given_and_text_is_typed(self):
        values = resolve_options(self.OPTIONS, {"count": "5", "detail": "yes"}, TODAY)
        assert values == {
            "start": date(2026, 1, 1),
            "end": TODAY,
            "count": 5,
            "period": "month",
            "detail": True,
        }

    def test_blank_text_is_not_set(self):
        assert resolve_options(self.OPTIONS, {"count": " "}, TODAY)["count"] is None

    @pytest.mark.parametrize(
        ("given", "key", "message"),
        [
            ({"start": "March"}, "start", "From must be a date (YYYY-MM-DD)."),
            ({"count": "x"}, "count", "Count must be a whole number."),
            ({"count": "13"}, "count", "Count must be from 1 to 12."),
            ({"period": "week"}, "period", "Group by must be one of month."),
            ({"detail": "maybe"}, "detail", "Detail must be yes or no."),
            ({"colour": "red"}, "colour", "This breadlet has no option named 'colour'."),
            ({"end": "2025-12-31"}, "end", "The end date is before the start date."),
        ],
    )
    def test_text_that_does_not_fit_names_its_option(self, given, key, message):
        with pytest.raises(BreadletOptionError) as caught:
            resolve_options(self.OPTIONS, given, TODAY)
        assert (caught.value.key, str(caught.value)) == (key, message)


class TestInstalledBreadlets:
    def _point(self, name, register):
        return SimpleNamespace(name=name, load=lambda: register)

    def test_an_installed_package_registers_its_breadlets(self):
        def register(manager):
            manager.register(Plugin("hello", "Hello", REPORT, lambda *_: None))

        manager = PluginManager()
        problems = load_installed(manager, lambda: [self._point("hello-pkg", register)])
        assert problems == []
        assert manager.get(REPORT, "hello").source == "installed: hello-pkg"

    def test_a_broken_package_is_reported_and_keeps_nothing(self):
        def register(manager):
            manager.register(Plugin("half", "Half", REPORT, lambda *_: None))
            raise RuntimeError("missing dependency")

        manager = PluginManager()
        problems = load_installed(manager, lambda: [self._point("broken", register)])
        assert [(item.name, item.reason) for item in problems] == [("broken", "missing dependency")]
        assert manager.get(REPORT, "half") is None


# ------------------------------------------------------------------ ring chart


def _ring(values, whole) -> ChartModel:
    return ChartModel(
        "ring",
        "Ring",
        PIE,
        tuple(f"C{index}" for index in range(len(values))),
        (ChartSeries("v", "Value", tuple(values), 1),),
        "USD",
        totals=(whole,),
    )


class TestRing:
    def test_slices_are_shares_of_the_whole_the_engine_reported(self):
        model = _ring([_money("30"), _money("10"), None, _money("-5")], _money("40"))
        assert model.slice_share(0) == 75.0
        assert model.slice_share(1) == 25.0
        assert model.slice_share(2) is None and model.slice_share(3) is None
        layout = chart_pie_layout(model, 0, 0, 600, 200)
        assert [item.category for item in layout.slices] == [0, 1]
        assert math.isclose(layout.slices[-1].end, 2 * math.pi)
        assert [item.slot for item in layout.slices] == [1, 2]

    def test_a_point_on_the_ring_or_its_legend_names_its_slice(self):
        layout = chart_pie_layout(_ring([_money("3"), _money("1")], _money("4")), 0, 0, 600, 200)
        radius = (layout.outer + layout.inner) / 2
        # Three o'clock is a quarter of the way round: inside the first slice (75%).
        assert layout.slice_at(layout.cx + radius, layout.cy).category == 0
        # Just past nine o'clock (three quarters round) is the start of the second.
        assert layout.slice_at(layout.cx - radius, layout.cy - 1).category == 1
        assert layout.slice_at(layout.cx, layout.cy) is None  # the hole
        assert layout.slice_at(layout.legend_x + 4, layout.legend_ys[1] - 4).category == 1

    def test_the_table_beside_a_ring_has_amounts_shares_and_the_whole(self):
        table = chart_table(_ring([_money("30"), _money("10")], _money("40")))
        assert [column.label for column in table.columns] == ["Category", "Amount", "Share"]
        assert [[cell.text for cell in row.cells] for row in table.rows] == [
            ["C0", "30.00", "75.0%"],
            ["C1", "10.00", "25.0%"],
            ["Total", "40.00", ""],
        ]

    def test_printing_draws_each_slice_with_its_exact_amount(self):
        svg = model_chart_svg(_ring([_money("30"), _money("10")], _money("40")))
        assert svg.count("<path") == 2
        assert "<title>C0: 30.00 USD (75.0%)</title>" in svg
        whole = model_chart_svg(_ring([_money("40")], _money("40")))
        assert "<circle" in whole and "<title>C0: 40.00 USD (100.0%)</title>" in whole


# ------------------------------------------------------------------ shipped reports


def _explorer(spending, income=()):
    def point(label, total, categories=()):
        return SimpleNamespace(
            label=label, actual=total, categories=categories, currency_incomplete=False
        )

    categories = [SimpleNamespace(account=f"h{index}", name=f"Cat {index}") for index in range(10)]
    return SimpleNamespace(
        categories=categories,
        spending=[point(label, total, cats) for label, total, cats in spending],
        income=[point(label, total) for label, total, _cats in income],
    )


class TestShippedReports:
    def test_the_ring_names_seven_categories_and_folds_the_rest_into_other(self):
        amounts = [(f"h{index}", _money(str(100 - index))) for index in range(10)]
        refunded = [("h9", _money("-200"))]
        explorer = _explorer(
            [("Jan", _money("0"), tuple(amounts[:9]) + tuple(refunded))],
        )
        model, net = spending_by_category(explorer, "USD")
        assert model.categories == (*(f"Cat {index}" for index in range(7)), "Other")
        assert model.series[0].values[-1] == _money("92") + _money("93")
        # The category refunded more than it spent is not a slice; the net counts it.
        assert model.totals == (sum((value for _h, value in amounts[:9]), Money(0)),)
        assert net == model.totals[0] - _money("200")

    def test_income_and_spending_are_side_by_side_per_period(self):
        explorer = _explorer(
            [("Jan", _money("40"), ()), ("Feb", _money("50"), ())],
            [("Jan", _money("100"), ()), ("Feb", _money("90"), ())],
        )
        model = income_and_spending(explorer)
        assert model.categories == ("Jan", "Feb")
        assert [series.values for series in model.series] == [
            (_money("100"), _money("90")),
            (_money("40"), _money("50")),
        ]

    def test_every_shipped_breadlet_runs_on_a_sample_book(self, book):
        for kind in (REPORT, DASHBOARD):
            for plugin in _manager().by_category(kind):
                document = plugin.produce(book, {}, TODAY)
                assert document.title and document.sections, plugin.id
                assert render_html(document).startswith("<!doctype html>"), plugin.id

    def test_spending_by_category_reads_the_books_spending(self, book):
        plugin = _manager().get(REPORT, "spending-by-category")
        document = plugin.produce(book, FEBRUARY, TODAY)
        assert document.subtitle == "2026-02-01 through 2026-02-28"
        chart = next(
            block for block in document.sections[0].blocks if isinstance(block, ModelChart)
        )
        assert chart.model.kind == PIE
        assert dict(zip(chart.model.categories, chart.model.series[0].values, strict=True)) == {
            "Housing": _money("1450"),
            "Groceries": _money("320"),
            "Utilities": _money("180"),
        }
        assert chart.model.totals == (_money("1950"),)

    def test_a_range_before_the_books_first_month_starts_there(self, book):
        plugin = _manager().get(REPORT, "income-and-spending")
        document = plugin.produce(book, {"start": "2020-01-01", "end": "2026-02-28"}, TODAY)
        assert document.subtitle == "2026-02-01 through 2026-02-28"
        assert "Feb 2026 | 4,200.00 | 1,950.00 | 2,250.00" in document.text()

    def test_the_document_has_one_json_form(self, book):
        plugin = _manager().get(REPORT, "spending-by-category")
        data = report_document_json(plugin.produce(book, FEBRUARY, TODAY))
        blocks = data["sections"][0]["blocks"]
        assert [block["type"] for block in blocks] == ["chart", "table"]
        assert blocks[0]["model"]["kind"] == "pie"
        assert blocks[1]["rows"][0]["cells"][0]["text"] == "Housing"
        json.dumps(data, default=str)


# ------------------------------------------------------------------ Dashboard


class TestDashboardBreadlets:
    def test_dashboard_breadlets_show_until_hidden(self, sample_path):
        db = DbSQLite()
        db.load(str(sample_path))
        try:
            server = book_server(db)
            shown = {item.id: item.shown for item in server.dashboard_settings().breadlets}
            assert shown == {"recent-income-and-spending": True, "spending-this-month": True}
            key = f"{BREADLET_PREFIX}spending-this-month"
            saved = server.save_dashboard_settings(SaveDashboardConfig(sections={key: False}))
            assert {item.id: item.shown for item in saved.breadlets}["spending-this-month"] is False
            assert {item.id: item.shown for item in server.dashboard_settings().breadlets} == {
                "recent-income-and-spending": True,
                "spending-this-month": False,
            }
        finally:
            db.close()

    def test_an_unknown_breadlet_is_refused_and_nothing_changes(self, sample_path):
        from breadsched.api import ApiError

        db = DbSQLite()
        db.load(str(sample_path))
        try:
            server = book_server(db)
            before = server.dashboard_settings()
            with pytest.raises(ApiError) as caught:
                server.save_dashboard_settings(
                    SaveDashboardConfig(sections={f"{BREADLET_PREFIX}nope": True}, liquidity_days=9)
                )
            assert caught.value.code == "dashboard.section.invalid"
            assert server.dashboard_settings() == before
        finally:
            db.close()


# ------------------------------------------------------------------ interfaces


@pytest.fixture
def web(sample_path):
    db = DbSQLite()
    db.load(str(sample_path))
    httpd = serve(db, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    def get(path: str):
        request = urllib.request.Request(
            base + path,
            headers={"X-BreadSched-Token": httpd.token, "X-BreadSched-Local": httpd.local_token},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    try:
        yield get
    finally:
        httpd.shutdown()
        httpd.server_close()
        db.close()


class TestWeb:
    def test_breadlets_are_listed_by_kind(self, web):
        status, listed = web("/api/breadlets?kind=report")
        assert status == 200 and listed["problems"] == []
        assert {item["kind"] for item in listed["breadlets"]} == {"report"}
        status, everything = web("/api/breadlets")
        assert {item["kind"] for item in everything["breadlets"]} == {
            "importer",
            "exporter",
            "report",
            "dashboard",
        }

    def test_a_report_runs_with_its_options(self, web):
        status, found = web("/api/breadlet?id=spending-by-category&start=2026-02-01&end=2026-02-28")
        assert status == 200
        assert found["breadlet"]["id"] == "spending-by-category"
        document = found["document"]
        assert document["subtitle"] == "2026-02-01 through 2026-02-28"
        assert document["sections"][0]["blocks"][0]["model"]["totals"] == ["1950.00"]

    def test_a_dashboard_breadlet_runs(self, web):
        status, found = web("/api/breadlet?kind=dashboard&id=spending-this-month")
        assert status == 200 and found["document"]["kind"] == "dashboard"

    def test_the_dashboard_lists_its_breadlets_among_its_sections(self, web):
        status, board = web("/api/dashboard")
        assert status == 200
        assert (
            board["config"]["section_titles"][f"{BREADLET_PREFIX}spending-this-month"]
            == "Spending this month"
        )
        assert [item["id"] for item in board["config"]["breadlets"]] == [
            "recent-income-and-spending",
            "spending-this-month",
        ]

    @pytest.mark.parametrize(
        ("path", "status", "code", "fields"),
        [
            ("/api/breadlet?id=nope", 404, "breadlet.not_found", ["id"]),
            ("/api/breadlet", 400, "query.missing", ["id"]),
            (
                "/api/breadlet?id=spending-by-category&start=soon",
                400,
                "breadlet.option.invalid",
                ["start"],
            ),
            ("/api/breadlet?id=spending-by-category&colour=red", 400, "query.unknown", ["colour"]),
            ("/api/breadlets?kind=widget", 400, "query.invalid", ["kind"]),
        ],
    )
    def test_refusals_name_the_field(self, web, path, status, code, fields):
        found_status, error = web(path)
        assert (found_status, error["code"], error["fields"]) == (status, code, fields)


class TestCommandLine:
    def test_breadlets_lists_reports_with_their_options(self, capsys):
        assert cli(["breadlets", "--kind", "report"]) == 0
        out = capsys.readouterr().out
        assert "spending-by-category" in out and "start= end=" in out

    def test_report_prints_text_json_and_html(self, sample_path, tmp_path, capsys):
        page = tmp_path / "spending.html"
        options = ["--option", "start=2026-02-01", "--option", "end=2026-02-28"]
        assert cli(["report", str(sample_path), "spending-by-category", *options]) == 0
        assert "Housing | 1,450.00 | 74.4%" in capsys.readouterr().out
        assert (
            cli(
                [
                    "report",
                    str(sample_path),
                    "spending-by-category",
                    *options,
                    "--json",
                    "--html",
                    str(page),
                ]
            )
            == 0
        )
        data = json.loads(capsys.readouterr().out)
        assert data["sections"][0]["blocks"][0]["type"] == "chart"
        assert "<svg" in page.read_text(encoding="utf-8")

    def test_a_bad_option_is_a_clean_error(self, sample_path, capsys):
        args = ["report", str(sample_path), "spending-by-category", "--option", "start=soon"]
        assert cli(args) == 2
        assert "start: From must be a date (YYYY-MM-DD)." in capsys.readouterr().err
        assert cli(["report", str(sample_path), "nope"]) == 2
