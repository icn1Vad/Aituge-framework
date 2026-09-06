"""Domain-neutral adaptive frontier search.

The kernel owns traversal mechanics and physical budgets.  Profiles own all
meaning: retrieval, applicability, relation trust, concept coverage, marginal
value and evidence construction.  Consequently a legal graph and a business
rule graph can reuse the same loop without pretending their policies are the
same.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Generic, TypeVar


UnitT = TypeVar("UnitT")
EvidenceT = TypeVar("EvidenceT")
RelationT = TypeVar("RelationT")


@dataclass(slots=True)
class AdaptiveSearchCandidate(Generic[UnitT]):
    unit: UnitT
    score: float
    channels: set[str] = field(default_factory=set)
    relation_path: list[str] = field(default_factory=list)
    rerank_score: float | None = None


@dataclass(frozen=True, slots=True)
class AdaptiveSearchPage(Generic[UnitT]):
    candidates: tuple[AdaptiveSearchCandidate[UnitT], ...]
    has_more: bool
    degraded_channels: frozenset[str] = frozenset()
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AdaptiveRelationExpansion(Generic[UnitT, RelationT]):
    relation_id: str
    relation: RelationT
    unit: UnitT
    score: float
    carries_consequence: bool = False


@dataclass(frozen=True, slots=True)
class AdaptiveExpansionPage(Generic[UnitT, RelationT]):
    neighbors: tuple[AdaptiveRelationExpansion[UnitT, RelationT], ...]
    degraded_channels: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class AdaptiveSearchBudget:
    candidate_page_size: int
    maximum_examined_candidates: int
    maximum_rounds: int
    maximum_wall_time_seconds: float
    minimum_relevance: float
    relative_relevance_floor: float


@dataclass(slots=True)
class AdaptiveSearchResult(Generic[EvidenceT, RelationT]):
    evidence: list[EvidenceT]
    relations: list[RelationT]
    covered_concepts: list[str]
    complete: bool
    degraded_channels: set[str]
    examined: int
    rounds: int
    safety_reached: bool
    diagnostics: list[str]


class AdaptiveEvidenceSearch(Generic[UnitT, EvidenceT, RelationT]):
    """Select evidence until coverage, exhaustion, low value or a safety cap."""

    def __init__(
        self,
        *,
        budget: AdaptiveSearchBudget,
        unit_id: Callable[[UnitT], str],
        fetch_page: Callable[[int], AdaptiveSearchPage[UnitT]],
        matched_concepts: Callable[[UnitT], list[str]],
        marginal_value: Callable[[AdaptiveSearchCandidate[UnitT], set[str]], float],
        coverage_sufficient: Callable[[set[str], list[EvidenceT]], bool],
        applicable: Callable[[UnitT], bool],
        reliable: Callable[[AdaptiveSearchCandidate[UnitT], list[str]], bool],
        build_evidence: Callable[[AdaptiveSearchCandidate[UnitT], list[str], float], EvidenceT],
        evidence_score: Callable[[EvidenceT], float],
        expand: Callable[[AdaptiveSearchCandidate[UnitT]], AdaptiveExpansionPage[UnitT, RelationT]],
        relation_id: Callable[[RelationT], str],
    ) -> None:
        self.budget = budget
        self.unit_id = unit_id
        self.fetch_page = fetch_page
        self.matched_concepts = matched_concepts
        self.marginal_value = marginal_value
        self.coverage_sufficient = coverage_sufficient
        self.applicable = applicable
        self.reliable = reliable
        self.build_evidence = build_evidence
        self.evidence_score = evidence_score
        self.expand = expand
        self.relation_id = relation_id

    def run(self, *, global_deadline: float | None = None) -> AdaptiveSearchResult[EvidenceT, RelationT]:
        frontier: dict[str, AdaptiveSearchCandidate[UnitT]] = {}
        selected: list[EvidenceT] = []
        selected_ids: set[str] = set()
        relations: dict[str, RelationT] = {}
        relation_consequences: dict[str, bool] = {}
        covered: set[str] = set()
        degraded: set[str] = set()
        diagnostics: list[str] = []
        offset = 0
        examined = 0
        rounds = 0
        exhausted = False
        safety_reached = False
        complete = False
        started = time.monotonic()

        while True:
            now = time.monotonic()
            if (
                examined >= self.budget.maximum_examined_candidates
                or rounds >= self.budget.maximum_rounds
                or now - started >= self.budget.maximum_wall_time_seconds
                or (global_deadline is not None and now >= global_deadline)
            ):
                safety_reached = True
                break
            if not frontier and not exhausted:
                page = self.fetch_page(offset)
                degraded.update(page.degraded_channels)
                diagnostics.extend(page.diagnostics)
                offset += self.budget.candidate_page_size
                exhausted = not page.has_more
                for candidate in page.candidates:
                    self._merge(frontier, candidate)

            available = [
                item for key, item in frontier.items() if key not in selected_ids
            ]
            if not available:
                if exhausted:
                    complete = self.coverage_sufficient(covered, selected)
                    break
                continue

            current = max(
                available,
                key=lambda item: (
                    self.marginal_value(item, covered),
                    item.score,
                    self.unit_id(item.unit),
                ),
            )
            current_marginal = self.marginal_value(current, covered)
            best_selected = max(
                (self.evidence_score(item) for item in selected),
                default=current_marginal,
            )
            dynamic_floor = max(
                self.budget.minimum_relevance,
                best_selected * self.budget.relative_relevance_floor,
            )
            matched = self.matched_concepts(current.unit)
            adds_concepts = bool(set(matched) - covered)
            carries_consequence = any(
                relation_consequences.get(relation_id, False)
                for relation_id in current.relation_path
            )
            has_coverage = self.coverage_sufficient(covered, selected)

            if (
                not selected
                and current.rerank_score is not None
                and not self.reliable(current, matched)
                and not adds_concepts
                and not (current.channels & {"EXACT", "RELATION"})
            ):
                # A calibrated rejection at the head of the frontier is a real
                # zero-evidence result. Do not scan lower pages to fill a quota.
                break
            if (
                selected
                and not adds_concepts
                and not carries_consequence
                and ((has_coverage and bool(current.relation_path)) or current_marginal < dynamic_floor)
            ):
                complete = has_coverage
                break

            frontier.pop(self.unit_id(current.unit), None)
            examined += 1
            rounds += 1
            if not self.applicable(current.unit):
                continue
            if current.score < self.budget.minimum_relevance and not matched:
                continue
            if not self.reliable(current, matched):
                continue

            evidence = self.build_evidence(current, matched, current_marginal)
            selected.append(evidence)
            selected_ids.add(self.unit_id(current.unit))
            covered.update(matched)

            expansion = self.expand(current)
            degraded.update(expansion.degraded_channels)
            for neighbor in expansion.neighbors:
                relations[neighbor.relation_id] = neighbor.relation
                relation_consequences[neighbor.relation_id] = neighbor.carries_consequence
                self._merge(
                    frontier,
                    AdaptiveSearchCandidate(
                        unit=neighbor.unit,
                        score=max(0.0, min(1.0, neighbor.score)),
                        channels={"RELATION"},
                        relation_path=[*current.relation_path, neighbor.relation_id],
                    ),
                )

        return AdaptiveSearchResult(
            evidence=selected,
            relations=sorted(relations.values(), key=self.relation_id),
            covered_concepts=list(covered),
            complete=complete,
            degraded_channels=degraded,
            examined=examined,
            rounds=rounds,
            safety_reached=safety_reached,
            diagnostics=diagnostics,
        )

    def _merge(
        self,
        frontier: dict[str, AdaptiveSearchCandidate[UnitT]],
        candidate: AdaptiveSearchCandidate[UnitT],
    ) -> None:
        key = self.unit_id(candidate.unit)
        existing = frontier.get(key)
        if existing is None:
            frontier[key] = AdaptiveSearchCandidate(
                unit=candidate.unit,
                score=candidate.score,
                channels=set(candidate.channels),
                relation_path=list(candidate.relation_path),
                rerank_score=candidate.rerank_score,
            )
            return
        existing.score = max(existing.score, candidate.score)
        existing.channels.update(candidate.channels)
        if candidate.rerank_score is not None:
            existing.rerank_score = candidate.rerank_score
        if candidate.relation_path and (
            not existing.relation_path
            or len(candidate.relation_path) < len(existing.relation_path)
        ):
            existing.relation_path = list(candidate.relation_path)
