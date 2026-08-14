"""Confidence-ranked module dispatch for ingestion."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from research_engine.domain.errors import DispatchError, DispatchMiss

if TYPE_CHECKING:
    from pathlib import Path

logger = structlog.get_logger()


class ModuleDispatcher:
    """Selects the best ingestion module for a given source."""

    def __init__(self) -> None:
        self._modules: list = []

    def register(self, module: object) -> None:
        self._modules.append(module)

    async def dispatch(self, source_path: Path, hint: str | None = None) -> object:
        if hint:
            for mod in self._modules:
                if mod.id == hint:
                    conf, reason = await mod.detect(source_path)
                    if conf > 0:
                        return mod
                    raise DispatchError(
                        f"Module '{hint}' rejected source {source_path}: {reason}"
                    )
            raise DispatchError(f"Unknown module hint: {hint}")

        candidates = []
        for mod in self._modules:
            try:
                confidence, reason = await mod.detect(source_path)
                if confidence > 0:
                    candidates.append((confidence, mod, reason))
            except Exception as e:
                logger.warning("module_detect_error", module=mod.id, error=str(e))

        if not candidates:
            raise DispatchMiss(str(source_path))

        candidates.sort(key=lambda x: x[0], reverse=True)
        best_conf, best_mod, best_reason = candidates[0]
        logger.info(
            "module_dispatched",
            source=str(source_path),
            module=best_mod.id,
            confidence=best_conf,
            reason=best_reason,
        )
        return best_mod
