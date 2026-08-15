"""Test helpers for the engine and for packs that integrate with it.

Published as part of the distribution, not kept in the core test tree, because
the isolation contract is something every pack needs and none should reinvent.
A pack that reimplements it gets it subtly wrong — the YourCloudLibrary suite
ingested real books into the researcher's live corpus and embedded them with an
8-dimensional stub, leaving 2,095 passages invisible to semantic search.

Two rules this package exists to enforce:

1. **Never truncate.** The dev database is where a real corpus lives. A test
   suite must remove exactly the rows it created and nothing else.
2. **Never default to the real corpus.** ``resolve_test_db_url`` steers packs at
   a dedicated database unless they opt out explicitly.
"""

from research_engine.testing.chunker_contract import (
    ABSOLUTE_MAX_TOKENS,
    CONTRACT_TEXTS,
    assert_chunker_contract,
    call_chunker,
)

__all__ = [
    "ABSOLUTE_MAX_TOKENS",
    "CONTRACT_TEXTS",
    "DEFAULT_TEST_DB_NAME",
    "Corpus",
    "CorpusFootprint",
    "assert_chunker_contract",
    "call_chunker",
    "ensure_test_database",
    "new_id",
    "resolve_test_db_url",
]

#: The corpus and database helpers are resolved on access, not on import.
#: They need a Postgres driver and a uuid7 implementation; the chunker contract
#: needs neither. A pack that only wants to hold its chunker to the contract was
#: otherwise forced to install a database stack to import this package — a
#: barrier in front of exactly the packs it exists to serve.
_LAZY = {
    "Corpus": ("research_engine.testing.corpus", "Corpus"),
    "new_id": ("research_engine.testing.corpus", "new_id"),
    "CorpusFootprint": ("research_engine.testing.database", "CorpusFootprint"),
    "DEFAULT_TEST_DB_NAME": ("research_engine.testing.database", "DEFAULT_TEST_DB_NAME"),
    "ensure_test_database": ("research_engine.testing.database", "ensure_test_database"),
    "resolve_test_db_url": ("research_engine.testing.database", "resolve_test_db_url"),
}


def __getattr__(name: str) -> object:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(target[0]), target[1])
