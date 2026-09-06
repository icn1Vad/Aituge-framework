from dataclasses import dataclass

import pytest

from contract.evidence_planning import (
    EvidencePlanningEngine,
    EvidencePlanningProfile,
    EvidencePlanningProfileDisabled,
    EvidencePlanningProfileNotFound,
)


@dataclass
class _Bundle:
    steps: list[str]

    @property
    def usable(self) -> bool:
        return True


class _Planner:
    def plan(self, request: str) -> _Bundle:
        return _Bundle([f"plan:{request}"])


class _Binder:
    profile_version = "test-binding-v1"

    def bind(self, bundle: _Bundle) -> _Bundle:
        bundle.steps.append("bind")
        return bundle


def test_engine_runs_the_named_profile_then_its_policy_binder() -> None:
    engine = EvidencePlanningEngine()
    engine.register(
        EvidencePlanningProfile(profile_id="legal", planner=_Planner(), binder=_Binder())
    )

    assert engine.execute("legal", "request-1").steps == ["plan:request-1", "bind"]
    assert engine.profile_ids() == ("legal",)


def test_engine_never_falls_back_to_another_profile() -> None:
    engine = EvidencePlanningEngine()
    engine.register(
        EvidencePlanningProfile(
            profile_id="rule-library",
            planner=_Planner(),
            binder=_Binder(),
            enabled=False,
        )
    )

    with pytest.raises(EvidencePlanningProfileDisabled):
        engine.execute("rule-library", "request")
    with pytest.raises(EvidencePlanningProfileNotFound):
        engine.execute("legal", "request")
    assert engine.profile_ids() == ()
    assert engine.profile_ids(include_disabled=True) == ("rule-library",)


def test_duplicate_profile_registration_is_rejected() -> None:
    engine = EvidencePlanningEngine()
    profile = EvidencePlanningProfile(
        profile_id="legal", planner=_Planner(), binder=_Binder()
    )
    engine.register(profile)
    with pytest.raises(ValueError, match="already registered"):
        engine.register(profile)
