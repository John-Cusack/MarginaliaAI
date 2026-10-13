"""Adapter that conforms the core profile repositories to the SDK
``ProfileClient`` Protocol.

Plugins with the ``ingest`` permission receive an instance as their
``profiles`` client. A profile is loaded whole: the plugin passes its key, its
version, the hash of the data it was read from, and every role it gives an
edition, and the stored roles become exactly those. Runs are opened and closed
against a profile, and each records the profile hash in force when it started
and the hash of its own config, so any result can be traced to the exact
declaration and settings that produced it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from research_engine.domain.errors import NotFoundError
from research_engine.domain.profiles import RoleDraft, RunStatus

if TYPE_CHECKING:
    from research_engine.adapters.storage.postgres.repositories import (
        PGEditionRepo,
        PGProfileRepo,
        PGRunRepo,
    )


class ProfileServiceAdapter:
    """Concrete implementation of the SDK ``ProfileClient`` Protocol."""

    def __init__(
        self,
        *,
        editions: PGEditionRepo,
        profiles: PGProfileRepo,
        runs: PGRunRepo,
        transaction_factory: Any,
    ) -> None:
        self._editions = editions
        self._profiles = profiles
        self._runs = runs
        self._tx_factory = transaction_factory

    async def load_profile(
        self,
        key: str,
        *,
        version: int,
        profile_hash: str,
        roles: list[dict[str, Any]],
        title: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Store a profile and replace its roles with exactly ``roles``.

        Every edition a role names must already be registered — a profile
        declares a view of the library, it does not add to it. Overlapping
        roles of one kind for one edition are refused, naming the edition.
        """
        try:
            drafts = [RoleDraft.model_validate(role) for role in roles]
        except ValidationError as exc:
            raise ValueError(f"profile {key!r}: {exc}") from exc
        edition_ids: dict[str, UUID] = {}
        for draft in drafts:
            if draft.edition_key not in edition_ids:
                edition = await self._editions.get_by_key(draft.edition_key)
                if edition is not None:
                    edition_ids[draft.edition_key] = edition.id
        missing = sorted({d.edition_key for d in drafts} - set(edition_ids))
        if missing:
            # A profile declares a view of the library; it does not add to it.
            raise NotFoundError("registered edition", ", ".join(missing))
        try:
            async with self._tx_factory() as tx:
                profile = await self._profiles.upsert(
                    tx,
                    key=key,
                    version=version,
                    profile_hash=profile_hash,
                    title=title,
                    settings=settings,
                )
                count = await self._profiles.replace_roles(
                    tx,
                    profile.id,
                    [(edition_ids[d.edition_key], d) for d in drafts],
                )
        except IntegrityError as exc:
            if "source_roles_no_overlap" in str(exc.orig):
                raise ValueError(
                    f"profile {key!r} gives one edition the same role twice over "
                    f"overlapping dates: {exc.orig}"
                ) from exc
            raise
        return {**profile.model_dump(mode="json"), "roles": count}

    async def get_profile(
        self, key: str, *, on: date | str | None = None
    ) -> dict[str, Any] | None:
        """The profile with its roles — all of them, or those in force on ``on``."""
        profile = await self._profiles.get_by_key(key)
        if profile is None:
            return None
        day = date.fromisoformat(on) if isinstance(on, str) else on
        roles = await self._profiles.roles(profile.id, on=day)
        return {
            **profile.model_dump(mode="json"),
            "roles": [r.model_dump(mode="json") for r in roles],
        }

    async def start_run(
        self,
        profile_key: str,
        *,
        stage: str,
        config: dict[str, Any] | None = None,
        code_version: str | None = None,
        model_versions: dict[str, Any] | None = None,
        items_in: int | None = None,
        cost_estimate: float | None = None,
    ) -> dict[str, Any]:
        """Open a run under the profile as loaded now."""
        profile = await self._profiles.get_by_key(profile_key)
        if profile is None:
            raise NotFoundError("profile", profile_key)
        config = config or {}
        async with self._tx_factory() as tx:
            run = await self._runs.start(
                tx,
                profile_id=profile.id,
                stage=stage,
                profile_hash=profile.profile_hash,
                config=config,
                config_hash=config_hash(config),
                code_version=code_version,
                model_versions=model_versions,
                items_in=items_in,
                cost_estimate=_money(cost_estimate),
            )
        return run.model_dump(mode="json")

    async def finish_run(
        self,
        run_id: str | UUID,
        *,
        status: str = "finished",
        items_out: int | None = None,
        stats: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self._tx_factory() as tx:
            run = await self._runs.finish(
                tx,
                UUID(str(run_id)),
                status=RunStatus(status),
                items_out=items_out,
                stats=stats,
            )
        return run.model_dump(mode="json")


def config_hash(config: dict[str, Any]) -> str:
    """SHA-256 of the config's canonical JSON: key order and spacing never count."""
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _money(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))
