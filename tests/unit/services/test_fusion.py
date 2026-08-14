"""Tests for search fusion algorithms."""

from __future__ import annotations

from uuid import uuid4

from research_engine.services.search.fusion import rrf_fuse, weighted_fuse


class TestRRFFusion:
    def test_single_list(self):
        pid1, pid2 = uuid4(), uuid4()
        result = rrf_fuse([(pid1, 0.9), (pid2, 0.7)])
        assert len(result) == 2
        assert result[0][0] == pid1
        assert result[0][1] > result[1][1]

    def test_two_lists_agreement(self):
        pid1, pid2, pid3 = uuid4(), uuid4(), uuid4()
        vec = [(pid1, 0.95), (pid2, 0.8), (pid3, 0.6)]
        kw = [(pid1, 0.9), (pid3, 0.85), (pid2, 0.4)]
        result = rrf_fuse(vec, kw)
        # pid1 should be top since it's ranked #1 in both lists
        assert result[0][0] == pid1

    def test_disjoint_lists(self):
        pid1, pid2 = uuid4(), uuid4()
        vec = [(pid1, 0.9)]
        kw = [(pid2, 0.8)]
        result = rrf_fuse(vec, kw)
        assert len(result) == 2
        # Both should have equal scores (rank 1 in their lists)
        assert abs(result[0][1] - result[1][1]) < 0.01

    def test_empty_lists(self):
        result = rrf_fuse([], [])
        assert result == []

    def test_breakdown_included(self):
        pid1 = uuid4()
        result = rrf_fuse([(pid1, 0.9)], [(pid1, 0.8)])
        assert "list_0" in result[0][2]
        assert "list_1" in result[0][2]
        assert result[0][2]["list_0"]["rank"] == 1


class TestWeightedFusion:
    def test_equal_weights(self):
        pid1, pid2 = uuid4(), uuid4()
        vec = [(pid1, 0.9), (pid2, 0.5)]
        kw = [(pid2, 0.9), (pid1, 0.5)]
        result = weighted_fuse(vec, kw, alpha=0.5)
        # With equal scores and equal weights, both should be equal
        assert len(result) == 2

    def test_vector_dominant(self):
        pid1, pid2, pid3 = uuid4(), uuid4(), uuid4()
        vec = [(pid1, 0.9), (pid3, 0.1)]
        kw = [(pid2, 0.9), (pid3, 0.1)]
        result = weighted_fuse(vec, kw, alpha=0.9)
        # pid1 should rank higher since we weight vector at 0.9
        assert result[0][0] == pid1

    def test_keyword_dominant(self):
        pid1, pid2, pid3 = uuid4(), uuid4(), uuid4()
        vec = [(pid1, 0.9), (pid3, 0.1)]
        kw = [(pid2, 0.9), (pid3, 0.1)]
        result = weighted_fuse(vec, kw, alpha=0.1)
        # pid2 should rank higher since we weight keyword at 0.9
        assert result[0][0] == pid2

    def test_empty(self):
        result = weighted_fuse([], [])
        assert result == []
