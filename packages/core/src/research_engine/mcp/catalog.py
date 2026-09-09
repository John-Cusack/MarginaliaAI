"""Live tool catalogue: installing a pack stops needing a restart.

Scope guard: this is for *install* (and unload), not for hot code reload.
Reloading a pack whose Python changed on disk would mean ``importlib.reload``,
stale references and half-swapped modules — a different and much worse
problem. Installing or removing a pack's *registration* is expressible here;
upgrading a pack's code still requires a restart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp import types


class ToolCatalog:
    """Mutable tool registry the MCP closures read through, never capture.

    ``_register_all`` used to close over ``tool_defs`` / ``handler_map``
    locals, so a pack loaded after startup was reachable via ``dispatch_tool``
    and invisible on the wire. The ``list_tools`` / ``call_tool`` closures now
    read through this object, and installing a pack is a ``replace_packs``
    away — no restart.
    """

    def __init__(self) -> None:
        self._core: list[types.Tool] = []
        self._core_handlers: dict[str, Any] = {}
        self._packs: list[types.Tool] = []
        self._pack_handlers: dict[str, Any] = {}
        self._listeners: list[Any] = []
        self.changed_version = 0

    def set_core(
        self, defs: list[types.Tool], handlers: dict[str, Any]
    ) -> None:
        """Fix the core slice. Called once at startup; packs never touch it."""
        self._core = list(defs)
        self._core_handlers = dict(handlers)

    def replace_packs(
        self, defs: list[types.Tool], handlers: dict[str, Any]
    ) -> None:
        """Swap the whole pack slice atomically; core entries are untouched.

        Takes the whole pack set — not one entry — so an unload is as
        expressible as a load, and a half-applied load cannot leave a handler
        whose def was never listed.
        """
        self._packs = list(defs)
        self._pack_handlers = dict(handlers)

    def update_core_tool(self, tool_def: types.Tool, handler: Any) -> None:
        """Replace one core entry by name, leaving the rest of the slice alone.

        Exists for exactly one caller: ``find_passages``. Its schema is
        resolved once from the registry's filter extensions, so a pack
        contributing an extension changes a *core* tool's signature — the one
        place the core/pack split genuinely leaks.
        """
        self._core = [
            tool_def if t.name == tool_def.name else t for t in self._core
        ]
        self._core_handlers[tool_def.name] = handler

    @property
    def defs(self) -> list[types.Tool]:
        return [*self._core, *self._packs]

    def handler(self, name: str) -> Any | None:
        if name in self._pack_handlers:
            return self._pack_handlers[name]
        return self._core_handlers.get(name)

    def subscribe(self, listener: Any) -> None:
        """Register for change events. The loader must not import the server,
        so notification flows the other way: the server subscribes here and
        the loader (via ``refresh_pack_tools``) emits.

        A listener is a zero-arg callable. Transports that hold live sessions
        use it to push ``notifications/tools/list_changed`` (see
        ``ServerSession.send_tool_list_changed``); the stdio transport serves
        each ``list_tools`` call from ``defs``, so it sees the new catalogue
        on the next call either way.
        """
        self._listeners.append(listener)

    def notify_changed(self) -> None:
        self.changed_version += 1
        for listener in list(self._listeners):
            listener()
