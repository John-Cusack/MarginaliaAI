"""Core services for the Dilemma Finder stages that run over the corpus."""

from research_engine.services.dilemma.blocking import HornEligibility
from research_engine.services.dilemma.materialize import ClaimMaterializer
from research_engine.services.dilemma.packets import build_packets, estimate
from research_engine.services.dilemma.scoring import PairScorer
from research_engine.services.dilemma.shortlist import ClaimShortlister

__all__ = [
    "ClaimMaterializer",
    "ClaimShortlister",
    "HornEligibility",
    "PairScorer",
    "build_packets",
    "estimate",
]
