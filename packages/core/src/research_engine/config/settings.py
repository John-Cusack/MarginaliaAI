"""Application settings loaded from environment / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import structlog
from pydantic import SecretStr
from pydantic_settings import BaseSettings

logger = structlog.get_logger()

ENV_PREFIX = "RE_"

#: Fields whose values must never be printed or logged.
SECRET_FIELDS = frozenset(
    {"anthropic_api_key", "openai_compatible_api_key", "embedding_api_key"}
)


class Settings(BaseSettings):
    # Database
    db_url: str = "postgresql+asyncpg://re_dev:re_dev_pass@localhost:5435/research_engine"
    auto_migrate: bool = True

    # LLM
    llm_provider: Literal["anthropic", "openai_compatible"] = "anthropic"
    anthropic_api_key: SecretStr | None = None
    openai_compatible_base_url: str | None = None
    openai_compatible_api_key: SecretStr | None = None
    default_llm_model: str = "claude-sonnet-4-5-20250929"
    #: Refuse LLM calls once this much estimated spend has accumulated in the
    #: rolling window. Unset means no ceiling. A backstop against runaway
    #: corpus-wide extraction, not an accounting control — see BudgetGuard.
    llm_budget_usd: float | None = None
    llm_budget_window_days: int = 30

    # Inference (embedding + reranking, optionally offloaded to a GPU host)
    #: Base URL of a `research-engine embed-server`, e.g.
    #: "http://john-super-server:9882". One server serves both models, so this
    #: is one address rather than two that must agree.
    inference_base_url: str | None = None

    #: Where embedding runs.
    #:
    #: - ``local_bge``  — always this machine. A set base URL is ignored, which
    #:   is the point: it is the off switch that does not make you delete the
    #:   address to use it.
    #: - ``remote_api`` — always the GPU host; fail if it is unreachable. Right
    #:   for a headless box with no usable accelerator of its own.
    #: - ``auto``       — GPU host when it answers; queries fall back to local,
    #:   corpus-wide work still fails loudly. See `adapters/inference/routing.py`
    #:   for why those two differ.
    embedding_provider: Literal["local_bge", "remote_api", "auto"] = "local_bge"
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024
    #: Deprecated alias for `inference_base_url`, kept so existing .env files
    #: keep working. Setting it no longer implies remote — the provider decides.
    embedding_base_url: str | None = None
    embedding_timeout: float = 120.0
    embedding_api_key: SecretStr | None = None

    # Reranker
    #: Same modes as `embedding_provider`, plus ``none`` to skip reranking
    #: entirely. Under ``auto`` an unreachable server means results come back
    #: unreranked and flagged, *not* reranked slowly on the CPU — measured, that
    #: costs 48.8 s of a 49.1 s search. Choose ``local_bge`` if this machine has
    #: a working accelerator and you want it used as a fallback.
    reranker_provider: Literal["local_bge", "remote_api", "auto", "none"] = "local_bge"
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    #: Reranking is interactive; a request slower than this has already failed
    #: the researcher whether or not it eventually returns.
    reranker_timeout: float = 30.0

    # Paths
    data_dir: Path = Path.home() / ".research-engine"
    plugins_dir: Path | None = None

    # Ingestion
    ingest_concurrency: int = 4
    embedding_batch_size: int = 32
    docling_device: Literal["cpu", "auto", "cuda"] = "cpu"

    # Extraction
    extraction_concurrency: int = 8
    extraction_retry_on_validation: bool = True

    # Search
    #: ISO 639-1 code assumed for documents whose language the parser does not
    #: report. Unset means index under Postgres' `simple` config (no stemming),
    #: which is safe for a mixed corpus but costs recall on a single-language
    #: one — set it to e.g. "en" if the corpus really is all English.
    default_language: str | None = None
    search_default_k: int = 20
    search_rerank_n: int = 30
    rrf_k: int = 60
    #: HNSW search breadth. Higher trades latency for recall. Measured on a
    #: 271k-vector corpus: 40 -> ~2.1 ms, 100 -> ~2.7 ms, 400 -> ~3.5 ms, against
    #: a 416 ms exact scan. 100 is the default because the extra millisecond is
    #: irrelevant next to reranking and the recall is meaningfully better.
    hnsw_ef_search: int = 100

    # Logging
    log_level: str = "INFO"
    log_format: Literal["pretty", "json"] = "pretty"

    model_config = {"env_prefix": ENV_PREFIX, "extra": "ignore"}

    @property
    def resolved_plugins_dir(self) -> Path:
        return self.plugins_dir or self.data_dir / "plugins"

    @property
    def resolved_inference_base_url(self) -> str | None:
        """The GPU host address, honouring the deprecated embedding-only name."""
        return self.inference_base_url or self.embedding_base_url or None


@dataclass(frozen=True)
class EnvFileResolution:
    """Which env file was used, and how it was chosen."""

    path: Path | None
    reason: str

    @property
    def exists(self) -> bool:
        return self.path is not None and self.path.is_file()


def find_env_file(start: Path | None = None) -> EnvFileResolution:
    """Locate the .env file, independent of where the process was started.

    ``env_file: ".env"`` in pydantic-settings resolves against the *current
    working directory*. That makes configuration depend on where you happened to
    invoke the CLI from: run it from a subdirectory and a different file, or no
    file, is read — silently, because a missing env file is not an error. That is
    how an unset API key and an unset spend ceiling can look identical to
    correctly-configured ones.

    Resolution order:

    1. ``RE_ENV_FILE`` if set — explicit always wins, and a bad path is loud.
    2. The nearest ``.env`` walking up from *start*, stopping at the directory
       that holds ``pyproject.toml`` so the search cannot wander into ``$HOME``.
    """
    override = os.environ.get(f"{ENV_PREFIX}ENV_FILE")
    if override:
        path = Path(override).expanduser()
        if not path.is_file():
            logger.warning(
                "env_file_missing",
                path=str(path),
                detail=f"{ENV_PREFIX}ENV_FILE points at a file that does not exist; "
                "settings fall back to environment variables and defaults.",
            )
            return EnvFileResolution(None, f"{ENV_PREFIX}ENV_FILE={override} (missing)")
        return EnvFileResolution(path.resolve(), f"{ENV_PREFIX}ENV_FILE")

    origin = (start or Path.cwd()).resolve()
    for directory in [origin, *origin.parents]:
        candidate = directory / ".env"
        if candidate.is_file():
            return EnvFileResolution(candidate, f"nearest .env above {origin}")
        if (directory / "pyproject.toml").is_file():
            break
    return EnvFileResolution(None, f"no .env found above {origin}")


def _env_file_keys(path: Path | None) -> set[str]:
    """Keys defined in an env file. Values are never read here."""
    if path is None or not path.is_file():
        return set()
    keys: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return keys
    for raw in lines:
        line = raw.strip().removeprefix("export ").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.add(line.split("=", 1)[0].strip().upper())
    return keys


@dataclass(frozen=True)
class SettingReport:
    """Where a single setting's value came from."""

    name: str
    env_var: str
    value: str
    source: str  # "override" | "environment" | "env file" | "default"
    is_secret: bool


def describe_settings(
    settings: Settings,
    resolution: EnvFileResolution,
    overrides: set[str] | None = None,
) -> list[SettingReport]:
    """Report each setting's resolved value and where it came from.

    Precedence mirrors pydantic-settings: explicit override, then environment
    variable, then env file, then the field default.
    """
    file_keys = _env_file_keys(resolution.path)
    overrides = overrides or set()
    reports: list[SettingReport] = []

    for name in type(settings).model_fields:
        env_var = f"{ENV_PREFIX}{name.upper()}"
        if name in overrides:
            source = "override"
        elif env_var in os.environ:
            source = "environment"
        elif env_var in file_keys:
            source = "env file"
        else:
            source = "default"

        raw = getattr(settings, name)
        is_secret = name in SECRET_FIELDS
        if is_secret:
            value = "SET" if raw is not None else "unset"
        elif raw is None:
            value = "unset"
        else:
            value = str(raw)

        reports.append(SettingReport(name, env_var, value, source, is_secret))
    return reports


def load_settings(**overrides: object) -> Settings:
    """Build Settings from an env file resolved independently of the cwd."""
    resolution = find_env_file()
    settings = Settings(_env_file=resolution.path, **overrides)  # type: ignore[arg-type]
    logger.debug(
        "settings_loaded",
        env_file=str(resolution.path) if resolution.path else None,
        reason=resolution.reason,
    )
    return settings
