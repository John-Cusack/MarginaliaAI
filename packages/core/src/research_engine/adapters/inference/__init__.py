"""Choosing where embedding and reranking run, and what happens when it is down."""

from research_engine.adapters.inference.gpu_host import (
    GpuHostError,
    ensure_gpu_host_ready,
)
from research_engine.adapters.inference.routing import (
    InferenceBackends,
    Workload,
    build_inference,
)

__all__ = [
    "GpuHostError",
    "InferenceBackends",
    "Workload",
    "build_inference",
    "ensure_gpu_host_ready",
]

