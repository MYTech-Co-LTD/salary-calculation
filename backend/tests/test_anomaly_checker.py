from decimal import Decimal

from backend.app.services.anomaly_checker import AnomalyChecker


def _checker(db_session):
    return AnomalyChecker(db_session, "2026-06")


def test_gift_qty_mismatch_produces_type7(db_session):
    c = _checker(db_session)
    sales_qm = {("R1", "6920001"): Decimal(3)}
    gift_qm = {("R1", "6920001"): Decimal(1)}
    c.check_gift_qty_mismatch(sales_qm, gift_qm, set(), {("R1", "6920001"): "低温奶"})
    out = c.get_anomalies()
    assert len(out) == 1
    assert out[0]["anomaly_type"] == "7"
    assert out[0]["entity_id"] == "R1|6920001"
    assert "销售件数" in out[0]["description"] and "赠送件数" in out[0]["description"]


def test_gift_qty_mismatch_skips_confirmed(db_session):
    c = _checker(db_session)
    c.check_gift_qty_mismatch({("R1", "b"): Decimal(3)}, {("R1", "b"): Decimal(1)},
                              {("R1", "b")}, {("R1", "b"): "n"})
    assert c.get_anomalies() == []


def test_gift_qty_mismatch_skips_equal(db_session):
    c = _checker(db_session)
    c.check_gift_qty_mismatch({("R1", "b"): Decimal(1)}, {("R1", "b"): Decimal(1)},
                              set(), {("R1", "b"): "n"})
    assert c.get_anomalies() == []
