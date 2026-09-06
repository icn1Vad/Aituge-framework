"""Domain-neutral evidence planning orchestration.

The package deliberately knows nothing about statutes, review rules, finance,
or workflow compliance.  A domain profile supplies its request/bundle models,
planner and binder; the engine supplies one stable execution boundary.
"""

from contract.evidence_planning.engine import (
    EvidencePlanningEngine,
    EvidencePlanningProfile,
    EvidencePlanningProfileDisabled,
    EvidencePlanningProfileNotFound,
)
from contract.evidence_planning.adaptive import (
    AdaptiveEvidenceSearch,
    AdaptiveExpansionPage,
    AdaptiveRelationExpansion,
    AdaptiveSearchBudget,
    AdaptiveSearchCandidate,
    AdaptiveSearchPage,
    AdaptiveSearchResult,
)
from contract.evidence_planning.ports import EvidenceBinder, EvidenceBundle, EvidencePlanner

__all__ = [
    "EvidenceBinder",
    "EvidenceBundle",
    "EvidencePlanner",
    "EvidencePlanningEngine",
    "EvidencePlanningProfile",
    "EvidencePlanningProfileDisabled",
    "EvidencePlanningProfileNotFound",
    "AdaptiveEvidenceSearch",
    "AdaptiveExpansionPage",
    "AdaptiveRelationExpansion",
    "AdaptiveSearchBudget",
    "AdaptiveSearchCandidate",
    "AdaptiveSearchPage",
    "AdaptiveSearchResult",
]
