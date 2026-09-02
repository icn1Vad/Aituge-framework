from __future__ import annotations

import hashlib
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from contract.application.idempotency import canonical_json
from contract.legal_evidence.models import (
    LegalApplicabilityDecision,
    LegalEvidence,
    LegalEvidenceBundle,
    LegalEvidenceConflict,
    LegalEvidenceIssue,
    LegalEvidencePlanRequest,
    LegalEvidenceRelease,
    LegalEvidenceRelationPath,
    LegalEvidenceVersionSnapshot,
    LegalIssueCoverage,
    LegalRelation,
    LegalRetrievalUnit,
    LegalSearchCandidate,
)

PLANNER_VERSION = "adaptive-legal-evidence-planner-v4"
_EXACT_REFERENCE = re.compile(
    r"《\s*([^》]{1,200}?)\s*》\s*"
    r"(第[零〇一二三四五六七八九十百千万0-9]+条)?"
)
_KEYWORD_FRAGMENT = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]{2,24}")
_GENERIC_KEYWORD_TERMS = {
    "合同",
    "审查",
    "风险",
    "法律",
    "问题",
    "规则",
    "我方",
    "相对方",
    "甲方",
    "乙方",
}
from contract.legal_evidence.ports import (
    LegalEmbeddingProvider,
    LegalEvidenceRepository,
    LegalReranker,
)

_DETERMINISTIC_RELATIONS = {
    "CITES",
    "BASED_ON",
    "IMPLEMENTS",
    "INTERPRETS",
    "AMENDS",
    "REPEALS",
    "REPLACES",
    "SUPPLEMENTS",
    "EXCEPTION_TO",
    "INTERNAL_REF",
}
_RELATION_WEIGHT = {
    "EXCEPTION_TO": 1.0,
    "AMENDS": 1.0,
    "REPEALS": 1.0,
    "REPLACES": 1.0,
    "INTERPRETS": 0.95,
    "BASED_ON": 0.9,
    "IMPLEMENTS": 0.9,
    "SUPPLEMENTS": 0.9,
    "CITES": 0.85,
    "INTERNAL_REF": 0.85,
}


@dataclass(frozen=True, slots=True)
class LegalEvidencePlannerSafety:
    candidate_page_size: int = 24
    maximum_examined_candidates: int = 500
    maximum_rounds: int = 64
    maximum_wall_time_seconds: float = 5.0
    maximum_total_examined_candidates: int = 2000
    maximum_total_rounds: int = 1000
    maximum_total_wall_time_seconds: float = 30.0
    minimum_relevance: float = 0.05
    # Qwen3 rerank scores below 0.65 are not sufficiently calibrated to turn a
    # merely topical lexical/vector hit into legal evidence. Exact references,
    # verified relation edges and literal required-concept matches remain
    # independently admissible.
    minimum_reranker_relevance: float = 0.65
    minimum_semantic_coverage_score: float = 0.75
    relative_relevance_floor: float = 0.90
    maximum_neighbors_per_unit: int = 128


@dataclass(slots=True)
class _FrontierItem:
    unit: LegalRetrievalUnit
    score: float
    channels: set[str] = field(default_factory=set)
    relation_path: list[str] = field(default_factory=list)
    rerank_score: float | None = None


class AdaptiveLegalEvidencePlanner:
    """Create a traceable evidence bundle whose size follows issue coverage.

    There is deliberately no result-count setting in this Interface. The only
    caps are physical loop/time budgets protecting the process from malformed
    data or graph cycles.
    """

    def __init__(
        self,
        repository: LegalEvidenceRepository,
        *,
        embedding_provider: LegalEmbeddingProvider | None = None,
        reranker: LegalReranker | None = None,
        safety: LegalEvidencePlannerSafety | None = None,
    ) -> None:
        self.repository = repository
        self.embedding_provider = embedding_provider
        self.reranker = reranker
        self.safety = safety or LegalEvidencePlannerSafety()

    def plan(self, request: LegalEvidencePlanRequest) -> LegalEvidenceBundle:
        release = self.repository.active_release()
        if release is None:
            return self._empty_bundle(request, "NO_ACTIVE_RELEASE")

        started = time.monotonic()
        evidence_by_unit: dict[str, LegalEvidence] = {}
        relation_by_id: dict[str, LegalRelation] = {}
        coverage: list[LegalIssueCoverage] = []
        degraded: set[str] = set()
        examined = 0
        rounds = 0
        safety_reached = False
        rerank_diagnostics: list[str] = []
        issue_count = len(request.issues)
        candidate_base, candidate_remainder = divmod(
            self.safety.maximum_total_examined_candidates,
            issue_count,
        )
        round_base, round_remainder = divmod(
            self.safety.maximum_total_rounds,
            issue_count,
        )
        global_deadline = started + self.safety.maximum_total_wall_time_seconds

        # Issues are domain-level and independent. Run them together so the
        # planner adds one bounded pre-review phase instead of serial model
        # latency in front of the existing seven-domain execution.
        with ThreadPoolExecutor(max_workers=min(7, len(request.issues))) as executor:
            futures = [
                executor.submit(
                    self._solve_issue,
                    request=request,
                    release=release,
                    issue=issue,
                    maximum_examined_candidates=min(
                        self.safety.maximum_examined_candidates,
                        candidate_base + int(index < candidate_remainder),
                    ),
                    maximum_rounds=min(
                        self.safety.maximum_rounds,
                        round_base + int(index < round_remainder),
                    ),
                    global_deadline=global_deadline,
                )
                for index, issue in enumerate(request.issues)
            ]
            results = [future.result() for future in futures]

        for issue, result in zip(request.issues, results, strict=True):
            examined += result.examined
            rounds += result.rounds
            degraded.update(result.degraded_channels)
            safety_reached = safety_reached or result.safety_reached
            rerank_diagnostics.extend(result.rerank_diagnostics)
            relation_by_id.update({item.relation_id: item for item in result.relations})
            issue_evidence_ids: list[str] = []
            for item in result.evidence:
                existing = evidence_by_unit.get(item.unit.unit_id)
                if existing is None:
                    evidence_by_unit[item.unit.unit_id] = item
                    issue_evidence_ids.append(item.evidence_id)
                    continue
                merged = existing.model_copy(
                    update={
                        "issue_ids": sorted(set(existing.issue_ids) | set(item.issue_ids)),
                        "check_codes": sorted(
                            set(existing.check_codes) | set(item.check_codes)
                        ),
                        "matched_concepts": sorted(
                            set(existing.matched_concepts) | set(item.matched_concepts)
                        ),
                        "retrieval_channels": sorted(
                            set(existing.retrieval_channels) | set(item.retrieval_channels)
                        ),
                        "relevance_score": max(
                            existing.relevance_score, item.relevance_score
                        ),
                    }
                )
                evidence_by_unit[item.unit.unit_id] = merged
                issue_evidence_ids.append(merged.evidence_id)
            coverage.append(
                LegalIssueCoverage(
                    issue_id=issue.issue_id,
                    required_concepts=list(issue.required_concepts),
                    covered_concepts=result.covered_concepts,
                    evidence_ids=issue_evidence_ids,
                    complete=result.complete,
                    confidence=result.confidence,
                )
            )

        evidence = list(evidence_by_unit.values())
        unresolved = [item.issue_id for item in coverage if not item.complete]
        conflicts = self._conflicts(evidence, list(relation_by_id.values()))
        if conflicts:
            cautions_by_evidence: dict[str, list[str]] = {}
            for conflict in conflicts:
                for evidence_id in conflict.evidence_ids:
                    cautions_by_evidence.setdefault(evidence_id, []).append(
                        f"{conflict.conflict_type}:{conflict.reason}"
                    )
            evidence = [
                item.model_copy(
                    update={
                        "cautions": sorted(set(cautions_by_evidence.get(item.evidence_id, ())))
                    }
                )
                for item in evidence
            ]
        if not evidence:
            status = "NO_RELEVANT_EVIDENCE"
        elif degraded or conflicts or unresolved or safety_reached:
            status = "DEGRADED"
        else:
            status = "READY"
        if coverage and not unresolved and not conflicts:
            stop_reason = "COVERAGE_SATISFIED"
        elif safety_reached:
            stop_reason = "SAFETY_BUDGET_REACHED"
        else:
            stop_reason = "CANDIDATES_EXHAUSTED"
        relation_paths = self._relation_paths(evidence, relation_by_id)
        applicability_decisions = self._applicability_decisions(evidence, request)
        version_snapshot = LegalEvidenceVersionSnapshot(
            legal_release_id=release.source_release_id,
            legal_projection_version=release.projection_version,
            relation_extractor_version=release.relation_extractor_version,
            embedding_model_version=release.embedding_model_version,
            reranker_version=request.reranker_version,
            planner_version=request.planner_version,
            review_as_of_date=request.review_as_of_date,
            contract_date=request.contract_date,
        )
        payload = {
            "bundle_version": "1.0",
            "status": status,
            "release_id": release.release_id,
            "version_snapshot": version_snapshot.model_dump(mode="json"),
            "issues": [item.model_dump(mode="json") for item in request.issues],
            "evidence": [item.model_dump(mode="json") for item in evidence],
            "relations": [
                item.model_dump(mode="json")
                for item in sorted(relation_by_id.values(), key=lambda value: value.relation_id)
            ],
            "relation_paths": [
                item.model_dump(mode="json") for item in relation_paths
            ],
            "applicability_decisions": [
                item.model_dump(mode="json") for item in applicability_decisions
            ],
            "coverage": [item.model_dump(mode="json") for item in coverage],
            "unresolved_issue_ids": unresolved,
            "conflicts": [item.model_dump(mode="json") for item in conflicts],
            "degraded_channels": sorted(degraded),
            "rerank_applied": any(
                item.rerank_score is not None for item in evidence
            ),
            "rerank_diagnostics": rerank_diagnostics,
            "stop_reason": stop_reason,
            "examined_candidate_count": examined,
            "round_count": rounds,
        }
        return LegalEvidenceBundle(
            bundle_hash=self._hash(payload),
            **payload,
        )

    @dataclass(slots=True)
    class _IssueResult:
        evidence: list[LegalEvidence]
        relations: list[LegalRelation]
        covered_concepts: list[str]
        complete: bool
        confidence: float
        degraded_channels: set[str]
        examined: int
        rounds: int
        safety_reached: bool
        rerank_diagnostics: list[str]

    def _solve_issue(
        self,
        *,
        request: LegalEvidencePlanRequest,
        release: LegalEvidenceRelease,
        issue: LegalEvidenceIssue,
        maximum_examined_candidates: int,
        maximum_rounds: int,
        global_deadline: float,
    ) -> _IssueResult:
        frontier: dict[str, _FrontierItem] = {}
        selected: list[LegalEvidence] = []
        selected_ids: set[str] = set()
        relations: dict[str, LegalRelation] = {}
        covered: set[str] = set()
        degraded: set[str] = set()
        offset = 0
        examined = 0
        rounds = 0
        exhausted = False
        safety_reached = False
        complete = False
        rerank_diagnostics: list[str] = []
        query_vector: list[float] | None = None
        issue_started = time.monotonic()
        if (
            self.embedding_provider is not None
            and release.embedding_profile_id == self.embedding_provider.profile_id
        ):
            try:
                query_vector = self.embedding_provider.embed_query(issue.query)
            # Provider adapters can fail with transport, schema, or vendor
            # exceptions. Vector retrieval is deliberately fail-open.
            except Exception:  # noqa: BLE001
                degraded.add("VECTOR")
        elif self.embedding_provider is not None:
            degraded.add("VECTOR")

        while True:
            if self._safety_reached(
                issue_started=issue_started,
                issue_examined=examined,
                issue_rounds=rounds,
            ) or (
                examined >= maximum_examined_candidates
                or rounds >= maximum_rounds
                or time.monotonic() >= global_deadline
            ):
                safety_reached = True
                break
            if not frontier and not exhausted:
                page, page_degraded, has_more, page_diagnostics = self._retrieve_page(
                    request=request,
                    release=release,
                    issue=issue,
                    query_vector=query_vector,
                    offset=offset,
                )
                degraded.update(page_degraded)
                rerank_diagnostics.extend(page_diagnostics)
                offset += self.safety.candidate_page_size
                exhausted = not has_more
                for item in page:
                    self._merge_frontier_item(frontier, item)
            available = [item for key, item in frontier.items() if key not in selected_ids]
            if not available:
                if exhausted:
                    complete = self._coverage_sufficient(
                        issue, covered, selected
                    )
                    break
                continue
            current = max(
                available,
                key=lambda item: (
                    self._marginal_value(issue, item, covered),
                    item.score,
                    item.unit.unit_id,
                ),
            )
            current_marginal = self._marginal_value(issue, current, covered)
            best_selected = max(
                (item.relevance_score for item in selected),
                default=current_marginal,
            )
            dynamic_floor = max(
                self.safety.minimum_relevance,
                best_selected * self.safety.relative_relevance_floor,
            )
            relation_types = {
                relations[relation_id].relation_type
                for relation_id in current.relation_path
                if relation_id in relations
            }
            carries_legal_consequence = bool(
                relation_types
                & {"AMENDS", "REPEALS", "REPLACES", "EXCEPTION_TO"}
            )
            adds_concepts = bool(
                set(self._matched_concepts(issue, current.unit)) - covered
            )
            coverage_sufficient = self._coverage_sufficient(
                issue, covered, selected
            )
            if (
                not selected
                and current.rerank_score is not None
                and current.rerank_score < self.safety.minimum_reranker_relevance
                and not adds_concepts
                and not (current.channels & {"EXACT", "RELATION"})
            ):
                # The highest-ranked hybrid candidate failed calibrated
                # semantic relevance and added no literal legal concept.
                # Lower retrieval pages must not be scanned merely to fill a
                # quota: this is the valid zero-evidence outcome.
                break
            if (
                selected
                and not adds_concepts
                and not carries_legal_consequence
                and (
                    (
                        coverage_sufficient
                        and bool(current.relation_path)
                    )
                    or current_marginal < dynamic_floor
                )
            ):
                # The highest-value remaining node adds neither an uncovered
                # legal concept, a modifying/repealing/exception consequence,
                # nor enough relative relevance. A normal graph edge with no
                # concept gain stops immediately once coverage is complete;
                # otherwise a high-scoring citation fan-out can consume the
                # entire physical budget without adding legal information.
                # Independent initial-recall candidates still use the dynamic
                # score floor, while amendment/repeal/replacement/exception
                # consequences remain eligible for conflict detection.
                # Low marginal value is a normal adaptive stop even when an
                # issue remains explicitly unresolved.  It must not be
                # relabelled as a physical safety-budget failure, and it must
                # not manufacture coverage for a concept the corpus did not
                # support.
                complete = coverage_sufficient
                break
            frontier.pop(current.unit.unit_id, None)
            examined += 1
            rounds += 1
            if not self._applicable(current.unit, request, issue):
                continue
            matched = self._matched_concepts(issue, current.unit)
            marginal = self._marginal_value(issue, current, covered)
            if current.score < self.safety.minimum_relevance and not matched:
                continue
            if not self._reliable_candidate(current, matched):
                # Reciprocal-rank fusion is intentionally relative: the best
                # vector hit receives a high fused score even when every
                # document is semantically weak.  Do not turn that relative
                # rank into legal evidence unless it is corroborated by an
                # exact/lexical hit, a verified graph edge, a required legal
                # concept, or an independently calibrated reranker score.
                continue
            evidence = LegalEvidence(
                evidence_id=self._stable_id(
                    "legal-evidence",
                    {"issue_id": issue.issue_id, "unit_id": current.unit.unit_id},
                ),
                issue_ids=[issue.issue_id],
                # A domain retrieval result is only a candidate for its checks.
                # The versioned binder performs the narrower, fail-closed
                # evidence-to-check decision after retrieval.
                check_codes=[],
                unit=current.unit,
                relevance_score=max(0.0, min(1.0, marginal)),
                rerank_score=current.rerank_score,
                matched_concepts=matched,
                retrieval_channels=sorted(current.channels),
                relation_path=list(current.relation_path),
            )
            selected.append(evidence)
            selected_ids.add(current.unit.unit_id)
            covered.update(matched)

            try:
                neighbors = self.repository.relation_neighbors(
                    release_id=release.release_id,
                    unit_id=current.unit.unit_id,
                    limit=self.safety.maximum_neighbors_per_unit,
                )
            # Relation projection is an optional enrichment channel.
            except Exception:  # noqa: BLE001
                neighbors = []
                degraded.add("RELATION")
            for relation, unit in neighbors:
                if (
                    relation.relation_type not in _DETERMINISTIC_RELATIONS
                    or relation.verification_status not in {"VERIFIED", "AUTO_VERIFIED"}
                ):
                    continue
                relations[relation.relation_id] = relation
                relation_score = (
                    current.score
                    * relation.confidence
                    * _RELATION_WEIGHT[relation.relation_type]
                )
                self._merge_frontier(
                    frontier,
                    LegalSearchCandidate(
                        unit=unit,
                        score=max(0.0, min(1.0, relation_score)),
                        channel="RELATION",
                    ),
                    relation_path=[*current.relation_path, relation.relation_id],
                )
            # Do not stop merely because a concept was seen. Continue through
            # high-value independent candidates and the verified relation
            # frontier; the marginal-value test above decides when coverage is
            # sufficient.

        confidence = max((item.relevance_score for item in selected), default=0.0)
        return self._IssueResult(
            evidence=selected,
            relations=sorted(relations.values(), key=lambda item: item.relation_id),
            covered_concepts=sorted(
                covered,
                key=lambda concept: issue.required_concepts.index(concept),
            ),
            complete=complete,
            confidence=confidence,
            degraded_channels=degraded,
            examined=examined,
            rounds=rounds,
            safety_reached=safety_reached,
            rerank_diagnostics=rerank_diagnostics,
        )

    def _retrieve_page(
        self,
        *,
        request: LegalEvidencePlanRequest,
        release: LegalEvidenceRelease,
        issue: LegalEvidenceIssue,
        query_vector: list[float] | None,
        offset: int,
    ) -> tuple[list[_FrontierItem], set[str], bool, list[str]]:
        limit = self.safety.candidate_page_size
        degraded: set[str] = set()
        exact: list[LegalSearchCandidate] = []
        references = [
            (match.group(1).strip(), match.group(2) or None)
            for match in _EXACT_REFERENCE.finditer(issue.query)
        ]
        if offset == 0 and references:
            try:
                exact = self.repository.exact_search(
                    release_id=release.release_id,
                    references=references,
                    jurisdiction=request.jurisdiction,
                    as_of_date=request.review_as_of_date.isoformat(),
                    limit=limit,
                )
            except Exception:  # noqa: BLE001
                degraded.add("EXACT")
        try:
            keyword = self.repository.keyword_search(
                release_id=release.release_id,
                query=self._keyword_query(issue),
                jurisdiction=request.jurisdiction,
                as_of_date=request.review_as_of_date.isoformat(),
                offset=offset,
                limit=limit,
            )
        # A broken keyword index must not break the seven-domain review.
        except Exception:  # noqa: BLE001
            keyword = []
            degraded.add("KEYWORD")
        vector: list[LegalSearchCandidate] = []
        if query_vector is not None and self.embedding_provider is not None:
            try:
                vector = self.repository.vector_search(
                    release_id=release.release_id,
                    query_vector=query_vector,
                    embedding_profile_id=self.embedding_provider.profile_id,
                    jurisdiction=request.jurisdiction,
                    as_of_date=request.review_as_of_date.isoformat(),
                    offset=offset,
                    limit=limit,
                )
            # Vector retrieval is optional and vendor exceptions are opaque.
            except Exception:  # noqa: BLE001
                degraded.add("VECTOR")
        fused = self._fuse(exact, keyword, vector, rank_offset=offset)
        diagnostics: list[str] = []
        if self.reranker is not None and fused:
            try:
                scores = self.reranker.rerank(
                    issue.query,
                    [item.unit for item in fused],
                )
                for item in fused:
                    item.rerank_score = scores[item.unit.unit_id]
                    item.score = scores[item.unit.unit_id]
                fused.sort(key=lambda item: (-item.score, item.unit.unit_id))
                diagnostics.append(f"RERANK_OK:{len(fused)}")
            # Reranking is optional; preserve the fused order on any adapter
            # failure and record the degraded channel in the bundle.
            except Exception as exc:  # noqa: BLE001
                degraded.add("RERANK")
                diagnostics.append(f"RERANK_DEGRADED:{type(exc).__name__}")
        return fused, degraded, len(keyword) == limit or len(vector) == limit, diagnostics

    @staticmethod
    def _keyword_query(issue: LegalEvidenceIssue) -> str:
        """Build a selective FTS query instead of indexing the full prompt.

        The full issue prompt contains parties, facts and repeated boilerplate.
        OR-ing all of those tokens can match most of a large legal corpus and
        turn one retrieval page into a table-wide ranking operation.  Legal
        concepts and evidence needs are the authoritative lexical intent; the
        complete issue remains available to vector retrieval and reranking.
        """

        values = [*issue.required_concepts, *issue.evidence_need]
        if issue.contract_object and issue.contract_object.upper() != "AUTO":
            values.append(issue.contract_object)
        terms: list[str] = []
        for value in values:
            normalized = re.sub(r"\s+", "", value).strip()
            if (
                2 <= len(normalized) <= 32
                and normalized not in _GENERIC_KEYWORD_TERMS
                and normalized not in terms
            ):
                terms.append(normalized)
        if not terms:
            for value in _KEYWORD_FRAGMENT.findall(issue.question):
                if value not in _GENERIC_KEYWORD_TERMS and value not in terms:
                    terms.append(value)
                if len(terms) >= 16:
                    break
        return " ".join(terms[:16]) or issue.query[:256]

    @staticmethod
    def _fuse(
        exact: list[LegalSearchCandidate],
        keyword: list[LegalSearchCandidate],
        vector: list[LegalSearchCandidate],
        *,
        rank_offset: int = 0,
    ) -> list[_FrontierItem]:
        by_id: dict[str, _FrontierItem] = {}
        active_channel_count = int(bool(exact)) + int(bool(keyword)) + int(bool(vector))
        channel_peaks = {
            "EXACT": max((item.score for item in exact), default=1.0),
            "KEYWORD": max((item.score for item in keyword), default=1.0),
            "VECTOR": max((item.score for item in vector), default=1.0),
        }
        normalized_relevance: dict[str, float] = {}
        for channel_results in (exact, keyword, vector):
            for rank, candidate in enumerate(channel_results, start=1):
                contribution = 1.0 / (60 + rank_offset + rank)
                peak = max(channel_peaks[candidate.channel], 1e-12)
                normalized_relevance[candidate.unit.unit_id] = (
                    normalized_relevance.get(candidate.unit.unit_id, 0.0)
                    + candidate.score / peak
                )
                current = by_id.get(candidate.unit.unit_id)
                if current is None:
                    by_id[candidate.unit.unit_id] = _FrontierItem(
                        unit=candidate.unit,
                        score=contribution,
                        channels={candidate.channel},
                    )
                else:
                    current.score += contribution
                    current.channels.add(candidate.channel)
        theoretical_peak = max(1, active_channel_count) / 61
        return [
            _FrontierItem(
                unit=item.unit,
                score=max(
                    0.0,
                    min(
                        1.0,
                        0.5 * (item.score / theoretical_peak)
                        + 0.5
                        * (
                            normalized_relevance[item.unit.unit_id]
                            / max(1, active_channel_count)
                        ),
                    ),
                ),
                channels=set(item.channels),
            )
            for item in sorted(by_id.values(), key=lambda value: (-value.score, value.unit.unit_id))
        ]

    @staticmethod
    def _merge_frontier_item(
        frontier: dict[str, _FrontierItem], candidate: _FrontierItem
    ) -> None:
        existing = frontier.get(candidate.unit.unit_id)
        if existing is None:
            frontier[candidate.unit.unit_id] = _FrontierItem(
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

    @staticmethod
    def _merge_frontier(
        frontier: dict[str, _FrontierItem],
        candidate: LegalSearchCandidate,
        *,
        relation_path: list[str] | None = None,
    ) -> None:
        existing = frontier.get(candidate.unit.unit_id)
        if existing is None:
            frontier[candidate.unit.unit_id] = _FrontierItem(
                unit=candidate.unit,
                score=candidate.score,
                channels={candidate.channel},
                relation_path=list(relation_path or ()),
            )
            return
        existing.score = max(existing.score, candidate.score)
        existing.channels.add(candidate.channel)
        if relation_path and (
            not existing.relation_path or len(relation_path) < len(existing.relation_path)
        ):
            existing.relation_path = list(relation_path)

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"\s+", "", value).casefold()

    def _matched_concepts(
        self, issue: LegalEvidenceIssue, unit: LegalRetrievalUnit
    ) -> list[str]:
        haystack = self._normalize(" ".join((unit.title, *unit.heading_path, unit.content)))
        return [
            concept
            for concept in issue.required_concepts
            if self._normalize(concept) in haystack
        ]

    def _marginal_value(
        self,
        issue: LegalEvidenceIssue,
        item: _FrontierItem,
        covered: set[str],
    ) -> float:
        matched = set(self._matched_concepts(issue, item.unit))
        new_count = len(matched - covered)
        concept_gain = (
            new_count / len(issue.required_concepts) if issue.required_concepts else 0.25
        )
        authority_factor = 1.0 if item.unit.metadata_verification_status == "VERIFIED" else 0.9
        return min(1.0, item.score * authority_factor + concept_gain)

    def _coverage_sufficient(
        self,
        issue: LegalEvidenceIssue,
        covered: set[str],
        selected: list[LegalEvidence],
    ) -> bool:
        if issue.required_concepts:
            if set(issue.required_concepts).issubset(covered):
                return True
        elif selected:
            return True
        # Literal matching cannot capture every Chinese legal paraphrase. A
        # calibrated reranker may establish semantic coverage, but only when
        # another retrieval channel, an exact hit, a verified relation path,
        # or at least one literal concept corroborates it.
        return any(
            item.rerank_score is not None
            and item.rerank_score >= self.safety.minimum_semantic_coverage_score
            and (
                len(item.retrieval_channels) >= 2
                or "EXACT" in item.retrieval_channels
                or bool(item.relation_path)
                or bool(item.matched_concepts)
            )
            for item in selected
        )

    def _reliable_candidate(
        self,
        item: _FrontierItem,
        matched_concepts: list[str],
    ) -> bool:
        if matched_concepts:
            return True
        if item.channels & {"EXACT", "RELATION"}:
            return True
        if item.rerank_score is not None:
            return item.rerank_score >= self.safety.minimum_reranker_relevance
        # Keep lexical retrieval usable when no reranker is configured or the
        # optional rerank channel degraded. When reranking did run, however,
        # its calibrated rejection threshold must also apply to keyword hits;
        # otherwise an unrelated common word can manufacture legal evidence.
        return "KEYWORD" in item.channels

    @staticmethod
    def _applicable(
        unit: LegalRetrievalUnit,
        request: LegalEvidencePlanRequest,
        issue: LegalEvidenceIssue,
    ) -> bool:
        requested = (request.jurisdiction or "").strip().upper()
        candidate = (unit.jurisdiction or "").strip().upper()
        if requested:
            if not candidate:
                return False
            if not (requested == candidate or requested.startswith(candidate + "-")):
                return False
        # Unknown dates/status are allowed as unverified evidence. They must not
        # be invented from filenames. Only verified temporal metadata excludes.
        if unit.metadata_verification_status == "VERIFIED":
            if unit.validity_status in {"EXPIRED", "REPEALED"}:
                return False
            if unit.effective_from and unit.effective_from > request.review_as_of_date:
                return False
            if unit.effective_to and unit.effective_to < request.review_as_of_date:
                return False
            if (
                issue.domain == "formation_validity_authority"
                and request.contract_date is not None
                and unit.effective_from is not None
                and unit.effective_from > request.contract_date
            ):
                return False
        return unit.metadata_verification_status != "REJECTED"

    def _safety_reached(
        self,
        *,
        issue_started: float,
        issue_examined: int,
        issue_rounds: int,
    ) -> bool:
        return (
            issue_examined >= self.safety.maximum_examined_candidates
            or issue_rounds >= self.safety.maximum_rounds
            or time.monotonic() - issue_started >= self.safety.maximum_wall_time_seconds
        )

    def _empty_bundle(
        self,
        request: LegalEvidencePlanRequest,
        status: str,
    ) -> LegalEvidenceBundle:
        coverage = [
            LegalIssueCoverage(
                issue_id=issue.issue_id,
                required_concepts=list(issue.required_concepts),
                covered_concepts=[],
                evidence_ids=[],
                complete=False,
                confidence=0,
            )
            for issue in request.issues
        ]
        payload = {
            "bundle_version": "1.0",
            "status": status,
            "release_id": None,
            "version_snapshot": None,
            "issues": [item.model_dump(mode="json") for item in request.issues],
            "evidence": [],
            "relations": [],
            "relation_paths": [],
            "applicability_decisions": [],
            "coverage": [item.model_dump(mode="json") for item in coverage],
            "unresolved_issue_ids": [item.issue_id for item in request.issues],
            "conflicts": [],
            "degraded_channels": [],
            "rerank_applied": False,
            "rerank_diagnostics": [],
            "stop_reason": "NO_ACTIVE_RELEASE",
            "examined_candidate_count": 0,
            "round_count": 0,
        }
        return LegalEvidenceBundle(bundle_hash=self._hash(payload), **payload)

    @staticmethod
    def _relation_paths(
        evidence: list[LegalEvidence],
        relations: dict[str, LegalRelation],
    ) -> list[LegalEvidenceRelationPath]:
        paths: list[LegalEvidenceRelationPath] = []
        for item in evidence:
            if not item.relation_path:
                continue
            first = relations.get(item.relation_path[0])
            if first is None:
                continue
            for issue_id in item.issue_ids:
                paths.append(
                    LegalEvidenceRelationPath(
                        issue_id=issue_id,
                        evidence_id=item.evidence_id,
                        source_unit_id=first.source_unit_id,
                        target_unit_id=item.unit.unit_id,
                        relation_ids=list(item.relation_path),
                    )
                )
        return paths

    @staticmethod
    def _applicability_decisions(
        evidence: list[LegalEvidence],
        request: LegalEvidencePlanRequest,
    ) -> list[LegalApplicabilityDecision]:
        decisions: list[LegalApplicabilityDecision] = []
        for item in evidence:
            unit = item.unit
            reasons: list[str] = []
            jurisdiction_decision = "MATCH" if unit.jurisdiction else "UNKNOWN"
            if unit.metadata_verification_status != "VERIFIED":
                outcome = "UNKNOWN_METADATA"
                temporal_decision = "UNKNOWN"
                reasons.append("法规时效元数据尚未核验")
            elif (
                request.contract_date is not None
                and unit.effective_from is not None
                and unit.effective_from > request.contract_date
            ):
                outcome = "REVIEW_DATE_ONLY"
                temporal_decision = "NOT_EFFECTIVE_AT_CONTRACT_DATE"
                reasons.append("法规生效日晚于合同日期，不得作为合同订立时依据")
            else:
                outcome = "APPLICABLE"
                temporal_decision = "MATCH" if unit.effective_from else "UNKNOWN"
                if unit.effective_from is None:
                    reasons.append("法规生效日期未知")
            for issue_id in item.issue_ids:
                decisions.append(
                    LegalApplicabilityDecision(
                        issue_id=issue_id,
                        evidence_id=item.evidence_id,
                        unit_id=unit.unit_id,
                        outcome=outcome,
                        jurisdiction_decision=jurisdiction_decision,
                        temporal_decision=temporal_decision,
                        review_as_of_date=request.review_as_of_date,
                        contract_date=request.contract_date,
                        reasons=reasons,
                    )
                )
        return decisions

    @staticmethod
    def _conflicts(
        evidence: list[LegalEvidence],
        relations: list[LegalRelation],
    ) -> list[LegalEvidenceConflict]:
        evidence_by_unit = {item.unit.unit_id: item for item in evidence}
        conflicts: list[LegalEvidenceConflict] = []
        for relation in relations:
            left = evidence_by_unit.get(relation.source_unit_id)
            right = evidence_by_unit.get(relation.target_unit_id)
            if left is None or right is None:
                continue
            evidence_ids = sorted({left.evidence_id, right.evidence_id})
            if relation.relation_type in {"REPEALS", "REPLACES", "AMENDS"}:
                conflicts.append(
                    LegalEvidenceConflict(
                        conflict_type="VALIDITY",
                        evidence_ids=evidence_ids,
                        reason=(
                            f"{relation.relation_type}关系存在，但当前版本/时点元数据"
                            "不足以确定优先适用依据"
                        ),
                    )
                )
            elif relation.relation_type == "EXCEPTION_TO":
                conflicts.append(
                    LegalEvidenceConflict(
                        conflict_type="RELATION",
                        evidence_ids=evidence_ids,
                        reason="一般规则与例外规则同时命中，需要在具体合同事实下消解",
                    )
                )
        verified_by_instrument: dict[str, list[LegalEvidence]] = {}
        for item in evidence:
            if item.unit.metadata_verification_status == "VERIFIED":
                verified_by_instrument.setdefault(item.unit.instrument_id, []).append(item)
        for values in verified_by_instrument.values():
            statuses = {item.unit.validity_status for item in values}
            version_ids = {item.unit.version_id for item in values}
            conflicting_statuses = len(statuses - {None}) > 1
            multiple_verified_active_versions = (
                len(version_ids) > 1 and statuses == {"ACTIVE"}
            )
            if conflicting_statuses or multiple_verified_active_versions:
                conflicts.append(
                    LegalEvidenceConflict(
                        conflict_type="APPLICABILITY",
                        evidence_ids=sorted(item.evidence_id for item in values),
                        reason="同一法规的已核验版本元数据显示互斥有效状态或多个现行版本",
                    )
                )
        return conflicts

    @staticmethod
    def _stable_id(prefix: str, payload: object) -> str:
        return f"{prefix}-" + hashlib.sha256(
            canonical_json(payload).encode("utf-8")
        ).hexdigest()[:32]

    @staticmethod
    def _hash(payload: object) -> str:
        return "sha256:" + hashlib.sha256(
            canonical_json(payload).encode("utf-8")
        ).hexdigest()
