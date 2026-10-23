"""Analysis boundary: LLM JSON contract, classifiers, and the review engine."""

from .classifier import (
    ClassificationOutcome,
    HubClassifier,
    StubClassifier,
    TracedClassifier,
    build_stage2_classifier,
)
from .contract import AnalysisResult, ContractError, parse_analysis
from .pipeline import ScanOutcome, scan

__all__ = [
    "AnalysisResult",
    "ContractError",
    "parse_analysis",
    "TracedClassifier",
    "ClassificationOutcome",
    "StubClassifier",
    "HubClassifier",
    "build_stage2_classifier",
    "ScanOutcome",
    "scan",
]
