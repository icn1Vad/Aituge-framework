import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from contract.rule_evidence.semantic_selection import select_rule_applicability
from risk_test_data import risk_plan_input


def snapshot():
    return NS(manifest={'snapshot_hash':'sha256:'+'1'*64},rules=[
        NS(party_stance=role,contract_type_path=['采购合同','技术服务合同'])
        for role in ['买受方','出卖方','服务方','委托方','甲方','乙方']])


class Runtime:
    def __init__(self,answer=None,fail=False):
        self.calls=[]; self.fail=fail
        self.answer=answer or dict(status='RESOLVED',business_roles=['买受方'],contract_types=['采购合同'],
                                  reason='我方为采购人，向对方采购设备',source_refs=['F001'])
    async def complete_with_usage(self,**kwargs):
        self.calls.append(kwargs)
        if self.fail: raise RuntimeError('provider unavailable')
        return NS(content=json.dumps(self.answer,ensure_ascii=False),prompt_tokens=100,completion_tokens=40)


def resolve(tmp_path,runtime,**overrides):
    args=dict(value=risk_plan_input(),snapshot=snapshot(),tenant_id='42',model_id='fake',
              runtime_factory=lambda:runtime,cache_directory=tmp_path,declared_roles=['采购人'])
    args.update(overrides)
    return asyncio.run(select_rule_applicability(**args))


def test_semantic_role_uses_catalogue_and_contract_and_cached_result(tmp_path):
    runtime=Runtime()
    first=resolve(tmp_path,runtime); second=resolve(tmp_path,runtime)
    assert first['business_roles']==['买受方'] and first['model_calls']==1
    assert second['cache_hit'] and second['model_calls']==0 and len(runtime.calls)==1
    payload=json.loads(runtime.calls[0]['messages'][0]['content'])
    assert payload['declared_business_roles']==['采购人']
    assert '买受方' in payload['role_catalog'].values() and '甲方' not in payload['role_catalog'].values()
    assert payload['fragments'] and payload['our_party']=='甲方单位'
    assert 'rules' not in payload and runtime.calls[0]['thinking_override'] is False


def test_multiple_roles_and_types_preserved_and_reversed_party_never_assumed(tmp_path):
    runtime=Runtime(dict(status='RESOLVED',business_roles=['出卖方','服务方','服务方'],
        contract_types=['采购合同','技术服务合同'],reason='甲方承担供货和安装服务',source_refs=[]))
    value=risk_plan_input().model_copy(update={'our_party':'供应商','counterparty':'客户'})
    result=resolve(tmp_path,runtime,value=value)
    assert set(result['business_roles'])=={'出卖方','服务方'}
    assert len(result['contract_types'])==2
    payload=json.loads(runtime.calls[0]['messages'][0]['content'])
    assert payload['our_party']=='供应商' and payload['perspective']=='PARTY_A'
    resolve(tmp_path,runtime,value=value.model_copy(update={'perspective':'PARTY_B','our_party':'客户','counterparty':'供应商'}))
    assert len(runtime.calls)==2


@pytest.mark.parametrize('bad',['new_role','new_type','empty_roles','empty_types','provider'])
def test_invalid_selection_is_recorded_unresolved_and_not_retried(tmp_path,bad):
    runtime=Runtime()
    if bad=='new_role': runtime.answer['business_roles']=['模型自造角色']
    elif bad=='new_type': runtime.answer['contract_types']=['自造合同']
    elif bad=='empty_roles': runtime.answer['business_roles']=[]
    elif bad=='empty_types': runtime.answer['contract_types']=[]
    else: runtime.fail=True
    first=resolve(tmp_path,runtime); second=resolve(tmp_path,runtime)
    assert first['status']=='UNRESOLVED' and first['business_roles']==[]
    assert second['cache_hit'] and len(runtime.calls)==1


def test_selection_cache_isolates_tenant_confirmed_party_and_snapshot(tmp_path):
    runtime=Runtime(); resolve(tmp_path,runtime)
    resolve(tmp_path,runtime,tenant_id='43')
    changed=snapshot(); changed.manifest['snapshot_hash']='sha256:'+'2'*64
    resolve(tmp_path,runtime,snapshot=changed)
    resolve(tmp_path,runtime,value=risk_plan_input().model_copy(update={'our_party':'人工更正公司'}))
    assert len(runtime.calls)==4


def test_semantic_alias_reaches_rule_reviewer_and_canonical_card_without_ui_changes(tmp_path):
    from contract.rule_evidence.execution import RuleLibraryExecution
    from contract.rule_evidence.finding_projection import merge_rule_findings
    from test_rule_library_shadow import snapshot as write_snapshot
    from test_rule_library_reviewer import FakeModel
    from test_rule_finding_projection import fixture, validate
    from contract.evidence_planning.review_result import RuleReviewResult
    (tmp_path/'formal').mkdir()
    (tmp_path/'routing').mkdir()
    value,_,formal=fixture(tmp_path/'formal')
    semantic=Runtime(); review=FakeModel()
    class Routing:
        async def complete_with_usage(self,**kwargs):
            return await (semantic if kwargs['review_unit_id']=='rule_library_selection' else review).complete_with_usage(**kwargs)
    execution=RuleLibraryExecution(write_snapshot(tmp_path/'routing'),tmp_path/'cache',
        runtime_factory=lambda _:Routing(),semantic_selection=True)
    async def run():
        return await execution.run(value,'42','fake',business_roles=['采购人'],infer_business_role=False)
    result=asyncio.run(run())
    assert result.status=='COMPLETED' and result.business_roles==['买受方']
    merged,linked=merge_rule_findings(formal,result,value)
    canonical=validate(merged)
    assert canonical.findings and all(d.finding_id for d in linked.decisions)
    transported=RuleReviewResult.model_validate_json(linked.model_dump_json(exclude_none=True))
    assert transported.semantic_selection['business_roles']==['买受方']
    again=asyncio.run(run())
    assert again.semantic_selection['model_calls']==0 and len(semantic.calls)==1 and review.calls==1


def test_unresolved_semantic_selection_never_reaches_rule_model(tmp_path):
    from contract.rule_evidence.execution import RuleLibraryExecution
    from test_rule_library_shadow import snapshot as write_snapshot
    runtime=Runtime(dict(status='UNRESOLVED',business_roles=[],contract_types=[],reason='信息不足',source_refs=[]))
    execution=RuleLibraryExecution(write_snapshot(tmp_path),tmp_path/'cache',runtime_factory=lambda _:runtime,
                                    semantic_selection=True)
    result=asyncio.run(execution.run(risk_plan_input(),'42','fake',business_roles=['采购人']))
    assert result.status=='SELECTION_UNRESOLVED' and result.model_calls==0
    assert len(runtime.calls)==1 and result.semantic_selection['status']=='UNRESOLVED'
