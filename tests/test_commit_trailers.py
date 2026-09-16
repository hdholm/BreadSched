import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_commit_trailers.py"
SPEC = importlib.util.spec_from_file_location("check_commit_trailers", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
check_commit_trailers = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = check_commit_trailers
SPEC.loader.exec_module(check_commit_trailers)


def commit(message: str, *, name: str = "A. Contributor", email: str = "a@example.com"):
    return check_commit_trailers.Commit("abc123", name, email, message)


def test_author_signoff_is_accepted():
    candidate = commit(
        "Explain the change\n\nSigned-off-by: A. Contributor <a@example.com>\n"
        "Assisted-by: Example Tool <tool@example.com>\n"
    )

    assert check_commit_trailers.signoff_problem(candidate) is None


def test_missing_signoff_is_rejected():
    problem = check_commit_trailers.signoff_problem(commit("Explain the change\n"))

    assert problem == "abc123: missing Signed-off-by: A. Contributor <a@example.com>"


def test_another_contributors_signoff_does_not_certify_the_author():
    candidate = commit("Change\n\nSigned-off-by: Reviewer <reviewer@example.com>\n")

    assert check_commit_trailers.signoff_problem(candidate) == (
        "abc123: canonical author A. Contributor <a@example.com> is not among the "
        "Signed-off-by trailers"
    )


def test_mailmap_alias_requires_the_canonical_signoff():
    candidate = commit(
        "Change\n\nSigned-off-by: A. Contributor <canonical@example.com>\n",
        email="alias@example.com",
    )
    mailmap = "A. Contributor <canonical@example.com> A. Contributor <alias@example.com>\n"

    assert check_commit_trailers.signoff_problem(candidate, mailmap) is None


def test_mailmap_alias_does_not_accept_the_noncanonical_signoff():
    candidate = commit(
        "Change\n\nSigned-off-by: A. Contributor <alias@example.com>\n",
        email="alias@example.com",
    )
    mailmap = "A. Contributor <canonical@example.com> A. Contributor <alias@example.com>\n"

    assert check_commit_trailers.signoff_problem(candidate, mailmap) == (
        "abc123: canonical author A. Contributor <canonical@example.com> is not among the "
        "Signed-off-by trailers"
    )
