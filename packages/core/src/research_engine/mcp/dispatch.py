"""Tool registration logic for core and plugin tools."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
from typing import TYPE_CHECKING, Any

import structlog
from mcp import types

from research_engine.domain.errors import PermissionDenied
from research_engine.mcp.catalog import ToolCatalog
from research_engine.mcp.errors import envelope, failed
from research_engine.mcp.tools import (
    citations,
    corpus_stats,
    events,
    extract,
    find_lemma,
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
    work_block_upsert,
    work_citations,
    work_cite,
    work_cite_entry,
    work_create,
    work_freeze,
    work_get,
    work_link,
    work_render,
    work_trace,
    work_validate,
    work_verify,
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
    # Below the passage: a lemma to the verses that hold it.
    find_lemma,
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
    work_verify,
    work_citations,
    work_cite_entry,
    work_render,
    work_create,
    work_get,
    work_block_upsert,
    work_cite,
    work_link,
    work_validate,
    work_trace,
    work_freeze,
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
    conforms. Returns an error message string if validation fails, None if
    valid.

    Validation contract (WI-6, Option A — shallow by decision, not by
    accident): the schema is a contract with the agent, not a guarantee to
    the handler. Nested objects are NOT descended into, array ``items`` are
    NOT checked, ``format`` is NOT enforced, and ``default`` is NOT applied.
    The richest schemas on this surface are exactly the nested ones
    (``find_passages.filters`` and every injected extension schema), so the
    tools that most look validated are the least validated. Handlers must
    treat every nested value as arbitrary JSON and re-check what they depend
    on (see e.g. ``work_cite_entry._checked_window``). Full ``jsonschema``
    validation stays out until packs come from outside this machine — at that
    point an unvalidated extension schema is untrusted input reaching a
    SQL-building filter, and Option B stops being optional.
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


def _make_core_handler(
    container: Any, handler_fn: Any, input_schema: dict[str, Any], tool_name: str
) -> Any:
    """One core tool's wire handler: validate, call, envelop failures."""

    async def _handle(arguments: dict[str, Any]) -> list[dict[str, Any]]:
        error = _validate_input(input_schema, arguments)
        if error:
            return [{"type": "text", "text": json.dumps(
                envelope("validation_error", error)
            )}]
        try:
            result = await handler_fn(container, **arguments)
            return [{"type": "text", "text": json.dumps(result, default=str)}]
        except PermissionDenied as e:
            logger.warning("plugin_permission_denied", tool=tool_name,
                           plugin=e.plugin, permission=e.permission)
            return [{"type": "text", "text": json.dumps(
                envelope("permission_denied", str(e),
                         {"plugin": e.plugin, "permission": e.permission})
            )}]
        except Exception as e:
            logger.error("tool_error", tool=tool_name, error=str(e))
            return [{"type": "text", "text": json.dumps(failed(tool_name, e))}]

    return _handle


def _find_passages_entry(
    container: Any, registry: Any
) -> tuple[types.Tool, Any]:
    """The ``find_passages`` catalogue entry with its current dynamic schema."""
    if registry is not None and hasattr(find_passages, "build_dynamic_schema"):
        input_schema = find_passages.build_dynamic_schema(registry)
    else:
        input_schema = find_passages.TOOL_SCHEMA
    tool_def = types.Tool(
        name=find_passages.TOOL_NAME,
        description=find_passages.TOOL_DESCRIPTION,
        inputSchema=input_schema,
    )
    return tool_def, _make_core_handler(
        container, find_passages.handler, input_schema, find_passages.TOOL_NAME
    )


def _build_core_entries(
    container: Any, registry: Any
) -> tuple[list[types.Tool], dict[str, Any]]:
    """Snapshot the core slice. ``find_passages`` resolves its filter-extension
    schema from the registry; everything else lists its static schema."""
    defs: list[types.Tool] = []
    handlers: dict[str, Any] = {}
    for module in CORE_TOOL_MODULES:
        if module.TOOL_NAME == "find_passages":
            tool_def, handler = _find_passages_entry(container, registry)
        else:
            tool_def = types.Tool(
                name=module.TOOL_NAME,
                description=module.TOOL_DESCRIPTION,
                inputSchema=module.TOOL_SCHEMA,
            )
            handler = _make_core_handler(
                container, module.handler, module.TOOL_SCHEMA, module.TOOL_NAME
            )
        defs.append(tool_def)
        handlers[module.TOOL_NAME] = handler
    logger.info("core_tools_registered", count=len(CORE_TOOL_MODULES))
    return defs, handlers


def _build_pack_entries(
    registry: Any, plugin_loader: Any
) -> tuple[list[types.Tool], dict[str, Any]]:
    """Snapshot the pack slice from the registry's ToolSpecs (WI-4).

    Scoped clients are resolved fresh on every build, so a rebuild after an
    install never serves a previous pack set's clients.
    """
    defs: list[types.Tool] = []
    handlers: dict[str, Any] = {}
    plugin_tools = registry.get_mcp_tools()
    tool_specs = registry.get_mcp_tool_specs()

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
        spec = tool_specs[tool_id]

        defs.append(types.Tool(
            name=tool_id,
            description=spec.description,
            inputSchema=spec.input_schema,
        ))

        async def _handle_pack(
            arguments: dict[str, Any],
            *,
            _fn: Any = plugin_handler_fn,
            _schema: dict[str, Any] = spec.input_schema,
            _name: str = tool_id,
        ) -> list[dict[str, Any]]:
            error = _validate_input(_schema, arguments)
            if error:
                return [{"type": "text", "text": json.dumps(
                    envelope("validation_error", error)
                )}]
            try:
                clients = _get_plugin_clients(_name)
                result = await _fn(**_select_clients(_fn, clients), **arguments)
                return [{"type": "text", "text": json.dumps(result, default=str)}]
            except PermissionDenied as e:
                logger.warning("plugin_permission_denied", tool=_name,
                               plugin=e.plugin, permission=e.permission)
                return [{"type": "text", "text": json.dumps(
                    envelope("permission_denied", str(e),
                             {"plugin": e.plugin, "permission": e.permission})
                )}]
            except Exception as e:
                logger.error("plugin_tool_error", tool=_name, error=str(e))
                return [{"type": "text", "text": json.dumps(failed(_name, e))}]

        handlers[tool_id] = _handle_pack

    if plugin_tools:
        logger.info("plugin_tools_registered", count=len(plugin_tools))
    return defs, handlers


def get_tool_catalog(container: Any) -> ToolCatalog:
    """The container's catalogue, creating and hanging it there if needed."""
    catalog = getattr(container, "tool_catalog", None)
    if catalog is None:
        catalog = ToolCatalog()
        container.tool_catalog = catalog
    return catalog


def refresh_pack_tools(container: Any) -> ToolCatalog:
    """Rebuild the pack slice of the live catalogue after an install/unload.

    Replaces the whole pack set atomically (an unload is as expressible as a
    load), rebuilds the ``find_passages`` core entry — a pack contributing a
    filter extension changes that core tool's signature — and emits the change
    event the server subscribes to.
    """
    catalog = get_tool_catalog(container)
    registry = getattr(container, "registry", None) or getattr(container, "plugin_registry", None)
    plugin_loader = getattr(container, "plugin_loader", None)
    if registry is None:
        logger.warning("pack_refresh_without_registry")
        return catalog
    pack_defs, pack_handlers = _build_pack_entries(registry, plugin_loader)
    catalog.replace_packs(pack_defs, pack_handlers)
    tool_def, handler = _find_passages_entry(container, registry)
    catalog.update_core_tool(tool_def, handler)
    catalog.notify_changed()
    logger.info("pack_tools_refreshed", pack_count=len(pack_defs))
    return catalog


def _register_all(server: Server, container: Any) -> None:
    """Register list_tools and call_tool handlers for all core + plugin tools.

    The low-level MCP Server uses two decorator-based handlers:
    - ``@server.list_tools()`` returns the full tool catalogue
    - ``@server.call_tool()`` dispatches a call by tool name

    Both closures read through the container's ``ToolCatalog`` rather than
    capturing a snapshot, so ``refresh_pack_tools`` after an install is
    visible on the wire without a restart.
    """
    registry = getattr(container, "registry", None) or getattr(container, "plugin_registry", None)
    plugin_loader = getattr(container, "plugin_loader", None)

    catalog = get_tool_catalog(container)
    catalog.set_core(*_build_core_entries(container, registry))
    if registry is not None:
        catalog.replace_packs(*_build_pack_entries(registry, plugin_loader))

    # Live sessions for change notification. The low-level Server keeps no
    # session registry of its own, so track them from the request context
    # inside the two closures. The loader emits through the catalogue (it
    # must not import the server); this subscription pushes
    # notifications/tools/list_changed to sessions that are still alive. A
    # client that never calls back still sees the new catalogue on its next
    # list_tools call, because the closures read the catalogue live.
    live_sessions: set[Any] = set()

    def _track_session() -> None:
        # Outside a request, or a server double without request_context
        # (tests, dispatch_tool): nothing to push to.
        with contextlib.suppress(LookupError, AttributeError):
            live_sessions.add(server.request_context.session)

    def _push_tool_list_changed() -> None:
        tracked = list(live_sessions)
        logger.info("tools_list_changed", version=catalog.changed_version,
                    sessions=len(tracked))
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (e.g. refresh from a test): next list call sees it
        for session in tracked:
            task = loop.create_task(session.send_tool_list_changed())

            def _prune(t: Any, s: Any = session) -> None:
                if t.exception() is not None:
                    live_sessions.discard(s)

            task.add_done_callback(_prune)

    catalog.subscribe(_push_tool_list_changed)

    # -- Register with the MCP server --

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        _track_session()
        return catalog.defs

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        _track_session()
        handler = catalog.handler(name)
        if handler is None:
            return [{"type": "text", "text": json.dumps(
                envelope("unknown_tool", f"Unknown tool: {name}")
            )}]
        return await handler(arguments)
