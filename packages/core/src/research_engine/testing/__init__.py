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

from research_engine.testing.corpus import Corpus, new_id
from research_engine.testing.database import (
    DEFAULT_TEST_DB_NAME,
    CorpusFootprint,
    ensure_test_database,
    resolve_test_db_url,
)

__all__ = [
    "DEFAULT_TEST_DB_NAME",
    "Corpus",
    "CorpusFootprint",
    "ensure_test_database",
    "new_id",
    "resolve_test_db_url",
]
