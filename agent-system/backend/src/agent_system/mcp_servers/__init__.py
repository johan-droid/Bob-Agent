"""MCP *servers* shipped with Bob Agent (the client lives in services/mcp.py).

Each module here is a standalone stdio MCP server: Bob's own MCP client
(``MCP_SERVERS`` JSON) can attach to it, so every capability it exposes enters
the canonical execution path (``mcp_list``/``mcp_call`` → registry →
validation → policy → approval → handler). Nothing here talks to the database,
permissions or events directly.
"""

from __future__ import annotations

__all__: list[str] = []
