"""Adaptive legal evidence planning for contract review.

The package exposes one deep Module Interface: ``AdaptiveLegalEvidencePlanner.plan``.
Storage, embeddings and projection imports remain implementation details behind
small repository/vectorizer seams.
"""

from contract.legal_evidence.binding import (
    LEGAL_EVIDENCE_CHECK_BINDING_VERSION,
    LegalEvidenceCheckBinder,
)
from contract.legal_evidence.models import (
    LegalEvidence,
    LegalEvidenceBundle,
    LegalEvidenceIssue,
    LegalEvidencePlanRequest,
    LegalEvidenceRelease,
    LegalIssueCoverage,
    LegalRelation,
    LegalRetrievalUnit,
)
from contract.legal_evidence.planner import AdaptiveLegalEvidencePlanner

__all__ = [
    "LEGAL_EVIDENCE_CHECK_BINDING_VERSION",
    "AdaptiveLegalEvidencePlanner",
    "LegalEvidence",
    "LegalEvidenceBundle",
    "LegalEvidenceCheckBinder",
    "LegalEvidenceIssue",
    "LegalEvidencePlanRequest",
    "LegalEvidenceRelease",
    "LegalIssueCoverage",
    "LegalRelation",
    "LegalRetrievalUnit",
]
