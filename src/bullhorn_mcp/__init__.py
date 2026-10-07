"""Bullhorn CRM MCP Server - Query jobs and candidates via natural language."""

__version__ = "0.1.0"

# Importing `server` here guarantees that importing `bullhorn_mcp` - the
# package itself, or any submodule such as `bullhorn_mcp.tools.jobs` -
# always fully initializes `server.py` (and transitively, the whole `tools`
# package) first, before any other code can observe a partially-initialized
# module. `server.py` and `tools/*.py` have a deliberate circular reference
# (each tools module does `from .. import server` and calls back into it at
# call time), and this import is what makes that reference safe in every
# import order, not just the "lucky" one.
from . import server  # noqa: F401
