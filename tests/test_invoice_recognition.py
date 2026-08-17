from decimal import Decimal

from backend.invoice_recognition import parse_invoice


def test_parse_vat_ordinary_invoice_fields() -> None:
    text = """
    电子发票（增值税普通发票）
    发票号码：26112000002169164236
    开票日期：2026年05月29日
    购买方名称：北京致远互联软件股份有限公司
    统一社会信用代码：91110108737656338N
    销售方名称：北京小白杨鸽子窝餐饮有限公司
    统一社会信用代码：91110112MACAHP9Q9Q
    金额 282.08
    税额 16.92
    价税合计（小写）￥299.00
    """

    invoice = parse_invoice("meal.pdf", text)

    assert len(invoice.invoice_id) == 64
    assert invoice.expense_category == "MEAL"
    assert invoice.invoice_type == "VAT_ORDINARY"
    assert invoice.invoice_number == "26112000002169164236"
    assert invoice.issue_date.isoformat() == "2026-05-29"
    assert invoice.buyer_name == "北京致远互联软件股份有限公司"
    assert invoice.buyer_tax_id == "91110108737656338N"
    assert invoice.seller_name == "北京小白杨鸽子窝餐饮有限公司"
    assert invoice.seller_tax_id == "91110112MACAHP9Q9Q"
    assert invoice.amount_excluding_tax == Decimal("282.08")
    assert invoice.tax_amount == Decimal("16.92")
    assert invoice.total_amount == Decimal("299.00")
    assert invoice.confidence == 1
    assert invoice.warnings == []


def test_parse_vat_special_invoice_fields() -> None:
    text = """
    电子发票（增值税专用发票）
    发票号码：26112000002169164237
    开票日期：2026年06月01日
    购买方名称：北京致远互联软件股份有限公司
    统一社会信用代码：91110108737656338N
    销售方名称：北京测试科技有限公司
    统一社会信用代码：91110108MA00000001
    金额 100.00
    税额 6.00
    价税合计（小写）￥106.00
    """

    invoice = parse_invoice("special.pdf", text)

    assert invoice.expense_category == "OTHER"
    assert invoice.invoice_type == "VAT_SPECIAL"
    assert invoice.invoice_number == "26112000002169164237"
    assert invoice.amount_excluding_tax == Decimal("100.00")
    assert invoice.tax_amount == Decimal("6.00")
    assert invoice.total_amount == Decimal("106.00")
    assert invoice.warnings == []


def test_parse_incomplete_invoice_requires_confirmation() -> None:
    text = """
    电子发票（增值税普通发票）
    开票日期：2026-08-06
    购买方名称：北京致远互联软件科技有限公司
    销售方名称：北京利通出行科技有限公司
    价税合计（小写）￥438.60
    """

    invoice = parse_invoice("taxi.pdf", text)

    assert invoice.total_amount == Decimal("438.60")
    assert invoice.confidence < 1
    assert "MISSING_INVOICE_NUMBER" in invoice.warnings


def test_expense_category_uses_invoice_content_and_file_name() -> None:
    assert parse_invoice("打车行程单.pdf", "价税合计 20.00").expense_category == "TRANSPORT"
    assert parse_invoice("invoice.pdf", "某某酒店 客房费 299.00").expense_category == "ACCOMMODATION"
    assert parse_invoice("invoice.pdf", "某某餐饮有限公司 299.00").expense_category == "MEAL"


def test_unknown_document_does_not_fabricate_invoice_data() -> None:
    invoice = parse_invoice("unknown.pdf", "普通文档，没有发票字段")

    assert invoice.invoice_type == "UNKNOWN"
    assert invoice.invoice_number is None
    assert invoice.buyer_name is None
    assert invoice.seller_name is None
    assert invoice.total_amount is None
    assert invoice.confidence == 0
    assert len(invoice.warnings) == 5
