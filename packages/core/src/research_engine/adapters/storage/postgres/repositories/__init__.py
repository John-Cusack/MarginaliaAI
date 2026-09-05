"""Postgres repository implementations."""

from research_engine.adapters.storage.postgres.repositories.authored import (
    PGWorkRepo,
    PGWorkRevisionRepo,
)
from research_engine.adapters.storage.postgres.repositories.citations import PGCitationRepo
from research_engine.adapters.storage.postgres.repositories.document_texts import (
    PGDocumentTextRepo,
)
from research_engine.adapters.storage.postgres.repositories.documents import PGDocumentRepo
from research_engine.adapters.storage.postgres.repositories.edges import PGEdgeRepo
from research_engine.adapters.storage.postgres.repositories.editions import PGEditionRepo
from research_engine.adapters.storage.postgres.repositories.entities import PGEntityRepo
from research_engine.adapters.storage.postgres.repositories.events import PGEventRepo
from research_engine.adapters.storage.postgres.repositories.extractions import (
    PGExtractionRepo,
    PGExtractionSchemaRepo,
)
from research_engine.adapters.storage.postgres.repositories.mentions import PGMentionRepo
from research_engine.adapters.storage.postgres.repositories.nodes import (
    PGDocumentNodeRepo,
)
from research_engine.adapters.storage.postgres.repositories.passages import PGPassageRepo
from research_engine.adapters.storage.postgres.repositories.plugins import PGInstalledPluginRepo
from research_engine.adapters.storage.postgres.repositories.provenance import (
    PGIngestionRunRepo,
    PGLLMCallLogRepo,
)
from research_engine.adapters.storage.postgres.repositories.spans import PGSourceSpanRepo
from research_engine.adapters.storage.postgres.repositories.waivers import PGWaiverRepo
from research_engine.adapters.storage.postgres.repositories.work_blocks import (
    PGWorkBlockRepo,
)
from research_engine.adapters.storage.postgres.repositories.work_links import PGWorkLinkRepo

__all__ = [
    "PGCitationRepo",
    "PGDocumentRepo",
    "PGDocumentNodeRepo",
    "PGDocumentTextRepo",
    "PGEdgeRepo",
    "PGEditionRepo",
    "PGEntityRepo",
    "PGEventRepo",
    "PGExtractionRepo",
    "PGExtractionSchemaRepo",
    "PGIngestionRunRepo",
    "PGInstalledPluginRepo",
    "PGLLMCallLogRepo",
    "PGMentionRepo",
    "PGPassageRepo",
    "PGSourceSpanRepo",
    "PGWaiverRepo",
    "PGWorkBlockRepo",
    "PGWorkLinkRepo",
    "PGWorkRepo",
    "PGWorkRevisionRepo",
]
