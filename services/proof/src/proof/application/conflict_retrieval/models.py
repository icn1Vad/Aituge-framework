from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ConflictRetrievalLimits:
    same_title: int = 6
    leaf_category: int = 6
    parent_category: int = 4
    global_recall: int = 2
    max_candidates: int = 60


@dataclass(slots=True)
class ConflictRetrievalResult:
    source: dict[str, Any]
    results: list[dict[str, Any]]
    candidate_counts: dict[str, int]
    branch_metadata: dict[str, Any] = field(default_factory=dict)
    reranker_used: bool = False
    reranker_model: str | None = None
    degraded: bool = False
    degradation_reasons: list[str] = field(default_factory=list)
    skipped_branches: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "results": self.results,
            "candidate_counts": self.candidate_counts,
            "branch_metadata": self.branch_metadata,
            "reranker_used": self.reranker_used,
            "reranker_model": self.reranker_model,
            "degraded": self.degraded,
            "degradation_reasons": self.degradation_reasons,
            "skipped_branches": self.skipped_branches,
        }
