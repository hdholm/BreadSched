"""Reports → Breadlet Reports: choose a report breadlet, set its options, and print it.

The options form is built from the breadlet's declared options; the text typed is
validated by the shared ``resolve_options``, and the breadlet reads the book only
through the book server, as it does in the browser and on the command line.
"""

from __future__ import annotations

from datetime import date

from ...api import ApiError
from ...api.local import book_server
from ...gen.plug import REPORT, BreadletOptionError, Plugin, PluginManager
from .. import printing
from ..gi_setup import Gtk
from ..remote_book import OpenBook
from ..widgets.bounded import BoundedWindow
from ..widgets.choice import bounded_dropdown
from ..widgets.date_entry import attach_date_keys, parse_date_text
from ..widgets.document_view import DocumentView
from ..widgets.help import help_row

__all__ = ["BreadletReportsDialog"]


class BreadletReportsDialog(BoundedWindow):
    """Every report breadlet, one at a time, with its options and its document."""

    def __init__(
        self,
        parent: Gtk.Window | None,
        db: OpenBook,
        breadlet: str | None = None,
        *,
        today: date | None = None,
    ) -> None:
        super().__init__(title="Breadlet Reports", transient_for=parent)
        self.db = db
        self.today = today or date.today()
        self.set_default_size(960, 680)
        manager = PluginManager.instance()
        self.breadlets: list[Plugin] = manager.by_category(REPORT)
        #: The option widgets of the chosen breadlet, by option key.
        self.inputs: dict[str, Gtk.Widget] = {}

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(12)
        self.set_child(outer)
        outer.append(help_row("reports"))
        for problem in manager.problems:
            note = Gtk.Label(
                label=f"Add-on {problem.name} did not load: {problem.reason}", xalign=0, wrap=True
            )
            note.add_css_class("error")
            outer.append(note)

        chooser = Gtk.Box(spacing=8)
        chooser.append(Gtk.Label(label="Report"))
        self.choice = bounded_dropdown([plugin.name for plugin in self.breadlets])
        self.choice.update_property([Gtk.AccessibleProperty.LABEL], ["Report"])
        chooser.append(self.choice)
        self.print_button = Gtk.Button(label="Print…")
        self.print_button.connect("clicked", lambda *_: self.print_report())
        chooser.append(self.print_button)
        outer.append(chooser)
        self.description = Gtk.Label(xalign=0, wrap=True)
        self.description.add_css_class("dim")
        outer.append(self.description)
        self.options = Gtk.Box(spacing=8)
        outer.append(self.options)
        self.message = Gtk.Label(xalign=0, wrap=True)
        self.message.add_css_class("error")
        outer.append(self.message)

        self.view = DocumentView()
        scroller = Gtk.ScrolledWindow(child=self.view, vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(320)
        outer.append(scroller)
        close = Gtk.Button(label="Close", halign=Gtk.Align.END)
        close.connect("clicked", lambda *_: self.close())
        outer.append(close)

        ids = [plugin.id for plugin in self.breadlets]
        if breadlet in ids:
            self.choice.set_selected(ids.index(breadlet))
        self.choice.connect("notify::selected", lambda *_: self.choose())
        if self.breadlets:
            self.choose()
        else:
            self.description.set_text("No report breadlets are installed.")

    @property
    def breadlet(self) -> Plugin | None:
        index = self.choice.get_selected()
        if index == Gtk.INVALID_LIST_POSITION or index >= len(self.breadlets):
            return None
        return self.breadlets[index]

    def choose(self) -> None:
        """Show the chosen breadlet's options, with their defaults, and run it."""
        plugin = self.breadlet
        while (child := self.options.get_first_child()) is not None:
            self.options.remove(child)
        self.inputs = {}
        if plugin is None:
            return
        self.description.set_text(plugin.description)
        for option in plugin.options:
            default = option.default_text(self.today)
            self.options.append(Gtk.Label(label=option.label))
            if option.kind == "choice":
                widget = bounded_dropdown([label for _value, label in option.choices])
                values = [value for value, _label in option.choices]
                if default in values:
                    widget.set_selected(values.index(default))
            elif option.kind == "flag":
                widget = Gtk.CheckButton(active=default.lower() in ("true", "yes", "1", "on"))
            else:
                widget = Gtk.Entry(text=default, width_chars=11)
                if option.kind == "date":
                    attach_date_keys(widget)
                widget.connect("activate", lambda *_: self.run())
                if option.help:
                    widget.set_placeholder_text(option.help)
            widget.update_property([Gtk.AccessibleProperty.LABEL], [option.label])
            self.inputs[option.key] = widget
            self.options.append(widget)
        run = Gtk.Button(label="Run")
        run.add_css_class("suggested-action")
        run.connect("clicked", lambda *_: self.run())
        self.options.append(run)
        self.run()

    def given(self) -> dict[str, str]:
        """The text of each option, as the shared option parser reads it."""
        plugin = self.breadlet
        given: dict[str, str] = {}
        for option in plugin.options if plugin else ():
            widget = self.inputs[option.key]
            if isinstance(widget, Gtk.DropDown):
                index = widget.get_selected()
                given[option.key] = option.choices[index][0] if index < len(option.choices) else ""
            elif isinstance(widget, Gtk.CheckButton):
                given[option.key] = "true" if widget.get_active() else "false"
            elif option.kind == "date" and widget.get_text().strip():
                try:
                    given[option.key] = parse_date_text(widget.get_text(), self.today).isoformat()
                except ValueError:
                    given[option.key] = widget.get_text()
            else:
                given[option.key] = widget.get_text()
        return given

    def run(self) -> bool:
        """Run the chosen breadlet; a refused option or book error is shown, not raised."""
        plugin = self.breadlet
        if plugin is None:
            return False
        try:
            document = plugin.produce(book_server(self.db), self.given(), self.today)
        except (BreadletOptionError, ApiError) as error:
            self.message.set_text(str(error))
            return False
        self.message.set_text("")
        self.view.show_document(document)
        return True

    def print_report(self) -> None:
        if self.view.document is not None:
            printing.print_document(self, self.view.document)
