"""Procurement adapter example. Consumes grounded facts, not raw model guesses.

No contract numbers, dates or quotas are hard-coded here. History=None means unknown,
not zero. Each expense line selects a confirmed installment independently.
"""
from decimal import Decimal, InvalidOperation
from .audit import AuditContext, AuditRecord

CHECK_CODE = "INSTALLMENT_LIMIT"


def money(value) -> Decimal:
    if value is None or isinstance(value, (bool, float)):
        raise ValueError("Money must be an exact decimal string or Decimal")
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Invalid money") from exc
    if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal("0.01")):
        raise ValueError("Invalid currency amount")
    return amount


def check_installments(context: AuditContext) -> list[AuditRecord]:
    facts = context.facts
    periods = facts.get("installments", {})
    lines = facts.get("expense_lines", [])
    if not lines:
        return [AuditRecord(CHECK_CODE, "PENDING", "尚无可核对的费用明细")]
    totals: dict[str, Decimal] = {}
    invalid_periods: set[str] = set()
    seen: set[str] = set()
    invalid_lines: set[str] = set()
    for line in lines:
        key, period = str(line.get("id", "")), str(line.get("installment_id", ""))
        if not key or key in seen:
            invalid_lines.add(key)
            invalid_periods.add(period)
        seen.add(key)
        definition = periods.get(period)
        if (not definition or not definition.get("currency")
                or line.get("currency") != definition["currency"]
                or line.get("association_confirmed") is not True):
            # Unconfirmed/mixed-currency siblings cannot form a definite total.
            invalid_periods.add(period)
        try:
            totals[period] = totals.get(period, Decimal("0")) + money(line.get("amount"))
        except ValueError:
            invalid_lines.add(key)
            invalid_periods.add(period)
    records = []
    for line in lines:
        key, period_id = str(line.get("id", "")), str(line.get("installment_id", ""))
        period = periods.get(period_id)
        refs = tuple(period.get("evidence_refs", ())) if period else ()
        def record(status, message):
            return AuditRecord(CHECK_CODE, status, message, key, refs)
        if key in invalid_lines or period_id in invalid_periods:
            records.append(record("PENDING", "同期明细的金额、编号、币种或期次关联尚未确认，无法核定累计额度"))
            continue
        if not period or line.get("association_confirmed") is not True or not refs:
            records.append(record("PENDING", "需确认本明细对应的合同期次及原文依据"))
            continue
        if not period.get("currency") or line.get("currency") != period["currency"]:
            records.append(record("PENDING", "币种未确认或不一致，不能直接比较金额"))
            continue
        try:
            cap = money(period.get("amount"))
            used = money(period.get("used_amount"))
        except ValueError:
            records.append(record("PENDING", "合同期次金额或历史已使用金额尚未核实"))
            continue
        if totals[period_id] + used > cap:
            records.append(record("RISK", f"本单该期明细合计 {totals[period_id]:.2f} 元，加已使用 {used:.2f} 元，超过该期额度 {cap:.2f} 元"))
        elif period.get("conditions_met") is False:
            records.append(record("RISK", "金额未超额，但该期付款条件尚未满足"))
        elif period.get("conditions_met") is not True:
            records.append(record("PENDING", "金额未超额，但该期付款条件尚待材料核实"))
        else:
            records.append(record("PASS", "已确认的本期金额及付款条件检查通过"))
    return records
