from __future__ import annotations

from proof.application.structure import STRUCTURE_ENGINE_VERSION, extract_policy_structure
from proof.domain import DocumentBlock, RetrievalUnit


CHUNKER_VERSION = STRUCTURE_ENGINE_VERSION


def split_into_clause_units(blocks: list[DocumentBlock]) -> list[RetrievalUnit]:
    return extract_policy_structure(blocks).units
