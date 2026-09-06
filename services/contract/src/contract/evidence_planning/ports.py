"""Small structural ports shared by every evidence-planning domain."""

from __future__ import annotations

from typing import Protocol, TypeVar


class EvidenceBundle(Protocol):
    """Minimum output contract required by the orchestration engine."""

    @property
    def usable(self) -> bool: ...


RequestT = TypeVar("RequestT")
BundleT = TypeVar("BundleT", bound=EvidenceBundle)


class EvidencePlanner(Protocol[RequestT, BundleT]):
    def plan(self, request: RequestT) -> BundleT: ...


class EvidenceBinder(Protocol[BundleT]):
    """Bind retrieved evidence to executable checks without changing its source."""

    @property
    def profile_version(self) -> str: ...

    def bind(self, bundle: BundleT) -> BundleT: ...
