"""Profile registry and fail-closed execution boundary for evidence planning."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Any, Generic

from contract.evidence_planning.ports import BundleT, EvidenceBinder, EvidencePlanner, RequestT


class EvidencePlanningProfileNotFound(KeyError):
    pass


class EvidencePlanningProfileDisabled(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EvidencePlanningProfile(Generic[RequestT, BundleT]):
    """One replaceable domain implementation behind the generic engine."""

    profile_id: str
    planner: EvidencePlanner[RequestT, BundleT]
    binder: EvidenceBinder[BundleT]
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.profile_id.strip():
            raise ValueError("Evidence planning profile_id must not be blank")


class EvidencePlanningEngine:
    """Run named evidence domains while keeping their schemas isolated.

    The engine is intentionally smaller than a domain planner. Retrieval,
    applicability, graph semantics and stop policies remain owned by the
    registered profile.  What is shared is the lifecycle boundary: select the
    explicitly named profile, reject disabled/unknown domains, plan, then bind.
    This lets a rule-library profile run beside the legal profile without
    changing or weakening the latter's public contract.
    """

    def __init__(self) -> None:
        self._profiles: dict[str, EvidencePlanningProfile[Any, Any]] = {}
        self._lock = RLock()

    def register(self, profile: EvidencePlanningProfile[Any, Any]) -> None:
        profile_id = profile.profile_id.strip()
        with self._lock:
            if profile_id in self._profiles:
                raise ValueError(f"Evidence planning profile already registered: {profile_id}")
            self._profiles[profile_id] = profile

    def execute(self, profile_id: str, request: RequestT) -> BundleT:
        with self._lock:
            profile = self._profiles.get(profile_id)
        if profile is None:
            raise EvidencePlanningProfileNotFound(profile_id)
        if not profile.enabled:
            raise EvidencePlanningProfileDisabled(profile_id)
        # Deliberately bind only after a complete plan. A binder is the policy
        # gate between topical evidence and executable review checks.
        return profile.binder.bind(profile.planner.plan(request))  # type: ignore[return-value]

    def profile_ids(self, *, include_disabled: bool = False) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                sorted(
                    profile_id
                    for profile_id, profile in self._profiles.items()
                    if include_disabled or profile.enabled
                )
            )
