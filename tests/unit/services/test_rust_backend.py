"""`RE_RUST_BACKEND` selection, including the Unicode-version guard.

`auto` may only take the Rust path when the accelerator's compiled Unicode
tables are the interpreter's: otherwise NFKC and word-character answers
differ between backends and "byte-identical" stops being true. A fake
`marginalia_rs` stands in, so these run with or without the wheel.
"""

from __future__ import annotations

import sys
import types
import unicodedata

import pytest

from research_engine import _rust


def _fake_accelerator(monkeypatch, **attrs):
    module = types.ModuleType("marginalia_rs")
    for name, value in attrs.items():
        setattr(module, name, value)
    monkeypatch.setitem(sys.modules, "marginalia_rs", module)
    return module


def test_auto_uses_rust_when_unicode_versions_match(monkeypatch):
    _fake_accelerator(monkeypatch, UNICODE_VERSION=unicodedata.unidata_version)
    monkeypatch.setenv("RE_RUST_BACKEND", "auto")
    assert _rust.backend() == "rust"


def test_auto_stays_python_on_a_unicode_mismatch(monkeypatch):
    _fake_accelerator(monkeypatch, UNICODE_VERSION="1.0.0")
    monkeypatch.setenv("RE_RUST_BACKEND", "auto")
    assert _rust.backend() == "python"
    assert _rust.rust_text() is None


def test_auto_stays_python_on_a_wheel_without_the_constant(monkeypatch):
    """Wheels older than the guard can't vouch for their tables."""
    _fake_accelerator(monkeypatch)
    monkeypatch.setenv("RE_RUST_BACKEND", "auto")
    assert _rust.backend() == "python"


def test_default_is_auto(monkeypatch):
    _fake_accelerator(monkeypatch, UNICODE_VERSION="1.0.0")
    monkeypatch.delenv("RE_RUST_BACKEND", raising=False)
    assert _rust.backend() == "python"


def test_forced_rust_skips_the_guard(monkeypatch):
    """Parity suites force the comparison even across Unicode versions."""
    _fake_accelerator(monkeypatch, UNICODE_VERSION="1.0.0")
    monkeypatch.setenv("RE_RUST_BACKEND", "rust")
    assert _rust.backend() == "rust"


def test_forced_rust_without_the_wheel_raises(monkeypatch):
    monkeypatch.setitem(sys.modules, "marginalia_rs", None)
    monkeypatch.setenv("RE_RUST_BACKEND", "rust")
    with pytest.raises(RuntimeError, match="not installed"):
        _rust.backend()


def test_auto_without_the_wheel_is_python(monkeypatch):
    monkeypatch.setitem(sys.modules, "marginalia_rs", None)
    monkeypatch.setenv("RE_RUST_BACKEND", "auto")
    assert _rust.backend() == "python"


def test_unknown_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("RE_RUST_BACKEND", "fortran")
    with pytest.raises(ValueError, match="Unrecognized"):
        _rust.backend()
