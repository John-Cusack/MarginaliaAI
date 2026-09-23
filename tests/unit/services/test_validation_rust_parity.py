"""The Rust validation seam must raise the domain exception types.

Unlike the string builders, this seam crosses *behavior*: the raised
objects are the real `research_engine.domain.errors` classes, constructed
from Rust with the same `__init__` args — so type, attributes, and message
are identical by construction, not by byte-pinning. The suite pins all
three across both backends: valid matrices pass silent, unknown keys
raise sorted/deduped, unknown extensions raise with the available list or
the no-registry hint.

Rust-forced cases skip when the accelerator wheel is absent; the Python
path always runs.
"""

from __future__ import annotations

import pytest

from research_engine.adapters.storage.postgres.repositories import (
    passages as passages_mod,
)
from research_engine.adapters.storage.postgres.repositories.passages import (
    SUPPORTED_FILTERS,
    validate_filters,
)
from research_engine.domain.errors import (
    UnknownFilterExtension,
    UnsupportedFilterError,
)


class TestValidateFiltersParity:
    @pytest.mark.parametrize(
        "filters",
        [
            {},
            {"language": "english"},
            {"document_types": ["letter"], "language": "german"},
            {"extensions": {}},
            {"extensions": None},
            {"extension_logic": "or"},
            {key: None for key in SUPPORTED_FILTERS},
        ],
    )
    def test_valid_filters_pass_silent(self, filters, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        assert validate_filters(filters) is None
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        assert validate_filters(filters) is None

    def test_unknown_keys_raise_sorted_and_deduped(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        filters = {"zzz": 1, "document_types": ["letter"], "aaa": 2}
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        with pytest.raises(UnsupportedFilterError) as py_exc:
            validate_filters(filters)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        with pytest.raises(UnsupportedFilterError) as rs_exc:
            validate_filters(filters)
        assert rs_exc.value.unknown == ["aaa", "zzz"]
        assert rs_exc.value.unknown == py_exc.value.unknown
        assert rs_exc.value.supported == py_exc.value.supported
        assert str(rs_exc.value) == str(py_exc.value)
        assert "document_types" in rs_exc.value.supported

    def test_key_check_precedes_extension_check(self, monkeypatch):
        """Unknown keys raise even when extensions are also unknown —
        the order is part of the contract."""
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        with pytest.raises(UnsupportedFilterError):
            validate_filters({"bogus": 1, "extensions": {"nope": True}}, None)

    def test_unknown_extension_reports_available(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        filters = {"extensions": {"nope": True}}
        available = {"has_extraction": object(), "other": object()}
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        with pytest.raises(UnknownFilterExtension) as py_exc:
            validate_filters(filters, available)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        with pytest.raises(UnknownFilterExtension) as rs_exc:
            validate_filters(filters, available)
        assert rs_exc.value.extension_id == "nope"
        assert rs_exc.value.available == ["has_extraction", "other"]
        assert str(rs_exc.value) == str(py_exc.value)
        assert "Available extensions" in str(rs_exc.value)

    def test_empty_registry_selects_hint(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        with pytest.raises(UnknownFilterExtension) as py_exc:
            validate_filters({"extensions": {"nope": True}}, None)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        with pytest.raises(UnknownFilterExtension) as rs_exc:
            validate_filters({"extensions": {"nope": True}}, None)
        assert str(rs_exc.value) == str(py_exc.value)
        assert "No filter extensions are registered" in str(rs_exc.value)

    def test_first_unknown_extension_wins(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        filters = {"extensions": {"zzz": 1, "aaa": 2}}
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        with pytest.raises(UnknownFilterExtension) as py_exc:
            validate_filters(filters, None)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        with pytest.raises(UnknownFilterExtension) as rs_exc:
            validate_filters(filters, None)
        assert rs_exc.value.extension_id == py_exc.value.extension_id == "zzz"

    def test_python_path_needs_no_wheel(self, monkeypatch):
        import sys

        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        monkeypatch.delitem(sys.modules, "marginalia_rs", raising=False)
        monkeypatch.setitem(sys.modules, "marginalia_rs", None)
        assert passages_mod.validate_filters({"language": "x"}) is None
        with pytest.raises(UnsupportedFilterError):
            passages_mod.validate_filters({"bogus": 1})
