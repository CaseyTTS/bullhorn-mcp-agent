"""MCP tool implementations.

Importing this package registers every ``@mcp.tool()`` as a side effect of
importing each submodule - nothing here should be imported for its names.
"""

from . import system, jobs, candidates, placements, generic  # noqa: F401
from . import setup  # noqa: F401
from . import notes  # noqa: F401
