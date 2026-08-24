"""Choosing where embedding and reranking run, and what happens when it is down."""

from research_engine.adapters.inference.routing import (
    InferenceBackends,
    Workload,
    build_inference,
)

__all__ = ["InferenceBackends", "Workload", "build_inference"]
