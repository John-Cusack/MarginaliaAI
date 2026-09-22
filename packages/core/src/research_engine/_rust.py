"""Optional Rust accelerator selection.

`marginalia-ai-accelerator` ships the `marginalia_rs` native extension with
byte-identical implementations of hot pure paths. This module decides, per
process, whether cut-over callers use it. The pure-Python implementation is
always the fallback — and the differential oracle — so a machine without the
accelerator (or without a Rust toolchain) behaves exactly like before.

``RE_RUST_BACKEND``: ``auto`` (default: Rust when importable, else Python),
``rust`` (force; missing accelerator raises loudly), ``python`` (force the
pure-Python path; the bisection/rollback switch).

Read dynamically on every call so tests can flip the backend with
``monkeypatch.setenv`` in one process. The ``import marginalia_rs`` itself
costs a ``sys.modules`` hit after the first import.
"""

from __future__ import annotations

import os
from typing import Any

#: Environment variable selecting the compute backend.
BACKEND_ENV_VAR = "RE_RUST_BACKEND"

#: Install hint surfaced when ``RE_RUST_BACKEND=rust`` but the wheel is absent.
_MISSING_HINT = (
    "RE_RUST_BACKEND=rust but the 'marginalia_rs' extension is not installed. "
    "Install it with pip install 'marginalia-ai[accelerated]' "
    "or set RE_RUST_BACKEND=python for the pure-Python path."
)


def backend() -> str:
    """The active backend: ``"rust"`` or ``"python"``.

    :raises RuntimeError: if ``RE_RUST_BACKEND=rust`` and the accelerator
        is not installed.
    :raises ValueError: on any other unrecognized mode.
    """
    mode = os.environ.get(BACKEND_ENV_VAR, "auto").strip().lower()
    if mode == "python":
        return "python"
    if mode == "rust":
        try:
            __import__("marginalia_rs")
        except ImportError as exc:
            raise RuntimeError(_MISSING_HINT) from exc
        return "rust"
    if mode == "auto":
        try:
            __import__("marginalia_rs")
        except ImportError:
            return "python"
        return "rust"
    raise ValueError(
        f"Unrecognized {BACKEND_ENV_VAR}={mode!r}; expected 'auto', 'rust' or 'python'."
    )


def rust_text() -> Any:
    """The ``marginalia_rs.text`` module when the Rust backend is active.

    Returns ``None`` on the Python path. Callers branch on this rather than
    importing ``marginalia_rs`` themselves, so every seam shares one switch.
    """
    if backend() != "rust":
        return None
    import marginalia_rs

    return marginalia_rs.text


def rust_chunk() -> Any:
    """The ``marginalia_rs.chunk`` module when the Rust backend is active.

    Returns ``None`` on the Python path. Fusion callers branch on this
    rather than importing ``marginalia_rs`` themselves, so every seam
    shares one switch.
    """
    if backend() != "rust":
        return None
    import marginalia_rs

    return marginalia_rs.chunk


def rust_parse() -> Any:
    """The ``marginalia_rs.parse`` module when the Rust backend is active.

    Returns ``None`` on the Python path. Parser modules branch on this
    rather than importing ``marginalia_rs`` themselves, so every seam
    shares one switch.
    """
    if backend() != "rust":
        return None
    import marginalia_rs

    return marginalia_rs.parse
