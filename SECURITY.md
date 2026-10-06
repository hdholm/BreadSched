# Security policy

BreadSched keeps household financial records on your own computer. Please report
security problems privately so they can be fixed before they are described in
public.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: on the repository's **Security** tab,
choose **Report a vulnerability**. Do not open a public issue, discussion, or pull
request for a suspected vulnerability.

Describe the problem, the BreadSched version (`breadsched --version`), the
interface (desktop, browser, or command line), and the steps that show it.

**Never attach an unsanitized financial book**, GnuCash file, bank statement, or
anything else containing real account numbers, balances, or transactions. Reduce
the problem to a synthetic example (for instance, `breadsched sample` creates a
synthetic book) or describe the structure that triggers it. The same rule applies
to every issue and pull request; see `CONTRIBUTING.md`.

## Supported versions

BreadSched is alpha software. Fixes are made on `main` and released in the next
alpha; earlier alphas are not patched.

## Scope

Examples of what to report:

- the browser interface accepting a request from another site or another local
  user, or serving anything other than its own page, stylesheet, and scripts;
- an imported file (GnuCash, OFX, QIF, CSV) that can write outside the book, run
  code, or exhaust memory or disk;
- a book, backup, or attachment written with broader permissions than the user's
  own, or financial data written to logs.

Wrong financial results are serious bugs but not usually security issues; report
them as an ordinary issue with a synthetic example.
