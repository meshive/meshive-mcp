"""meshive-mcp — remote MCP server for the Meshive GPU Cloud.

A thin tool layer on top of the `meshive` SDK (PyPI). It holds no business logic; it
(1) passes the request's Bearer API key to the SDK client,
(2) trims responses to a size models read well, and
(3) translates exceptions into instructions for the model (code/message/next_step).
"""
from ._version import __version__

__all__ = ["__version__"]
