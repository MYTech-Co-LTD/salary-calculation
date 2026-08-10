"""DB 数据 → salary_engine 对象的桥接（计算前组装引擎入参）。"""
import calendar
from datetime import date
from decimal import Decimal

from salary_engine.models import Product, Store, RateTable, SalesLine
from backend.app.db import Product as ProductRow, Store as StoreRow
from backend.app.db import MonthlyTarget, SalaryPolicyVersion, Duty, SalesRecord, GiftDeduction


def days_in_month(month: str) -> int:
    y, m = map(int, month.split("-"))
    return calendar.monthrange(y, m)[1]


def rates_from_db(db, policy_version_id: int = None) -> RateTable:
    """加载费率表（单一真值源：SalaryPolicyVersion）。

    策略存百分数（与 UI 编辑器一致），此处 ÷100 转分数供引擎使用（ADR-009）。
    指定 policy_version_id 则用锁定版本，否则取 is_current=True。
    """
    if policy_version_id:
        pv = db.get(SalaryPolicyVersion, policy_version_id)
    else:
        pv = db.query(SalaryPolicyVersion).filter_by(is_current=True).first()
    if not pv:
        raise ValueError("费率策略不存在，请先创建并激活工资策略")
    cr = (pv.content or {}).get("commission_rates", {}) or {}
    rates = {}
    for cls, by_bucket in cr.items():
        for bucket, by_tier in by_bucket.items():
            for tier, pct in by_tier.items():
                rates[(cls, bucket, tier)] = Decimal(str(pct)) / Decimal(100)
    return RateTable(version=pv.version, effective_from=pv.effective_from, rates=rates)


def products_from_db(db) -> dict:
    return {r.barcode: Product(r.barcode, r.name, r.spec, r.category,
                               Decimal(r.cost) if r.cost is not None else None,
                               bool(r.exclude_commission))
            for r in db.query(ProductRow).all()}


def stores_from_db(db) -> dict:
    return {r.name: Store(r.name, r.group, r.store_class, r.supervisor or "")
            for r in db.query(StoreRow).all()}


def targets_from_db(db, month: str) -> dict:
    return {r.store: Decimal(r.target)
            for r in db.query(MonthlyTarget).filter_by(month=month).all()}


def duty_override_from_db(db, month: str) -> dict:
    return {(r.store, r.duty_date): r.salesperson
            for r in db.query(Duty).filter_by(month=month).all()}


def gift_deduction_from_db(db, month: str) -> dict:
    """已确认的赠送扣除 → {(receipt,barcode): deduct_qty}（带符号，ADR-023）。"""
    return {(r.receipt, r.barcode): Decimal(str(r.deduct_qty))
            for r in db.query(GiftDeduction).filter_by(month=month).all()}


def sales_lines_from_db(db, month: str) -> list:
    """从 SalesRecord 构建 SalesLine（计算真值源），携带 sales_record_id。
    不过滤——把该月全部销售/退货行原样交给引擎；过滤（赠送/不计提成/非乳品）由引擎做。"""
    rows = db.query(SalesRecord).filter_by(month=month).all()
    out = []
    for r in rows:
        out.append(SalesLine(
            receipt=r.receipt, src_order=r.src_order, store=r.store, sale_date=r.sale_date,
            barcode=r.barcode, product_name=r.product_name or "", qty=r.qty, amount=r.amount,
            unit_price=r.unit_price, is_return=r.is_return, is_online=r.is_online,
            cashier=r.cashier or "", salesperson=r.salesperson or "",
            sales_record_id=r.id))
    return out
