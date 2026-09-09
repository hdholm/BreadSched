"""GTK4 user interface.

Importing this package does not import GTK: the ``gi`` dependency is confined to
the modules below it, so the core and the CLI run on a machine with no GUI stack
installed at all.  Use ``cashperspective.gui.app.main`` to start the interface.
"""

__all__ = ["app", "viewmanager"]
