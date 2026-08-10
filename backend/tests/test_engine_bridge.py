from datetime import date
from decimal import Decimal
from sqlalchemy.orm import Session
from backend.app.db import Base, Product, Store, MonthlyTarget, Duty, SalaryPolicyVersion
from backend.app.services.engine_bridge import (
    rates_from_db, products_from_db, stores_from_db, targets_from_db,
    duty_override_from_db, days_in_month,
)


def _db():
    from sqlalchemy import create_engine
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return Session(eng)


def test_days_in_month():
    assert days_in_month("2026-06") == 30
    assert days_in_month("2024-02") == 29  # 闰年


def test_rates_roundtrip():
    """策略存百分数，rates_from_db 在边界 ÷100 转分数（ADR-009）。"""
    s = _db()
    cr = {"A": {"GE_100": {"低温高毛": "13"}}}  # 13（百分数）
    s.add(SalaryPolicyVersion(version=1, effective_from=date(2026, 6, 1), is_current=True,
                              content={"margin_rules": {}, "commission_rates": cr}))
    s.commit()
    table = rates_from_db(s)
    assert table.rates[("A", "GE_100", "低温高毛")] == Decimal("0.13")  # 13% -> 0.13


def test_rates_from_db_reads_salary_policy_divides_by_100():
    """TDD：单一真值源 SalaryPolicyVersion，百分数（15）÷100 = 分数（0.15）。"""
    s = _db()
    s.add(SalaryPolicyVersion(
        version=1, effective_from=date(2026, 1, 1), is_current=True,
        content={"margin_rules": {},
                 "commission_rates": {"A": {"GE_100": {"常温高毛": "15"}}}}))  # 15%
    s.commit()
    rt = rates_from_db(s, None)
    assert rt.rates[("A", "GE_100", "常温高毛")] == Decimal("0.15")  # 15% -> 0.15


def test_rates_from_db_locks_to_policy_version_id():
    """指定 policy_version_id 时用锁定版本，忽略 is_current。"""
    # 注：写端锁定 m.policy_version_id 在 T4.1 实现；此处仅验读端锁定逻辑。
    s = _db()
    s.add(SalaryPolicyVersion(
        version=1, effective_from=date(2026, 1, 1), is_current=True,
        content={"margin_rules": {},
                 "commission_rates": {"A": {"GE_100": {"常温高毛": "15"}}}}))  # 15%
    s.add(SalaryPolicyVersion(
        version=2, effective_from=date(2026, 6, 1), is_current=False,
        content={"margin_rules": {},
                 "commission_rates": {"A": {"GE_100": {"常温高毛": "20"}}}}))  # 20%
    s.commit()
    locked = s.query(SalaryPolicyVersion).filter_by(version=2).one()
    rt = rates_from_db(s, locked.id)
    assert rt.rates[("A", "GE_100", "常温高毛")] == Decimal("0.20")  # 锁定 v2：20% -> 0.20


def test_rates_from_db_404_when_no_policy():
    """没有激活策略时抛 ValueError（路由层翻译 404，见 workflow._run_compute）。"""
    import pytest
    s = _db()
    with pytest.raises(ValueError):
        rates_from_db(s, None)


def test_products_and_targets_bridge():
    s = _db()
    s.add(Product(barcode="6920001", name="低温奶", spec="200ml", category="低温奶", cost=2))
    s.add(Store(name="福景店", group="1组", store_class="A", supervisor="胡总"))
    s.add(MonthlyTarget(month="2026-06", store="福景店", target=84000))
    s.commit()
    p = products_from_db(s)
    assert p["6920001"].cost == 2 and p["6920001"].category == "低温奶"
    st = stores_from_db(s)
    assert st["福景店"].store_class == "A"
    tg = targets_from_db(s, "2026-06")
    assert tg["福景店"] == 84000


def test_duty_override_bridge():
    s = _db()
    s.add(Duty(month="2026-06", store="福景店", duty_date=date(2026, 6, 1), salesperson="高睿"))
    s.commit()
    ov = duty_override_from_db(s, "2026-06")
    assert ov[("福景店", date(2026, 6, 1))] == "高睿"


def test_sales_lines_from_db_carries_id():
    from backend.app.db import SessionLocal, SalesRecord
    from backend.app.services.engine_bridge import sales_lines_from_db
    from datetime import date
    db = SessionLocal()
    try:
        db.add(SalesRecord(month="2026-01", receipt="R1", store="S", sale_date=date(2026,1,1),
                           barcode="B", qty=1, amount=10, unit_price=10, salesperson="高睿",
                           cashier="", is_return=False, is_online=False, tag="有效"))
        db.commit()
        lines = sales_lines_from_db(db, "2026-01")
        assert len(lines) == 1
        assert lines[0].sales_record_id is not None
        assert lines[0].receipt == "R1"
    finally:
        db.query(SalesRecord).filter_by(receipt="R1").delete(); db.commit(); db.close()


def test_gift_deduction_from_db(db_session):
    from decimal import Decimal
    from backend.app.db import GiftDeduction
    from backend.app.services.engine_bridge import gift_deduction_from_db
    db_session.add(GiftDeduction(month="2026-06", receipt="R1", barcode="6920001",
                                 sales_qty=3, gift_qty=1, deduct_qty=1,
                                 reason="", resolution="", status="confirmed"))
    db_session.commit()
    out = gift_deduction_from_db(db_session, "2026-06")
    assert out == {("R1", "6920001"): Decimal(1)}
