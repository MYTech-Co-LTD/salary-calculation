from decimal import Decimal
from salary_engine.importer import (load_products_from_rows, load_stores_from_rows,
                                    load_sales_from_rows, load_gift_keys_from_rows)


def test_load_products_merges_cost():
    info = [["国际条码", "商品名称", "规格", "类别"],
            ["6920001", "低温奶A", "200ml", "低温奶"]]
    cost = [["商品条码", "商品名称", "销售成本"],
            ["6920001", "低温奶A（件）", "20"]]
    products = load_products_from_rows(info, cost)
    assert products["6920001"].category == "低温奶"
    assert products["6920001"].cost == Decimal("20")


def test_load_products_missing_cost_is_none():
    info = [["国际条码", "商品名称", "规格", "类别"],
            ["6920002", "常温奶B", "1L", "常温奶"]]
    products = load_products_from_rows(info, [])
    assert products["6920002"].cost is None


def test_load_stores():
    rows = [["主管", "类别", "组别", "名称", "本月目标"],
            ["胡总", "A", "1组", "福景店", "84000"],
            ["", "", "", "标题行忽略", ""],
            ["李秀军", "B", "2组", "魅力之城店", "79000"]]
    stores, targets = load_stores_from_rows(rows)
    assert stores["福景店"].store_class == "A"
    assert stores["福景店"].group == "1组"
    assert stores["福景店"].supervisor == "胡总"
    assert targets["福景店"] == Decimal("84000")
    assert stores["魅力之城店"].store_class == "B"


def test_load_sales_flags_return_and_online():
    header = ["序号", "销售方式", "订单渠道", "小票单号", "源单号", "机构名称",
              "销售时间", "国际条码", "商品名称", "数量", "销售金额", "销售单价", "收银员名称"]
    rows = [
        ["1", "销售", "线下", "R001", "", "福景店", "2026-06-01", "6920001", "奶", "1", "3", "3", "高睿"],
        ["2", "退货", "线下", "R002", "R001", "福景店", "2026-06-02", "6920001", "奶", "-1", "-3", "3", "高睿"],
        ["3", "销售", "线上", "R003", "", "福景店", "2026-06-01", "6920001", "奶", "1", "3", "3", "线上人"],
    ]
    lines = load_sales_from_rows([header] + rows)
    assert len(lines) == 3
    assert lines[0].is_return is False and lines[0].is_online is False
    assert lines[0].store == "福景店"   # 保留原始门店名，由 calculator.clean_store 清洗
    assert lines[0].salesperson == "高睿"   # 当班人取自『收银员名称』
    assert lines[1].is_return is True and lines[1].amount == Decimal("-3")
    assert lines[1].src_order == "R001"
    assert lines[2].is_online is True


def test_load_gift_keys():
    rows = [["序号", "订单号", "国际条码", "商品名称"],
            ["1", "R001", "6920001", "奶"],
            ["2", "R009", "6920002", "奶2"]]
    keys = load_gift_keys_from_rows(rows)
    assert ("R001", "6920001") in keys
    assert ("R009", "6920002") in keys
    assert len(keys) == 2


def test_load_stores_tolerates_spaced_header():
    # 真实『2026.6全部』表头是『名  称』带空格，匹配需容错
    rows = [["主管", "类别", "组  别", "名  称", "本月目标"],
            ["胡总", "A", "1组", "福景店", "84000"]]
    stores, targets = load_stores_from_rows(rows)
    assert "福景店" in stores
    assert targets["福景店"] == Decimal("84000")


from salary_engine.importer import load_gift_qty_map_from_rows


def test_load_gift_qty_map_aggregates_same_key():
    rows = [
        ["序号", "订单号/小票单号", "国际条码", "数量"],
        ["1", "R001", "6920001", "2"],
        ["2", "R001", "6920001", "1"],   # 同 key 聚合 → 3
        ["3", "R002", "6920002", "1"],
    ]
    qm = load_gift_qty_map_from_rows(rows)
    assert qm[("R001", "6920001")] == Decimal(3)
    assert qm[("R002", "6920002")] == Decimal(1)


def test_load_gift_qty_map_missing_qty_col_defaults_one():
    rows = [["订单号", "国际条码"], ["R001", "6920001"]]
    qm = load_gift_qty_map_from_rows(rows)
    assert qm[("R001", "6920001")] == Decimal(1)
