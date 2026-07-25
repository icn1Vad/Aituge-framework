"""Deterministic contract-party role mapping for internal risk review.

The review perspective identifies which contractual side is ``our_party``.
Code must never infer PARTY_A/PARTY_B from argument order or assume that
``our_party`` is always 甲方.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


PartyPerspective = Literal["PARTY_A", "PARTY_B"]
ContractRole = Literal["甲方", "乙方"]


@dataclass(frozen=True, slots=True)
class ContractPartyRoles:
    perspective: PartyPerspective
    party_a_name: str
    party_b_name: str
    our_role: ContractRole
    counterparty_role: ContractRole
    our_party: str
    counterparty: str

    def name_for_role(self, role: ContractRole) -> str:
        return self.party_a_name if role == "甲方" else self.party_b_name

    def role_for_name(self, name: str) -> ContractRole | None:
        normalized = name.strip()
        if normalized == self.party_a_name:
            return "甲方"
        if normalized == self.party_b_name:
            return "乙方"
        return None

    def aliases_for_our_party(self) -> tuple[str, str]:
        return self.our_role, self.our_party

    def aliases_for_counterparty(self) -> tuple[str, str]:
        return self.counterparty_role, self.counterparty


def contract_party_roles(
    *,
    perspective: PartyPerspective | str,
    our_party: str,
    counterparty: str,
) -> ContractPartyRoles:
    actual = str(getattr(perspective, "value", perspective))
    if actual == "PARTY_A":
        return ContractPartyRoles(
            perspective="PARTY_A",
            party_a_name=our_party,
            party_b_name=counterparty,
            our_role="甲方",
            counterparty_role="乙方",
            our_party=our_party,
            counterparty=counterparty,
        )
    if actual == "PARTY_B":
        return ContractPartyRoles(
            perspective="PARTY_B",
            party_a_name=counterparty,
            party_b_name=our_party,
            our_role="乙方",
            counterparty_role="甲方",
            our_party=our_party,
            counterparty=counterparty,
        )
    raise ValueError(f"unsupported contract perspective: {actual}")


def text_names_role(text: str, aliases: tuple[str, str]) -> bool:
    return any(alias and alias in text for alias in aliases)
