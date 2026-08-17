"""Runtime entrypoint for the research engine."""

from __future__ import annotations

import asyncio
import logging

import structlog

from research_engine.composition import build_container
from research_engine.config import Settings, load_settings


def configure_logging(settings: Settings) -> None:
    """Configure structured logging."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer()
            if settings.log_format == "pretty"
            else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level.upper(), logging.INFO)
        ),
    )


async def run_mcp_server(settings: Settings | None = None) -> None:
    """Start the MCP server."""
    settings = settings or load_settings()
    configure_logging(settings)

    from research_engine.mcp.server import build_mcp_server

    container = await build_container(settings)
    try:
        server = build_mcp_server(container)
        # MCP stdio transport
        from mcp.server.stdio import stdio_server
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        await container.close()


def main() -> None:
    """CLI-invokable entrypoint."""
    asyncio.run(run_mcp_server())


if __name__ == "__main__":
    main()
