from __future__ import annotations

import hashlib
import re

from contract.application.idempotency import canonical_json
from contract.evidence_planning import (
    AdaptiveEvidenceSearch,
    AdaptiveExpansionPage,
    AdaptiveRelationExpansion,
    AdaptiveSearchBudget,
    AdaptiveSearchCandidate,
    AdaptiveSearchPage,
)
from contract.rule_evidence.models import (
    ReviewRuleSnapshot,
    RuleEvidence,
    RuleEvidenceBundle,
    RuleEvidenceIssue,
    RuleEvidencePlanRequest,
    RuleIssueCoverage,
    RuleLibraryRelation,
)


_CONSEQUENCE_RELATIONS = {"EXCEPTION_TO", "CONFLICTS_WITH", "SUPERSEDES"}


class AdaptiveRuleEvidencePlanner:
    """Deterministic first profile connecting ``biz_review_rule`` snapshots.

    It deliberately makes no model calls. The first integration establishes a
    versioned, applicable and traceable rule-evidence path; vector projection
    can be added later behind the same profile without changing consumers.
    """

    def __init__(self, *, maximum_candidates: int = 500, maximum_seconds: float = 2.0,
                 check_policy=None) -> None:
        self.maximum_candidates = maximum_candidates
        self.maximum_seconds = maximum_seconds
        self.check_policy = check_policy

    def plan(self, request: RuleEvidencePlanRequest) -> RuleEvidenceBundle:
        evidence_by_rule: dict[str, RuleEvidence] = {}
        relation_by_id: dict[str, RuleLibraryRelation] = {}
        coverage: list[RuleIssueCoverage] = []
        examined = 0
        rounds = 0
        safety_reached = False

        applicable_rules = [
            rule for rule in request.rules if self._applicable(rule, request)
        ]
        rule_by_id = {rule.rule_id: rule for rule in applicable_rules}
        relations_by_source: dict[str, list[RuleLibraryRelation]] = {}
        for relation in request.relations:
            if relation.verification_status != "VERIFIED":
                continue
            if relation.source_rule_id not in rule_by_id or relation.target_rule_id not in rule_by_id:
                continue
            relations_by_source.setdefault(relation.source_rule_id, []).append(relation)

        for issue in request.issues:
            result = self._solve_issue(
                request=request,
                issue=issue,
                rules=applicable_rules,
                rule_by_id=rule_by_id,
                relations_by_source=relations_by_source,
            )
            examined += result.examined
            rounds += result.rounds
            safety_reached = safety_reached or result.safety_reached
            relation_by_id.update({item.relation_id: item for item in result.relations})
            evidence_ids: list[str] = []
            for item in result.evidence:
                existing = evidence_by_rule.get(item.rule.rule_id)
                if existing is None:
                    evidence_by_rule[item.rule.rule_id] = item
                    evidence_ids.append(item.evidence_id)
                    continue
                merged = existing.model_copy(
                    update={
                        "issue_ids": sorted(set(existing.issue_ids) | set(item.issue_ids)),
                        "matched_concepts": sorted(
                            set(existing.matched_concepts) | set(item.matched_concepts)
                        ),
                        "retrieval_channels": sorted(
                            set(existing.retrieval_channels) | set(item.retrieval_channels)
                        ),
                        "relevance_score": max(existing.relevance_score, item.relevance_score),
                    }
                )
                evidence_by_rule[item.rule.rule_id] = merged
                evidence_ids.append(merged.evidence_id)
            coverage.append(
                RuleIssueCoverage(
                    issue_id=issue.issue_id,
                    required_concepts=issue.required_concepts,
                    covered_concepts=sorted(
                        result.covered_concepts,
                        key=lambda value: issue.required_concepts.index(value),
                    ),
                    evidence_ids=evidence_ids,
                    complete=result.complete,
                )
            )

        evidence = list(evidence_by_rule.values())
        unresolved = [item.issue_id for item in coverage if not item.complete]
        status = (
            "NO_RELEVANT_EVIDENCE"
            if not evidence
            else "DEGRADED"
            if unresolved or safety_reached
            else "READY"
        )
        stop_reason = (
            "COVERAGE_SATISFIED"
            if coverage and not unresolved
            else "SAFETY_BUDGET_REACHED"
            if safety_reached
            else "CANDIDATES_EXHAUSTED"
        )
        payload = {
            "bundle_version": "1.0",
            "source_version": request.source_version,
            "snapshot_hash": request.snapshot_hash,
            "planner_version": request.planner_version,
            "review_standard": request.review_standard,
            "preview_only": request.preview_pending,
            "status": status,
            "issues": [item.model_dump(mode="json") for item in request.issues],
            "evidence": [item.model_dump(mode="json") for item in evidence],
            "relations": [
                item.model_dump(mode="json")
                for item in sorted(relation_by_id.values(), key=lambda value: value.relation_id)
            ],
            "coverage": [item.model_dump(mode="json") for item in coverage],
            "unresolved_issue_ids": unresolved,
            "stop_reason": stop_reason,
            "examined_candidate_count": examined,
            "round_count": rounds,
        }
        return RuleEvidenceBundle(bundle_hash=self._hash(payload), **payload)

    def _solve_issue(
        self,
        *,
        request: RuleEvidencePlanRequest,
        issue: RuleEvidenceIssue,
        rules: list[ReviewRuleSnapshot],
        rule_by_id: dict[str, ReviewRuleSnapshot],
        relations_by_source: dict[str, list[RuleLibraryRelation]],
    ):
        snapshot_hash = request.snapshot_hash
        ranked = sorted(
            (
                AdaptiveSearchCandidate(
                    unit=rule,
                    score=self._score(issue, rule),
                    channels={
                        "EXACT"
                        if self._exact(issue, rule)
                        else "KEYWORD"
                    },
                )
                for rule in rules
                if self.check_policy is None or self.check_policy(issue, rule)
            ),
            key=lambda item: (-item.score, item.unit.rule_id),
        )
        ranked = [item for item in ranked if item.score > 0]

        def fetch_page(offset: int) -> AdaptiveSearchPage[ReviewRuleSnapshot]:
            page = ranked[offset : offset + 64]
            return AdaptiveSearchPage(
                candidates=tuple(page),
                has_more=offset + 64 < len(ranked),
            )

        def expand(
            current: AdaptiveSearchCandidate[ReviewRuleSnapshot],
        ) -> AdaptiveExpansionPage[ReviewRuleSnapshot, RuleLibraryRelation]:
            values = []
            for relation in relations_by_source.get(current.unit.rule_id, ()):
                target = rule_by_id[relation.target_rule_id]
                values.append(
                    AdaptiveRelationExpansion(
                        relation_id=relation.relation_id,
                        relation=relation,
                        unit=target,
                        score=current.score * relation.confidence,
                        carries_consequence=relation.relation_type in _CONSEQUENCE_RELATIONS,
                    )
                )
            return AdaptiveExpansionPage(neighbors=tuple(values))

        search = AdaptiveEvidenceSearch[
            ReviewRuleSnapshot,
            RuleEvidence,
            RuleLibraryRelation,
        ](
            budget=AdaptiveSearchBudget(
                candidate_page_size=64,
                maximum_examined_candidates=self.maximum_candidates,
                maximum_rounds=self.maximum_candidates,
                maximum_wall_time_seconds=self.maximum_seconds,
                minimum_relevance=0.08,
                relative_relevance_floor=0.80,
            ),
            unit_id=lambda rule: rule.rule_id,
            fetch_page=fetch_page,
            matched_concepts=lambda rule: self._matched_concepts(issue, rule),
            marginal_value=lambda item, covered: min(
                1.0,
                item.score
                + (
                    len(set(self._matched_concepts(issue, item.unit)) - covered)
                    / max(1, len(issue.required_concepts))
                ),
            ),
            coverage_sufficient=lambda covered, selected: (
                set(issue.required_concepts).issubset(covered)
                if issue.required_concepts
                else bool(selected)
            ),
            applicable=lambda rule: self._applicable(rule, request),
            reliable=lambda item, matched: bool(
                matched or "EXACT" in item.channels or item.score >= 0.35
            ),
            build_evidence=lambda item, matched, marginal: RuleEvidence(
                evidence_id=self._stable_id(
                    "rule-evidence",
                    {"snapshot": snapshot_hash, "rule_id": item.unit.rule_id},
                ),
                issue_ids=[issue.issue_id],
                check_codes=[],
                rule=item.unit,
                # Coverage novelty orders traversal; it is not retrieval relevance.
                # Persisting the novelty bonus inflated the relative floor to .8,
                # suppressing a second equally relevant rule on the same topic.
                relevance_score=max(0.0, min(1.0, item.score)),
                matched_concepts=matched,
                retrieval_channels=sorted(item.channels),
                relation_path=list(item.relation_path),
            ),
            evidence_score=lambda item: item.relevance_score,
            expand=expand,
            relation_id=lambda relation: relation.relation_id,
        )
        return search.run()

    @classmethod
    def _score(cls, issue: RuleEvidenceIssue, rule: ReviewRuleSnapshot) -> float:
        if cls._exact(issue, rule):
            return 1.0
        matched = cls._matched_concepts(issue, rule)
        concept_score = len(matched) / max(1, len(issue.required_concepts))
        query_bigrams = cls._bigrams(" ".join((issue.query, *issue.facts)))
        rule_bigrams = cls._bigrams(
            " ".join(
                (
                    rule.review_direction,
                    rule.name,
                    rule.content,
                    rule.review_method,
                    rule.reference_basis or "",
                )
            )
        )
        overlap = len(query_bigrams & rule_bigrams) / max(1, len(query_bigrams))
        if not matched and len(query_bigrams & rule_bigrams) < 2:
            return 0.0
        return min(0.99, 0.65 * concept_score + 0.35 * overlap)

    @classmethod
    def _matched_concepts(
        cls,
        issue: RuleEvidenceIssue,
        rule: ReviewRuleSnapshot,
    ) -> list[str]:
        haystack = cls._normalize(
            " ".join(
                (
                    rule.review_direction,
                    rule.name,
                    rule.content,
                    rule.review_method,
                    rule.reference_basis or "",
                )
            )
        )
        return [
            concept for concept in issue.required_concepts if cls._normalize(concept) in haystack
        ]

    @classmethod
    def _exact(cls, issue: RuleEvidenceIssue, rule: ReviewRuleSnapshot) -> bool:
        query = cls._normalize(issue.query)
        return any(
            cls._normalize(value) in query
            for value in (rule.code, rule.name, rule.reference_basis or "")
            if len(cls._normalize(value)) >= 2
        )

    @classmethod
    def _applicable(
        cls,
        rule: ReviewRuleSnapshot,
        request: RuleEvidencePlanRequest,
    ) -> bool:
        allowed_statuses = {"active", "pending"} if request.preview_pending else {"active"}
        if rule.status.strip().casefold() not in allowed_statuses:
            return False
        if rule.review_standard.strip().casefold() != request.review_standard:
            return False
        if rule.tenant_id not in {"0", request.tenant_id}:
            return False
        if rule.effective_from and rule.effective_from > request.review_as_of_date:
            return False
        if rule.effective_to and rule.effective_to < request.review_as_of_date:
            return False
        requested_jurisdiction = (request.jurisdiction or "").strip().upper()
        rule_jurisdiction = (rule.jurisdiction or "").strip().upper()
        if rule_jurisdiction and (
            not requested_jurisdiction
            or not (
                requested_jurisdiction == rule_jurisdiction
                or requested_jurisdiction.startswith(rule_jurisdiction + "-")
            )
        ):
            return False
        # Literal A/B and a business role are separate dimensions, not substitutes.
        business_roles = set(getattr(request, "business_roles", []) or [])
        if getattr(request, "business_role", None):
            business_roles.add(request.business_role)
        if not (cls._stance_matches(rule.party_stance, request.perspective)
                or any(cls._stance_matches(rule.party_stance, role) for role in business_roles)):
            return False
        if rule.rule_type.strip().casefold() == "dedicated":
            requested_types = {cls._normalize(value) for value in
                               [request.contract_type, *request.contract_type_aliases] if value.strip()}
            candidates = [rule.contract_type_id or "", *rule.contract_type_path]
            if not any(
                value
                and cls._normalize(value) in requested_types
                for value in candidates
            ):
                return False
        return True

    @classmethod
    def _stance_matches(cls, value: str | None, perspective: str) -> bool:
        stance = cls._normalize(value or "")
        if stance in {"", "neutral", "all", "any", "both", "双方", "中立"}:
            return True
        expected = cls._normalize(perspective)
        aliases = {
            "partya": {"partya", "甲方", "甲方视角"},
            "partyb": {"partyb", "乙方", "乙方视角"},
        }
        accepted = next((group for group in aliases.values() if expected in group), {expected})
        return stance in accepted

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"[\s_-]+", "", value).casefold()

    @classmethod
    def _bigrams(cls, value: str) -> set[str]:
        normalized = cls._normalize(value)
        return {normalized[index : index + 2] for index in range(max(0, len(normalized) - 1))}

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
