"""Print reports natively through GTK, or open them in the system browser.

Dashboard, Plan, and Projection print through GTK's own print dialog
(:func:`print_report`): the report layout is drawn with Pango and cairo by
:class:`~breadsched.gui.report_printer.ReportPrinter`, and the dialog offers the
platform's printers, its preview, and printing to a PDF file. A report with an
optional section (the Plan's category detail) adds a **Report** tab to the dialog
to include it. :func:`export_pdf` draws the same pages straight to a PDF file.

The older route, a private self-contained HTML page opened in the default browser,
stays available as **File → Print in Browser…**; the dialog reports
(:func:`print_document`) fall back to it when GTK printing fails.
"""

from __future__ import annotations

import atexit
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import cairo

from ..plugins.export.report_layout import ReportDocument
from .gi_setup import Gio, Gtk
from .report_printer import ReportPrinter

__all__ = [
    "export_pdf",
    "print_document",
    "open_print_preview",
    "print_report",
    "write_print_preview",
]

_PREVIEWS: set[Path] = set()

#: A4 landscape in points: the page used when the platform offers no default.
_DEFAULT_PAGE = (841.89, 595.28)
_MARGIN = 34.0

#: Page setup and print settings chosen in this session, reused next time.
_SESSION: dict[str, object] = {}


def write_print_preview(document: str) -> Path:
    """Write one owner-readable temporary report and return its path."""
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="breadsched-print-",
        suffix=".html",
        delete=False,
    ) as target:
        target.write(document)
        path = Path(target.name)
    _PREVIEWS.add(path)
    return path


def open_print_preview(document: str) -> Path:
    """Open a generated report in the default browser and its print dialog."""
    path = write_print_preview(document)
    Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)
    return path


def export_pdf(
    document: ReportDocument,
    path: str | Path,
    *,
    include_optional: bool = False,
    page_size: tuple[float, float] = _DEFAULT_PAGE,
) -> int:
    """Draw the report to a PDF file without a dialog; return its page count."""
    width, height = page_size
    surface = cairo.PDFSurface(str(path), width, height)
    # PDF 1.4 keeps page objects uncompressed, which any reader handles.
    surface.restrict_to_version(cairo.PDFVersion.VERSION_1_4)
    try:
        cr = cairo.Context(surface)
        printer = ReportPrinter(document, include_optional=include_optional)
        cr.translate(_MARGIN, _MARGIN)
        pages = printer.paginate(cr, width - 2 * _MARGIN, height - 2 * _MARGIN)
        for number in range(pages):
            cr.save()
            printer.draw_page(cr, number)
            cr.restore()
            cr.show_page()
    finally:
        surface.finish()
    return pages


def print_operation(
    document: ReportDocument, *, include_optional: bool = False
) -> Gtk.PrintOperation:
    """A print operation that lays out and draws ``document``."""
    operation = Gtk.PrintOperation()
    operation.set_job_name(f"BreadSched {document.title}")
    operation.set_unit(Gtk.Unit.POINTS)
    operation.set_embed_page_setup(True)
    operation.set_use_full_page(False)
    setup = _SESSION.get("page_setup")
    if not isinstance(setup, Gtk.PageSetup):
        setup = Gtk.PageSetup()
        setup.set_orientation(Gtk.PageOrientation.LANDSCAPE)
    operation.set_default_page_setup(setup)
    settings = _SESSION.get("settings")
    if isinstance(settings, Gtk.PrintSettings):
        operation.set_print_settings(settings)
    state: dict[str, Any] = {"include": include_optional, "printer": None}

    if document.has_optional:
        operation.set_custom_tab_label("Report")

        def create_widget(_operation):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            for side in ("top", "bottom", "start", "end"):
                getattr(box, f"set_margin_{side}")(12)
            check = Gtk.CheckButton(label=document.optional_label)
            check.set_active(state["include"])
            box.append(check)
            return box

        def apply_widget(_operation, widget):
            check = widget.get_first_child()
            state["include"] = bool(check.get_active())

        operation.connect("create-custom-widget", create_widget)
        operation.connect("custom-widget-apply", apply_widget)

    def begin(operation, context) -> None:
        printer = ReportPrinter(document, include_optional=bool(state["include"]))
        pages = printer.paginate(
            context.get_cairo_context(), context.get_width(), context.get_height()
        )
        state["printer"] = printer
        operation.set_n_pages(pages)

    def draw(_operation, context, number: int) -> None:
        printer = state["printer"]
        assert isinstance(printer, ReportPrinter)
        printer.draw_page(context.get_cairo_context(), number)

    operation.connect("begin-print", begin)
    operation.connect("draw-page", draw)
    return operation


def print_report(
    parent: Gtk.Window | None,
    document: ReportDocument,
    *,
    on_error: Callable[[str], None] | None = None,
) -> Gtk.PrintOperationResult:
    """Show GTK's print dialog for ``document``: print, preview, or save a PDF."""
    operation = print_operation(document)
    try:
        result = operation.run(Gtk.PrintOperationAction.PRINT_DIALOG, parent)
    except Exception as exc:  # noqa: BLE001 - reported to the user, never lost
        if on_error is not None:
            on_error(f"Could not print {document.title}: {exc}")
        return Gtk.PrintOperationResult.ERROR
    if result == Gtk.PrintOperationResult.APPLY:
        _SESSION["settings"] = operation.get_print_settings()
        _SESSION["page_setup"] = operation.get_default_page_setup()
    return result


def print_document(parent: Gtk.Window | None, document: ReportDocument) -> None:
    """Print natively; if GTK printing fails, open the report in the browser."""
    failures: list[str] = []
    result = print_report(parent, document, on_error=failures.append)
    if result == Gtk.PrintOperationResult.ERROR:
        from ..plugins.export.html_report import render_html

        open_print_preview(render_html(document))


@atexit.register
def _remove_previews() -> None:
    for path in _PREVIEWS:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
