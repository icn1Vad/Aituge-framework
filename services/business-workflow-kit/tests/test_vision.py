import asyncio
import io
import json
import pytest
from PIL import Image
from business_workflow_kit.vision import (VisionError, render_pages, image_catalog, normalize_extraction,
    normalize_review, extract_document, review_documents, RULES)

def png():
    output=io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(output,"PNG")
    return output.getvalue()

def test_image_evidence_identity_is_backend_owned():
    data=png(); pages=render_pages(data); catalog=image_catalog(data,pages)
    assert len(catalog)==1 and catalog[0]["page_number"]==1
    assert catalog[0]["document_version"]!=catalog[0]["image_sha256"]
    assert catalog==image_catalog(data,pages)
    assert "quoted_text" not in catalog[0]

def test_actual_pdfium_renders_every_pdf_page_with_pinned_runtime():
    pytest.importorskip("pypdfium2")
    from pypdf import PdfWriter
    output=io.BytesIO(); writer=PdfWriter()
    writer.add_blank_page(width=100,height=150);writer.add_blank_page(width=200,height=100)
    writer.write(output); data=output.getvalue()
    pages=render_pages(data)
    assert len(pages)==2 and all(page[0].startswith(b"\xff\xd8") for page in pages)
    assert [row['page_number'] for row in image_catalog(data,pages)]==[1,2]

@pytest.mark.parametrize("data", [b"",b"not a document",b"%PDF-broken"])
def test_invalid_files_are_explicit_not_empty_success(data):
    with pytest.raises(VisionError): render_pages(data)

def test_bad_fact_does_not_destroy_valid_siblings_or_invent_positions():
    catalog=image_catalog(png(),render_pages(png())); ref=catalog[0]["source_id"]
    result=normalize_extraction({"facts":[{"field":"signatures","value":["可见签名"],"evidence_refs":[ref],"page_number":999},
        {"field":"date","value":"invented","evidence_refs":["wrong"]}],"installments":[]},catalog,"ACCEPTANCE")
    assert len(result["facts"])==1 and result["warnings"]
    assert isinstance(result["facts"][0]["value"],str)
    assert "page_number" not in result["facts"][0]

def test_empty_extraction_requires_review():
    assert normalize_extraction({},[],"CONTRACT")["status"]=="NEEDS_REVIEW"

@pytest.mark.parametrize("kind", ["CONTRACT", "INVOICE", "ACCEPTANCE"])
def test_auto_classifies_and_extracts_in_one_visual_call(kind):
    class Fake:
        calls=0
        async def complete(self, **kwargs):
            self.calls+=1
            assert "AUTO" in kwargs["prompt"] and kwargs["pages"]
            ref=kwargs["catalog"][0]["source_id"]
            return {"classification":{"kind":kind,"evidence_refs":[ref]},
                "facts":[{"field":"project_name","value":"项目","evidence_refs":[ref]}],
                "installments":[{"label":"第二期","payer":"甲方","amount":"100","currency":"CNY","conditions":"验收后","evidence_refs":[ref]}]},{}
    client=Fake(); result=asyncio.run(extract_document(png(),"AUTO",client))
    assert client.calls==1 and result["kind"]==kind and result["status"]=="EXTRACTED"
    assert bool(result["installments"])==(kind=="CONTRACT")
    assert not result["classificationPending"]


@pytest.mark.parametrize("labels", [["*生产生活服务*服务费"], ["设备维护及技术服务"], ["*生产生活服务*服务费", "设备维护费"]])
def test_invoice_item_labels_are_requested_and_preserved_from_the_same_visual_call(labels):
    class Fake:
        calls=0
        async def complete(self, **kwargs):
            self.calls+=1
            assert kwargs["pages"] and "item_name" in kwargs["prompt"]
            assert "保留星号和文字顺序" in kwargs["prompt"] and "不生成 expense_type" in kwargs["prompt"]
            ref=kwargs["catalog"][0]["source_id"]
            return {"classification":{"kind":"INVOICE","evidence_refs":[ref]},
                "facts":[{"field":"item_name","value":label,"evidence_refs":[ref]} for label in labels]},{}
    client=Fake();result=asyncio.run(extract_document(png(),"AUTO",client))
    assert client.calls==1 and result["kind"]=="INVOICE"
    assert [fact["value"] for fact in result["facts"]]==labels
    assert all(fact["evidence_refs"]==[result["evidence"][0]["source_id"]] for fact in result["facts"])


@pytest.mark.parametrize("value", [["服务费"], {"name":"服务费"}, 123])
def test_nontext_invoice_items_are_not_serialized_into_expense_text(value):
    result=normalize_extraction({"facts":[{"field":"item_name","value":value,"evidence_refs":["P1"]},
        {"field":"invoice_number","value":"123","evidence_refs":["P1"]}]},[{"source_id":"P1"}],"INVOICE")
    assert result["facts"][0]["value"] is None
    assert result["facts"][1]["value"]=="123" and result["status"]=="EXTRACTED"

@pytest.mark.parametrize("classification", [None,{}, {"kind":"INVOICE","evidence_refs":["fake"]}, {"kind":[],"evidence_refs":[]}, {"kind":"OTHER","evidence_refs":["P1"]}])
def test_unknown_or_ungrounded_category_stays_pending_without_losing_facts(classification):
    result=normalize_extraction({"classification":classification,"facts":[{"field":"project_name","value":"项目","evidence_refs":["P1"]}]},[{"source_id":"P1"}],"AUTO")
    assert result["kind"]=="OTHER" and result["classificationPending"]
    assert result["status"]=="NEEDS_REVIEW" and result["facts"]

def test_explicit_category_remains_compatible_with_existing_clients():
    result=normalize_extraction({"classification":{"kind":"INVOICE","evidence_refs":["P1"]},"facts":[{"field":"project_name","value":"项目","evidence_refs":["P1"]}]},[{"source_id":"P1"}],"ACCEPTANCE")
    assert result["kind"]=="ACCEPTANCE" and result["status"]=="EXTRACTED"
    assert result["classification"]["evidence_refs"]==[]  # A user hint is not a visual classification for AUTO cache reuse.

def test_cross_document_evidence_is_not_limited_to_check_category():
    payload={"lines":[{"id":"L1"}],"documents":[{"evidence":[{"source_id":"acceptance-page"}]}]}
    records=normalize_review({"records":[{"check_code":"PAYMENT_CONDITIONS","subject_id":"L1","status":"PASS",
        "message":"本期验收条件有材料支持","evidence_refs":["acceptance-page"]}]},payload)
    assert records[0]["status"]=="PASS"

def test_unknown_ref_is_isolated_and_other_installment_is_not_passed():
    payload={"lines":[{"id":"second"},{"id":"third"}],"documents":[{"evidence":[{"source_id":"A1"}]}]}
    rows=normalize_review({"records":[{"check_code":"PAYMENT_CONDITIONS","subject_id":"second","status":"PASS","message":"验收材料齐全","evidence_refs":["wrong"]}]},payload)
    assert {row["subjectId"] for row in rows}=={"second","third"}
    assert all(row["status"]=="PENDING" for row in rows)
    assert any(row["checkCode"]=="AUDIT_COVERAGE" for row in rows)

def test_empty_inputs_do_not_spend_a_model_call():
    class Never:
        async def complete(self,**kwargs): raise AssertionError("must not call model")
    assert asyncio.run(review_documents({"documents":[],"lines":[{"id":"one"}]},Never()))["records"][0]["status"]=="PENDING"

def test_real_image_parts_reach_vision_transport_not_ocr(monkeypatch):
    class Fake:
        async def complete(self,**kwargs):
            assert kwargs["pages"][0][0].startswith(b"\xff\xd8")
            assert kwargs["model_id"]=="deepseek-v4-flash-vision-exp"
            ref=kwargs["catalog"][0]["source_id"]
            return {"facts":[{"field":"signatures","value":"一处可见手写签名，真实性未核验","evidence_refs":[ref]}]}, {"model":"test"}
    result=asyncio.run(extract_document(png(),"ACCEPTANCE",Fake()))
    assert result["method"]=="VISION_API" and result["status"]=="EXTRACTED"


def test_whole_bill_review_targets_all_rules_without_repeating_document_fragments():
    payload={"lines":[{"id":"second"},{"id":"third"}],"documents":[{
        "facts":[{"field":"buyer_name","value":"买方","evidence_refs":["A1"]}],
        "evidence":[{"source_id":"A1"}],"warnings":["未经证据绑定的日期猜测"],
        "originalRecognition":{"warnings":["旧猜测"]},"unverified_model_notes":["模型反向日期猜测"],
        "normalization_warnings":["格式诊断"]}]}
    class Fake:
        calls=0
        async def complete(self,**kwargs):
            self.calls+=1
            data=json.loads(kwargs["prompt"].split("\n")[-1])
            assert data["review_targets"]==[
                {"subject_id":subject,"check_codes":[rule["code"] for rule in RULES]}
                for subject in ["second","third"]]
            assert "未经证据绑定的日期猜测" not in kwargs["prompt"]
            assert "模型反向日期猜测" not in kwargs["prompt"]
            assert "格式诊断" not in kwargs["prompt"]
            assert "originalRecognition" not in data["data"]["documents"][0]
            assert data["data"]["documents"][0]["facts"]==payload["documents"][0]["facts"]
            return {"records":[{"check_code":rule["code"],"subject_id":subject,"status":"PASS",
                "message":"依据材料逐项核对","evidence_refs":["A1"]}
                for subject in ["second","third"] for rule in RULES]},{}
    client=Fake(); result=asyncio.run(review_documents(payload,client))
    assert client.calls==1
    assert len(result["records"])==len(RULES)*2
    assert all(row["status"]=="PASS" for row in result["records"])
    assert payload["documents"][0]["warnings"]==["未经证据绑定的日期猜测"]


def test_free_form_date_warning_is_not_a_published_recognition_notice():
    catalog=image_catalog(png(),render_pages(png())); ref=catalog[0]["source_id"]
    wrong="2025年12月11日晚于2025年12月30日"
    raw={"warnings":[wrong],"facts":[{"field":"party_a_signing_date","value":"2025-12-11","evidence_refs":[ref]}],
         "recognition_issues":[{"field":"party_b_signing_date","code":"BLANK","evidence_refs":[ref],"message":wrong}]}
    result=normalize_extraction(raw,catalog,"CONTRACT")
    assert result["warnings"]==[]
    assert result["unverified_model_notes"]==[wrong]
    assert result["facts"][0]["value"]=="2025-12-11"
    assert result["recognition_issues"]==[{"field":"party_b_signing_date","code":"BLANK","evidence_refs":[ref]}]


def test_date_and_recognition_issue_contract_disallows_unbound_unsupported_diagnoses():
    from business_workflow_kit.vision import EXTRACTION_PROMPT, PROMPT_VERSION
    assert PROMPT_VERSION=="huatai-vision-6"
    for field in ["party_a_signing_date","party_b_signing_date","delivery_deadline"]: assert field in EXTRACTION_PROMPT
    catalog=image_catalog(png(),render_pages(png())); ref=catalog[0]["source_id"]
    result=normalize_extraction({"recognition_issues":[
        {"field":"party_a_signing_date","code":"LATER_THAN_DELIVERY","evidence_refs":[ref]},
        {"field":"party_b_signing_date","code":"BLANK","evidence_refs":["fake"]}]},catalog,"CONTRACT")
    assert result["recognition_issues"]==[]


@pytest.mark.parametrize("field,value",[("check_code",[]),("subject_id",{}),("status",[])])
def test_malformed_model_fields_are_local_not_a_whole_review_exception(field,value):
    bad={"check_code":"PAYMENT_CONDITIONS","subject_id":"second","status":"PASS",
         "message":"错误条目","evidence_refs":["A1"]}
    bad[field]=value
    good={"check_code":"CONTRACT_MATCH","subject_id":"third","status":"PASS",
          "message":"正常条目保留","evidence_refs":["A1"]}
    payload={"lines":[{"id":"second"},{"id":"third"}],"documents":[{"evidence":[{"source_id":"A1"}]}]}
    rows=normalize_review({"records":[bad,good]},payload)
    assert any(row["subjectId"]=="third" and row["status"]=="PASS" for row in rows)
    assert any(row["subjectId"]=="second" and row["status"]=="PENDING" for row in rows)

@pytest.mark.parametrize("requirement,expected", [("REQUIRED","REQUIRED"),("NOT_REQUIRED","NOT_REQUIRED"),(None,"UNKNOWN"),([],"UNKNOWN"),("PASS","UNKNOWN")])
def test_acceptance_requirement_is_typed_and_evidence_bound(requirement,expected):
    period={"label":"本期","conditions":"本期支付条件","acceptance_requirement":requirement,"evidence_refs":["P1"]}
    result=normalize_extraction({"installments":[period]},[{"source_id":"P1"}],"CONTRACT")
    assert result["installments"][0]["acceptance_requirement"]==expected
    period["evidence_refs"]=["invented"]
    assert not normalize_extraction({"installments":[period]},[{"source_id":"P1"}],"CONTRACT")["installments"]
