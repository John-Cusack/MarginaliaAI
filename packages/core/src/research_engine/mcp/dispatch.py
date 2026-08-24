"""Tool registration logic for core and plugin tools."""

from __future__ import annotations

import inspect
import json
from typing import TYPE_CHECKING, Any

import structlog
from mcp import types

from research_engine.mcp.tools import (
    citations,
    corpus_stats,
    events,
    extract,
    find_mentions,
    find_passages,
    get_document,
    get_document_outline,
    get_entity,
    get_passage_context,
    ingest_execute,
    list_extraction_schemas,
    list_filters,
    llm_usage,
    locate_passage,
    provenance_of,
    query_extractions,
    read_node,
    resolve_entity,
    search_sources,
    similar_to,
    timeline_compare,
    upsert_edge,
    upsert_entity,
    upsert_event,
    verify_quote,
)

if TYPE_CHECKING:
    from mcp.server.lowlevel.server import Server

logger = structlog.get_logger()


def _select_clients(handler: Any, clients: dict[str, Any]) -> dict[str, Any]:
    """Pass only the scoped clients a plugin handler actually declares.

    ``build_plugin_clients`` returns the full set of scoped clients (corpus,
    entity, event, extraction, llm, http, ingestion, edge). A handler that
    declares only a subset — e.g. ``async def h(corpus, *, query)`` — would get
    ``TypeError: unexpected keyword argument 'entity'`` if every client were
    splatted in. Filter to the handler's declared parameters, but pass the full
    set when the handler accepts ``**kwargs`` (the documented SDK pattern).
    """
    if not clients:
        return {}
    try:
        params = inspect.signature(handler).parameters
    except (TypeError, ValueError):
        return clients
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return clients
    return {k: v for k, v in clients.items() if k in params}


# All core tool modules, in the order they should appear in listings.
CORE_TOOL_MODULES = [
    find_passages,
    get_document,
    get_passage_context,
    similar_to,
    # Structure: the map, the read, and the hit-to-place lookup.
    get_document_outline,
    read_node,
    locate_passage,
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
    llm_usage,
    citations,
    upsert_entity,
    upsert_event,
    verify_quote,
    upsert_edge,
    list_filters,
    search_sources,
    ingest_execute,
]


_CORE_TOOL_MAP = {module.TOOL_NAME: module for module in CORE_TOOL_MODULES}


async def dispatch_tool(
    container: Any, tool_id: str, arguments: dict[str, Any] | None = None
) -> Any:
    """Invoke a registered core or plugin tool by id, returning its raw result.

    Shares the same handler + scoped-client machinery used by the MCP transport,
    so orchestration tools (e.g. ``ingest_execute``) can call other tools
    without going through the wire protocol. Raises ``ValueError`` for unknown
    tool ids.
    """
    arguments = arguments or {}

    core = _CORE_TOOL_MAP.get(tool_id)
    if core is not None:
        return await core.handler(container, **arguments)

    registry = getattr(container, "registry", None) or getattr(container, "plugin_registry", None)
    plugin_loader = getattr(container, "plugin_loader", None)
    if registry is not None:
        plugin_tools = registry.get_mcp_tools()
        # Plugin tools register under dotted ids (e.g. "acad.discover_by_doi"),
        # but agent-facing IngestActions use the underscored MCP name
        # ("acad_discover_by_doi"). Match either form.
        matched_id = None
        if tool_id in plugin_tools:
            matched_id = tool_id
        else:
            for registered_id in plugin_tools:
                if registered_id.replace(".", "_") == tool_id:
                    matched_id = registered_id
                    break
        if matched_id is not None:
            clients: dict[str, Any] = {}
            plugin_name = registry.get_tool_plugin(matched_id)
            if plugin_name and plugin_loader:
                clients = plugin_loader.build_plugin_clients(plugin_name)
            handler = plugin_tools[matched_id]
            return await handler(**_select_clients(handler, clients), **arguments)

    raise ValueError(f"Unknown tool: {tool_id}")


# JSON Schema primitive type -> accepted Python type(s). ``integer`` excludes
# bool (a subclass of int); ``number`` accepts both int and float.
_JSON_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def _validate_input(schema: dict[str, Any], arguments: dict[str, Any]) -> str | None:
    """Lightweight JSON Schema validation: required fields, types, and enums.

    Checks that every ``required`` field is present, and for each provided field
    that has a declared ``type``/``enum`` in ``properties``, that the value
    conforms. Not a full JSON Schema implementation (no nested/array-item or
    format validation). Returns an error message string if validation fails,
    None if valid.
    """
    required = schema.get("required", [])
    for field in required:
        if field not in arguments:
            return f"Missing required field: '{field}'"

    properties = schema.get("properties", {})
    for field, value in arguments.items():
        spec = properties.get(field)
        if not isinstance(spec, dict):
            continue

        expected = spec.get("type")
        accepted = _JSON_TYPE_MAP.get(expected) if expected else None
        if accepted is not None:
            # bool is a subclass of int — reject it for integer/number.
            if expected in ("integer", "number") and isinstance(value, bool):
                return f"Field '{field}' must be of type {expected}"
            if not isinstance(value, accepted):
                return f"Field '{field}' must be of type {expected}"

        enum = spec.get("enum")
        if enum is not None and value not in enum:
            return f"Field '{field}' must be one of {enum}"

    return None


def register_core_tools(server: Server, container: Any) -> None:
    """Register all core and plugin tools on the MCP server instance."""
    _register_all(server, container)


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
                    result = await _fn(**_select_clients(_fn, clients), **arguments)
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
