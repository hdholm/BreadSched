"""Locally hosted web interface.

Importing this package does not start anything. Use ``breadsched serve``, or
``breadsched.web.transport.serve`` for programmatic control. The former
``breadsched.web.server.serve`` path remains a compatibility re-export.
"""

__all__ = ["resources", "server", "transport"]
