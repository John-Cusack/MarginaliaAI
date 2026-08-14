"""MCP server for the MarginaliaAI research engine.

Exposes all core tools and plugin-contributed tools over stdio transport
using the ``mcp`` library.  The composition root (``Container``) wires
services, repositories, and adapters together; this module simply
bridges that into the MCP protocol.

Usage as a script::

    python -m research_engine.mcp.server

Or programmatically::

    from research_engine.mcp.server import build_mcp_server
    server = build_mcp_server(container)
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from mcp.server import Server
from mcp.server.stdio import stdio_server

from research_engine.config.settings import load_settings
from research_engine.mcp.dispatch import register_core_tools

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Server construction
# ---------------------------------------------------------------------------

def build_mcp_server(container: Any) -> Server:
    """Create an MCP ``Server`` and register all tools.

    Parameters
    ----------
    container:
        A fully-wired :class:`Container` from ``composition.build_container``.

    Returns
    -------
    Server
        The configured MCP server, ready to be run with a transport.
    """
    server = Server("research-engine")

    # Register all core + plugin tools (plugin tools are wired alongside core).
    register_core_tools(server, container)

    logger.info("mcp_server_built", server_name="research-engine")
    return server


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _run() -> None:
    """Build the container, wire the server, start stdio transport."""
    from research_engine.composition import build_container

    settings = load_settings()
    container = await build_container(settings)

    server = build_mcp_server(container)

    logger.info("mcp_server_starting", transport="stdio")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    """Synchronous entry point for ``python -m research_engine.mcp.server``."""
    asyncio.run(_run())


if __name__ == "__main__":
    main()
