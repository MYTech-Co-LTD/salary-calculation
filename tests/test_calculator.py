from datetime import date
from decimal import Decimal
from salary_engine.calculator import compute
from salary_engine.rates import seed_rate_table
from salary_engine.models import Product, RateTable, SalesLine, Store


def test_basic_commission_lowtemp_highmargin_classA_full(stores, products):
    # 福景店(A) 6/1 低温高毛，达成100%档：比例13%；销售额3 → 提成0.39
    target = {"福景店": Decimal("3")}
    sales = [SalesLine("R001", None, "福景店", date(2026, 6, 1), "6920001",
                       "低温奶", Decimal(1), Decimal(3), Decimal(3),
                       is_return=False, is_online=False, salesperson="高睿")]
    result = compute(sales, products, stores, target,
                     seed_rate_table(), month="2026-06", days=30)
    assert result.commission_by_person["高睿"] == Decimal("0.39")
    assert result.warnings == []


def test_gift_excluded(products, stores):
    target = {"福景店": Decimal("3")}
    sales = [SalesLine("R001", None, "福景店", date(2026, 6, 1), "6920001",
                       "低温奶", Decimal(1), Decimal(3), Decimal(3),
                       False, False, "高睿")]
    gifts = {("R001", "6920001")}  # 这笔是赠送 → 剔除
    result = compute(sales, products, stores, target, seed_rate_table(),
                     month="2026-06", days=30, gift_keys=gifts)
    assert result.commission_by_person.get("高睿", Decimal(0)) == Decimal(0)


def test_return_precise_offset(products, stores):
    # 卖3元后退货3元（同源单号+条码）→ 净0，提成0
    target = {"福景店": Decimal("100")}
    sales = [
        SalesLine("R001", None, "福景店", date(2026, 6, 1), "6920001", "奶",
                  Decimal(1), Decimal(3), Decimal(3), is_return=False, is_online=False, salesperson="高睿"),
        SalesLine("R002", "R001", "福景店", date(2026, 6, 2), "6920001", "奶",
                  Decimal(-1), Decimal(-3), Decimal(3), is_return=True, is_online=False, salesperson="高睿"),
    ]
    result = compute(sales, products, stores, target, seed_rate_table(),
                     month="2026-06", days=30)
    assert result.commission_by_person.get("高睿", Decimal(0)) == Decimal(0)


def test_missing_target_warns(products, stores):
    # 门店无月度目标 → 记 warning（供 Web 层标红阻断）
    sales = [SalesLine("R001", None, "福景店", date(2026, 6, 1), "6920001", "奶",
                       Decimal(1), Decimal(3), Decimal(3), is_return=False, is_online=False, salesperson="高睿")]
    result = compute(sales, products, stores, {}, seed_rate_table(),
                     month="2026-06", days=30)
    assert any("缺月度目标" in w and "福景店" in w for w in result.warnings)


def test_per_store_achievement_cross_store(products):
    # 高睿 6/1 福景店(A,目标30000)、6/2 魅力之城店(B,目标60000)，各卖1500。
    # 按「人×店」分别算：两个店各自一个达成档，提成分别算后相加。
    stores = {"福景店": Store("福景店", "1组", "A"),
              "魅力之城店": Store("魅力之城店", "2组", "B")}
    targets = {"福景店": Decimal("30000"), "魅力之城店": Decimal("60000")}
    sales = [
        SalesLine("R1", None, "福景店", date(2026, 6, 1), "6920001", "奶",
                  Decimal(500), Decimal("1500"), Decimal(3),
                  is_return=False, is_online=False, salesperson="高睿"),
        SalesLine("R2", None, "魅力之城店", date(2026, 6, 2), "6920001", "奶",
                  Decimal(500), Decimal("1500"), Decimal(3),
                  is_return=False, is_online=False, salesperson="高睿"),
    ]
    r = compute(sales, products, stores, targets, seed_rate_table(),
                month="2026-06", days=30)
    # 福景店：目标30000/30×1=1000，业绩1500 → 150% → GE_100；A低温高毛13% → 195
    assert r.breakdown[("高睿", "福景店")]["achievement"] == Decimal("1.5")
    assert r.breakdown[("高睿", "福景店")]["commission"] == Decimal("195")
    # 魅力之城店：目标60000/30×1=2000，业绩1500 → 75% → 70_80；B低温高毛11% → 165
    assert r.breakdown[("高睿", "魅力之城店")]["bucket"] == "70_80"
    assert r.breakdown[("高睿", "魅力之城店")]["commission"] == Decimal("165")
    # 个人合计 = 195 + 165 = 360
    assert r.commission_by_person["高睿"] == Decimal("360")


def test_per_store_achievement_multi_day_same_store(products, stores):
    # 同一人在福景店干两天：同店多天合并算这一个店的达成档
    target = {"福景店": Decimal("30000")}  # 日目标=1000
    sales = [
        SalesLine("R1", None, "福景店", date(2026, 6, 1), "6920001", "奶",
                  Decimal(1), Decimal("2000"), Decimal(3),
                  is_return=False, is_online=False, salesperson="高睿"),
        SalesLine("R2", None, "福景店", date(2026, 6, 2), "6920001", "奶",
                  Decimal(1), Decimal("200"), Decimal(3),
                  is_return=False, is_online=False, salesperson="高睿"),
    ]
    r = compute(sales, products, stores, target, seed_rate_table(),
                month="2026-06", days=30)
    # 福景店两天：目标1000×2=2000，业绩2200 → 110% → GE_100
    assert r.breakdown[("高睿", "福景店")]["achievement"] == Decimal("1.1")
    # 2200 × A类低温高毛13% = 286
    assert r.commission_by_person["高睿"] == Decimal("286")


def _sl(receipt, store, d, amount, sp="高睿", barcode="B1"):
    return SalesLine(receipt=receipt, src_order=None, store=store, sale_date=d,
                     barcode=barcode, product_name="奶", qty=1, amount=Decimal(amount),
                     unit_price=Decimal(amount), is_return=False, is_online=False,
                     cashier="", salesperson=sp)


def test_c3_duty_days_count_zero_sales_day():
    # C3：当班天数应取自当班表（含零销售当班日），而非『有销售的天数』。
    # 本用例为审计反例：6/2 当班但零销售 → 必须计入目标，否则达成率虚高。
    products = {"B1": Product("B1", "奶", "", "常温奶", Decimal(5), False)}
    stores = {"福景店": Store("福景店", "1组", "A", "")}
    targets = {"福景店": Decimal(3000)}
    rate_table = RateTable(version=1, effective_from=date(2026, 6, 1), rates={
        ("A", "GE_100", "常温高毛"): Decimal("0.13"),
        ("A", "LT_70", "常温高毛"): Decimal("0.09"),
    })
    sales = [_sl("R1", "福景店", date(2026, 6, 1), 100)]   # 仅 6/1 有销售
    # 当班表：高睿 6/1 与 6/2 都当班（6/2 零销售）
    duty = {("福景店", date(2026, 6, 1)): "高睿", ("福景店", date(2026, 6, 2)): "高睿"}
    res = compute(sales, products, stores, targets, rate_table, "2026-06", 30, duty_override=duty)
    bd = res.breakdown[("高睿", "福景店")]
    # 正确：目标=3000/30*2=200，达成=100/200=0.5 → LT_70
    assert bd["target"] == Decimal(200), f"target={bd['target']}"
    assert bd["bucket"] == "LT_70", f"bucket={bd['bucket']}"


def test_per_line_detail_rows_sum_to_total():
    products = {"B1": Product("B1", "奶", "", "常温奶", Decimal(5), False)}
    stores = {"S": Store("S", "1组", "A", "")}
    targets = {"S": Decimal(1000)}
    rt = RateTable(version=1, effective_from=date(2026, 6, 1),
                   rates={("A", "GE_100", "常温高毛"): Decimal("0.13")})
    sales = [
        _sl("R1", "S", date(2026, 6, 1), 60),
        _sl("R1", "S", date(2026, 6, 1), 40),
        SalesLine(receipt="R2", src_order="R1", store="S", sale_date=date(2026, 6, 1),
                  barcode="B1", product_name="奶", qty=1, amount=Decimal(-10),
                  unit_price=Decimal(10), is_return=True, is_online=False,
                  cashier="", salesperson="高睿"),
    ]
    duty = {("S", date(2026, 6, 1)): "高睿"}
    res = compute(sales, products, stores, targets, rt, "2026-06", 30, duty_override=duty)
    # 逐行：60×.13 + 40×.13 + (-10)×.13 = 11.70
    assert sum((d.commission for d in res.details), Decimal(0)) == Decimal("11.70")
    assert res.commission_by_person["高睿"] == Decimal("11.70")
    # 退货行带 退货 标签（ADR-019 统一负数）
    assert any(d.tag == "退货" and d.amount == Decimal(-10) for d in res.details)


def test_excluded_lines_emit_zero_commission_detailrows():
    products = {"B1": Product("B1", "奶", "", "常温奶", Decimal(5), False),
                "B2": Product("B2", "非奶", "", "常温奶", Decimal(5), True)}  # exclude_commission
    stores = {"S": Store("S", "1组", "A", "")}
    targets = {"S": Decimal(1000)}
    rt = RateTable(version=1, effective_from=date(2026, 6, 1),
                   rates={("A", "GE_100", "常温高毛"): Decimal("0.13")})
    sales = [
        _sl("R1", "S", date(2026, 6, 1), 100),                       # 有效
        _sl("R2", "S", date(2026, 6, 1), 20, barcode="B2"),           # 不计提成
        _sl("R3", "S", date(2026, 6, 1), 30, barcode="B9"),           # 非乳品（B9 不在 products）
    ]
    gift_keys = {("R4", "B1")}
    sales.append(_sl("R4", "S", date(2026, 6, 1), 50))               # 赠送
    duty = {("S", date(2026, 6, 1)): "高睿"}
    res = compute(sales, products, stores, targets, rt, "2026-06", 30,
                  gift_keys=gift_keys, duty_override=duty)
    tags = {d.tag for d in res.details}
    assert {"有效计提", "不计提成", "非乳品", "赠送剔除"} <= tags
    # 剔除行 0 提成
    assert all(d.commission == 0 for d in res.details
               if d.tag in ("不计提成", "非乳品", "赠送剔除"))
    # 不变量：逐行全覆盖入参
    assert len(res.details) == len(sales)
    # 总额仅来自有效计提
    assert sum((d.commission for d in res.details), Decimal(0)) == Decimal("13.00")


def test_h1_cost_only_category_none_excluded_as_nondairy_with_warning():
    # H1 (ADR-010): cost-only 条码（在 销售成本.xlsx 有、商品信息表 无 → category=None）
    # 毛利>10% 时旧逻辑会走到 classify_tier(None, margin) 抛 ValueError 崩溃。
    # 决策：按非乳品排除 + warning「缺分类: <barcode> <name>」，不崩溃。
    products = {"B1": Product("B1", "成本表奶", "", None, Decimal(5), False)}  # category=None
    stores = {"S": Store("S", "1组", "A", "")}
    targets = {"S": Decimal(1000)}
    rt = RateTable(version=1, effective_from=date(2026, 6, 1),
                   rates={("A", "GE_100", "常温高毛"): Decimal("0.13")})
    # 单价10、成本5 → 毛利率(10-5)/10=50% > 10% → 旧逻辑 classify_tier(None, .5) 崩溃
    sales = [_sl("R1", "S", date(2026, 6, 1), 10)]
    duty = {("S", date(2026, 6, 1)): "高睿"}
    res = compute(sales, products, stores, targets, rt, "2026-06", 30, duty_override=duty)
    # 不崩溃；产出 非乳品 标签的 0 提成 DetailRow
    rows = [d for d in res.details if d.barcode == "B1"]
    assert len(rows) == 1, f"expected 1 row for B1, got {len(rows)}: {rows}"
    assert rows[0].tag == "非乳品"
    assert rows[0].commission == Decimal(0)
    # warning：缺分类: <barcode> <name>
    assert any("缺分类" in w and "B1" in w for w in res.warnings), res.warnings
    # 不入提成聚合
    assert res.commission_by_person.get("高睿", Decimal(0)) == Decimal(0)


def test_c2_per_line_tier_for_same_receipt_different_price():
    """C2：同一 (receipt, barcode) 多行若单价不同，每行按自己的毛利率档算提成，
    而非全部用首行 s0 的档（规格 §2.3「按每笔实际成交判定」）。

    行1 单价6/成本5 → 毛利16.67% → 低温高毛(13%)；行2 单价5.5/成本5 → 毛利9.09% → 特价(1%)。
    当前 bug：两行都用首行 s0 的 tier(低温高毛) → 行2 误算 5.5×13%=0.715（应 0.055）。
    """
    products = {"B1": Product("B1", "奶", "", "低温奶", Decimal(5), False)}
    stores = {"S": Store("S", "1组", "A", "")}
    targets = {"S": Decimal("11.5")}  # 日目标≈0.38，业绩11.5 → 达成>>100% → GE_100
    rt = RateTable(version=1, effective_from=date(2026, 6, 1),
                   rates={("A", "GE_100", "低温高毛"): Decimal("0.13")})
    sales = [
        _sl("R1", "S", date(2026, 6, 1), 6),           # 单价6 → 低温高毛
        _sl("R1", "S", date(2026, 6, 1), "5.5"),        # 单价5.5 → 特价
    ]
    duty = {("S", date(2026, 6, 1)): "高睿"}
    res = compute(sales, products, stores, targets, rt, "2026-06", 30, duty_override=duty)
    rows = [d for d in res.details if d.barcode == "B1" and d.tag == "有效计提"]
    assert len(rows) == 2
    by_price = {d.amount: d for d in rows}
    # 行1：6 × 13% = 0.78，低温高毛
    assert by_price[Decimal("6")].tier == "低温高毛"
    assert by_price[Decimal("6")].rate == Decimal("0.13")
    assert by_price[Decimal("6")].commission == Decimal("0.78")
    # 行2：5.5 × 1% = 0.055，特价（而非首行的低温高毛）
    assert by_price[Decimal("5.5")].tier == "特价"
    assert by_price[Decimal("5.5")].rate == Decimal("0.01")
    assert by_price[Decimal("5.5")].commission == Decimal("0.055")


def _gift_sales(receipt, n, amt):
    """造 n 行同 (receipt,barcode) 的销售，每行 qty1/amount=amt。"""
    return [SalesLine(receipt, None, "福景店", date(2026, 6, 1), "6920001", "低温奶",
                      Decimal(1), Decimal(amt), Decimal(amt),
                      is_return=False, is_online=False, salesperson="高睿") for _ in range(n)]


def test_gift_deduction_partial(products, stores):
    # 买2送1：3 行不对，这里造 2 行各 amt5=总10；gift_deduction 扣1 → factor 0.5 → 计提成金额5
    target = {"福景店": Decimal("100")}
    sales = _gift_sales("R1", 2, 5)
    gifts = {("R1", "6920001")}
    gd = {("R1", "6920001"): Decimal(1)}
    r = compute(sales, products, stores, target, seed_rate_table(),
                month="2026-06", days=30, gift_keys=gifts, gift_deduction=gd)
    deducted = [d for d in r.details if d.tag == "赠送扣除"]
    assert len(deducted) == 2
    assert sum((d.amount for d in deducted), Decimal(0)) == Decimal("5")  # 10 × 0.5


def test_gift_deduction_all_deducted_when_sales_less(products, stores):
    # 销售1件、扣 min(1,..)=1 → factor 0 → 全扣，金额0
    target = {"福景店": Decimal("100")}
    sales = _gift_sales("R1", 1, 5)
    gifts = {("R1", "6920001")}
    gd = {("R1", "6920001"): Decimal(1)}
    r = compute(sales, products, stores, target, seed_rate_table(),
                month="2026-06", days=30, gift_keys=gifts, gift_deduction=gd)
    deducted = [d for d in r.details if d.tag == "赠送扣除"]
    assert len(deducted) == 1
    assert deducted[0].amount == Decimal(0)


def test_gift_deduction_clamps_factor_when_deduct_exceeds_total(products, stores):
    """I1 正确性：deduct_qty > 组进入聚合的 total_qty 时 factor 必须 clamp 到 0，
    不出负提成。

    根因：confirm 端点的 sales_q（SalesRecord 全量非退货）≠ calculator 的 total_qty
    （步骤1 过滤后已剔除非乳品/不计考核店）；同组有行被剔时 deduct_qty 可能 > total_qty。
    无 clamp 时 factor = 1 - 3/1 = -2 → 行 amount = 5 × -2 = -10 → 负提成。
    有 clamp 时 factor = max(0, -2) = 0 → 该组判为全扣（金额 0），不出负值。
    """
    target = {"福景店": Decimal("100")}
    # 组 (R1, 6920001) 进入聚合的总 qty = 1（仅 1 行销售）
    sales = _gift_sales("R1", 1, 5)
    gifts = {("R1", "6920001")}
    # deduct_qty=3 > total_qty=1：模拟 confirm 端点用未过滤全量 sales_q 得到的扣减量
    gd = {("R1", "6920001"): Decimal(3)}
    r = compute(sales, products, stores, target, seed_rate_table(),
                month="2026-06", days=30, gift_keys=gifts, gift_deduction=gd)
    deducted = [d for d in r.details if d.tag == "赠送扣除"]
    assert len(deducted) == 1
    # factor clamp 到 0 → 行 amount 不为负（== 0）
    assert deducted[0].amount >= Decimal(0), f"factor 未 clamp，出负值: {deducted[0].amount}"
    assert deducted[0].amount == Decimal(0)
    assert deducted[0].commission >= Decimal(0), f"出负提成: {deducted[0].commission}"
    # 整组不出负提成
    assert r.commission_by_person.get("高睿", Decimal(0)) >= Decimal(0)
