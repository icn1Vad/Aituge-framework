"""One bounded real-model check against the user's local procurement test document."""
import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree
from zipfile import ZipFile
from datetime import date

from aituge_model.config import ModelRuntimeProvider
from service.conversation.llm_runner import LlmRuntime
from contract.rule_evidence.models import RuleEvidenceIssue, RuleEvidencePlanRequest
from contract.rule_evidence.planner import AdaptiveRuleEvidencePlanner
from contract.rule_evidence.binding import RuleEvidenceBinder
from contract.rule_evidence.snapshot import LocalRuleSnapshot
from contract.rule_evidence.reviewer import RuleLibraryReviewer, cached_rule_review


async def main():
    import os
    document = Path("/samples/C02_设备采购_R2_付款验收风险.docx")
    digest = hashlib.sha256(document.read_bytes()).hexdigest()
    with ZipFile(document) as archive:
        xml = ElementTree.fromstring(archive.read("word/document.xml"))
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs = ["".join(t.text or "" for t in p.findall(".//w:t", ns)) for p in xml.findall(".//w:p", ns)]
    clause = next(p for p in paragraphs if p.startswith("3.1 "))
    snapshot = LocalRuleSnapshot("/rule-data")
    matches = [rule for rule in snapshot.rules if rule.name == "预付款比例与担保机制"
               and rule.review_standard == "neutral" and rule.party_stance == "买受方"
               and "采购合同" in rule.contract_type_path]
    if len(matches) != 1:
        raise ValueError("The exact existing source rule is not unique")
    issue = RuleEvidenceIssue(issue_id="rule-issue-" + "a" * 32, domain="commercial_financial",
                              query="预付款比例与担保机制", facts=[clause],
                              required_concepts=["预付款"], check_codes=["CF-005"])
    request = RuleEvidencePlanRequest(review_id="rule-smoke-c02r2", generation_id="doc-" + digest[:32],
        tenant_id="42", contract_type="PROCUREMENT", contract_type_aliases=["采购合同"], perspective="PARTY_A",
        business_role="买受方", review_standard="neutral", preview_pending=True,
        review_as_of_date=date(2026, 9, 5), source_version=snapshot.manifest["source_version"],
        frozen_snapshot_hash=snapshot.manifest["snapshot_hash"], issues=[issue], rules=matches)
    bundle = RuleEvidenceBinder().bind(AdaptiveRuleEvidencePlanner().plan(request))
    source = SimpleNamespace(source_id="smoke-source-c02r2-3-1", block_id="docx-paragraph-3-1",
                             char_start=0, quoted_text=clause, allowed_check_codes=["CF-005"])
    plan = SimpleNamespace(review_id=request.review_id, generation_id=request.generation_id,
                           perspective="PARTY_A", plan_hash="sha256:" + digest,
                           contexts=[SimpleNamespace(evidence_sources=[source])])
    observation = {"bundle": bundle.model_dump(mode="json"), "business_role": "买受方", "selection_warnings": []}
    provider = ModelRuntimeProvider.from_environment(directory=os.environ.get("MODEL_CONFIG_DIR"), pack_id=os.environ.get("MODEL_PACK_ID"))
    model_id = provider.active_pack.llm.id
    async def run():
        runtime = LlmRuntime("42", model_runtime_provider=provider, provider_max_retries=0)
        return await RuleLibraryReviewer(runtime, max_calls=1, max_prompt_chars=6000).review(
            observation, plan, tenant_id="42", model_id=model_id, mode="PREVIEW")
    result = await cached_rule_review("/rule-output/model-cache", {
        "document_sha256": digest, "bundle_hash": bundle.bundle_hash, "model_id": model_id,
        "version": "rule-model-smoke-v1"}, run)
    Path("/rule-output/c02-rule-review-result.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    print(json.dumps({"status": result.status, "model_calls": result.model_calls,
                      "prompt_tokens": result.prompt_tokens, "completion_tokens": result.completion_tokens,
                      "decisions": [d.model_dump(mode="json") for d in result.decisions],
                      "diagnostics": result.diagnostics}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
