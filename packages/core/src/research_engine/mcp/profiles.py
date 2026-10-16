"""Which tools an MCP server exposes, when it should not expose all of them.

A corpus served to other agents is a reference, not a workbench. Every core
tool used to be listed and callable on every server, including the ones that
write (`work_publish`, `upsert_*`, `claim_upsert`), spend LLM money
(`extract`), or call any other tool without validating its input
(`ingest_execute`). A profile names the set a deployment means to offer; the
catalogue filters both `list_tools` and `call_tool` through it, and
`dispatch_tool` refuses anything outside it, so a trampoline cannot reach past
the list either.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from research_engine.config.settings import Settings

logger = structlog.get_logger()

#: Read-only search, reading, locating and quote verification. Nothing here
#: writes to the corpus, ingests, or calls an LLM.
REFERENCE_TOOLS: frozenset[str] = frozenset({
    "find_passages",
    "get_document",
    "get_passage_context",
    "similar_to",
    "get_document_outline",
    "read_node",
    "locate_passage",
    "list_available_filters",
    "corpus_stats",
    "verify_quote",
    "citations",
})

PROFILES: dict[str, frozenset[str]] = {"reference": REFERENCE_TOOLS}


def resolve_tool_allowlist(settings: Settings | Any) -> frozenset[str] | None:
    """The tool names this server may expose, or None for every tool.

    `RE_MCP_TOOLS` alone is the allowlist. With a profile it can only narrow
    the profile: an allowlist that silently widened a read-only profile would
    make the profile name a lie.
    """
    profile = getattr(settings, "mcp_profile", None)
    explicit = getattr(settings, "resolved_mcp_tools", None)
    if profile is None and explicit is None:
        return None
    allowed = PROFILES[profile] if profile is not None else None
    if explicit is not None:
        if allowed is not None:
            widened = explicit - allowed
            if widened:
                logger.warning(
                    "mcp_tools_outside_profile_ignored",
                    profile=profile,
                    tools=sorted(widened),
                )
            allowed = allowed & explicit
        else:
            allowed = explicit
    return allowed
