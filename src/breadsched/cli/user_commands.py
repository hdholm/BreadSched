"""The book host's users, passwords, and device tokens (decision 0004).

These act on this computer account's own user directory: the account that runs the
host is its administrator and owns the file, so they need no password. That is also
how a forgotten administrator password is reset.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from ..api.users import UserDirectory, UserError
from ..gen.services.contracts import ServiceError
from ..presentation import service_error_message
from .common import AddCommand, CommandError, emit, table

__all__ = ["register"]


def _refused(error: UserError) -> CommandError:
    return CommandError(service_error_message(ServiceError(error.code, error.fields)))


def _password(args: argparse.Namespace, prompt: str) -> str:
    """A password from standard input (``--password-stdin``) or typed, never an
    argument, which other users could read in the process list."""
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass(f"{prompt}: ")
    if getpass.getpass(f"{prompt} again: ") != first:
        raise CommandError("the passwords do not match")
    return first


def _user_json(user) -> dict:
    return {
        "name": user.name,
        "display_name": user.display_name,
        "administrator": user.administrator,
        "this_computer": user.host_account,
        "has_password": user.has_password,
        "must_change": user.must_change,
    }


def cmd_users(args: argparse.Namespace) -> int:
    """List, add, or remove the host's users, or reset one's password."""
    directory = UserDirectory()
    try:
        if args.action == "list":
            found = directory.users()
            emit(
                [_user_json(user) for user in found],
                args,
                table(
                    [
                        [
                            user.name,
                            user.display_name,
                            "yes" if user.administrator else "",
                            "this computer" if user.host_account else "",
                        ]
                        for user in found
                    ],
                    ["user", "name", "administrator", "account"],
                ),
            )
            return 0
        if not args.name:
            raise CommandError(f"users {args.action} needs a user name")
        if args.action == "add":
            user = directory.add_user(
                args.name,
                _password(args, "Temporary password"),
                display_name=args.display_name or "",
                administrator=args.administrator,
            )
            emit(
                _user_json(user),
                args,
                f"Added {user.name}; they choose their own password when they first sign in.",
            )
        elif args.action == "remove":
            directory.remove_user(args.name)
            emit({"removed": args.name}, args, f"Removed {args.name}")
        else:
            user = directory.reset_password(args.name, _password(args, "Temporary password"))
            emit(
                _user_json(user),
                args,
                f"Reset {user.name}'s password; they choose a new one when they next sign in.",
            )
    except UserError as error:
        raise _refused(error) from error
    return 0


def cmd_password(args: argparse.Namespace) -> int:
    """Set this computer account's own password, needed only to sign in elsewhere."""
    directory = UserDirectory()
    user = directory.host_user()
    try:
        current = None
        if user.has_password:
            current = (
                sys.stdin.readline().rstrip("\n")
                if args.password_stdin
                else getpass.getpass("Current password: ")
            )
        changed = directory.set_password(
            user.name, _password(args, "New password"), current=current
        )
    except UserError as error:
        raise _refused(error) from error
    emit(_user_json(changed), args, f"Set the password for {changed.name}")
    return 0


def cmd_token(args: argparse.Namespace) -> int:
    """Make, list, or revoke device tokens: one for a script or another device."""
    directory = UserDirectory()
    try:
        user = directory.user(args.user) if args.user else directory.host_user()
    except UserError as error:
        raise _refused(error) from error
    if args.action == "create":
        if not args.name:
            raise CommandError("token create needs a name for the device, such as 'Backup script'")
        token = directory.issue_token(user, args.name)
        emit(
            {"user": user.name, "name": args.name, "token": token},
            args,
            f"{token}\nKeep it in a file only you can read (--token-file); it is not shown again.",
        )
        return 0
    if args.action == "revoke":
        if not args.name or not directory.revoke(args.name, user):
            raise CommandError("no such token for that user (see 'breadsched token list')")
        emit({"revoked": args.name}, args, f"Revoked {args.name}")
        return 0
    listed = directory.tokens(user)
    emit(
        [
            {
                "id": item.id,
                "name": item.name,
                "kind": item.kind,
                "created": item.created,
                "last_used": item.last_used,
                "expires": item.expires,
            }
            for item in listed
        ],
        args,
        table(
            [[item.id, item.name, item.kind, item.last_used or "never"] for item in listed],
            ["id", "name", "kind", "last used"],
        ),
    )
    return 0


def register(add: AddCommand) -> None:
    """Add the user subcommands."""
    users = add("users", "List, add, or remove the people who may use this computer's books", False)
    users.add_argument("action", choices=("list", "add", "remove", "reset-password"))
    users.add_argument("name", nargs="?", help="the user's sign-in name")
    users.add_argument("--display-name", help="the name shown for the user (add)")
    users.add_argument("--administrator", action="store_true", help="may manage users (add)")
    users.add_argument(
        "--password-stdin", action="store_true", help="read the password from standard input"
    )
    users.set_defaults(func=cmd_users)

    password = add(
        "password",
        "Set this computer account's password, for signing in from another device",
        False,
    )
    password.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the current (if any) and new password from standard input, one per line",
    )
    password.set_defaults(func=cmd_password)

    token = add("token", "Make, list, or revoke device tokens for scripts and devices", False)
    token.add_argument("action", choices=("create", "list", "revoke"))
    token.add_argument("name", nargs="?", help="the device's name (create) or token id (revoke)")
    token.add_argument("--user", help="whose tokens (default: this computer's account)")
    token.set_defaults(func=cmd_token)
