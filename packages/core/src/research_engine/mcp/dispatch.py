"""Tool registration logic for core and plugin tools."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import structlog
from mcp import types

from research_engine.mcp.tools import (
    corpus_stats,
    events,
    extract,
    find_mentions,
    find_passages,
    get_document,
    get_entity,
    get_passage_context,
    list_extraction_schemas,
    list_filters,
    provenance_of,
    query_extractions,
    resolve_entity,
    search_sources,
    similar_to,
    timeline_compare,
    upsert_edge,
    upsert_entity,
    upsert_event,
)

if TYPE_CHECKING:
    from mcp.server.lowlevel.server import Server

logger = structlog.get_logger()

# All core tool modules, in the order they should appear in listings.
CORE_TOOL_MODULES = [
    find_passages,
    get_document,
    get_passage_context,
    similar_to,
    resolve_entity,
    get_entity,
    find_mentions,
    events,
    timeline_compare,
    extract,
    list_extraction_schemas,
    query_extractions,
    provenance_of,
    corpus_stats,
    upsert_entity,
    upsert_event,
    upsert_edge,
    list_filters,
    search_sources,
]


def _validate_input(schema: dict[str, Any], arguments: dict[str, Any]) -> str | None:
    """Basic JSON Schema validation for required fields and types.

    Returns an error message string if validation fails, None if valid.
    """
    required = schema.get("required", [])
    for field in required:
        if field not in arguments:
            return f"Missing required field: '{field}'"
    return None


def register_core_tools(server: Server, container: Any) -> None:
    """Register all core tools on the MCP server instance."""
    _register_all(server, container)


def register_plugin_tools(server: Server, container: Any) -> None:
    """No-op — plugin tools are registered in _register_all alongside core tools."""
    pass


def _register_all(server: Server, container: Any) -> None:
    """Register list_tools and call_tool handlers for all core + plugin tools.

    The low-level MCP Server uses two decorator-based handlers:
    - ``@server.list_tools()`` returns the full tool catalogue
    - ``@server.call_tool()`` dispatches a call by tool name
    """
    # -- Build tool catalogue --
    # TODO(hot-reload): tool_defs and handler_map are captured in the list_tools/call_tool
    # closures below. To support live plugin install without server restart, lift these
    # into a ToolCatalog object on the container and emit notifications/tools/list_changed
    # after PluginLoader.load_plugin(...) mutates it. See plan: restart-on-install.
    tool_defs: list[types.Tool] = []
    handler_map: dict[str, Any] = {}  # tool_name -> async handler(arguments)

    # Resolve dynamic schema for find_passages if extensions are loaded
    registry = getattr(container, "registry", None) or getattr(container, "plugin_registry", None)

    # Core tools
    for module in CORE_TOOL_MODULES:
        tool_name: str = module.TOOL_NAME
        description: str = module.TOOL_DESCRIPTION
        # Use dynamic schema for find_passages when registry is available
        if tool_name == "find_passages" and registry and hasattr(module, "build_dynamic_schema"):
            input_schema: dict[str, Any] = module.build_dynamic_schema(registry)
        else:
            input_schema = module.TOOL_SCHEMA
        handler_fn = module.handler

        tool_defs.append(types.Tool(
            name=tool_name,
            description=description,
            inputSchema=input_schema,
        ))

        async def _make_handler(
            arguments: dict[str, Any],
            *,
            _fn: Any = handler_fn,
            _schema: dict[str, Any] = input_schema,
            _name: str = tool_name,
        ) -> list[dict[str, Any]]:
            error = _validate_input(_schema, arguments)
            if error:
                return [{"type": "text", "text": json.dumps(
                    {"error": {"code": "validation_error", "message": error, "details": None}}
                )}]
            try:
                result = await _fn(container, **arguments)
                return [{"type": "text", "text": json.dumps(result, default=str)}]
            except Exception as e:
                logger.error("tool_error", tool=_name, error=str(e))
                return [{"type": "text", "text": json.dumps(
                    {"error": {"code": f"{_name}_failed", "message": str(e), "details": None}}
                )}]

        handler_map[tool_name] = _make_handler

    logger.info("core_tools_registered", count=len(CORE_TOOL_MODULES))

    # Plugin tools — inject scoped clients instead of raw container
    plugin_loader = getattr(container, "plugin_loader", None)
    if registry:
        plugin_tools = registry.get_mcp_tools()

        # Pre-build scoped clients for each plugin that contributes tools
        _plugin_clients_cache: dict[str, dict[str, Any]] = {}

        def _get_plugin_clients(tool_id: str) -> dict[str, Any]:
            plugin_name = registry.get_tool_plugin(tool_id)
            if not plugin_name:
                return {}
            if plugin_name not in _plugin_clients_cache:
                if plugin_loader:
                    _plugin_clients_cache[plugin_name] = plugin_loader.build_plugin_clients(plugin_name)
                else:
                    _plugin_clients_cache[plugin_name] = {}
            return _plugin_clients_cache[plugin_name]

        for tool_id, plugin_handler_fn in plugin_tools.items():
            p_description = getattr(plugin_handler_fn, "_tool_description", tool_id)
            p_input_schema = getattr(plugin_handler_fn, "_tool_input_schema", {})

            tool_defs.append(types.Tool(
                name=tool_id,
                description=p_description,
                inputSchema=p_input_schema,
            ))

            async def _make_plugin_handler(
                arguments: dict[str, Any],
                *,
                _fn: Any = plugin_handler_fn,
                _schema: dict[str, Any] = p_input_schema,
                _name: str = tool_id,
            ) -> list[dict[str, Any]]:
                error = _validate_input(_schema, arguments)
                if error:
                    return [{"type": "text", "text": json.dumps(
                        {"error": {"code": "validation_error", "message": error, "details": None}}
                    )}]
                try:
                    clients = _get_plugin_clients(_name)
                    result = await _fn(**clients, **arguments)
                    return [{"type": "text", "text": json.dumps(result, default=str)}]
                except Exception as e:
                    logger.error("plugin_tool_error", tool=_name, error=str(e))
                    return [{"type": "text", "text": json.dumps(
                        {"error": {"code": f"{_name}_failed", "message": str(e), "details": None}}
                    )}]

            handler_map[tool_id] = _make_plugin_handler

        if plugin_tools:
            logger.info("plugin_tools_registered", count=len(plugin_tools))

    # -- Register with the MCP server --

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        return tool_defs

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        handler = handler_map.get(name)
        if handler is None:
            return [{"type": "text", "text": json.dumps(
                {"error": {"code": "unknown_tool", "message": f"Unknown tool: {name}", "details": None}}
            )}]
        return await handler(arguments)
