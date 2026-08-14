import openpyxl
from datetime import date
from decimal import Decimal


def _ws(path, header, rows):
    wb = openpyxl.Workbook(); ws = wb.active; ws.append(header)
    for r in rows: ws.append(r)
    wb.save(path)


def auth_header(client):
    t = client.post("/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    return {"Authorization": f"Bearer {t}"}


def test_import_products(tmp_path, client):
    h = auth_header(client)
    info = tmp_path / "info.xlsx"
    _ws(info, ["国际条码", "商品名称", "规格", "类别"], [["6920001", "低温奶", "200ml", "低温奶"]])
    cost = tmp_path / "cost.xlsx"
    _ws(cost, ["商品条码", "商品名称", "销售成本"], [["6920001", "低温奶（件）", "20"]])
    with open(info, "rb") as fi, open(cost, "rb") as fc:
        r = client.post("/import/products", headers=h,
                        files={"info": ("info.xlsx", fi), "cost": ("cost.xlsx", fc)})
    assert r.status_code == 200
    assert r.json()["products"] >= 1
    p = client.get("/products", headers=h).json()
    assert any(x["barcode"] == "6920001" and float(x["cost"]) == 20 for x in p)


def test_import_stores(tmp_path, client):
    h = auth_header(client)
    stores = tmp_path / "stores.xlsx"
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "cfg"
    ws.append(["主管", "类别", "组别", "名称", "本月目标"])
    ws.append(["胡总", "A", "1组", "福景店", 84000])
    wb.save(stores)
    with open(stores, "rb") as f:
        r = client.post("/import/stores", headers=h,
                        data={"sheet": "cfg"}, files={"file": ("stores.xlsx", f)})
    assert r.status_code == 200
    assert r.json()["stores"] >= 1
    s = {x["name"]: x for x in client.get("/stores", headers=h).json()}
    assert s["福景店"]["store_class"] == "A"


def _line(start, i):
    """构造一条 SalesLine（receipt 由 start+i 决定，便于批量造数据）"""
    from salary_engine.models import SalesLine
    return SalesLine(receipt=f"R{start + i}", src_order=None, store="S",
                     sale_date=date(2026, 1, 1), barcode="B", product_name="奶",
                     qty=1, amount=Decimal(1), unit_price=Decimal(1),
                     is_return=False, is_online=False, cashier="", salesperson="高睿")


def _lines(start, n):
    return [_line(start, i) for i in range(n)]


def test_import_sales_bulk_and_reimport_clears_stale(db_session):
    """T6.1: 批量导入正确性 + 重导清旧行（修 H4）

    - 500 行一次导入 → db_count==500（批量）
    - 同月重导 300 行（R500..R799）→ db_count==300（旧 R0..R499 已清除）
    """
    from backend.app.services.sales_importer import import_sales_to_db
    from backend.app.db import SalesRecord

    db = db_session

    # 首次导入 500 行
    r1 = import_sales_to_db(db, "2026-01", _lines(0, 500), set())
    assert r1["total"] == 500
    assert r1["db_count"] == 500
    assert db.query(SalesRecord).filter_by(month="2026-01").count() == 500

    # 重导 300 行（不同的 receipt）—— H4：旧行应被清除
    r2 = import_sales_to_db(db, "2026-01", _lines(500, 300), set())
    assert r2["total"] == 300
    assert r2["db_count"] == 300  # 不是 800
    # 旧 R0..R499 已不在库中
    assert db.query(SalesRecord).filter_by(month="2026-01", receipt="R0").count() == 0
    assert db.query(SalesRecord).filter_by(month="2026-01", receipt="R499").count() == 0
    # 新行存在
    assert db.query(SalesRecord).filter_by(month="2026-01", receipt="R500").count() == 1
    assert db.query(SalesRecord).filter_by(month="2026-01", receipt="R799").count() == 1


def test_import_sales_bulk_within_file_duplicates(db_session):
    """T6.1: 同一文件内重复键应折叠（upsert 而非报错/重复）"""
    from backend.app.services.sales_importer import import_sales_to_db
    from backend.app.db import SalesRecord

    db = db_session
    # 同一文件里两条完全一样的行（命中唯一约束）
    sales = _lines(0, 1) + _lines(0, 1)
    r = import_sales_to_db(db, "2026-01", sales, set())
    assert r["total"] == 2
    assert r["db_count"] == 1  # 唯一键折叠后只剩 1 行


def test_import_sales_persists_raw_to_extra(db_session):
    """T6.2: SalesLine.raw 全字段留底 → SalesRecord.extra（台账对账用）

    raw 里的源 Excel 字段（含中文/数字混合）应原样落到 extra JSON 列。
    """
    from backend.app.services.sales_importer import import_sales_to_db
    from backend.app.db import SalesRecord
    from salary_engine.models import SalesLine

    db = db_session
    raw = {"some_source_col": "v", "另一列": 7}
    line = SalesLine(receipt="RAW1", src_order=None, store="S",
                     sale_date=date(2026, 1, 1), barcode="B", product_name="奶",
                     qty=1, amount=Decimal(1), unit_price=Decimal(1),
                     is_return=False, is_online=False, cashier="", salesperson="高睿",
                     raw=raw)
    r = import_sales_to_db(db, "2026-01", [line], set())
    assert r["db_count"] == 1

    rec = db.query(SalesRecord).filter_by(month="2026-01", receipt="RAW1").one()
    assert rec.extra == {"some_source_col": "v", "另一列": 7}


def test_import_sales_empty_raw_persists_none(db_session):
    """T6.2: raw 为空 → extra 存 None（干净，避免 {} 噪音）。"""
    from backend.app.services.sales_importer import import_sales_to_db
    from backend.app.db import SalesRecord

    db = db_session
    # _line 默认不传 raw → 默认 {} → extra 落 None
    r = import_sales_to_db(db, "2026-01", _lines(0, 1), set())
    assert r["db_count"] == 1
    rec = db.query(SalesRecord).filter_by(month="2026-01", receipt="R0").one()
    assert rec.extra is None


def test_import_sales_dedup_later_row_wins(db_session):
    """ADR-024 Part A：同文件重复键后写覆盖前写（内存去重与原 upsert 语义一致）"""
    from backend.app.services.sales_importer import import_sales_to_db
    from backend.app.db import SalesRecord

    db = db_session
    first = _line(0, 0)   # 唯一键：R0/店/日期/条码/金额 相同
    later = _line(0, 0)
    later.qty = Decimal(99)          # 后行 qty 不同（其余键字段相同）
    later.product_name = "后写商品"
    r = import_sales_to_db(db, "2026-01", [first, later], set())
    assert r["total"] == 2
    assert r["db_count"] == 1
    row = db.query(SalesRecord).filter_by(month="2026-01").one()
    assert row.qty == Decimal(99) and row.product_name == "后写商品"  # 后写胜


def test_load_sales_from_rows_populates_raw_all_columns():
    """T6.2: load_sales_from_rows 把全部表头→值塞进 SalesLine.raw（含引擎不需要的列）。"""
    from salary_engine.importer import load_sales_from_rows

    header = ["序号", "销售时间", "小票单号", "源单号", "机构名称", "国际条码",
              "商品名称", "数量", "销售金额", "销售单价", "销售方式", "订单渠道",
              "收银员名称", "备注列A", "额外台账字段"]
    row = [1, "2026-01-01 10:00", "T001", "", "福景店", "6920001",
           "低温奶", 2, 20, 10, "销售", "线下", "高睿", "_some_value", "X"]
    lines = load_sales_from_rows([header, row])
    assert len(lines) == 1
    s = lines[0]
    # 引擎字段照常
    assert s.receipt == "T001"
    assert s.barcode == "6920001"
    # raw 含全部 15 列（含引擎不用到的「备注列A」「额外台账字段」）
    assert s.raw["备注列A"] == "_some_value"
    assert s.raw["额外台账字段"] == "X"
    assert s.raw["小票单号"] == "T001"
    assert s.raw["销售金额"] == 20
