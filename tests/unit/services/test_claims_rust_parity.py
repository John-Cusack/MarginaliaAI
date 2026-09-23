"""The Rust claim-validation seam must raise the ledger refusal type.

Like the filter seam, this crosses *behavior*: the raised objects are the
real `ClaimWriteRefused` class, constructed from Rust with the same
`__init__` args — code, message, and the detail map identical by
construction. The suite pins all three across both backends: clean lists
pass silent, duplicate pairs and self-edges refuse with the edge index,
and missing targets refuse as `not_found` with the ref detail.

Rust-forced cases skip when the accelerator wheel is absent; the Python
path always runs.
"""

from __future__ import annotations

import pytest

from research_engine.domain.claims import ClaimEdgeDraft
from research_engine.services.argument import claims as claims_mod
from research_engine.services.argument.claims import (
    ClaimService,
    ClaimWriteRefused,
    _missing_target_refusal,
)


def _edge(target_ref: str, relation: str = "supports") -> ClaimEdgeDraft:
    return ClaimEdgeDraft(target_ref=target_ref, relation=relation, confidence=1.0, note=None)


class TestClaimEdgeParity:
    @pytest.mark.parametrize(
        "edges",
        [
            [],
            [_edge("C2")],
            [_edge("C2"), _edge("C2", "contradicts"), _edge("C3")],
            [_edge("C2", "depends_on"), _edge("C2", "refines")],
        ],
    )
    def test_clean_lists_pass_silent(self, edges, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        assert ClaimService._validate_edges("C1", edges) is None
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        assert ClaimService._validate_edges("C1", edges) is None

    def test_duplicate_pairs_refuse_with_index(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        edges = [_edge("C2"), _edge("C3", "contradicts"), _edge("C2")]
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        with pytest.raises(ClaimWriteRefused) as py_exc:
            ClaimService._validate_edges("C1", edges)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        with pytest.raises(ClaimWriteRefused) as rs_exc:
            ClaimService._validate_edges("C1", edges)
        assert rs_exc.value.code == py_exc.value.code == "invalid_input"
        assert rs_exc.value.detail == py_exc.value.detail == {"edge_index": 2}
        assert str(rs_exc.value) == str(py_exc.value)
        assert "duplicates an earlier target and relation" in str(rs_exc.value)

    def test_self_edges_refuse_with_quoted_ref(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        with pytest.raises(ClaimWriteRefused) as py_exc:
            ClaimService._validate_edges("C1", [_edge("C1")])
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        with pytest.raises(ClaimWriteRefused) as rs_exc:
            ClaimService._validate_edges("C1", [_edge("C1")])
        assert str(rs_exc.value) == str(py_exc.value)
        assert "points claim 'C1' at itself" in str(rs_exc.value)
        assert rs_exc.value.detail == {"edge_index": 0}

    def test_same_target_different_relation_passes(self, monkeypatch):
        """Only the (target, relation) pair dedupes — the write path relies
        on reaching the database for anything finer."""
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        assert (
            ClaimService._validate_edges("C1", [_edge("C2", "supports"), _edge("C2", "rebuts")])
            is None
        )


class TestMissingTargetParity:
    def test_refusal_shape_matches(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        with pytest.raises(ClaimWriteRefused) as py_exc:
            _missing_target_refusal("C9")
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        with pytest.raises(ClaimWriteRefused) as rs_exc:
            _missing_target_refusal("C9")
        assert rs_exc.value.code == py_exc.value.code == "not_found"
        assert rs_exc.value.detail == py_exc.value.detail == {"target_ref": "C9"}
        assert str(rs_exc.value) == str(py_exc.value)
        assert "Target claim 'C9' does not exist." in str(rs_exc.value)

    def test_python_path_needs_no_wheel(self, monkeypatch):
        import sys

        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        monkeypatch.delitem(sys.modules, "marginalia_rs", raising=False)
        monkeypatch.setitem(sys.modules, "marginalia_rs", None)
        assert claims_mod.ClaimService._validate_edges("C1", []) is None
        with pytest.raises(ClaimWriteRefused) as exc:
            claims_mod._missing_target_refusal("C9")
        assert exc.value.code == "not_found"
