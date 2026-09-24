"""Rust backend selection.

Every ``marginalia-ai`` wheel carries ``research_engine._native``, a Rust
extension with byte-identical implementations of hot pure paths. This module
decides, per call, whether cut-over callers use it. The pure-Python
implementation stays the fallback and the differential oracle.

``RE_RUST_BACKEND``: ``auto`` (default: Rust when the extension is importable
*and* was built for this interpreter's Unicode version, else Python), ``rust``
(force; a missing extension raises loudly), ``python`` (force the pure-Python
path; the bisection/rollback switch).

The Unicode check is what makes "byte-identical" true. The extension's NFKC
and word-character tables are compiled in; Python's come from ``unicodedata``
and change with the interpreter (3.11 is Unicode 14.0, 3.13 is 15.1, 3.14 is
16.0). ``auto`` only takes the Rust path when ``_native.UNICODE_VERSION``
equals ``unicodedata.unidata_version``; on any other interpreter it stays on
Python. ``rust`` skips the check so parity suites can still force the
comparison.

The extension is only missing from a source tree nobody built (a checkout
imported without ``uv sync``/``pip install``); ``auto`` then runs on Python.

Read dynamically on every call so tests can flip the backend with
``monkeypatch.setenv`` in one process. The import itself costs a
``sys.modules`` hit after the first.
"""

from __future__ import annotations

import os
import unicodedata
from importlib import import_module
from typing import Any

#: Environment variable selecting the compute backend.
BACKEND_ENV_VAR = "RE_RUST_BACKEND"

#: Where the extension lives inside the package.
NATIVE_MODULE = "research_engine._native"

#: Hint surfaced when ``RE_RUST_BACKEND=rust`` but the extension is absent.
_MISSING_HINT = (
    "RE_RUST_BACKEND=rust but research_engine._native is not built. Install "
    "marginalia-ai from a wheel, or run `uv sync` in a checkout (it compiles "
    "the extension; needs a Rust toolchain), or set RE_RUST_BACKEND=python."
)


def _native() -> Any:
    return import_module(NATIVE_MODULE)


def backend() -> str:
    """The active backend: ``"rust"`` or ``"python"``.

    :raises RuntimeError: if ``RE_RUST_BACKEND=rust`` and the extension is
        not built.
    :raises ValueError: on any other unrecognized mode.
    """
    mode = os.environ.get(BACKEND_ENV_VAR, "auto").strip().lower()
    if mode == "python":
        return "python"
    if mode == "rust":
        try:
            _native()
        except ImportError as exc:
            raise RuntimeError(_MISSING_HINT) from exc
        return "rust"
    if mode == "auto":
        try:
            native = _native()
        except ImportError:
            return "python"
        if getattr(native, "UNICODE_VERSION", None) != unicodedata.unidata_version:
            return "python"
        return "rust"
    raise ValueError(
        f"Unrecognized {BACKEND_ENV_VAR}={mode!r}; expected 'auto', 'rust' or 'python'."
    )


def rust_text() -> Any:
    """The extension's ``text`` module when the Rust backend is active.

    Returns ``None`` on the Python path. Callers branch on this rather than
    importing the extension themselves, so every seam shares one switch.
    """
    return _native().text if backend() == "rust" else None


def rust_chunk() -> Any:
    """The extension's ``chunk`` module when the Rust backend is active."""
    return _native().chunk if backend() == "rust" else None


def rust_parse() -> Any:
    """The extension's ``parse`` module when the Rust backend is active."""
    return _native().parse if backend() == "rust" else None
