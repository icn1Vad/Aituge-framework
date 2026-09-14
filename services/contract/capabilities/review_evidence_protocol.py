"""Model-facing source selection; legacy I/A bindings stay server-side only.

This adapter does not validate a legal conclusion. It fixes provenance before
the existing domain/business validators run, and never guesses a missing ID.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from pydantic import ValidationError

EVIDENCE_PROTOCOL_VERSION = "model-selected-evidence-v7-contract-and-law-ids"
DIRECT_CHECK_REQUIRED_FIELDS = ['check_code', 'status', 'decision_note', 'findings']
SOURCE_SELECTION_RULES = [
    "主证据由模型从本批合同原文目录中选择primary_evidence_source_ids；原文可跨检查引用，检索归类不是引用权限。候选触发材料不是自动结论。",
    "一个Source ID已绑定事实与原文。不要输出evidence_type、ir_ref、evidence_ref、原文位置或自己配对编号；程序负责这些字段。",
    "法律依据在legal_evidence_ids中选择法律目录编号，并在issue/decision_summary说明适用关系；法条不得放入合同证据ID数组。",
    "原文中的时间、前提、否定及例外优先于拆分后的事实摘要；证据不足用未决状态，不把局部未见说成全文缺失。",
]


class SourceSelectionError(ValueError):
    def __init__(self, message: str, check_code: str | None = None, *, issues=None, normalized_output=None):
        super().__init__(message)
        self.check_code = check_code
        self.issues = issues or []
        self.check_codes = sorted({i['check_code'] for i in self.issues if i.get('check_code')}) or ([check_code] if check_code else [])
        self.normalized_output = normalized_output
        self.repairable = not any(i.get('repairable') is False for i in self.issues)
        self.validation_stage = ('EVIDENCE' if any(i['stage']=='EVIDENCE' for i in self.issues)
                                 else 'SCHEMA' if any(i['stage']=='SCHEMA' for i in self.issues) else 'BUSINESS')
        self.code = ('RISK_EVIDENCE_SELECTION_INVALID' if self.validation_stage=='EVIDENCE'
                     else 'RISK_DIRECT_SCHEMA_INVALID' if self.validation_stage=='SCHEMA'
                     else next((i['error_code'] for i in self.issues if i.get('error_code')), 'RISK_DIRECT_SCHEMA_INVALID'))


def select_source_ids(value: Any, allowed: set[str], *, required: bool = True) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(s, str) or not s for s in value):
        raise SourceSelectionError("primary/source selection must be an explicit array of source IDs")
    if required and not value:
        raise SourceSelectionError("A resolved judgment requires selected primary evidence")
    if len(value) != len(set(value)):
        raise SourceSelectionError("Duplicate source IDs are not independent evidence")
    if set(value) - allowed:
        raise SourceSelectionError("Unknown, legal, or out-of-scope absence ID; select real sources from the current contract catalogue: "
                                   + json.dumps(sorted(set(value) - allowed), ensure_ascii=False))
    return list(value)


def model_legal_catalog(catalog: list[dict]) -> list[dict]:
    # Preserve the frozen law ID for explicit selection; no model-generated
    # coordinates, versions, hashes or prose-based source reconstruction.
    return copy.deepcopy(catalog)


@dataclass
class DirectSourceCatalog:
    bindings: dict[str, dict] = field(default_factory=dict)
    records: dict[str, dict] = field(default_factory=dict)
    allowed: dict[str, set[str]] = field(default_factory=dict)
    complete_scopes: set[str] = field(default_factory=set)
    known_scopes: set[str] = field(default_factory=set)
    allow_absence_assessment: bool = False
    anchor_ids: dict[str, str] = field(default_factory=dict)

    @classmethod
    def build(cls, request, ir_refs, anchor_refs):
        catalog = cls(allow_absence_assessment=request.unit_id == "commercial_financial")
        specs = {s.check_code for s in request.assigned_check_specs}
        scopes = {s.check_code: s for s in request.check_task_scopes}
        catalog.known_scopes = set(scopes)
        catalog.complete_scopes = {code for code, scope in scopes.items() if scope.complete}
        policies = {p.check_code: p for p in getattr(request, "check_evidence_policies", [])}
        existing = {(s.ir_item_id, s.anchor_id): s for s in getattr(request, "evidence_sources", [])}
        anchors = {a.anchor_id: (ref, a) for ref, a in anchor_refs.items()}
        for ir_ref, item in ir_refs.items():
            for anchor in item.source_anchors:
                ar, excerpt = anchors[anchor.anchor_id]
                source = existing.get((item.item_id, anchor.anchor_id))
                key = json.dumps([request.generation_id, item.item_id, anchor.anchor_id, excerpt.quoted_text_hash])
                sid = source.source_id if source else "contract-es-" + hashlib.sha256(key.encode()).hexdigest()[:32]
                catalog.bindings[sid] = dict(evidence_type=source.evidence_type if source else "TEXT_QUOTE",
                    ir_ref=ir_ref, evidence_ref=ar, checked_scope=None, verification_note=None)
                catalog.records[sid] = dict(ir_type=item.ir_type, subject=item.subject,
                    predicate=item.predicate, object=item.object, quoted_text=excerpt.quoted_text)
                catalog.anchor_ids[sid] = anchor.anchor_id
                for code in specs:
                    policy, scope = policies.get(code), scopes.get(code)
                    if policy is not None and sid not in policy.allowed_evidence_source_ids:
                        continue
                    if policy is None and scope is not None and (item.item_id not in scope.provided_item_ids
                            or anchor.anchor_id not in scope.provided_anchor_ids):
                        continue
                    catalog.allowed.setdefault(code, set()).add(sid)
        for source in getattr(request, "absence_evidence_sources", []):
            policy = policies.get(source.check_code)
            if policy is None or source.source_id not in policy.allowed_absence_source_ids:
                continue
            catalog.bindings[source.source_id] = dict(evidence_type="ABSENCE", ir_ref=None,
                evidence_ref=None, checked_scope=source.checked_scope, verification_note=source.verification_method)
            catalog.records[source.source_id] = dict(source_kind="SCOPED_ABSENCE",
                checked_scope=source.checked_scope, missing_target=source.missing_target,
                verification_method=source.verification_method)
            catalog.allowed.setdefault(source.check_code, set()).add(source.source_id)
        for code in specs:
            catalog.allowed.setdefault(code, set())
        # Retrieval ownership is not an access boundary within the same frozen
        # contract batch. Share verbatim text, but never borrow absence proofs
        # from another check or sources from another document/generation.
        text_ids = {sid for sid, binding in catalog.bindings.items() if binding['evidence_type'] != 'ABSENCE'}
        for code in specs:
            catalog.allowed[code].update(text_ids)
        return catalog

    def prompt_contract(self) -> dict:
        return {"version": EVIDENCE_PROTOCOL_VERSION, "rules": SOURCE_SELECTION_RULES + [
            "Finding必须填写primary_evidence_source_ids；可多选，不设固定数量。不得再输出evidence对象。",
            "若检查项只有一条Finding，检查级decision_evidence_source_ids可作为该Finding的明确证据选择；多条Finding须逐条选择，不自动分配。",
            "条款已有但留空、含糊或不完整，选该条款Source，不得当作全文缺失。",
            "财务的纯缺失判断单独填写absence_assessments[{checked_scope,verification_note}]；只有scope_complete的检查可用，且不是OCR/附件完整性证明。",
            *(["CF-005的candidate_evidence_source_ids只选已有保障或付款原文，不填Evidence对象。"] if 'CF-005' in self.allowed else [])],
            "allowed_source_ids_by_check": {k: sorted(v) for k, v in self.allowed.items()},
            "absence_assessment_allowed_checks": sorted(self.complete_scopes) if self.allow_absence_assessment else []}

    def ids_for_refs(self, ir_refs, anchor_refs, check_code=None):
        return sorted(sid for sid, binding in self.bindings.items()
                      if binding["ir_ref"] in ir_refs and binding["evidence_ref"] in anchor_refs
                      and (check_code is None or sid in self.allowed.get(check_code, set())))

    def decode(self, raw: dict, *, schema_model=None, normalize=None, validate_check=None) -> dict:
        result = copy.deepcopy(raw)
        checks = result.get("check_results", result.get("checks"))
        if not isinstance(checks, list):
            return result  # JSON/schema layer reports this independently.
        issues = []
        def issue(code, path, message, *, stage='EVIDENCE', value=None):
            issues.append(dict(stage=stage, check_code=code, path=path, type='source_selection' if stage=='EVIDENCE' else 'invalid_shape',
                message=message, rejected_value=value, allowed_source_ids=sorted(self.allowed.get(code, set()))))
        def select(value, code, path, required=True):
            try:
                return select_source_ids(value, self.allowed.get(code, set()), required=required)
            except SourceSelectionError as exc:
                issue(code, path, str(exc), value=value)
                return []
        for ci, check in enumerate(checks):
            if not isinstance(check, dict):
                issue(None, ['check_results', ci], 'Each check must be an object', stage='SCHEMA')
                continue
            code = check.get("check_code")
            path = ['check_results', ci]
            if not isinstance(code, str):
                issue(None, path+['check_code'], 'check_code must be a string', stage='SCHEMA')
                continue
            # A legacy alias is a spelling correction, not a new judgment.
            if 'check_status' in check:
                alias = check['check_status']
                if 'status' not in check and isinstance(alias, str) and alias in {'REVIEWED','NOT_APPLICABLE','FAILED'}:
                    check['status'] = check.pop('check_status')
                elif check.get('status') == alias:
                    check.pop('check_status')
                else:
                    issue(code, path+['check_status'], 'check_status conflicts with status or is invalid; resolve explicitly', stage='SCHEMA', value=alias)
            findings = check.get('findings', [])
            if not isinstance(findings, list):
                issue(code, path+['findings'], 'findings must be an explicit array; do not discard an unknown result', stage='SCHEMA', value=findings)
                findings = []
            # A check with exactly one Finding has an unambiguous owner for
            # model-selected check evidence. Reuse that explicit selection,
            # never a retrieval candidate or a source inferred from prose.
            # Multiple Findings still need their own model-selected bindings.
            if len(findings) == 1 and isinstance(findings[0], dict):
                only = findings[0]
                if (only.get('primary_evidence_source_ids') in (None, [])
                        and not only.get('absence_assessments')
                        and isinstance(check.get('decision_evidence_source_ids'), list)
                        and check['decision_evidence_source_ids']):
                    only['primary_evidence_source_ids'] = copy.deepcopy(check['decision_evidence_source_ids'])
            for fi, finding in enumerate(findings):
                fp = path+['findings', fi]
                if not isinstance(finding, dict):
                    issue(code, fp, 'Each Finding must be an object', stage='SCHEMA')
                    continue
                owner = finding.get('check_code')
                if owner is None and code in self.allowed and code.startswith('CF-'):
                    # FVA already binds optional metadata in _resolve_finding_fields.
                    # CF used to require the model to repeat this server-known ID.
                    finding['check_code'] = code
                elif owner is not None and owner != code:
                    issue(code, fp+['check_code'], 'Finding check_code conflicts with its enclosing check', stage='BUSINESS', value=owner)
                    issues[-1]['error_code'] = ('RISK_FINDING_CHECK_INVALID' if code.startswith('CF-') else 'RISK_FINDING_CHECK_CONFLICT')
                if "evidence" in finding or "evidence_source_ids" in finding:
                    issue(code, fp, 'Legacy evidence/type/I-A output is not accepted; select primary_evidence_source_ids')
                missing = finding.pop('absence_assessments', [])
                if not isinstance(missing, list):
                    issue(code, fp+['absence_assessments'], 'absence_assessments must be an array', stage='SCHEMA')
                    missing = []
                ids = select(finding.pop('primary_evidence_source_ids', None), code, fp+['primary_evidence_source_ids'], required=not missing)
                evidence = [copy.deepcopy(self.bindings[sid]) for sid in ids]
                if missing and (not self.allow_absence_assessment or code not in self.known_scopes):
                    issue(code, fp+['absence_assessments'], 'Absence cannot be inferred from missing scope metadata')
                else:
                    # A known partial scope reaches the existing quarantine
                    # validator. It is kept as an unresolved observation, not
                    # a verified Finding and not a reason to lose other checks.
                    for ai, assessment in enumerate(missing):
                        if (not isinstance(assessment, dict) or set(assessment) != {"checked_scope", "verification_note"}
                                or any(not isinstance(v, str) or not v.strip() for v in assessment.values())):
                            issue(code, fp+['absence_assessments',ai], 'Absence requires an explicit scope and reason, without text IDs')
                            continue
                        evidence.append(dict(evidence_type="ABSENCE", ir_ref=None, evidence_ref=None, **assessment))
                finding['evidence'] = evidence
            if 'candidate_evidence' in check:
                issue(code, path+['candidate_evidence'], 'Select candidate_evidence_source_ids, not candidate_evidence objects')
            if code == 'CF-005':
                ids = select(check.pop('candidate_evidence_source_ids', None), code, path+['candidate_evidence_source_ids'], required=False)
                check['candidate_evidence'] = [copy.deepcopy(self.bindings[sid]) for sid in ids]
            status = check.get('status')
            if schema_model is not None and isinstance(check.get('decision_note'), str) and not check['decision_note'].strip():
                issue(code, path+['decision_note'], 'decision_note must contain a nonblank explanation', stage='SCHEMA')
            if not isinstance(status, str):
                issue(code, path+['status'], 'status must be a string', stage='SCHEMA', value=status)
            resolved_negative = isinstance(status, str) and status in {'REVIEWED','NOT_APPLICABLE'} and not findings
            decision_ids = select(check.get('decision_evidence_source_ids', []), code, path+['decision_evidence_source_ids'],
                                  required=resolved_negative and bool(self.allowed.get(code)))
            check['decision_evidence_source_ids'] = decision_ids
            check['decision_anchor_ids'] = sorted({self.anchor_ids[sid] for sid in decision_ids if sid in self.anchor_ids})
        # A source error must not prevent independent schema validation. The
        # diagnostic projection is never accepted if ANY layer reports errors.
        validation_value = result
        if normalize is not None:
            try:
                validation_value = normalize(copy.deepcopy(result))
            except (ValueError, TypeError):
                # Malformed input still reaches the typed validator below.
                validation_value = result
        if schema_model is not None:
            try:
                schema_model.model_validate(validation_value)
            except ValidationError as exc:
                for error in exc.errors(include_input=False):
                    loc = list(error['loc'])
                    code = None
                    if len(loc)>1 and loc[0]=='check_results' and isinstance(loc[1],int) and loc[1]<len(checks):
                        row = checks[loc[1]]
                        code = row.get('check_code') if isinstance(row,dict) and isinstance(row.get('check_code'),str) else None
                    # Empty internal Evidence caused by a rejected selection is
                    # not a second model field to repair. Keep the wire location.
                    if 'evidence' in loc and any(i['path'][:4]==loc[:4] and 'primary_evidence_source_ids' in i['path'] for i in issues):
                        continue
                    if any(i['path']==loc and i['stage']=='SCHEMA' for i in issues):
                        continue
                    issues.append(dict(stage='SCHEMA',check_code=code,path=loc,type=error['type'],message=error['msg']))
            # Reuse the domain validator for structurally valid, source-bound
            # siblings. One bad source must not hide another check's business
            # error until the only repair has already been spent. Never certify
            # the diagnostic projection or synthesize a missing source/verdict.
            if validate_check is not None:
                for ci, row in enumerate(validation_value.get('check_results', [])):
                    if not isinstance(row, dict) or row.get('check_code') not in self.allowed:
                        continue
                    code = row['check_code']
                    if any(i.get('check_code') == code for i in issues):
                        continue
                    try:
                        single = schema_model.model_validate({'check_results': [row]})
                    except ValidationError:
                        continue  # Already collected by the envelope validator.
                    for error in validate_check(single):
                        error = copy.deepcopy(error)
                        loc = error.get('path', [])
                        error['path'] = ['check_results', ci, *loc[2:]] if loc[:1] == ['check_results'] else ['check_results', ci, *loc]
                        error['check_code'] = code
                        issues.append(error)
        if issues:
            raise SourceSelectionError('; '.join(f"{i['check_code'] or '?'} {i['path']}: {i['message']}" for i in issues),
                                       issues[0].get('check_code'), issues=issues, normalized_output=validation_value)
        return result


def direct_wire_schema(schema: dict, *, negative_evidence_checks=None) -> dict:
    """Reuse the business schema; expose selection fields instead of internal Evidence."""
    result = copy.deepcopy(schema)
    ids = {"type": "array", "items": {"type": "string"}, "uniqueItems": True}
    def visit(node):
        if not isinstance(node, dict):
            return
        props = node.get("properties", {})
        required = node.get("required", [])
        props.pop('decision_anchor_ids', None)  # server-owned, never model coordinates
        if 'findings' in props and 'status' in props and 'decision_note' in props:
            node['required'] = list(dict.fromkeys([*required, *DIRECT_CHECK_REQUIRED_FIELDS]))
            condition = {'status': {'enum':['REVIEWED','NOT_APPLICABLE']}, 'findings': {'type':'array','maxItems':0}}
            if negative_evidence_checks is not None:
                condition['check_code'] = {'enum':list(negative_evidence_checks)}
            if negative_evidence_checks is None or negative_evidence_checks:
                node.setdefault('allOf', []).append({'if':{'properties':condition,'required':['check_code','status','findings']},
                    'then':{'required':['decision_evidence_source_ids'], 'properties':{'decision_evidence_source_ids':{**ids,'minItems':1}}}})
        if "evidence" in props:
            props.pop("evidence")
            props.pop("evidence_source_ids", None)
            props.pop('check_code', None)
            node["required"] = [v for v in required if v not in {"evidence",'check_code'}] + ["primary_evidence_source_ids"]
            props["primary_evidence_source_ids"] = copy.deepcopy(ids)
            props["absence_assessments"] = {"type": "array", "items": {"type": "object",
                "additionalProperties": False, "required": ["checked_scope", "verification_note"],
                "properties": {key: {"type": "string", "minLength": 1} for key in ("checked_scope", "verification_note")}}}
        if "candidate_evidence" in props:
            props.pop("candidate_evidence")
            props["candidate_evidence_source_ids"] = copy.deepcopy(ids)
        for key, value in list(node.items()):
            if key == "$defs":
                # Evidence definitions are no longer part of the model contract.
                for name in list(value):
                    if name.endswith("EvidenceDraft"):
                        value.pop(name)
            if isinstance(value, dict):
                visit(value)
            elif isinstance(value, list):
                for entry in value:
                    visit(entry)
    visit(result)
    return result


def scope_direct_repair_context(payload: dict, targets: list[str]) -> dict:
    """Narrow the requested outputs, NOT the frozen source/legal catalogue."""
    context = copy.deepcopy(payload)
    for field in ('assigned_check_specs', 'assigned_checks'):
        if field in context:
            legend = context.get('assigned_check_legend', [])
            code_index = legend.index('check_code') if 'check_code' in legend else None
            context[field] = [row for row in context[field] if
                (row.get('check_code') in targets if isinstance(row, dict) else
                 isinstance(row, list) and code_index is not None and len(row)>code_index and row[code_index] in targets)]
    if 'decision_policies' in context:
        context['decision_policies'] = {k:v for k,v in context['decision_policies'].items() if k in targets}
    contract = context.get('output_contract', {})
    for key in ('required_check_codes', 'expected_check_codes'):
        if key in contract:
            contract[key] = list(targets)
    conditional = contract.get('conditional_required_check_fields', {}).get('when', {})
    if 'check_codes' in conditional:
        conditional['check_codes'] = [code for code in conditional['check_codes'] if code in targets]
    if 'CF-005' not in targets:
        context.pop('cf005_candidate', None)
        contract.pop('cf005_required_fields', None)
        for item in (contract, context.get('source_selection_contract', {})):
            if 'rules' in item:
                item['rules'] = [rule for rule in item['rules'] if 'CF-005' not in rule]
    return context


def configure_direct_prompt(payload: dict, catalog: DirectSourceCatalog, *, response_schema=None) -> None:
    """Remove legacy binding vocabulary from both instructions and candidates."""
    payload["source_selection_contract"] = catalog.prompt_contract()
    visible = set().union(*catalog.allowed.values())
    payload["contract_evidence_catalog"] = {sid: catalog.records[sid] for sid in sorted(visible)}
    for key in ("projected_ir", "projected_ir_legend", "source_excerpts", "source_excerpt_legend"):
        payload.pop(key, None)
    def candidate(row, legend=None, check_code=None):
        item = dict(zip(legend, row)) if legend else dict(row)
        ir = item.pop("candidate_ir_refs", [])
        anchors = item.pop("candidate_evidence_refs", [])
        code = check_code or item.get('check_code')
        item["trigger_material_source_ids"] = catalog.ids_for_refs(ir, anchors, code)
        return item
    if 'CF-005' not in catalog.allowed:
        payload.pop('cf005_candidate', None)
        payload['output_contract'].pop('cf005_required_fields', None)
    if "cf005_candidate" in payload:
        old = payload["cf005_candidate"]
        value = candidate(old, check_code='CF-005')
        value["candidate_type"] = "PAYMENT_TIMING_AND_SECURITY_REVIEW"
        value["large_payment_share_observed"] = value.pop("substantial_prepayment")
        value["instruction"] = ("这是待核实的付款议题，不是已成立的预付款风险。先确认付款方及完整付款前提；"
            "比例大不等于付款早，保证金不完整也不能证明存在预付款。")
        value["payment_source_ids"] = catalog.ids_for_refs(value.pop("payment_ir_refs", []), value.pop("payment_evidence_refs", []), 'CF-005')
        payload["cf005_candidate"] = value
    if "deterministic_candidate_legend" in payload:
        legend = payload.pop("deterministic_candidate_legend")
        payload["deterministic_candidates"] = [candidate(row, legend) for row in payload["deterministic_candidates"]]
    for check in payload.get("assigned_checks", []):
        if not isinstance(check, dict):
            continue
        check.pop("allowed_evidence_sources", None)
        check.pop("allowed_absence_sources", None)
        if "deterministic_candidates" in check:
            legend = ["candidate_id", "candidate_type", "trigger_reason", "candidate_ir_refs", "candidate_evidence_refs", "requires_model_decision"]
            check["deterministic_candidates"] = [candidate(row, legend, check.get('check_code')) for row in check["deterministic_candidates"]]
    contract = payload["output_contract"]
    contract['required_check_fields'] = list(DIRECT_CHECK_REQUIRED_FIELDS)
    contract['status_enum'] = ['REVIEWED','NOT_APPLICABLE','FAILED']
    contract.pop('check_status_enum', None)
    negative_checks = sorted(code for code, ids in catalog.allowed.items() if ids)
    contract['conditional_required_check_fields'] = {
        'when': {'check_codes':negative_checks, 'status':['REVIEWED','NOT_APPLICABLE'], 'findings':[]},
        'required':['decision_evidence_source_ids'], 'minimum_selected_sources':1,
        'rule':'无Finding的已审查/不适用判断，必须在数组中选择支撑判断的本批合同Source ID；不能只把编号写在decision_note中。'}
    if response_schema is not None:
        contract['response_schema'] = direct_wire_schema(response_schema, negative_evidence_checks=negative_checks)
    contract["rules"] = [rule for rule in contract["rules"] if not any(word in rule for word in
        ("evidence_type", "ir_ref", "evidence_ref", "candidate_evidence", "TEXT_QUOTE", "ABSENCE", "allowed_evidence_sources"))]
    if 'CF-005' not in catalog.allowed:
        contract['rules'] = [rule for rule in contract['rules'] if 'CF-005' not in rule]
    contract["rules"].extend(catalog.prompt_contract()["rules"])
    contract['rules'].append('每个检查输出decision_evidence_source_ids数组：无Finding但判已审查/不适用时，选择实际支撑判断的本批合同Source；未知不能冒充无风险。已有Finding或没有可用Source时可为空。不要输出decision_anchor_ids，原文位置由程序绑定。')
    contract['rules'].append('检查状态字段名固定为status，不是check_status。Finding不输出check_code：程序从所属检查绑定，禁止另选检查。')
    contract["finding_required_fields"] = ["primary_evidence_source_ids" if key == "evidence" else key
        for key in contract["finding_required_fields"] if key != 'check_code']
    contract['finding_optional_fields'] = sorted(set(contract.get('finding_optional_fields', [])) | {'legal_evidence_ids'})
    if "cf005_candidate" in payload:
        contract["cf005_required_fields"] = ["candidate_decision", "identified_security_mechanisms", "candidate_evidence_source_ids"]
        contract["rules"].extend([
            "CF-005必须同时输出三个cf005_required_fields；identified_security_mechanisms和candidate_evidence_source_ids即使无内容也输出[]，其他检查不输出这三个字段。",
            "CF-005用TRIGGER_NOT_MET表示明确不成立的提前付款触发条件；用RISK_NOT_CONFIRMED表示有具体保障排除风险，此时保障名称与candidate_evidence_source_ids均须非空。",
            "CF-005的RISK_CONFIRMED必须有与原文相符的Finding；INSUFFICIENT_EVIDENCE必须status=FAILED，不得把未知当成触发不成立。",
        ])
    contract.pop("evidence_allowed_fields", None)
    contract.pop("minimal_evidence_shapes", None)
    if "legal_evidence_catalog" in payload:
        payload["legal_evidence_catalog"] = model_legal_catalog(payload["legal_evidence_catalog"])
