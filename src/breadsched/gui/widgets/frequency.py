"""The schedule editors' "Every N <unit>" control (#370).

One spin button and one drop-down replace the old list of presets, so any
interval of days, weeks, months, or years can be entered, and an imported rule
such as every 4 weeks is an ordinary, editable choice. The units come from
:mod:`breadsched.presentation.schedules`, shared with the browser.
"""

from __future__ import annotations

from collections.abc import Callable

from ...gen.lib.recurrence import PeriodType
from ...presentation import (
    FIXED_INTERVAL_UNITS,
    FREQUENCY_UNITS,
    MAX_INTERVAL,
    frequency_unit,
)
from ..gi_setup import Gtk
from .choice import bounded_dropdown

__all__ = ["FrequencyPicker"]


class FrequencyPicker(Gtk.Box):
    """Every [N] [unit]: the period and interval a schedule repeats on."""

    def __init__(self, period: PeriodType = PeriodType.MONTH, interval: int = 1) -> None:
        super().__init__(spacing=6)
        self.every_label = Gtk.Label(label="Every")
        self.append(self.every_label)
        self.every = Gtk.SpinButton.new_with_range(1, MAX_INTERVAL, 1)
        self.every.set_numeric(True)
        self.every.update_property([Gtk.AccessibleProperty.LABEL], ["Repeat every"])
        self.append(self.every)
        self.unit = bounded_dropdown([label for _key, label, _period in FREQUENCY_UNITS])
        self.unit.update_property([Gtk.AccessibleProperty.LABEL], ["Repeat unit"])
        self.unit.connect("notify::selected", lambda *_: self._show_every())
        self.append(self.unit)
        #: The interval an imported twice-a-month rule keeps (it may repeat only
        #: every other month, for example), which this control does not show.
        self._kept_interval = 1
        self.set_value(period, interval)

    def set_value(self, period: PeriodType, interval: int) -> None:
        key = frequency_unit(period) or "months"
        index = next(i for i, (item, _l, _p) in enumerate(FREQUENCY_UNITS) if item == key)
        self._kept_interval = interval if period is PeriodType.SEMI_MONTH else 1
        self.every.set_value(min(max(interval, 1), MAX_INTERVAL))
        self.unit.set_selected(index)
        self._show_every()

    def unit_key(self) -> str:
        return FREQUENCY_UNITS[min(self.unit.get_selected(), len(FREQUENCY_UNITS) - 1)][0]

    def value(self) -> tuple[PeriodType, int]:
        """(period, interval) as chosen."""
        key, _label, period = FREQUENCY_UNITS[
            min(self.unit.get_selected(), len(FREQUENCY_UNITS) - 1)
        ]
        if key in FIXED_INTERVAL_UNITS:
            return period, self._kept_interval if key == "semimonthly" else 1
        return period, int(self.every.get_value())

    def connect_changed(self, callback: Callable[..., object]) -> None:
        self.unit.connect("notify::selected", callback)
        self.every.connect("value-changed", callback)

    def _show_every(self) -> None:
        fixed = self.unit_key() in FIXED_INTERVAL_UNITS
        self.every.set_visible(not fixed)
        self.every_label.set_visible(not fixed)
