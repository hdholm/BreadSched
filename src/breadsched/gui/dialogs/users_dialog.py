"""Preferences → Users: the people and devices that may use this computer's books.

Decision 0004. GTK runs as the computer account, which is the host's administrator
and owns its user directory, so this window works on that directory directly. Used
alone, it says so and offers only what is needed to use a book from another
device: a password for yourself, or a token for a device or script.
"""

from __future__ import annotations

from collections.abc import Callable

from ...api.users import User, UserDirectory, UserError
from ..gi_setup import Gtk
from ..widgets.bounded import BoundedWindow
from ..widgets.help import help_row

__all__ = ["UsersDialog"]


class UsersDialog(BoundedWindow):
    """You, the other people who may use the books, and your devices."""

    def __init__(self, parent: Gtk.Window | None, directory: UserDirectory | None = None) -> None:
        super().__init__(title="Users and devices", transient_for=parent, modal=True)
        self.set_destroy_with_parent(True)
        self.directory = directory if directory is not None else UserDirectory()
        self.set_default_size(560, -1)
        #: The form the window shows in place of its lists, if any.
        self.form: _Form | None = None

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(18)
        self.set_child(outer)
        outer.append(help_row("preferences"))
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.append(self.content)
        self.status = Gtk.Label(xalign=0, wrap=True)
        self.status.add_css_class("dim")
        outer.append(self.status)
        close = Gtk.Button(label="Close", halign=Gtk.Align.END)
        close.connect("clicked", lambda *_: self.close())
        outer.append(close)
        self.refresh()

    # ------------------------------------------------------------------ lists

    def refresh(self) -> None:
        self.form = None
        _clear(self.content)
        you = self.directory.host_user()
        others = [user for user in self.directory.users() if not user.host_account]

        self._heading("You")
        password = "Change your password…" if you.has_password else "Set a password…"
        self.content.append(
            _row(
                Gtk.Label(
                    label=f"{you.display_name} ({you.name}), this computer's account",
                    xalign=0,
                    hexpand=True,
                    wrap=True,
                ),
                _button(
                    password, self._ask_own_password, "Needed only to sign in from another device"
                ),
            )
        )

        self._heading("People")
        if not others:
            self.content.append(
                Gtk.Label(
                    label="Only you use this computer's books. Add someone to let them "
                    "sign in from their own device.",
                    xalign=0,
                    wrap=True,
                )
            )
        for user in others:
            notes = [user.name]
            if user.administrator:
                notes.append("administrator")
            if user.must_change:
                notes.append("chooses a password at next sign-in")
            self.content.append(
                _row(
                    Gtk.Label(
                        label=f"{user.display_name} ({', '.join(notes)})",
                        xalign=0,
                        hexpand=True,
                        wrap=True,
                    ),
                    _button("Reset password…", lambda *_, u=user: self._ask_reset(u)),
                    _button("Remove", lambda *_, u=user: self._remove(u)),
                )
            )
        self.content.append(_row(_button("Add a person…", self._ask_add)))

        self._heading("Your devices")
        tokens = self.directory.tokens(you)
        if not tokens:
            self.content.append(Gtk.Label(label="None yet.", xalign=0))
        for token in tokens:
            used = token.last_used or "never"
            kind = "this computer" if token.host_files else token.kind
            self.content.append(
                _row(
                    Gtk.Label(
                        label=f"{token.name} ({kind}; last used {used})",
                        xalign=0,
                        hexpand=True,
                        wrap=True,
                    ),
                    _button("Revoke", lambda *_, t=token.id: self._revoke(t)),
                )
            )
        self.content.append(
            _row(
                _button(
                    "Add a device or script…",
                    self._ask_token,
                    "A token for a script or a device that cannot sign in",
                )
            )
        )

    def _heading(self, text: str) -> None:
        label = Gtk.Label(label=text, xalign=0)
        label.add_css_class("summary-label")
        self.content.append(label)

    def _done(self, message: str) -> None:
        self.refresh()
        self.status.set_text(message)

    def _attempt(self, action: Callable[[], str]) -> None:
        try:
            message = action()
        except UserError as error:
            self.status.set_text(error.message)
            return
        self._done(message)

    # ------------------------------------------------------------------ forms

    def _show(self, form: _Form) -> None:
        self.form = form
        _clear(self.content)
        self.content.append(form)
        self.status.set_text("")
        form.first.grab_focus()

    def _ask_own_password(self, *_args) -> None:
        you = self.directory.host_user()
        fields = [("new", "New password"), ("again", "New password again")]
        if you.has_password:
            fields.insert(0, ("current", "Current password"))

        def save(values: dict[str, str]) -> str:
            if values["new"] != values["again"]:
                raise UserError("users.password.mismatch", ("again",))
            self.directory.set_password(you.name, values["new"], current=values.get("current"))
            return "Your password is set; use it to sign in from another device."

        self._show(_Form("Your password", fields, "Save", self, save))

    def _ask_add(self, *_args) -> None:
        def add(values: dict[str, str]) -> str:
            user = self.directory.add_user(
                values["name"].strip(),
                values["password"],
                display_name=values["display_name"],
                administrator=values["administrator"] == "yes",
            )
            return (
                f"Added {user.display_name}. Tell them their user name and temporary "
                "password; they choose their own when they first sign in."
            )

        self._show(
            _Form(
                "Add a person",
                [
                    ("name", "User name"),
                    ("display_name", "Name shown"),
                    ("password", "Temporary password"),
                ],
                "Add",
                self,
                add,
                check=("administrator", "May manage users"),
            )
        )

    def _ask_reset(self, user: User) -> None:
        def reset(values: dict[str, str]) -> str:
            self.directory.reset_password(user.name, values["password"])
            return f"{user.display_name} chooses a new password when they next sign in."

        self._show(
            _Form(
                f"Reset {user.display_name}'s password",
                [("password", "Temporary password")],
                "Reset",
                self,
                reset,
            )
        )

    def _ask_token(self, *_args) -> None:
        def make(values: dict[str, str]) -> str:
            name = values["name"].strip() or "Device"
            token = self.directory.issue_token(self.directory.host_user(), name)
            self.refresh()
            shown = Gtk.Label(label=token, xalign=0, wrap=True, selectable=True)
            shown.set_name("new-token")
            self.content.prepend(shown)
            self.content.prepend(
                Gtk.Label(
                    label=f"The token for {name}, shown only now. Keep it in a file only "
                    "you can read (--token-file).",
                    xalign=0,
                    wrap=True,
                )
            )
            return ""

        self._show(
            _Form(
                "Add a device or script",
                [("name", "Its name")],
                "Make token",
                self,
                make,
                keep_open=True,
            )
        )

    # ---------------------------------------------------------------- actions

    def _remove(self, user: User) -> None:
        def remove() -> str:
            self.directory.remove_user(user.name)
            return f"Removed {user.display_name}."

        self._attempt(remove)

    def _revoke(self, token_id: str) -> None:
        def revoke() -> str:
            self.directory.revoke(token_id, self.directory.host_user())
            return "Revoked."

        self._attempt(revoke)


class _Form(Gtk.Box):
    """A few labelled entries, with a button that submits them and one that cancels."""

    def __init__(
        self,
        title: str,
        fields: list[tuple[str, str]],
        action: str,
        dialog: UsersDialog,
        submit: Callable[[dict[str, str]], str],
        *,
        check: tuple[str, str] | None = None,
        keep_open: bool = False,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        heading = Gtk.Label(label=title, xalign=0)
        heading.add_css_class("heading")
        self.append(heading)
        self.entries: dict[str, Gtk.Editable] = {}
        for key, label in fields:
            secret = "password" in key or key in ("current", "new", "again")
            entry: Gtk.Editable = Gtk.PasswordEntry(show_peek_icon=True) if secret else Gtk.Entry()
            entry.update_property([Gtk.AccessibleProperty.LABEL], [label])
            self.append(Gtk.Label(label=label, xalign=0))
            self.append(entry)
            self.entries[key] = entry
        self.first = next(iter(self.entries.values()))
        self.check: Gtk.CheckButton | None = None
        if check is not None:
            self.check = Gtk.CheckButton(label=check[1])
            self._check_key = check[0]
            self.append(self.check)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: dialog.refresh())
        buttons.append(cancel)
        self.submit_button = Gtk.Button(label=action)
        self.submit_button.add_css_class("suggested-action")
        buttons.append(self.submit_button)
        self.append(buttons)

        def on_submit(*_args) -> None:
            values = {key: entry.get_text() for key, entry in self.entries.items()}
            if self.check is not None:
                values[self._check_key] = "yes" if self.check.get_active() else ""
            try:
                message = submit(values)
            except UserError as error:
                dialog.status.set_text(error.message)
                return
            if not keep_open:
                dialog.refresh()
            dialog.status.set_text(message)

        self.submit_button.connect("clicked", on_submit)

    def fill(self, **values: str) -> None:
        """Type ``values`` into the entries (for tests and scripted use)."""
        for key, value in values.items():
            self.entries[key].set_text(value)


def _clear(box: Gtk.Box) -> None:
    child = box.get_first_child()
    while child is not None:
        box.remove(child)
        child = box.get_first_child()


def _row(*children: Gtk.Widget) -> Gtk.Box:
    row = Gtk.Box(spacing=8)
    for child in children:
        row.append(child)
    return row


def _button(label: str, callback: Callable[..., None], tooltip: str = "") -> Gtk.Button:
    button = Gtk.Button(label=label)
    if tooltip:
        button.set_tooltip_text(tooltip)
    button.connect("clicked", callback)
    return button
