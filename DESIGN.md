# BreadSched Household Financial Manager design

This document describes BreadSched as it is implemented: its boundaries, who owns
each concern, the invariants the code keeps, and the reasons for its main choices.
It is not a history and not a list of future work: completed changes are in
[`CHANGELOG.md`](CHANGELOG.md), user-facing rules and steps are in the
[User Guide](src/breadsched/USER_GUIDE.md), and unfinished work is in
[`ROADMAP.md`](ROADMAP.md). Detailed acceptance evidence lives in the tests named
here, in `docs/quality/`, and in the versioned notes under `docs/releases/`.

This file holds the product boundary; each other part of the design is its own
document under `docs/design/`, listed below.

Contents:

1. [Product boundary](#product-boundary)
2. [Architecture and ownership](docs/design/architecture.md)
3. [Domain model and exact money](docs/design/domain-model.md)
4. [Storage, transactions, and recovery](docs/design/storage.md)
5. [Dated planning and scenarios](docs/design/planning.md)
6. [Valuation and reporting](docs/design/valuation-and-reporting.md)
7. [Household workflows](docs/design/household-workflows.md)
8. [Interoperability and source ownership](docs/design/interoperability.md)
9. [User interfaces](docs/design/user-interfaces.md)
10. [Security](docs/design/security.md)
11. [Validation, packaging, and releases](docs/design/validation-and-releases.md)

Decision records under `docs/design/decisions/` keep the measurements behind
choices that were tested before being made:

- [0001: Keep transactions as JSON with a derived split index](docs/design/decisions/0001-transaction-storage.md)
- [0002: Third-party packages outside the core, and where online quotes come from](docs/design/decisions/0002-dependencies-and-online-quotes.md)

## Product boundary

BreadSched is a household-finance application. Its long-term direction is to cover
the household ledger, planning, scenario, projection, reconciliation, investment,
and related workflows needed for household finance without attempting
to introduce business-accounting breadth.

GnuCash compatibility is a core architectural constraint rather than a one-time
migration feature. Imported data
must retain enough identity and semantics for users to continue maintaining an
existing GnuCash book while using BreadSched-specific planning and projection.

GTK4 is the reference interface and Linux is the primary native desktop target.
The web interface is required to maintain functional parity. Other platforms may
ultimately use GTK packaging or a web-based presentation, but those are delivery
choices over the same application services and financial engines.
