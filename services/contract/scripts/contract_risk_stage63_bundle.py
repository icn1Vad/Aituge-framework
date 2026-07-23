#!/usr/bin/env python3
"""Stage 6.3 fixed-fixture domain and five-unit Bundle acceptance runner."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
for path in (
    ROOT,
    ROOT / "backend",
    ROOT / "backend" / "single-agent",
    ROOT / "services" / "contract" / "src",
    ROOT / "services" / "contract" / "tests",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from common.system_constants import DEFAULT_TENANT_ID
from common.tokenization import estimate_tokens_in_text
from contract.risk.plan_builder import RiskReviewPlanBuilder
from risk_fixture_loader import EXPECTED_HASHES, load_fixed_risk_plan_input
from services.contract.capabilities.risk_review_bundle import (
    BASE_UNIT_IDS,
    BaseBundleExecutionError,
    GENERIC_UNIT_IDS,
    GenericAttemptArtifact,
    GenericBaseDirectReviewer,
    _GENERIC_SYSTEM_PROMPT,
    _PO_CANDIDATE_SYSTEM_PROMPT,
    _build_generic_candidates,
    _canonical_risk_key,
    _generic_prompt,
    _materialize_po_candidate_decisions,
    _merge_unit_result,
    _parse_po_candidate_output,
    _po_evidence_catalog,
    bundle_duration_summary,
    execute_base_risk_review_bundle,
    generic_request_from_context,
)
from services.contract.capabilities.risk_review import DirectReviewError

FIXTURE_ID = "service-outsourcing-0829-v1"

FVA002_FIXED_EXPECTATION = {
    "assessment_type": "EXTERNAL_VERIFICATION_REQUIRED",
    "external_verification_required": True,
    "reason_code": "INSUFFICIENT_EVIDENCE",
    "finding_count": 0,
    "text_basis": [
        "A069：合同第11.2条约定双方加盖公章后生效。",
        "A070及签署页：只列明签订日期和双方公司盖章位，未出现签署人或代表人冲突。",
        "合同没有明确记载无授权、越权或把授权书作为成立/生效条件。",
    ],
    "external_materials": [
        "法定代表人证明",
        "授权委托书",
        "董事会或股东会批准",
        "营业执照",
        "内部审批材料",
    ],
}

PO_FIXED_CANDIDATE_ORACLE = {
    "risk-candidate-f23588a0aba5e1bf2a45244191e7507f": {
        "check_code": "PO-002",
        "candidate_type": "RIGHTS_OBLIGATIONS_IMBALANCE",
        "candidate_strength": "STRONG_SIGNAL",
        "primary_evidence_source_ids": [
            "risk-es-9ac678593347e2a0757f8434050799c3",
            "risk-es-be19e2b2b9aad2bf16d943667e710314",
            "risk-es-89ee83e3b83fd268dc26b2bfd1d71b92",
            "risk-es-5de5b471fffbf32db2e283e066535cae",
            "risk-es-37a4ee0ada74c10464e4b0665a4177a1",
            "risk-es-7c5f14eaf7aaaa41d11116e6737fa99c",
            "risk-es-b2a0e4e231449e11492970e89c98b21d",
        ],
        "allowed_counter_evidence_source_ids": [
            "risk-es-9ac678593347e2a0757f8434050799c3",
            "risk-es-be19e2b2b9aad2bf16d943667e710314",
            "risk-es-89ee83e3b83fd268dc26b2bfd1d71b92",
            "risk-es-5de5b471fffbf32db2e283e066535cae",
            "risk-es-37a4ee0ada74c10464e4b0665a4177a1",
            "risk-es-7c5f14eaf7aaaa41d11116e6737fa99c",
            "risk-es-b2a0e4e231449e11492970e89c98b21d",
        ],
        "severity_rule_id": "PO_UNILATERAL_CONTROL_V1",
    },
    "risk-candidate-d3ba1e8323ba6f8fca2814e624649fcd": {
        "check_code": "PO-004",
        "candidate_type": "QUALITY_STANDARD_UNMEASURABLE",
        "candidate_strength": "HARD_RULE",
        "primary_evidence_source_ids": [
            "risk-es-20a2874d258f6ff13a42aa5c671c4999",
            "risk-es-7a84e492eef27fd9d2eacfe063ba01a9",
            "risk-es-95245329bae1c6b65d253b6b9a0d52f0",
        ],
        "allowed_counter_evidence_source_ids": [],
        "severity_rule_id": "PO_QUALITY_STANDARD_V1",
    },
    "risk-candidate-8e72fb4be26cbb7ccf8b2809358e4b7f": {
        "check_code": "PO-004",
        "candidate_type": "ACCEPTANCE_MECHANISM_ABSENT",
        "candidate_strength": "HARD_RULE",
        "primary_evidence_source_ids": [
            "risk-as-d15e93b470bc5822079b2fb0bdeeaaca",
        ],
        "allowed_counter_evidence_source_ids": [],
        "severity_rule_id": "PO_ACCEPTANCE_ABSENT_V1",
    },
    "risk-candidate-a893dd5ab2e4715192d4166408f634b9": {
        "check_code": "PO-005",
        "candidate_type": "ASSIGNMENT_SUBCONTRACT_REVIEW",
        "candidate_strength": "SEMANTIC_REVIEW",
        "primary_evidence_source_ids": [
            "risk-es-474447ab75e9baa280de2cfc8531ffa2",
            "risk-es-be19e2b2b9aad2bf16d943667e710314",
        ],
        "allowed_counter_evidence_source_ids": [
            "risk-es-474447ab75e9baa280de2cfc8531ffa2",
            "risk-es-be19e2b2b9aad2bf16d943667e710314",
        ],
        "severity_rule_id": "PO_ASSIGNMENT_SUBCONTRACT_V1",
    },
    "risk-candidate-b3ee5f4a798cf39d6b13746cc00de138": {
        "check_code": "PO-006",
        "candidate_type": "CHANGE_CONTROL_REVIEW",
        "candidate_strength": "HARD_RULE",
        "primary_evidence_source_ids": [
            "risk-es-2cfa05ffc3bc0f28030e8b2fb0f23b19",
            "risk-es-9ac678593347e2a0757f8434050799c3",
            "risk-es-7cb56e90cf46d1e0a5cf3ac14281c50c",
        ],
        "allowed_counter_evidence_source_ids": [
            "risk-es-2cfa05ffc3bc0f28030e8b2fb0f23b19",
            "risk-es-9ac678593347e2a0757f8434050799c3",
            "risk-es-7cb56e90cf46d1e0a5cf3ac14281c50c",
        ],
        "severity_rule_id": "PO_CHANGE_CONTROL_V1",
    },
    "risk-candidate-3fb5689c020fc9f26905476ee01fd506": {
        "check_code": "PO-007",
        "candidate_type": "WARRANTY_SUPPORT_REVIEW",
        "candidate_strength": "STRONG_SIGNAL",
        "primary_evidence_source_ids": [
            "risk-es-8c5e6223f65e302ef5e0e69cd08fd222",
            "risk-es-29729522a5abf0ac43d97bc20f512fb0",
            "risk-es-90484045c38a16a8c2f78a547044c5aa",
            "risk-es-ab59e1ad647c2a4713408ffebc3a076a",
            "risk-es-375039c9952c8967c3feff00bf28b6e7",
            "risk-es-076468effb564ab877396fb0ed44fe22",
        ],
        "allowed_counter_evidence_source_ids": [
            "risk-es-8c5e6223f65e302ef5e0e69cd08fd222",
            "risk-es-29729522a5abf0ac43d97bc20f512fb0",
            "risk-es-90484045c38a16a8c2f78a547044c5aa",
            "risk-es-ab59e1ad647c2a4713408ffebc3a076a",
            "risk-es-375039c9952c8967c3feff00bf28b6e7",
            "risk-es-076468effb564ab877396fb0ed44fe22",
        ],
        "severity_rule_id": "PO_WARRANTY_SUPPORT_V1",
    },
}

PO_FIXED_CANDIDATE_DECISION_ORACLE = {
    "risk-candidate-f23588a0aba5e1bf2a45244191e7507f": {
        "allowed_supporting_evidence_source_ids": [
            "risk-es-7cb56e90cf46d1e0a5cf3ac14281c50c",
        ],
        "allowed_control_codes": [
            "LIMIT_UNILATERAL_CONTROL",
            "ADD_NOTICE_REQUIREMENT",
        ],
        "expected_verdict": "RISK",
        "deterministic_severity_factors": ["UNILATERAL_CONTROL"],
    },
    "risk-candidate-d3ba1e8323ba6f8fca2814e624649fcd": {
        "allowed_supporting_evidence_source_ids": [
            "risk-es-8c5e6223f65e302ef5e0e69cd08fd222",
        ],
        "allowed_control_codes": [
            "DEFINE_MEASURABLE_STANDARD",
            "ADD_FORMAL_ACCEPTANCE_PROCEDURE",
        ],
        "expected_verdict": "RISK",
        "deterministic_severity_factors": ["MISSING_CORE_MECHANISM"],
    },
    "risk-candidate-8e72fb4be26cbb7ccf8b2809358e4b7f": {
        "allowed_supporting_evidence_source_ids": [
            "risk-es-8c5e6223f65e302ef5e0e69cd08fd222",
            "risk-es-20a2874d258f6ff13a42aa5c671c4999",
            "risk-es-7a84e492eef27fd9d2eacfe063ba01a9",
            "risk-es-95245329bae1c6b65d253b6b9a0d52f0",
        ],
        "allowed_control_codes": [
            "DEFINE_MEASURABLE_STANDARD",
            "ADD_FORMAL_ACCEPTANCE_PROCEDURE",
            "DEFINE_ACCEPTANCE_PERIOD",
            "ADD_RECTIFICATION_AND_RETEST",
        ],
        "expected_verdict": "RISK",
        "deterministic_severity_factors": ["MISSING_CORE_MECHANISM"],
    },
    "risk-candidate-a893dd5ab2e4715192d4166408f634b9": {
        "allowed_supporting_evidence_source_ids": [
            "risk-es-bea3e6a5eb0810503297a3d00cef56cc",
            "risk-es-b77ebf64a562acbac5350bc76dff554a",
            "risk-es-68f8523487abfae960c89094c4dc3d54",
            "risk-es-4de7b26c60b50a418048d0974c585a48",
            "risk-es-e2557a1d10ae3df678ffbee0f43da5fa",
            "risk-es-333f806a1ceebe8da8fb812d26f99cef",
            "risk-es-858ac871f9a428ff746d00b0dec96fcf",
        ],
        "allowed_control_codes": [
            "ADD_NOTICE_REQUIREMENT",
            "ADD_CONFIDENTIALITY_GUARD",
        ],
        "expected_verdict": "NO_RISK",
        "deterministic_severity_factors": [],
    },
    "risk-candidate-b3ee5f4a798cf39d6b13746cc00de138": {
        "allowed_supporting_evidence_source_ids": [
            "risk-es-90484045c38a16a8c2f78a547044c5aa",
            "risk-es-ab59e1ad647c2a4713408ffebc3a076a",
            "risk-es-89ee83e3b83fd268dc26b2bfd1d71b92",
            "risk-es-b2a0e4e231449e11492970e89c98b21d",
        ],
        "allowed_control_codes": [
            "ADD_WRITTEN_CHANGE_PROCEDURE",
            "LINK_CHANGE_TO_FEE_AND_SCHEDULE",
            "LIMIT_UNILATERAL_CONTROL",
        ],
        "expected_verdict": "RISK",
        "deterministic_severity_factors": ["UNILATERAL_CONTROL"],
    },
    "risk-candidate-3fb5689c020fc9f26905476ee01fd506": {
        "allowed_supporting_evidence_source_ids": [],
        "allowed_control_codes": [
            "DEFINE_MEASURABLE_STANDARD",
            "ADD_RECTIFICATION_AND_RETEST",
            "ADD_NOTICE_REQUIREMENT",
        ],
        "expected_verdict": "RISK",
        "deterministic_severity_factors": [],
    },
}

PO_FIXED_ROOT_ORACLE = [
    {
        "source_candidate_ids": [
            "risk-candidate-59f89828e544a97fd6605f4ffae73d37",
            "risk-candidate-45e8c685f30dde843aa970b7677faf48",
        ],
        "root_type": "CORE_SCOPE_AND_DELIVERY_IMBALANCE",
        "root_severity_rule_id": "PO_SCOPE_DELIVERY_ROOT_V1",
        "risk_level": "HIGH",
    },
    {
        "source_candidate_ids": [
            "risk-candidate-f23588a0aba5e1bf2a45244191e7507f",
        ],
        "root_type": "RIGHTS_OBLIGATIONS_IMBALANCE",
        "root_severity_rule_id": "PO_UNILATERAL_CONTROL_V1",
        "risk_level": "MEDIUM",
    },
    {
        "source_candidate_ids": [
            "risk-candidate-d3ba1e8323ba6f8fca2814e624649fcd",
        ],
        "root_type": "QUALITY_STANDARD_UNMEASURABLE",
        "root_severity_rule_id": "PO_QUALITY_STANDARD_V1",
        "risk_level": "MEDIUM",
    },
    {
        "source_candidate_ids": [
            "risk-candidate-8e72fb4be26cbb7ccf8b2809358e4b7f",
        ],
        "root_type": "ACCEPTANCE_MECHANISM_ABSENT",
        "root_severity_rule_id": "PO_ACCEPTANCE_ABSENT_V1",
        "risk_level": "HIGH",
    },
    {
        "source_candidate_ids": [
            "risk-candidate-b3ee5f4a798cf39d6b13746cc00de138",
        ],
        "root_type": "CHANGE_CONTROL_REVIEW",
        "root_severity_rule_id": "PO_CHANGE_CONTROL_V1",
        "risk_level": "MEDIUM",
    },
    {
        "source_candidate_ids": [
            "risk-candidate-3fb5689c020fc9f26905476ee01fd506",
        ],
        "root_type": "WARRANTY_SUPPORT_REVIEW",
        "root_severity_rule_id": "PO_WARRANTY_SUPPORT_V1",
        "risk_level": "MEDIUM",
    },
]

PO_FIXED_FINAL_FINDING_ORACLE = {
    "finding_count": 6,
    "risk_levels": {
        "CORE_SCOPE_AND_DELIVERY_IMBALANCE": "HIGH",
        "RIGHTS_OBLIGATIONS_IMBALANCE": "MEDIUM",
        "QUALITY_STANDARD_UNMEASURABLE": "MEDIUM",
        "ACCEPTANCE_MECHANISM_ABSENT": "HIGH",
        "CHANGE_CONTROL_REVIEW": "MEDIUM",
        "WARRANTY_SUPPORT_REVIEW": "MEDIUM",
    },
}

ICD_FIXED_CANDIDATE_ORACLE = {
    "risk-candidate-deae1a711f1c0e62f32b0b103787e2f7": {
        "check_code": "ICD-004",
        "candidate_type": "CONFIDENTIALITY_PROTECTION_REVIEW",
        "expected_verdict": "NO_RISK",
        "primary_evidence_source_ids": [
            "risk-es-5066caa2620728097020c24b4545ba5f",
            "risk-es-82b91b4c24161302a465973d47c3bc01",
        ],
        "deterministic_severity_factors": [],
    },
    "risk-candidate-c196cdf44d5fd5ae9e283bae508778fb": {
        "check_code": "ICD-004",
        "candidate_type": "CONFIDENTIALITY_COMPLETENESS_ABSENT",
        "expected_verdict": "RISK",
        "primary_evidence_source_ids": [
            "risk-as-3b9679086a28da4efe9c2d78c23a09a2",
        ],
        "deterministic_severity_factors": ["MISSING_CORE_MECHANISM"],
    },
}

ICD_FIXED_ROOT_ORACLE = {
    "source_candidate_ids": [
        "risk-candidate-c196cdf44d5fd5ae9e283bae508778fb",
    ],
    "root_type": "CONFIDENTIALITY_COMPLETENESS_ABSENT",
    "root_severity_rule_id": (
        "ICD_CONFIDENTIALITY_COMPLETENESS_ABSENT_ROOT_V1"
    ),
    "risk_level": "MEDIUM",
    "finding_count": 1,
}

LRE_FIXED_CANDIDATE_ORACLE = {
    "risk-candidate-511da56611f96c390deb71be5ffda2e6": {
        "check_code": "LRE-001",
        "candidate_type": "BROAD_BREACH_TRIGGER_REVIEW",
        "expected_verdict": "RISK",
        "primary_evidence_source_ids": [
            "risk-es-40e3d874d33a7b864fe154f416b442ea",
        ],
        "deterministic_severity_factors": [],
    },
    "risk-candidate-44b755698d067232f0cbf8183ef74ac0": {
        "check_code": "LRE-004",
        "candidate_type": "OVERBROAD_INDEMNITY_REVIEW",
        "expected_verdict": "RISK",
        "primary_evidence_source_ids": [
            "risk-es-004ed223348f156f5c128bcfbd7448e8",
            "risk-es-40e3d874d33a7b864fe154f416b442ea",
        ],
        "deterministic_severity_factors": ["OVERBROAD_INDEMNITY"],
    },
    "risk-candidate-7812d49ef67c4aab35c1d44d1d8c94bb": {
        "check_code": "LRE-002",
        "candidate_type": "OVERBROAD_LOSS_SCOPE_REVIEW",
        "expected_verdict": "RISK",
        "primary_evidence_source_ids": [
            "risk-es-40e3d874d33a7b864fe154f416b442ea",
        ],
        "deterministic_severity_factors": ["INDIRECT_LOSS_EXPOSURE"],
    },
    "risk-candidate-086b7f97a3f3ac925e5c72fa3a6b2d42": {
        "check_code": "LRE-002",
        "candidate_type": "CUMULATIVE_REMEDIES_REVIEW",
        "expected_verdict": "RISK",
        "primary_evidence_source_ids": [
            "risk-es-21d41c08f7c9159085b835e5c3fc605e",
        ],
        "deterministic_severity_factors": ["CUMULATIVE_REMEDIES"],
    },
    "risk-candidate-60fd2fcc20fd2b36b11fec38ac3343bb": {
        "check_code": "LRE-003",
        "candidate_type": "LIABILITY_CAP_ABSENT",
        "expected_verdict": "RISK",
        "primary_evidence_source_ids": [
            "risk-es-40e3d874d33a7b864fe154f416b442ea",
            "risk-as-8d1152663f3a3bd96fcc81e5d3930833",
        ],
        "deterministic_severity_factors": [
            "UNLIMITED_LIABILITY",
            "MISSING_CORE_MECHANISM",
        ],
    },
    "risk-candidate-0412973ee3c6d56a9332aa16f6c0782a": {
        "check_code": "LRE-005",
        "candidate_type": "TERMINATION_RIGHTS_REVIEW",
        "expected_verdict": "NO_RISK",
        "primary_evidence_source_ids": [
            "risk-es-c636dc0b8999b3c274ad04d6d8a9184c",
            "risk-es-2c37942088fe4632747635e9a18df6f7",
            "risk-es-677a2200afac5ca0aa9b8a86e1c5296d",
            "risk-es-c85d98ec1c0e5609b3b5a8b77752f10a",
            "risk-es-31af359c823282997f33778e2d15e413",
            "risk-es-82ee8418f94862d2cd34507d6527040c",
            "risk-es-fc8419d77ee45cf1d4d2cce610b64635",
            "risk-es-a11807dfb2bc944bbf259aa506044175",
            "risk-es-f4a285b4c1f101d3eabe586e3e938001",
            "risk-es-360b7931a80fe685e19104febb97ba4c",
            "risk-es-ac421dc9a54625b7b1ac1eed0dc7b145",
            "risk-es-88049f95fe5f916b591fff8f5bcb2e5a",
            "risk-es-468f8d5ed76405f4d0078a547f6eca90",
        ],
        "deterministic_severity_factors": [],
    },
    "risk-candidate-631308ca37bed4f274d9daee328a6929": {
        "check_code": "LRE-007",
        "candidate_type": "FORCE_MAJEURE_MECHANISM_ABSENT",
        "expected_verdict": "RISK",
        "primary_evidence_source_ids": [
            "risk-as-9c4de9dc855e5b4e118c2f8768795cab",
        ],
        "deterministic_severity_factors": [
            "MISSING_CORE_MECHANISM",
            "NO_EFFECTIVE_REMEDY",
        ],
    },
    "risk-candidate-3f38e3975d034bee2339233a735340e6": {
        "check_code": "LRE-008",
        "candidate_type": "DISPUTE_RESOLUTION_ABSENT",
        "expected_verdict": "RISK",
        "primary_evidence_source_ids": [
            "risk-as-c1f459270ff5c548ca30e042b14bf15b",
        ],
        "deterministic_severity_factors": [
            "MISSING_CORE_MECHANISM",
            "NO_EFFECTIVE_REMEDY",
        ],
    },
}

LRE_FIXED_ROOT_ORACLE = [
    {
        "source_candidate_ids": [
            "risk-candidate-511da56611f96c390deb71be5ffda2e6",
            "risk-candidate-44b755698d067232f0cbf8183ef74ac0",
            "risk-candidate-7812d49ef67c4aab35c1d44d1d8c94bb",
            "risk-candidate-60fd2fcc20fd2b36b11fec38ac3343bb",
        ],
        "root_type": "UNBOUNDED_LIABILITY_EXPOSURE",
        "root_severity_rule_id": "LRE_UNBOUNDED_LIABILITY_ROOT_V1",
        "risk_level": "HIGH",
    },
    {
        "source_candidate_ids": [
            "risk-candidate-086b7f97a3f3ac925e5c72fa3a6b2d42",
        ],
        "root_type": "CUMULATIVE_REMEDIES_REVIEW",
        "root_severity_rule_id": "LRE_CUMULATIVE_REMEDIES_V1",
        "risk_level": "MEDIUM",
    },
    {
        "source_candidate_ids": [
            "risk-candidate-631308ca37bed4f274d9daee328a6929",
        ],
        "root_type": "FORCE_MAJEURE_MECHANISM_ABSENT",
        "root_severity_rule_id": "LRE_FORCE_MAJEURE_ABSENT_V1",
        "risk_level": "MEDIUM",
    },
    {
        "source_candidate_ids": [
            "risk-candidate-3f38e3975d034bee2339233a735340e6",
        ],
        "root_type": "DISPUTE_RESOLUTION_ABSENT",
        "root_severity_rule_id": "LRE_DISPUTE_ABSENT_V1",
        "risk_level": "MEDIUM",
    },
]

LRE_FIXED_FINAL_FINDING_ORACLE = {
    "finding_count": 4,
    "forbidden_root_types": {
        "AUTOMATIC_RENEWAL",
        "RESTRICTED_EXIT_WINDOW",
        "DISPUTE_CLAUSE_CONFLICT",
        "FOREIGN_OR_BURDENSOME_FORUM",
        "TERMINATION_RIGHTS_REVIEW",
        "TERMINATION_SETTLEMENT_REVIEW",
        "BROAD_BREACH_TRIGGER_REVIEW",
    },
}


class AtomicAttemptArtifactSink:
    """Persist each isolated acceptance Attempt immediately and atomically."""

    def __init__(self, path: Path, *, metadata: dict[str, Any]) -> None:
        self.path = path
        self.records: list[dict[str, Any]] = []
        self.payload: dict[str, Any] = {
            "artifact_type": "CONTRACT_RISK_GENERIC_ATTEMPTS_V1",
            "status": "RUNNING",
            **metadata,
            "attempts": self.records,
            "failure": None,
        }
        self._write()

    def __call__(self, value: GenericAttemptArtifact) -> None:
        self.records.append(value.model_dump(mode="json"))
        self._write()

    def finalize(
        self,
        *,
        status: str,
        failure: dict[str, Any] | None = None,
    ) -> None:
        self.payload["status"] = status
        self.payload["failure"] = failure
        self._write()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = (
            json.dumps(self.payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        )
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)


def _hashes(fixture_dir: Path) -> None:
    for name, expected in EXPECTED_HASHES.items():
        actual = hashlib.sha256((fixture_dir / name).read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError(f"Fixture hash mismatch: {name}")


def _validate_po_fixture_oracle(batch_specs: list[dict[str, Any]]) -> None:
    actual = {
        candidate["candidate_id"]: {
            key: candidate[key]
            for key in (
                "check_code",
                "candidate_type",
                "candidate_strength",
                "primary_evidence_source_ids",
                "allowed_counter_evidence_source_ids",
                "severity_rule_id",
            )
        }
        for batch in batch_specs
        for candidate in batch["deterministic_candidates"]
        if candidate["check_code"] in {
            "PO-002",
            "PO-004",
            "PO-005",
            "PO-006",
            "PO-007",
        }
    }
    if actual != PO_FIXED_CANDIDATE_ORACLE:
        raise RuntimeError(
            "Fixed PO Candidate Oracle changed before real-model acceptance"
        )
    decision_actual = {
        candidate["candidate_id"]: {
            "allowed_supporting_evidence_source_ids": candidate[
                "allowed_supporting_evidence_source_ids"
            ],
            "allowed_control_codes": candidate["allowed_control_codes"],
            "expected_verdict": PO_FIXED_CANDIDATE_DECISION_ORACLE[
                candidate["candidate_id"]
            ]["expected_verdict"],
            "deterministic_severity_factors": candidate[
                "deterministic_severity_factors"
            ],
        }
        for batch in batch_specs
        for candidate in batch["deterministic_candidates"]
        if candidate["candidate_id"] in PO_FIXED_CANDIDATE_DECISION_ORACLE
    }
    if decision_actual != PO_FIXED_CANDIDATE_DECISION_ORACLE:
        raise RuntimeError(
            "Fixed PO Evidence/Control/Decision Oracle changed before "
            "real-model acceptance"
        )


def _validate_icd_fixture_oracle(batch_specs: list[dict[str, Any]]) -> None:
    candidates = {
        candidate["candidate_id"]: candidate
        for batch in batch_specs
        for candidate in batch.get(
            "all_internal_candidates",
            batch["deterministic_candidates"],
        )
    }
    if set(candidates) != set(ICD_FIXED_CANDIDATE_ORACLE):
        raise RuntimeError(
            "Fixed ICD Candidate Oracle changed before real-model acceptance"
        )
    for candidate_id, oracle in ICD_FIXED_CANDIDATE_ORACLE.items():
        candidate = candidates[candidate_id]
        for field in (
            "check_code",
            "candidate_type",
            "primary_evidence_source_ids",
            "deterministic_severity_factors",
        ):
            if candidate[field] != oracle[field]:
                raise RuntimeError(
                    f"Fixed ICD Candidate Oracle changed: "
                    f"{candidate_id}.{field}"
                )


def _validate_lre_fixture_oracle(batch_specs: list[dict[str, Any]]) -> None:
    candidates = {
        candidate["candidate_id"]: candidate
        for batch in batch_specs
        for candidate in batch.get(
            "all_internal_candidates",
            batch["deterministic_candidates"],
        )
    }
    if set(candidates) != set(LRE_FIXED_CANDIDATE_ORACLE):
        raise RuntimeError(
            "Fixed LRE Candidate Oracle changed before real-model acceptance"
        )
    for candidate_id, oracle in LRE_FIXED_CANDIDATE_ORACLE.items():
        candidate = candidates[candidate_id]
        for field in (
            "check_code",
            "candidate_type",
            "primary_evidence_source_ids",
            "deterministic_severity_factors",
        ):
            if candidate[field] != oracle[field]:
                raise RuntimeError(
                    f"Fixed LRE Candidate Oracle changed: "
                    f"{candidate_id}.{field}"
                )


def _summary_evidence_source_id(
    item,
    *,
    source_by_binding: dict[tuple[str, str], str],
    absence_by_scope: dict[tuple[str, str], str],
) -> str:
    if item.evidence_type == "ABSENCE":
        # Commercial ABSENCE evidence is materialized directly from the
        # reviewed response and is not required to repeat an internal Plan
        # verification_method verbatim.  Its deterministic technical identity
        # therefore comes from the two frozen formal Evidence fields rather
        # than a lossy reverse lookup into the Plan.
        exact_source_id = absence_by_scope.get(
            (item.checked_scope, item.verification_note)
        )
        if exact_source_id is not None:
            return exact_source_id
        return "ABSENCE:" + hashlib.sha256(
            (
                f"{item.checked_scope or ''}\n"
                f"{item.verification_note or ''}"
            ).encode("utf-8")
        ).hexdigest()
    return source_by_binding[(item.source_ir_item_id, item.anchor_id)]


def _unit_summary(
    result,
    *,
    plan,
    wall_duration_ms: int,
    batch_results,
) -> dict[str, Any]:
    findings = result.findings
    evidence = [
        item for finding in findings for item in finding.evidence_candidates
    ]
    source_by_binding = {
        (source.ir_item_id, source.anchor_id): source.source_id
        for context in plan.contexts
        if context.unit_id == result.unit_id
        for source in context.evidence_sources
    }
    absence_by_scope = {
        (source.checked_scope, source.verification_method): source.source_id
        for context in plan.contexts
        if context.unit_id == result.unit_id
        for source in context.absence_evidence_sources
    }
    canonical_source_risk_keys = [
        [
            finding.check_code,
            finding.risk_type,
            finding.risk_level,
            sorted(
                _summary_evidence_source_id(
                    item,
                    source_by_binding=source_by_binding,
                    absence_by_scope=absence_by_scope,
                )
                for item in finding.evidence_candidates
            ),
        ]
        for finding in findings
    ]
    canonical_root_stability_keys = [
        [
            root.check_code,
            root.root_type,
            root.risk_level,
            list(root.source_candidate_ids),
            list(root.core_primary_evidence_source_ids),
        ]
        for root in result.canonical_risk_roots
    ]
    allowed_risk_types_by_check = {
        spec.check_code: list(spec.allowed_risk_types)
        for unit in plan.review_units
        if str(unit.unit_id) == str(result.unit_id)
        for spec in unit.check_specs
    }
    candidate_core_keys = [
        [
            item.candidate_id,
            item.verdict,
            (
                item.candidate_type
                if item.candidate_type
                in allowed_risk_types_by_check[item.check_code]
                else (
                    allowed_risk_types_by_check[item.check_code][0]
                    if len(allowed_risk_types_by_check[item.check_code]) == 1
                    else None
                )
            ),
            item.risk_level,
            item.primary_evidence_source_ids,
        ]
        for item in result.candidate_decisions
    ]
    return {
        "unit_id": result.unit_id,
        "status": result.status,
        "wall_duration_ms": wall_duration_ms,
        "batch_duration_ms": [
            item.model_duration_ms for item in result.call_metrics
        ],
        "batch_metrics": [
            {
                "batch_id": item.batch_id,
                "duration_ms": item.duration_ms,
                "model_call_count": item.model_call_count,
                "repair_count": item.repair_count,
                "schema_repair_count": item.schema_repair_count,
                "evidence_selection_repair_count": (
                    item.evidence_selection_repair_count
                ),
                "evidence_binding_normalization_count": (
                    item.evidence_binding_normalization_count
                ),
                "ignored_model_link_fields_count": (
                    item.ignored_model_link_fields_count
                ),
                "deterministic_enrichment_count": (
                    item.deterministic_enrichment_count
                ),
                "check_code_enrichment_count": item.check_code_enrichment_count,
                "category_enrichment_count": item.category_enrichment_count,
                "risk_type_enrichment_count": item.risk_type_enrichment_count,
                "ignored_model_check_code_count": (
                    item.ignored_model_check_code_count
                ),
                "ignored_model_category_count": (
                    item.ignored_model_category_count
                ),
                "category_conflict_count": item.category_conflict_count,
                "deterministic_enrichments": [
                    enrichment.model_dump(mode="json")
                    for enrichment in item.deterministic_enrichments
                ],
                "candidate_decisions": [
                    decision.model_dump(mode="json")
                    for decision in item.candidate_decisions
                ],
                "canonical_risk_roots": [
                    root.model_dump(mode="json")
                    for root in item.canonical_risk_roots
                ],
                "check_decisions": [
                    decision.model_dump(mode="json")
                    for decision in item.check_decisions
                ],
                "supporting_primary_overlap_count": (
                    item.supporting_primary_overlap_count
                ),
                "decision_summary_perspective_warning_count": (
                    item.decision_summary_perspective_warning_count
                ),
                "proposed_severity_factor_count": (
                    item.proposed_severity_factor_count
                ),
                "accepted_severity_factor_count": (
                    item.accepted_severity_factor_count
                ),
                "rejected_severity_factor_count": (
                    item.rejected_severity_factor_count
                ),
                "selected_evidence_source_ids": (
                    item.selected_evidence_source_ids
                ),
                "tool_call_count": item.tool_call_count,
                "prompt_tokens": item.prompt_tokens,
                "cached_tokens": item.cached_tokens,
                "completion_tokens": item.completion_tokens,
                "total_tokens": item.total_tokens,
                "ttft_ms": [
                    metric.time_to_first_token_ms for metric in item.call_metrics
                ],
            }
            for item in batch_results
        ],
        "model_call_count": result.model_call_count,
        "repair_count": result.repair_count,
        "schema_repair_count": result.schema_repair_count,
        "evidence_selection_repair_count": (
            result.evidence_selection_repair_count
        ),
        "evidence_binding_normalization_count": (
            result.evidence_binding_normalization_count
        ),
        "ignored_model_link_fields_count": (
            result.ignored_model_link_fields_count
        ),
        "deterministic_enrichment_count": result.deterministic_enrichment_count,
        "check_code_enrichment_count": result.check_code_enrichment_count,
        "category_enrichment_count": result.category_enrichment_count,
        "risk_type_enrichment_count": result.risk_type_enrichment_count,
        "ignored_model_check_code_count": (
            result.ignored_model_check_code_count
        ),
        "ignored_model_category_count": (
            result.ignored_model_category_count
        ),
        "category_conflict_count": result.category_conflict_count,
        "deterministic_enrichments": [
            item.model_dump(mode="json")
            for item in result.deterministic_enrichments
        ],
        "candidate_count": len(result.candidate_decisions),
        "candidate_core_keys": candidate_core_keys,
        "candidate_decisions": [
            item.model_dump(mode="json")
            for item in result.candidate_decisions
        ],
        "canonical_root_count": len(result.canonical_risk_roots),
        "canonical_risk_roots": [
            item.model_dump(mode="json")
            for item in result.canonical_risk_roots
        ],
        "check_decisions": [
            item.model_dump(mode="json")
            for item in result.check_decisions
        ],
        "supporting_primary_overlap_count": (
            result.supporting_primary_overlap_count
        ),
        "decision_summary_perspective_warning_count": (
            result.decision_summary_perspective_warning_count
        ),
        "proposed_severity_factor_count": (
            result.proposed_severity_factor_count
        ),
        "accepted_severity_factor_count": (
            result.accepted_severity_factor_count
        ),
        "rejected_severity_factor_count": (
            result.rejected_severity_factor_count
        ),
        "selected_evidence_source_ids": result.selected_evidence_source_ids,
        "tool_call_count": result.tool_call_count,
        "prompt_tokens": result.prompt_tokens,
        "cached_tokens": result.cached_tokens,
        "completion_tokens": result.completion_tokens,
        "total_tokens": result.total_tokens,
        "ttft_ms": [
            item.time_to_first_token_ms for item in result.call_metrics
        ],
        "check_codes": [item.check_code for item in result.check_results],
        "check_statuses": {
            item.check_code: item.status for item in result.check_results
        },
        "reason_codes": {
            item.check_code: item.reason_code for item in result.check_results
        },
        "fva_assessments": [
            item.model_dump(mode="json") for item in result.fva_assessments
        ],
        "finding_count": len(findings),
        "evidence_count": len(evidence),
        "canonical_risk_keys": [
            list(_canonical_risk_key(item)) for item in findings
        ],
        "canonical_source_risk_keys": canonical_source_risk_keys,
        "canonical_root_stability_keys": canonical_root_stability_keys,
        "findings": [item.model_dump(mode="json") for item in findings],
        "trace_ids": result.trace_ids,
        "call_metrics": [
            item.model_dump(mode="json") for item in result.call_metrics
        ],
    }


async def _run_one_unit(
    plan,
    unit,
    *,
    tenant_id: str,
    model_id: str,
    attempt_artifact_sink: AtomicAttemptArtifactSink | None = None,
):
    contexts = {item.batch_id: item for item in plan.contexts}
    reviewer = GenericBaseDirectReviewer()
    started = time.perf_counter()
    batch_results = await asyncio.gather(
        *(
            reviewer.review(
                generic_request_from_context(contexts[batch_id]),
                tenant_id=tenant_id,
                model_id=model_id,
                framework_run_id=f"stage63-{unit.unit_id}-{batch_id[-8:]}",
                attempt_artifact_sink=attempt_artifact_sink,
                allow_evidence_selection_repair=(
                    str(unit.unit_id) != "performance_obligations"
                ),
            )
            for batch_id in unit.batch_ids
        )
    )
    wall_duration_ms = round((time.perf_counter() - started) * 1000)
    merged = _merge_unit_result(
        unit,
        {item.batch_id: item for item in batch_results},
    )
    return merged, wall_duration_ms, batch_results


def _append_unit_run_and_validate(
    unit,
    runs: list[dict[str, Any]],
    raw_results: list[list[dict[str, Any]]],
    *,
    summary: dict[str, Any],
    raw_batch_results: list[dict[str, Any]],
) -> list[str]:
    """Record one completed repetition and immediately execute every gate."""

    runs.append(summary)
    raw_results.append(raw_batch_results)
    return _validate_unit_runs(unit, runs)


def _canonical_risk_stability_sets(
    unit_id: str,
    runs: list[dict[str, Any]],
) -> list[set[str]]:
    key_field = (
        "canonical_root_stability_keys"
        if unit_id in {
            "performance_obligations",
            "ip_confidentiality_data",
            "liability_remedies_exit",
        }
        else "canonical_risk_keys"
    )
    return [
        {
            json.dumps(
                item,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            for item in run[key_field]
        }
        for run in runs
    ]


def _validate_unit_runs(unit, runs: list[dict[str, Any]]) -> list[str]:
    failures = []
    expected_codes = [item.check_code for item in unit.check_specs]
    for index, run in enumerate(runs, 1):
        if run["check_codes"] != expected_codes:
            failures.append(f"run {index}: Check coverage mismatch")
        if any(value == "FAILED" for value in run["check_statuses"].values()):
            failures.append(f"run {index}: required Check failed")
        if run["model_call_count"] != len(unit.batch_ids):
            failures.append(f"run {index}: normal model call count is not one per Batch")
        if run["repair_count"] != 0:
            failures.append(f"run {index}: Schema repair occurred")
        if (
            str(unit.unit_id) == "performance_obligations"
            and run["evidence_selection_repair_count"] != 0
        ):
            failures.append(
                f"run {index}: PO Evidence Selection Repair occurred"
            )
        if (
            str(unit.unit_id) == "performance_obligations"
            and run["evidence_binding_normalization_count"] != 0
        ):
            failures.append(
                f"run {index}: PO used legacy Evidence binding normalization"
            )
        if str(unit.unit_id) == "performance_obligations":
            candidate_ids = [
                item["candidate_id"] for item in run["candidate_decisions"]
            ]
            if not candidate_ids or len(candidate_ids) != len(set(candidate_ids)):
                failures.append(
                    f"run {index}: Candidate coverage is empty or duplicated"
                )
            if any(
                item["verdict"] == "NO_RISK"
                and not item["decision_summary"]
                for item in run["candidate_decisions"]
            ):
                failures.append(
                    f"run {index}: NO_RISK Candidate lacks a decision basis"
                )
            if any(
                not item["primary_evidence_source_ids"]
                for item in run["candidate_decisions"]
            ):
                failures.append(
                    f"run {index}: Candidate Primary Evidence is missing"
                )
            if str(unit.unit_id) == "performance_obligations":
                for item in run["candidate_decisions"]:
                    oracle = PO_FIXED_CANDIDATE_DECISION_ORACLE.get(
                        item["candidate_id"]
                    )
                    if oracle is None:
                        continue
                    if item["verdict"] != oracle["expected_verdict"]:
                        failures.append(
                            f"run {index}: {item['candidate_id']} verdict changed"
                        )
                    for factor_code in oracle[
                        "deterministic_severity_factors"
                    ]:
                        field_name = factor_code.lower()
                        if not item["severity_factors"].get(
                            field_name,
                            False,
                        ):
                            failures.append(
                                f"run {index}: {item['candidate_id']} lost "
                                f"deterministic factor {factor_code}"
                            )
                    controls = item["recommended_control_codes"]
                    if item["verdict"] == "RISK" and not controls:
                        failures.append(
                            f"run {index}: RISK Candidate has no Control Code"
                        )
                    if any(
                        code not in oracle["allowed_control_codes"]
                        for code in controls
                    ):
                        failures.append(
                            f"run {index}: Candidate selected an unknown Control Code"
                        )
            po003 = [
                item
                for item in run["candidate_decisions"]
                if item["check_code"] == "PO-003"
            ]
            if (
                len(po003) != 1
                or po003[0]["verdict"] != "NO_RISK"
                or po003[0]["decision_source"]
                != "DETERMINISTIC_PRECONDITION"
                or po003[0]["po003_precondition"] is None
                or po003[0]["po003_precondition"][
                    "model_review_required"
                ]
            ):
                failures.append(
                    f"run {index}: PO-003 deterministic precondition Oracle failed"
                )
            po006 = [
                item
                for item in run["candidate_decisions"]
                if item["check_code"] == "PO-006"
            ]
            if (
                len(po006) != 1
                or po006[0]["verdict"] != "RISK"
                or po006[0]["risk_level"] != "MEDIUM"
            ):
                failures.append(
                    f"run {index}: PO-006 Root severity Oracle failed"
                )
            for item in run["candidate_decisions"]:
                accepted = set(
                    item["validated_semantic_severity_factors"]
                )
                rejected = {
                    factor["factor_code"]
                    for factor in item["rejected_severity_factors"]
                }
                if accepted & rejected:
                    failures.append(
                        f"run {index}: Severity Factor accepted/rejected overlap"
                    )
                if (
                    len(item["proposed_semantic_severity_factors"])
                    != len(accepted) + len(rejected)
                ):
                    failures.append(
                        f"run {index}: Severity Factor audit is incomplete"
                    )
            expected_roots = {
                tuple(item["source_candidate_ids"]): item
                for item in PO_FIXED_ROOT_ORACLE
            }
            actual_roots = {
                tuple(item["source_candidate_ids"]): item
                for item in run["canonical_risk_roots"]
            }
            if set(actual_roots) != set(expected_roots):
                failures.append(
                    f"run {index}: Canonical Root grouping does not match "
                    "the fixed Fixture"
                )
            else:
                for source_ids, oracle in expected_roots.items():
                    actual = actual_roots[source_ids]
                    if (
                        actual["root_type"] != oracle["root_type"]
                        or actual["root_severity_rule_id"]
                        != oracle["root_severity_rule_id"]
                        or actual["risk_level"] != oracle["risk_level"]
                    ):
                        failures.append(
                            f"run {index}: Canonical Root {source_ids} "
                            "does not match its Root Oracle"
                        )
            if run["finding_count"] != PO_FIXED_FINAL_FINDING_ORACLE[
                "finding_count"
            ]:
                failures.append(
                    f"run {index}: Final Finding count does not match "
                    "the fixed Fixture"
                )
            for finding in run["findings"]:
                if (
                    finding["perspective"] != "PARTY_A"
                    or finding["our_party"] != "杭州戎一教育科技有限公司"
                    or finding["counterparty"]
                    != "苏州爱兔格人工智能科技有限公司"
                ):
                    failures.append(
                        f"run {index}: formal Finding perspective is polluted"
                    )
                formal_text = "".join(
                    (
                        finding["title"],
                        finding["issue"],
                        finding["impact_to_our_party"],
                        finding["suggestion"],
                    )
                )
                if "我方作为乙方" in formal_text:
                    failures.append(
                        f"run {index}: wrong party wording entered Final Finding"
                    )
        if str(unit.unit_id) == "ip_confidentiality_data":
            candidate_ids = [
                item["candidate_id"] for item in run["candidate_decisions"]
            ]
            if not candidate_ids or len(candidate_ids) != len(set(candidate_ids)):
                failures.append(
                    f"run {index}: ICD Candidate coverage is empty or duplicated"
                )
            if any(
                not item["primary_evidence_source_ids"]
                for item in run["candidate_decisions"]
            ):
                failures.append(
                    f"run {index}: ICD Candidate Primary Evidence is missing"
                )
            if run["evidence_selection_repair_count"] != 0:
                failures.append(
                    f"run {index}: ICD Evidence Selection Repair occurred"
                )
            if run["evidence_binding_normalization_count"] != 0:
                failures.append(
                    f"run {index}: ICD used legacy Evidence normalization"
                )
            for item in run["candidate_decisions"]:
                accepted = set(
                    item["validated_semantic_severity_factors"]
                )
                rejected = {
                    factor["factor_code"]
                    for factor in item["rejected_severity_factors"]
                }
                if accepted & rejected:
                    failures.append(
                        f"run {index}: ICD Severity audit overlaps"
                    )
                if (
                    len(item["proposed_semantic_severity_factors"])
                    != len(accepted) + len(rejected)
                ):
                    failures.append(
                        f"run {index}: ICD Severity audit is incomplete"
                    )
            actual = {
                item["candidate_id"]: item
                for item in run["candidate_decisions"]
            }
            if set(actual) != set(ICD_FIXED_CANDIDATE_ORACLE):
                failures.append(
                    f"run {index}: ICD Candidate Oracle coverage changed"
                )
            else:
                for candidate_id, oracle in ICD_FIXED_CANDIDATE_ORACLE.items():
                    item = actual[candidate_id]
                    if item["verdict"] != oracle["expected_verdict"]:
                        failures.append(
                            f"run {index}: {candidate_id} verdict changed"
                        )
                    if (
                        item["primary_evidence_source_ids"]
                        != oracle["primary_evidence_source_ids"]
                    ):
                        failures.append(
                            f"run {index}: {candidate_id} Primary Evidence changed"
                        )
                    for factor_code in oracle[
                        "deterministic_severity_factors"
                    ]:
                        if not item["severity_factors"].get(
                            factor_code.lower(),
                            False,
                        ):
                            failures.append(
                                f"run {index}: {candidate_id} lost "
                                f"{factor_code}"
                            )
            if len(run["canonical_risk_roots"]) != 1:
                failures.append(
                    f"run {index}: ICD Canonical Root count changed"
                )
            else:
                root = run["canonical_risk_roots"][0]
                for field in (
                    "source_candidate_ids",
                    "root_type",
                    "root_severity_rule_id",
                    "risk_level",
                ):
                    if root[field] != ICD_FIXED_ROOT_ORACLE[field]:
                        failures.append(
                            f"run {index}: ICD Root Oracle changed at {field}"
                        )
            if run["finding_count"] != ICD_FIXED_ROOT_ORACLE["finding_count"]:
                failures.append(
                    f"run {index}: ICD Final Finding count changed"
                )
        if str(unit.unit_id) == "liability_remedies_exit":
            if run["evidence_selection_repair_count"] != 0:
                failures.append(
                    f"run {index}: LRE Evidence Selection Repair occurred"
                )
            if run["evidence_binding_normalization_count"] != 0:
                failures.append(
                    f"run {index}: LRE used legacy Evidence normalization"
                )
            actual_candidates = {
                item["candidate_id"]: item
                for item in run["candidate_decisions"]
            }
            if set(actual_candidates) != set(LRE_FIXED_CANDIDATE_ORACLE):
                failures.append(
                    f"run {index}: LRE Candidate Oracle coverage changed"
                )
            else:
                for candidate_id, oracle in LRE_FIXED_CANDIDATE_ORACLE.items():
                    item = actual_candidates[candidate_id]
                    if item["verdict"] != oracle["expected_verdict"]:
                        failures.append(
                            f"run {index}: {candidate_id} verdict changed"
                        )
                    if (
                        item["primary_evidence_source_ids"]
                        != oracle["primary_evidence_source_ids"]
                    ):
                        failures.append(
                            f"run {index}: {candidate_id} Primary Evidence changed"
                        )
                    for factor_code in oracle[
                        "deterministic_severity_factors"
                    ]:
                        if not item["severity_factors"].get(
                            factor_code.lower(),
                            False,
                        ):
                            failures.append(
                                f"run {index}: {candidate_id} lost "
                                f"{factor_code}"
                            )
                    accepted = set(
                        item["validated_semantic_severity_factors"]
                    )
                    rejected = {
                        factor["factor_code"]
                        for factor in item["rejected_severity_factors"]
                    }
                    if accepted & rejected:
                        failures.append(
                            f"run {index}: LRE Severity audit overlaps"
                        )
                    if (
                        len(item["proposed_semantic_severity_factors"])
                        != len(accepted) + len(rejected)
                    ):
                        failures.append(
                            f"run {index}: LRE Severity audit is incomplete"
                        )
                    if (
                        item["verdict"] == "RISK"
                        and not item["recommended_control_codes"]
                    ):
                        failures.append(
                            f"run {index}: LRE RISK Candidate has no Control Code"
                        )
            expected_roots = {
                tuple(item["source_candidate_ids"]): item
                for item in LRE_FIXED_ROOT_ORACLE
            }
            actual_roots = {
                tuple(item["source_candidate_ids"]): item
                for item in run["canonical_risk_roots"]
            }
            if set(actual_roots) != set(expected_roots):
                failures.append(
                    f"run {index}: LRE Canonical Root grouping changed"
                )
            else:
                for source_ids, oracle in expected_roots.items():
                    actual = actual_roots[source_ids]
                    for field in (
                        "root_type",
                        "root_severity_rule_id",
                        "risk_level",
                    ):
                        if actual[field] != oracle[field]:
                            failures.append(
                                f"run {index}: LRE Root {source_ids} "
                                f"changed at {field}"
                            )
            if (
                run["finding_count"]
                != LRE_FIXED_FINAL_FINDING_ORACLE["finding_count"]
            ):
                failures.append(
                    f"run {index}: LRE Final Finding count changed"
                )
            if any(
                item["root_type"]
                in LRE_FIXED_FINAL_FINDING_ORACLE["forbidden_root_types"]
                for item in run["canonical_risk_roots"]
            ):
                failures.append(
                    f"run {index}: LRE generated a forbidden Fixture risk"
                )
            for finding in run["findings"]:
                if (
                    finding["perspective"] != "PARTY_A"
                    or finding["our_party"]
                    != "杭州戎一教育科技有限公司"
                    or finding["counterparty"]
                    != "苏州爱兔格人工智能科技有限公司"
                ):
                    failures.append(
                        f"run {index}: LRE formal Finding perspective is polluted"
                    )
        if run["tool_call_count"] != 0:
            failures.append(f"run {index}: Tool call count is nonzero")
        if run["wall_duration_ms"] > 60000:
            failures.append(f"run {index}: Unit hard performance limit exceeded")
        if any(
            item["duration_ms"] > 60000 for item in run["batch_metrics"]
        ):
            failures.append(f"run {index}: Batch hard performance limit exceeded")
        for finding in run["findings"]:
            if not finding["evidence_candidates"]:
                failures.append(f"run {index}: Finding has no Evidence")
            for evidence in finding["evidence_candidates"]:
                if evidence["evidence_type"] == "ABSENCE":
                    if (
                        not evidence["checked_scope"]
                        or not evidence["verification_note"]
                    ):
                        failures.append(f"run {index}: invalid ABSENCE Evidence")
                elif (
                    not evidence["source_ir_item_id"]
                    or not evidence["anchor_id"]
                    or not evidence["block_id"]
                    or not evidence["quoted_text"]
                    or not evidence["quoted_text_hash"]
                ):
                    failures.append(f"run {index}: invalid source Evidence")
        if str(unit.unit_id) == "formation_validity_authority":
            assessments = run["fva_assessments"]
            if len(assessments) != 1:
                failures.append(
                    f"run {index}: FVA-002 assessment is missing or duplicated"
                )
            else:
                assessment = assessments[0]
                if (
                    assessment["assessment_type"]
                    != FVA002_FIXED_EXPECTATION["assessment_type"]
                    or assessment["external_verification_required"]
                    != FVA002_FIXED_EXPECTATION[
                        "external_verification_required"
                    ]
                ):
                    failures.append(
                        f"run {index}: FVA-002 does not match the frozen "
                        "Fixture expectation"
                    )
                if (
                    run["reason_codes"].get("FVA-002")
                    != FVA002_FIXED_EXPECTATION["reason_code"]
                ):
                    failures.append(
                        f"run {index}: FVA-002 reason code is not "
                        "INSUFFICIENT_EVIDENCE"
                    )
                if any(
                    finding["check_code"] == "FVA-002"
                    for finding in run["findings"]
                ):
                    failures.append(
                        f"run {index}: external-verification state produced "
                        "an FVA-002 Finding"
                    )
    risk_sets = _canonical_risk_stability_sets(str(unit.unit_id), runs)
    if any(value != risk_sets[0] for value in risk_sets[1:]):
        failures.append("repeated runs have substantively different canonical risks")
    if str(unit.unit_id) in {
        "performance_obligations",
        "ip_confidentiality_data",
        "liability_remedies_exit",
    }:
        candidate_core = [
            {
                json.dumps(
                    item,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                for item in run["candidate_core_keys"]
                if item[0]
            }
            for run in runs
        ]
        if any(value != candidate_core[0] for value in candidate_core[1:]):
            failures.append(
                "Candidate runs have different verdict/risk type/"
                "risk level/Primary Evidence"
            )
        root_core = [
            {
                json.dumps(
                    {
                        "root_id": item["root_id"],
                        "root_type": item["root_type"],
                        "source_candidate_ids": item["source_candidate_ids"],
                        "severity_factors": item["severity_factors"],
                        "risk_level": item["risk_level"],
                        "primary_evidence_source_ids": item[
                            "primary_evidence_source_ids"
                        ],
                        "recommended_control_codes": item[
                            "recommended_control_codes"
                        ],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                for item in run["canonical_risk_roots"]
            }
            for run in runs
        ]
        if any(value != root_core[0] for value in root_core[1:]):
            failures.append(
                "Candidate runs have different Canonical Root grouping, severity or "
                "Core Evidence"
            )
    return failures


def _po_severity_explanations() -> list[dict[str, Any]]:
    return [
        {
            "check_code": "PO-002",
            "candidate_id": "risk-candidate-f23588a0aba5e1bf2a45244191e7507f",
            "candidate_type": "RIGHTS_OBLIGATIONS_IMBALANCE",
            "old_candidate_minimum": "HIGH",
            "revised_root_minimum": "MEDIUM",
            "deterministic_severity_factors": ["UNILATERAL_CONTROL"],
            "reason": (
                "Primary Evidence proves unilateral control, but does not also "
                "prove no effective remedy and a severe operational, schedule "
                "or financial impact. The old Candidate-level HIGH Oracle was "
                "stricter than the frozen severity rule."
            ),
        },
        {
            "check_code": "PO-004",
            "candidate_id": "risk-candidate-d3ba1e8323ba6f8fca2814e624649fcd",
            "candidate_type": "QUALITY_STANDARD_UNMEASURABLE",
            "old_candidate_minimum": "HIGH",
            "revised_root_minimum": "MEDIUM",
            "deterministic_severity_factors": ["MISSING_CORE_MECHANISM"],
            "reason": (
                "The Candidate proves an unmeasurable quality standard, but "
                "the evidence does not independently prove operational impact. "
                "The separate acceptance-absence Root remains HIGH."
            ),
        },
        {
            "check_code": "PO-006",
            "candidate_id": "risk-candidate-b3ee5f4a798cf39d6b13746cc00de138",
            "candidate_type": "CHANGE_CONTROL_REVIEW",
            "old_candidate_minimum": "HIGH",
            "revised_root_minimum": "MEDIUM",
            "deterministic_severity_factors": ["UNILATERAL_CONTROL"],
            "reason": (
                "The Candidate and model output establish unilateral change "
                "control and a missing mechanism, but not the additional no-"
                "effective-remedy and severe-impact combination required for HIGH."
            ),
        },
    ]


def _validate_po_materialized_oracles(
    candidate_decisions,
    roots,
    findings,
) -> list[str]:
    failures: list[str] = []
    decisions_by_id = {item.candidate_id: item for item in candidate_decisions}
    if len(decisions_by_id) != len(candidate_decisions):
        failures.append("Candidate Decisions are duplicated")
    for candidate_id, oracle in PO_FIXED_CANDIDATE_DECISION_ORACLE.items():
        decision = decisions_by_id.get(candidate_id)
        if decision is None:
            failures.append(f"Candidate Decision is missing: {candidate_id}")
            continue
        if decision.verdict != oracle["expected_verdict"]:
            failures.append(f"Candidate verdict changed: {candidate_id}")
        for factor_code in oracle["deterministic_severity_factors"]:
            if not getattr(decision.severity_factors, factor_code.lower()):
                failures.append(
                    f"Candidate lost deterministic factor {factor_code}: "
                    f"{candidate_id}"
                )
    expected_roots = {
        tuple(item["source_candidate_ids"]): item
        for item in PO_FIXED_ROOT_ORACLE
    }
    actual_roots = {
        tuple(item.source_candidate_ids): item for item in roots
    }
    if set(actual_roots) != set(expected_roots):
        failures.append("Canonical Root grouping changed")
    else:
        for source_ids, oracle in expected_roots.items():
            root = actual_roots[source_ids]
            if (
                root.root_type != oracle["root_type"]
                or root.root_severity_rule_id
                != oracle["root_severity_rule_id"]
                or root.risk_level != oracle["risk_level"]
            ):
                failures.append(
                    f"Canonical Root does not match Oracle: {source_ids}"
                )
            if (
                len(root.primary_evidence_source_ids)
                != len(set(root.primary_evidence_source_ids))
                or len(root.supporting_evidence_source_ids)
                != len(set(root.supporting_evidence_source_ids))
                or len(root.recommended_control_codes)
                != len(set(root.recommended_control_codes))
            ):
                failures.append(
                    f"Canonical Root contains duplicated sources or controls: "
                    f"{source_ids}"
                )
    finding_ids = [item.finding_local_id for item in findings]
    if len(finding_ids) != len(set(finding_ids)):
        failures.append("Final Findings are duplicated")
    if len(findings) != PO_FIXED_FINAL_FINDING_ORACLE["finding_count"]:
        failures.append("Final Finding count changed")
    finding_by_id = {item.finding_local_id: item for item in findings}
    if any(root.finding_local_id not in finding_by_id for root in roots):
        failures.append("Canonical Root is not linked to one Final Finding")
    canonical_keys = [_canonical_risk_key(item) for item in findings]
    if len(canonical_keys) != len(set(canonical_keys)):
        failures.append("Post-materialization duplicate Finding gate failed")
    return failures


def _run_po_offline_replay(args, plan) -> dict[str, Any]:
    if args.replay_attempts is None:
        raise RuntimeError("--replay-attempts is required for po-replay")
    payload = json.loads(args.replay_attempts.read_text(encoding="utf-8"))
    attempts = payload.get("attempts")
    if not isinstance(attempts, list):
        raise RuntimeError("Replay Artifact has no Attempt list")
    accepted_by_batch: dict[str, list[dict[str, Any]]] = {}
    for attempt in attempts:
        if (
            attempt.get("unit_id") == "performance_obligations"
            and attempt.get("accepted") is True
            and isinstance(attempt.get("raw_response"), str)
        ):
            accepted_by_batch.setdefault(attempt["batch_id"], []).append(
                attempt
            )
    unit = next(
        item
        for item in plan.review_units
        if str(item.unit_id) == "performance_obligations"
    )
    contexts = {
        item.batch_id: generic_request_from_context(item)
        for item in plan.contexts
        if item.batch_id in unit.batch_ids
    }
    if set(accepted_by_batch) != set(unit.batch_ids):
        raise RuntimeError(
            "Replay Artifact must contain one accepted Attempt for each PO Batch"
        )

    replay_counts = {len(values) for values in accepted_by_batch.values()}
    if len(replay_counts) != 1:
        raise RuntimeError(
            "Replay Artifact has an unequal accepted Attempt count per PO Batch"
        )
    replay_count = next(iter(replay_counts))
    replay_runs = []
    all_failures: list[str] = []
    started = time.perf_counter()
    for replay_index in range(replay_count):
        all_candidates = []
        all_roots = []
        all_findings = []
        batch_replays = []
        for batch_id in unit.batch_ids:
            request = contexts[batch_id]
            _prompt, ir_refs, anchor_refs = _generic_prompt(request)
            candidates = _build_generic_candidates(
                request,
                ir_refs,
                {item.anchor_id: ref for ref, item in anchor_refs.items()},
            )
            model_candidates = [
                item for item in candidates if item.requires_model_decision
            ]
            candidates_by_id = {
                item.candidate_id: item for item in model_candidates
            }
            expected_ids = tuple(item.candidate_id for item in model_candidates)
            source_attempt = accepted_by_batch[batch_id][replay_index]
            replay_object = json.loads(source_attempt["raw_response"])
            replay_object["candidate_decisions"] = [
                item
                for item in replay_object["candidate_decisions"]
                if item.get("candidate_id") in candidates_by_id
            ]
            response, raw_object = _parse_po_candidate_output(
                json.dumps(replay_object, ensure_ascii=False),
                expected_ids=expected_ids,
                candidates_by_id=candidates_by_id,
            )
            catalog = _po_evidence_catalog(
                request,
                candidates,
                ir_refs,
                anchor_refs,
            )
            (
                _coverage,
                findings,
                decisions,
                roots,
                _check_decisions,
                _supporting_overlap,
                _perspective_warnings,
            ) = _materialize_po_candidate_decisions(
                request,
                response,
                candidates,
                catalog,
                ir_refs,
                anchor_refs,
            )
            all_candidates.extend(decisions)
            all_roots.extend(roots)
            all_findings.extend(findings)
            batch_replays.append(
                {
                    "batch_id": batch_id,
                    "source_attempt_index": replay_index + 1,
                    "raw_object": raw_object,
                    "candidate_decisions": [
                        item.model_dump(mode="json") for item in decisions
                    ],
                    "canonical_risk_roots": [
                        item.model_dump(mode="json") for item in roots
                    ],
                    "findings": [
                        item.model_dump(mode="json") for item in findings
                    ],
                }
            )
        failures = _validate_po_materialized_oracles(
            all_candidates,
            all_roots,
            all_findings,
        )
        po003 = next(
            item
            for item in all_candidates
            if item.check_code == "PO-003"
        )
        po006 = next(
            item
            for item in all_candidates
            if item.check_code == "PO-006"
        )
        if (
            po003.verdict != "NO_RISK"
            or po003.decision_source != "DETERMINISTIC_PRECONDITION"
        ):
            failures.append("PO-003 false positive survived its precondition")
        if po006.risk_level != "MEDIUM":
            failures.append("PO-006 unsupported factors changed Root severity")
        all_failures.extend(
            f"replay {replay_index + 1}: {failure}"
            for failure in failures
        )
        replay_runs.append(
            {
                "replay_index": replay_index + 1,
                "model_call_count": 0,
                "candidate_count": len(all_candidates),
                "canonical_root_count": len(all_roots),
                "finding_count": len(all_findings),
                "po003_decision": po003.model_dump(mode="json"),
                "po006_decision": po006.model_dump(mode="json"),
                "candidate_decisions": [
                    item.model_dump(mode="json") for item in all_candidates
                ],
                "canonical_risk_roots": [
                    item.model_dump(mode="json") for item in all_roots
                ],
                "findings": [
                    item.model_dump(mode="json") for item in all_findings
                ],
                "batch_replays": batch_replays,
                "failures": failures,
            }
        )
    duration_ms = round((time.perf_counter() - started) * 1000)
    return {
        "artifact_type": "CONTRACT_RISK_STAGE63_PO_ROOT_OFFLINE_REPLAY_V1",
        "phase": "po-replay",
        "fixture_hashes": EXPECTED_HASHES,
        "source_attempt_artifact": str(args.replay_attempts),
        "source_attempt_sha256": hashlib.sha256(
            args.replay_attempts.read_bytes()
        ).hexdigest(),
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "model_call_count": 0,
        "duration_ms": duration_ms,
        "replay_count": replay_count,
        "replay_runs": replay_runs,
        "severity_explanations": _po_severity_explanations(),
        "status": "PASSED" if not all_failures else "FAILED",
        "failures": all_failures,
    }


def _build_plan_audit(args, value, plan) -> dict[str, Any]:
    """Build a zero-model deterministic audit for one Stage 6.3 domain."""
    unit_id = args.unit_id or "ip_confidentiality_data"
    unit = next(
        item for item in plan.review_units if str(item.unit_id) == unit_id
    )
    repeated = [RiskReviewPlanBuilder().build(value) for _ in range(100)]
    plan_ids = {item.plan_id for item in repeated}
    plan_hashes = {item.plan_hash for item in repeated}
    batch_specs: list[dict[str, Any]] = []
    total_fixture_ir_count = sum(
        len(items)
        for items in value.stage_result.semantic_ir.model_dump(
            mode="json"
        ).values()
    )
    for batch_id in unit.batch_ids:
        context = next(
            generic_request_from_context(item)
            for item in plan.contexts
            if item.batch_id == batch_id
        )
        prompt, ir_refs, anchor_refs = _generic_prompt(context)
        candidates = _build_generic_candidates(
            context,
            ir_refs,
            {item.anchor_id: ref for ref, item in anchor_refs.items()},
        )
        catalog = _po_evidence_catalog(
            context,
            candidates,
            ir_refs,
            anchor_refs,
        )
        batch_specs.append(
            {
                "batch_id": batch_id,
                "check_codes": [
                    item.check_code for item in context.assigned_check_specs
                ],
                "estimated_input_tokens": context.estimated_input_tokens,
                "prompt_token_estimate": (
                    estimate_tokens_in_text(_PO_CANDIDATE_SYSTEM_PROMPT)
                    + estimate_tokens_in_text(prompt)
                ),
                "projected_ir_count": len(ir_refs),
                "total_fixture_ir_count": total_fixture_ir_count,
                "contains_all_fixture_ir": len(ir_refs) == total_fixture_ir_count,
                "evidence_source_count": len(catalog.evidence_sources),
                "absence_source_count": len(catalog.absence_sources),
                "candidates": [
                    item.model_dump(mode="json") for item in candidates
                ],
                "candidate_count_by_check": {
                    check_code: sum(
                        item.check_code == check_code for item in candidates
                    )
                    for check_code in (
                        item.check_code
                        for item in context.assigned_check_specs
                    )
                },
                "evidence_source_count_by_check": {
                    check_code: len(
                        catalog.allowed_source_ids_by_check.get(check_code, ())
                    )
                    for check_code in (
                        item.check_code
                        for item in context.assigned_check_specs
                    )
                },
            }
        )
    failures = []
    if len(plan_ids) != 1 or len(plan_hashes) != 1:
        failures.append("Plan ID or Plan Hash changed across 100 builds")
    expected_batch_count = {
        "ip_confidentiality_data": 1,
        "liability_remedies_exit": 2,
    }.get(unit_id)
    if expected_batch_count is not None and len(unit.batch_ids) != expected_batch_count:
        failures.append(
            f"{unit_id} must remain {expected_batch_count} deterministic Batch(es)"
        )
    if any(item["estimated_input_tokens"] >= 6000 for item in batch_specs):
        failures.append(f"{unit_id} context reached the 6000-token hard limit")
    if any(item["prompt_token_estimate"] >= 6000 for item in batch_specs):
        failures.append(
            f"{unit_id} Candidate prompt reached the 6000-token hard limit"
        )
    if any(item["contains_all_fixture_ir"] for item in batch_specs):
        failures.append(f"{unit_id} received all 101 Fixture IR items")
    return {
        "artifact_type": "CONTRACT_RISK_STAGE63_PLAN_AUDIT_V1",
        "phase": "plan",
        "unit_id": unit_id,
        "fixture_hashes": EXPECTED_HASHES,
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "repeated_plan_build_count": len(repeated),
        "stable_plan_id_count": len(plan_ids),
        "stable_plan_hash_count": len(plan_hashes),
        "model_call_count": 0,
        "batch_specs": batch_specs,
        "status": "PASSED" if not failures else "FAILED",
        "failures": failures,
    }


async def _run_domains(args, plan) -> dict[str, Any]:
    units = {
        str(unit.unit_id): unit
        for unit in plan.review_units
        if str(unit.unit_id) in GENERIC_UNIT_IDS
    }
    artifact: dict[str, Any] = {
        "artifact_type": "CONTRACT_RISK_STAGE63_DOMAIN_ACCEPTANCE_V1",
        "phase": "domains",
        "fixture_hashes": EXPECTED_HASHES,
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "fva002_fixed_expectation": FVA002_FIXED_EXPECTATION,
        "domains": {},
        "status": "RUNNING",
    }
    all_failures = []
    selected_unit_ids = (
        (args.unit_id,) if args.unit_id is not None else GENERIC_UNIT_IDS
    )
    for unit_id in selected_unit_ids:
        unit = units[unit_id]
        attempt_sink = (
            AtomicAttemptArtifactSink(
                args.attempt_output,
                metadata={
                    "phase": "domains",
                    "unit_id": unit_id,
                    "fixture_hashes": EXPECTED_HASHES,
                    "plan_id": plan.plan_id,
                    "plan_hash": plan.plan_hash,
                },
            )
            if args.attempt_output is not None
            else None
        )
        contexts = {
            item.batch_id: generic_request_from_context(item)
            for item in plan.contexts
            if item.batch_id in unit.batch_ids
        }
        prompt_estimates = []
        batch_specs = []
        for batch_id in unit.batch_ids:
            prompt, ir_refs, anchor_refs = _generic_prompt(contexts[batch_id])
            prompt_estimate = (
                estimate_tokens_in_text(
                    _PO_CANDIDATE_SYSTEM_PROMPT
                    if unit_id in {
                        "performance_obligations",
                        "ip_confidentiality_data",
                        "liability_remedies_exit",
                    }
                    else _GENERIC_SYSTEM_PROMPT
                )
                + estimate_tokens_in_text(prompt)
            )
            prompt_estimates.append(prompt_estimate)
            prompt_payload = json.loads(prompt.split("\n", 1)[1])
            prompt_checks = prompt_payload["assigned_checks"]
            candidate_legend = prompt_payload.get("candidate_legend")
            internal_candidates = _build_generic_candidates(
                contexts[batch_id],
                ir_refs,
                {item.anchor_id: ref for ref, item in anchor_refs.items()},
            )
            internal_by_id = {
                item.candidate_id: item for item in internal_candidates
            }
            deterministic_precondition_decisions = [
                {
                    "candidate_id": item.candidate_id,
                    "check_code": item.check_code,
                    "candidate_type": item.candidate_type,
                    "decision_source": "DETERMINISTIC_PRECONDITION",
                    "verdict": "NO_RISK",
                    "po003_precondition": (
                        item.po003_precondition.model_dump(mode="json")
                        if item.po003_precondition is not None
                        else None
                    ),
                    "icd_perspective_precondition": (
                        item.icd_perspective_precondition.model_dump(mode="json")
                        if item.icd_perspective_precondition is not None
                        else None
                    ),
                }
                for item in internal_candidates
                if not item.requires_model_decision
            ]
            deterministic_candidates = []
            for check in prompt_checks:
                for candidate in check.get(
                    "candidates",
                    check.get("deterministic_candidates", []),
                ):
                    prompt_candidate = (
                        dict(zip(candidate_legend, candidate, strict=True))
                        if candidate_legend is not None
                        else dict(candidate)
                    )
                    internal = internal_by_id[prompt_candidate["candidate_id"]]
                    prompt_candidate.update(
                        {
                            "canonical_root_type": internal.canonical_root_type,
                            "root_severity_rule_id": (
                                internal.root_severity_rule_id
                            ),
                            "merge_group_id": internal.merge_group_id,
                            "merge_compatible_candidate_types": (
                                internal.merge_compatible_candidate_types
                            ),
                            "core_primary_evidence_source_ids": (
                                internal.core_primary_evidence_source_ids
                            ),
                            "context_primary_evidence_source_ids": (
                                internal.context_primary_evidence_source_ids
                            ),
                            "deterministic_severity_factors": (
                                internal.deterministic_severity_factors
                            ),
                        }
                    )
                    deterministic_candidates.append(
                        {
                            "check_code": check["check_code"],
                            **prompt_candidate,
                        }
                    )
            batch_specs.append(
                {
                    "batch_id": batch_id,
                    "check_codes": [
                        item.check_code
                        for item in contexts[batch_id].assigned_check_specs
                    ],
                    "required_ir_types": sorted(
                        {
                            ir_type
                            for spec in contexts[
                                batch_id
                            ].assigned_check_specs
                            for ir_type in spec.required_ir_types
                        }
                    ),
                    "estimated_input_tokens": contexts[
                        batch_id
                    ].estimated_input_tokens,
                    "prompt_token_estimate": prompt_estimate,
                    "projected_ir_count": len(ir_refs),
                    "source_excerpt_count": len(anchor_refs),
                    "evidence_source_count": len(
                        {
                            source[0]
                            for check in prompt_checks
                            for source in check.get(
                                "allowed_evidence_sources",
                                [],
                            )
                        }
                    ),
                    "absence_evidence_source_count": len(
                        {
                            source[0]
                            for check in prompt_checks
                            for source in check.get(
                                "allowed_absence_sources",
                                [],
                            )
                        }
                    ),
                    "source_counts_by_check": {
                        check["check_code"]: len(
                            check.get("allowed_evidence_sources", [])
                        )
                        for check in prompt_checks
                    },
                    "absence_source_counts_by_check": {
                        check["check_code"]: len(
                            check.get("allowed_absence_sources", [])
                        )
                        for check in prompt_checks
                    },
                    "deterministic_candidates": deterministic_candidates,
                    "all_internal_candidates": [
                        item.model_dump(mode="json")
                        for item in internal_candidates
                    ],
                    "deterministic_precondition_decisions": (
                        deterministic_precondition_decisions
                    ),
                }
            )
        if unit_id == "performance_obligations":
            _validate_po_fixture_oracle(batch_specs)
        if unit_id == "ip_confidentiality_data":
            _validate_icd_fixture_oracle(batch_specs)
        if unit_id == "liability_remedies_exit":
            _validate_lre_fixture_oracle(batch_specs)
        if any(value > 6000 for value in prompt_estimates):
            failure = (
                f"{unit_id}: prompt hard limit exceeded: {prompt_estimates}"
            )
            artifact["domains"][unit_id] = {
                "batch_ids": list(unit.batch_ids),
                "check_codes": [item.check_code for item in unit.check_specs],
                "prompt_token_estimates": prompt_estimates,
                "batch_specs": batch_specs,
                "runs": [],
                "raw_batch_results": [],
                "failures": [failure],
            }
            artifact["status"] = "FAILED"
            artifact["failures"] = [failure]
            return artifact
        runs = []
        raw_results = []
        for run_index in range(1, args.repetitions + 1):
            try:
                result, wall_duration_ms, batch_results = await _run_one_unit(
                    plan,
                    unit,
                    tenant_id=args.tenant_id,
                    model_id=args.model_id,
                    attempt_artifact_sink=attempt_sink,
                )
            except DirectReviewError as exc:
                failure = {
                    "unit_id": unit_id,
                    "run_index": run_index,
                    "error_code": exc.code,
                    "error_message": str(exc),
                    "attempt_diagnostic_count": len(exc.attempt_diagnostics),
                }
                if attempt_sink is not None:
                    attempt_sink.finalize(status="FAILED", failure=failure)
                artifact["domains"][unit_id] = {
                    "batch_ids": list(unit.batch_ids),
                    "check_codes": [item.check_code for item in unit.check_specs],
                    "prompt_token_estimates": prompt_estimates,
                    "batch_specs": batch_specs,
                    "runs": runs,
                    "raw_batch_results": raw_results,
                    "failures": [failure],
                }
                artifact["status"] = "FAILED"
                artifact["failures"] = [failure]
                return artifact
            except Exception as exc:
                failure = {
                    "unit_id": unit_id,
                    "run_index": run_index,
                    "error_code": type(exc).__name__,
                    "error_message": str(exc),
                    "attempt_diagnostic_count": len(
                        attempt_sink.records if attempt_sink is not None else []
                    ),
                }
                if attempt_sink is not None:
                    attempt_sink.finalize(status="FAILED", failure=failure)
                artifact["domains"][unit_id] = {
                    "batch_ids": list(unit.batch_ids),
                    "check_codes": [item.check_code for item in unit.check_specs],
                    "prompt_token_estimates": prompt_estimates,
                    "batch_specs": batch_specs,
                    "runs": runs,
                    "raw_batch_results": raw_results,
                    "failures": [failure],
                }
                artifact["status"] = "FAILED"
                artifact["failures"] = [failure]
                return artifact
            failures = _append_unit_run_and_validate(
                unit,
                runs,
                raw_results,
                summary=_unit_summary(
                    result,
                    plan=plan,
                    wall_duration_ms=wall_duration_ms,
                    batch_results=batch_results,
                ),
                raw_batch_results=[
                    item.model_dump(mode="json") for item in batch_results
                ],
            )
            if failures:
                all_failures.extend(
                    f"{unit_id}: {item}" for item in failures
                )
                durations = [item["wall_duration_ms"] for item in runs]
                artifact["domains"][unit_id] = {
                    "batch_ids": list(unit.batch_ids),
                    "check_codes": [
                        item.check_code for item in unit.check_specs
                    ],
                    "prompt_token_estimates": prompt_estimates,
                    "batch_specs": batch_specs,
                    "duration_ms": {
                        "min": min(durations),
                        "median": round(statistics.median(durations)),
                        "max": max(durations),
                    },
                    "runs": runs,
                    "raw_batch_results": raw_results,
                    "failures": failures,
                    "fail_fast": {
                        "failed_run_index": run_index,
                        "requested_repetitions": args.repetitions,
                        "executed_repetitions": len(runs),
                        "skipped_repetitions": (
                            args.repetitions - len(runs)
                        ),
                    },
                }
                if attempt_sink is not None:
                    attempt_sink.finalize(
                        status="FAILED",
                        failure={
                            "run_index": run_index,
                            "quality_failures": failures,
                        },
                    )
                artifact["status"] = "FAILED"
                artifact["failures"] = all_failures
                return artifact
        failures: list[str] = []
        all_failures.extend(f"{unit_id}: {item}" for item in failures)
        durations = [item["wall_duration_ms"] for item in runs]
        artifact["domains"][unit_id] = {
            "batch_ids": list(unit.batch_ids),
            "check_codes": [item.check_code for item in unit.check_specs],
            "prompt_token_estimates": prompt_estimates,
            "batch_specs": batch_specs,
            "duration_ms": {
                "min": min(durations),
                "median": round(statistics.median(durations)),
                "max": max(durations),
            },
            "runs": runs,
            "raw_batch_results": raw_results,
            "failures": failures,
        }
        if failures:
            if attempt_sink is not None:
                attempt_sink.finalize(
                    status="FAILED",
                    failure={"quality_failures": failures},
                )
            artifact["status"] = "FAILED"
            artifact["failures"] = all_failures
            return artifact
        if attempt_sink is not None:
            attempt_sink.finalize(status="PASSED")
    artifact["status"] = "PASSED"
    artifact["failures"] = []
    return artifact


def _bundle_summary(bundle, *, plan) -> dict[str, Any]:
    findings = [
        finding for unit in bundle.units for finding in unit.findings
    ]
    batch_by_id = {item.batch_id: item for item in bundle.batch_results}
    unit_metric_by_id = {
        item.unit_id: item for item in bundle.metrics.unit_metrics
    }
    unit_acceptance_summaries = [
        _unit_summary(
            unit,
            plan=plan,
            wall_duration_ms=unit_metric_by_id[unit.unit_id].wall_duration_ms,
            batch_results=[batch_by_id[batch_id] for batch_id in unit.batch_ids],
        )
        for unit in bundle.units
    ]
    return {
        "status": bundle.status,
        "bundle_id": bundle.bundle_id,
        "identity": bundle.identity.model_dump(mode="json"),
        "wall_duration_ms": bundle.metrics.wall_duration_ms,
        "queue_duration_ms": bundle.metrics.queue_duration_ms,
        "peak_concurrency": bundle.metrics.peak_concurrency,
        "batch_count": bundle.metrics.batch_count,
        "unit_count": bundle.metrics.unit_count,
        "model_call_count": bundle.metrics.model_call_count,
        "repair_count": bundle.metrics.repair_count,
        "tool_call_count": bundle.metrics.tool_call_count,
        "prompt_tokens": bundle.metrics.prompt_tokens,
        "cached_tokens": bundle.metrics.cached_tokens,
        "completion_tokens": bundle.metrics.completion_tokens,
        "total_tokens": bundle.metrics.total_tokens,
        "slowest_batch_id": bundle.metrics.slowest_batch_id,
        "slowest_batch_duration_ms": bundle.metrics.slowest_batch_duration_ms,
        "slowest_unit_id": bundle.metrics.slowest_unit_id,
        "slowest_unit_duration_ms": bundle.metrics.slowest_unit_duration_ms,
        "batch_execution_metrics": [
            item.model_dump(mode="json") for item in bundle.metrics.batch_metrics
        ],
        "unit_execution_metrics": [
            item.model_dump(mode="json") for item in bundle.metrics.unit_metrics
        ],
        "cross_unit_overlap_candidates": [
            item.model_dump(mode="json")
            for item in bundle.cross_unit_overlap_candidates
        ],
        "check_codes": [
            item.check_code for unit in bundle.units for item in unit.check_results
        ],
        "finding_count": len(findings),
        "evidence_count": sum(
            len(item.evidence_candidates) for item in findings
        ),
        "canonical_risk_keys": [
            list(_canonical_risk_key(item)) for item in findings
        ],
        "unit_summaries": [
            {
                "unit_id": unit.unit_id,
                "batch_ids": unit.batch_ids,
                "model_call_count": unit.model_call_count,
                "repair_count": unit.repair_count,
                "duration_ms": unit.duration_ms,
                "check_codes": [
                    item.check_code for item in unit.check_results
                ],
                "finding_count": len(unit.findings),
            }
            for unit in bundle.units
        ],
        "unit_acceptance_summaries": unit_acceptance_summaries,
    }


def _validate_commercial_bundle_run(
    run: dict[str, Any],
    *,
    run_index: int,
) -> list[str]:
    failures: list[str] = []
    cf005 = [
        item for item in run["findings"] if item["check_code"] == "CF-005"
    ]
    if len(cf005) != 1:
        failures.append(
            f"run {run_index}: Commercial CF-005 Finding is missing or duplicated"
        )
        return failures
    finding = cf005[0]
    if finding["risk_level"] != "HIGH":
        failures.append(f"run {run_index}: Commercial CF-005 is not HIGH")
    evidence_types = {
        item["evidence_type"] for item in finding["evidence_candidates"]
    }
    if evidence_types.isdisjoint({"TEXT_QUOTE", "CONTEXT"}):
        failures.append(
            f"run {run_index}: Commercial CF-005 lacks payment source Evidence"
        )
    if "ABSENCE" not in evidence_types:
        failures.append(
            f"run {run_index}: Commercial CF-005 lacks safeguard ABSENCE Evidence"
        )
    return failures


def _commercial_core_stability_key(run: dict[str, Any]) -> dict[str, Any]:
    cf005 = [
        item
        for item in run["canonical_source_risk_keys"]
        if item[0] == "CF-005"
    ]
    normalized_cf005 = [
        [
            item[0],
            item[1],
            item[2],
            sorted(
                source_id
                for source_id in item[3]
                if not source_id.startswith("ABSENCE:")
            ),
            any(
                source_id.startswith("ABSENCE:")
                for source_id in item[3]
            ),
        ]
        for item in cf005
    ]
    return {
        "cf003_status": run["check_statuses"].get("CF-003"),
        "cf003_reason": run["reason_codes"].get("CF-003"),
        "cf004_status": run["check_statuses"].get("CF-004"),
        "cf004_reason": run["reason_codes"].get("CF-004"),
        "cf005": normalized_cf005,
    }


def _validate_commercial_bundle_runs(
    runs: list[dict[str, Any]],
) -> list[str]:
    if not runs:
        return ["Commercial Bundle has no completed run"]
    first = _commercial_core_stability_key(runs[0])
    return [
        (
            f"run {index}: Commercial CF-003/CF-004/CF-005 core "
            "risk, level or Evidence changed"
        )
        for index, run in enumerate(runs[1:], 2)
        if _commercial_core_stability_key(run) != first
    ]


def _bundle_core_stability_set(value: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for unit in value["unit_acceptance_summaries"]:
        unit_id = unit["unit_id"]
        if unit_id == "commercial_financial":
            key = ["commercial_financial", _commercial_core_stability_key(unit)]
            keys.add(
                json.dumps(
                    key,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            continue
        key_field = (
            "canonical_root_stability_keys"
            if unit_id
            in {
                "performance_obligations",
                "ip_confidentiality_data",
                "liability_remedies_exit",
            }
            else "canonical_risk_keys"
        )
        keys.update(
            json.dumps(
                [unit_id, item],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            for item in unit[key_field]
        )
    return keys


def _validate_bundles(
    bundles: list[dict[str, Any]],
    *,
    plan,
) -> list[str]:
    failures = []
    unit_by_id = {
        str(item.unit_id): item
        for item in plan.review_units
        if str(item.unit_id) in BASE_UNIT_IDS
    }
    for index, value in enumerate(bundles, 1):
        if len(value["check_codes"]) != 34 or len(set(value["check_codes"])) != 34:
            failures.append(f"run {index}: 34 Check coverage failed")
        if value["batch_count"] != 7 or value["unit_count"] != 5:
            failures.append(f"run {index}: Batch or Unit count failed")
        if value["model_call_count"] != 7:
            failures.append(f"run {index}: normal call count is not 7")
        if value["repair_count"] != 0:
            failures.append(f"run {index}: a repair occurred")
        if value["tool_call_count"] != 0:
            failures.append(f"run {index}: Tool call count is nonzero")
        if value["peak_concurrency"] != 7:
            failures.append(f"run {index}: peak concurrency is not 7")
        if value["wall_duration_ms"] > 60000:
            failures.append(f"run {index}: Bundle hard performance limit exceeded")
        if any(
            metric["wall_duration_ms"] > 60000
            for metric in value["batch_execution_metrics"]
        ):
            failures.append(f"run {index}: Batch hard performance limit exceeded")
        if any(
            metric["status"] != "COMPLETED"
            for metric in value["batch_execution_metrics"]
        ):
            failures.append(f"run {index}: a required Batch did not complete")
        for unit_run in value["unit_acceptance_summaries"]:
            unit_id = unit_run["unit_id"]
            unit_runs = [
                prior["unit_acceptance_summaries"][
                    next(
                        item_index
                        for item_index, item in enumerate(
                            prior["unit_acceptance_summaries"]
                        )
                        if item["unit_id"] == unit_id
                    )
                ]
                for prior in bundles[:index]
            ]
            if unit_id == "commercial_financial":
                failures.extend(
                    f"{unit_id}: {failure}"
                    for failure in _validate_unit_runs(
                        unit_by_id[unit_id],
                        [unit_run],
                    )
                )
                failures.extend(
                    _validate_commercial_bundle_run(
                        unit_run,
                        run_index=index,
                    )
                )
                failures.extend(
                    _validate_commercial_bundle_runs(unit_runs)
                )
            else:
                failures.extend(
                    f"{unit_id}: {failure}"
                    for failure in _validate_unit_runs(
                        unit_by_id[unit_id],
                        unit_runs,
                    )
                )
    risk_sets = [_bundle_core_stability_set(value) for value in bundles]
    if any(value != risk_sets[0] for value in risk_sets[1:]):
        failures.append("three Bundles have substantively different canonical risks")
    return failures


async def _run_bundles(
    args,
    plan,
    *,
    contract_hash: str,
    fixture_id: str,
) -> dict[str, Any]:
    bundles = []
    raw = []
    attempt_sink = (
        AtomicAttemptArtifactSink(
            args.attempt_output,
            metadata={
                "phase": "bundle",
                "fixture_id": fixture_id,
                "contract_hash": contract_hash,
                "plan_id": plan.plan_id,
                "plan_hash": plan.plan_hash,
            },
        )
        if args.attempt_output is not None
        else None
    )
    for index in range(1, args.repetitions + 1):
        try:
            value = await execute_base_risk_review_bundle(
                plan,
                tenant_id=args.tenant_id,
                model_id=args.model_id,
                contract_hash=contract_hash,
                fixture_id=fixture_id,
                framework_run_id=f"stage63-bundle-{index}",
                attempt_artifact_sink=attempt_sink,
            )
        except BaseBundleExecutionError as exc:
            if attempt_sink is not None:
                attempt_sink.finalize(
                    status="FAILED",
                    failure=exc.failure.model_dump(mode="json"),
                )
            return {
                "artifact_type": "CONTRACT_RISK_STAGE63_BUNDLE_ACCEPTANCE_V1",
                "phase": "bundle",
                "fixture_hashes": EXPECTED_HASHES,
                "plan_id": plan.plan_id,
                "plan_hash": plan.plan_hash,
                "status": "FAILED",
                "duration_ms": None,
                "bundles": bundles,
                "raw_bundles": raw,
                "failure": exc.failure.model_dump(mode="json"),
                "failures": [
                    f"run {index}: {exc.failure.error_code}: "
                    f"{exc.failure.error_message}"
                ],
            }
        bundles.append(_bundle_summary(value, plan=plan))
        raw.append(value.model_dump(mode="json"))
        round_failures = _validate_bundles(bundles, plan=plan)
        if round_failures:
            if attempt_sink is not None:
                attempt_sink.finalize(
                    status="FAILED",
                    failure={
                        "run_index": index,
                        "quality_failures": round_failures,
                    },
                )
            return {
                "artifact_type": "CONTRACT_RISK_STAGE63_BUNDLE_ACCEPTANCE_V1",
                "phase": "bundle",
                "fixture_hashes": EXPECTED_HASHES,
                "plan_id": plan.plan_id,
                "plan_hash": plan.plan_hash,
                "status": "FAILED",
                "duration_ms": bundle_duration_summary(
                    [
                        __import__(
                            "services.contract.capabilities.risk_review_bundle",
                            fromlist=["BaseRiskReviewBundle"],
                        ).BaseRiskReviewBundle.model_validate(item)
                        for item in raw
                    ]
                ),
                "bundles": bundles,
                "raw_bundles": raw,
                "failures": round_failures,
            }
    failures = _validate_bundles(bundles, plan=plan)
    if attempt_sink is not None:
        attempt_sink.finalize(
            status="PASSED" if not failures else "FAILED",
            failure=(
                None
                if not failures
                else {"quality_failures": failures}
            ),
        )
    return {
        "artifact_type": "CONTRACT_RISK_STAGE63_BUNDLE_ACCEPTANCE_V1",
        "phase": "bundle",
        "fixture_hashes": EXPECTED_HASHES,
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "status": "PASSED" if not failures else "FAILED",
        "duration_ms": bundle_duration_summary(
            [
                # The raw model validates the same payload and keeps the
                # acceptance summary independent from object identity.
                __import__(
                    "services.contract.capabilities.risk_review_bundle",
                    fromlist=["BaseRiskReviewBundle"],
                ).BaseRiskReviewBundle.model_validate(item)
                for item in raw
            ]
        ),
        "bundles": bundles,
        "raw_bundles": raw,
        "failures": failures,
    }


async def _main(args) -> None:
    fixture_dir = args.fixture_dir.resolve(strict=True)
    _hashes(fixture_dir)
    value = load_fixed_risk_plan_input(fixture_dir)
    semantic = value.stage_result.semantic_ir.model_dump(mode="json")
    if sum(len(items) for items in semantic.values()) != 101:
        raise RuntimeError("Fixed Fixture must contain 101 IR items")
    if len(value.source_blocks) != 93:
        raise RuntimeError("Fixed Fixture must contain 93 Blocks")
    plan = RiskReviewPlanBuilder().build(value)
    fixture_payload = json.loads(
        (
            fixture_dir
            / "contract-risk-review-fixture-service-outsourcing-0829-v1.json"
        ).read_text("utf-8")
    )
    contract_hash = fixture_payload["source_document"]["content_sha256"]
    if args.phase == "plan":
        artifact = _build_plan_audit(args, value, plan)
    elif args.phase == "domains":
        artifact = await _run_domains(args, plan)
    elif args.phase == "po-replay":
        artifact = _run_po_offline_replay(args, plan)
    else:
        artifact = await _run_bundles(
            args,
            plan,
            contract_hash=contract_hash,
            fixture_id=FIXTURE_ID,
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    args.output.write_text(text, "utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "status": artifact["status"],
                "failures": artifact["failures"],
                "duration_ms": artifact.get("duration_ms"),
            },
            ensure_ascii=False,
        )
    )
    if artifact["status"] != "PASSED":
        raise SystemExit(2)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--phase",
        choices=("plan", "domains", "po-replay", "bundle"),
        required=True,
    )
    parser.add_argument("--fixture-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--attempt-output", type=Path)
    parser.add_argument("--replay-attempts", type=Path)
    parser.add_argument("--unit-id", choices=GENERIC_UNIT_IDS)
    parser.add_argument("--tenant-id", default=DEFAULT_TENANT_ID)
    parser.add_argument("--model-id", default="deepseek-v4-pro")
    parser.add_argument("--repetitions", type=int, default=3, choices=range(1, 6))
    asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    main()
