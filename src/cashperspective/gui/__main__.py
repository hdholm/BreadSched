"""Allow ``python -m cashperspective.gui`` as an alternative to the installed script."""

from .launcher import main

if __name__ == "__main__":
    raise SystemExit(main())
