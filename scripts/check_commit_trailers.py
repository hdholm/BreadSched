#!/usr/bin/env python3
"""Require the commit author's DCO sign-off over a Git revision range."""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass

_MAILMAP_ENTRY = re.compile(
    r"^(?P<canonical_name>[^<]+?)\s*<(?P<canonical_email>[^>]+)>\s+"
    r"(?P<alias_name>[^<]+?)\s*<(?P<alias_email>[^>]+)>$"
)


@dataclass(frozen=True)
class Commit:
    sha: str
    author_name: str
    author_email: str
    message: str

    @property
    def author_identity(self) -> str:
        return f"{self.author_name} <{self.author_email}>"


def _git(*arguments: str, input_text: str | None = None) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        check=True,
        input=input_text,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def commits_in_range(base: str, head: str) -> list[Commit]:
    shas = _git("rev-list", "--reverse", f"{base}..{head}").splitlines()
    commits = []
    for sha in shas:
        raw = _git("show", "-s", "--format=%H%x00%an%x00%ae%x00%B", sha)
        commit_sha, author_name, author_email, message = raw.split("\0", 3)
        commits.append(Commit(commit_sha, author_name, author_email, message))
    return commits


def parsed_trailers(message: str) -> list[tuple[str, str]]:
    output = _git("interpret-trailers", "--parse", input_text=message)
    trailers = []
    for line in output.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            trailers.append((key.strip().lower(), value.strip()))
    return trailers


def load_mailmap(policy_ref: str) -> str:
    try:
        return _git("show", f"{policy_ref}:.mailmap")
    except subprocess.CalledProcessError:
        return ""


def canonical_author_identity(commit: Commit, mailmap: str) -> str:
    for raw_line in mailmap.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        match = _MAILMAP_ENTRY.fullmatch(line)
        if not match:
            continue
        if (
            match["alias_name"].strip() == commit.author_name
            and match["alias_email"].casefold() == commit.author_email.casefold()
        ):
            return f"{match['canonical_name'].strip()} <{match['canonical_email']}>"
    return commit.author_identity


def signoff_problem(commit: Commit, mailmap: str = "") -> str | None:
    required_identity = canonical_author_identity(commit, mailmap)
    signoffs = [value for key, value in parsed_trailers(commit.message) if key == "signed-off-by"]
    if required_identity in signoffs:
        return None
    if signoffs:
        return (
            f"{commit.sha}: canonical author {required_identity} is not among the "
            "Signed-off-by trailers"
        )
    return f"{commit.sha}: missing Signed-off-by: {required_identity}"


def main(arguments: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if arguments is None else arguments)
    if len(args) not in {2, 3}:
        print("usage: check_commit_trailers.py BASE HEAD [POLICY_REF]", file=sys.stderr)
        return 2

    commits = commits_in_range(args[0], args[1])
    mailmap = load_mailmap(args[2] if len(args) == 3 else args[0])
    problems = [problem for commit in commits if (problem := signoff_problem(commit, mailmap))]
    if not commits:
        print("No commits found in the requested range.", file=sys.stderr)
        return 2
    if problems:
        print("Developer Certificate of Origin check failed:", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        print("Amend each commit with the author's `git commit --signoff`.", file=sys.stderr)
        return 1

    print(f"DCO sign-off verified for {len(commits)} commit(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
