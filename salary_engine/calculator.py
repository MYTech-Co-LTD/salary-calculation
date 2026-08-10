"""提成计算主流程（规格 §2、§3）。"""
import re
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal

from salary_engine.margin import gross_margin, classify_tier
from salary_engine.rates import achievement_bucket, lookup_rate
from salary_engine.onduty import infer_duty


def clean_store(name: str) -> str:
    """剥离销售流水门店名的 [10026] 前缀和 （来思尔） 供应商后缀，对齐门店档案。"""
    name = str(name)
    name = re.sub(r"^\[[^\]]*\]", "", name)      # 去 [10026] 前缀
    name = re.sub(r"（[^（）]*）$", "", name)      # 去结尾中文括号后缀
    name = re.sub(r"\([^()]*\)$", "", name)       # 去结尾英文括号后缀
    return name.strip()


@dataclass
class DetailRow:
    store: str
    sale_date: date
    salesperson: str
    barcode: str
    product_name: str
    tier: str
    store_class: str
    bucket: str
    rate: Decimal
    amount: Decimal
    commission: Decimal
    tag: str = "有效计提"          # 有效计提/退货/赠送剔除/赠送扣除/不计提成/非乳品/不计考核(ADR-017/019/023)
    sales_record_id: int = None


@dataclass
class ComputeResult:
    details: list = field(default_factory=list)
    commission_by_person: dict = field(default_factory=dict)
    commission_by_store: dict = field(default_factory=dict)
    breakdown: dict = field(default_factory=dict)  # {(person,store): {sales,target,achievement,bucket,commission}}
    warnings: list = field(default_factory=list)


def compute(sales_lines, products, stores, targets, rate_table,
            month: str, days: int, gift_keys=None, duty_override=None,
            excluded_stores=None, gift_deduction=None):
    """主流程。返回 ComputeResult。

    - gift_keys: {(订单号, 条码)} 赠送集合，命中的销售行整组剔除。
    - gift_deduction: {(订单号, 条码): deduct_qty} 已确认按件数扣除的组；
      命中且在此的组不整组剔，按 1−deduct_qty/组总qty 均摊，剩余计提成（ADR-023）。
    - duty_override: {(store,date): salesperson} 人工确认当班；为 None 则自动推断。
    """
    gift_keys = gift_keys or set()
    gift_deduction = gift_deduction or {}
    warnings = []
    missing_target_stores = set()

    # 1) 清洗门店名；剔除赠送；跳过非乳品；拆分销售/退货
    #    剔除行不再静默丢弃，收集到 excluded 末尾逐行发 0 提成 DetailRow（ADR-008 台账全覆盖）
    excluded_stores = excluded_stores or set()
    sales, returns, excluded = [], [], []
    for ln in sales_lines:
        ln = replace(ln, store=clean_store(ln.store))  # 仅改门店名，保留其余字段
        if ln.store in excluded_stores:            # 不计考核店（ADR-017）
            excluded.append((ln, "不计考核")); continue
        key = (ln.receipt, ln.barcode)
        if key in gift_keys and key not in gift_deduction:
            excluded.append((ln, "赠送剔除")); continue
        product = products.get(ln.barcode)
        if product is None or product.category is None:
            excluded.append((ln, "非乳品"))
            if product is not None and product.category is None:
                warnings.append(f"缺分类: {ln.barcode} {ln.product_name}")
            continue
        if product.exclude_commission:
            excluded.append((ln, "不计提成")); continue
        (returns if ln.is_return else sales).append(ln)

    # 2) 按 (receipt, barcode) 聚合销售（退货不再关联原单，统一按负数处理——ADR-019「谁退扣谁」）
    groups = defaultdict(lambda: {"sales": []})
    for s in sales:
        groups[(s.receipt, s.barcode)]["sales"].append(s)

    # 已确认按件数扣除的组：算剩余系数（均摊口径，ADR-023）
    # clamp 到 0：confirm 端点的 sales_q（SalesRecord 全量非退货）≠ 本函数 total_qty
    # （步骤1 已剔除非乳品/不计考核店），同组有行被剔时 deduct_qty 可能 > total_qty →
    # factor 为负 → 负提成。max(0, …) 把该组判为全扣（金额 0），不出负值。
    deduct_factors = {}  # (receipt,barcode) -> Decimal 剩余比例
    for dkey, deduct_qty in gift_deduction.items():
        g = groups.get(dkey)
        if not g or not g["sales"]:
            continue
        total_qty = sum((s.qty for s in g["sales"]), Decimal(0))
        if total_qty == 0:
            continue
        deduct_factors[dkey] = max(Decimal(0), Decimal(1) - (Decimal(str(deduct_qty)) / total_qty))

    # 3) 当班表（用已清洗门店名的线下销售推断；人工 override 由 Web 提供）
    duty = duty_override if duty_override is not None else infer_duty(sales)

    def group_net(g):
        return sum((s.amount for s in g["sales"]), Decimal(0))

    # 4) 门店×天乳品销售额（达成率用）：各组销售净额 + 退货(负)
    daily_sales = defaultdict(Decimal)
    for g in groups.values():
        if not g["sales"]:
            continue
        s0 = g["sales"][0]
        net = group_net(g)
        factor = deduct_factors.get((s0.receipt, s0.barcode), Decimal(1))
        daily_sales[(s0.store, s0.sale_date)] += net * factor
    for r in returns:
        daily_sales[(r.store, r.sale_date)] += r.amount  # 负数

    # 5) 按「人×店」分别算达成率（规格 §2.4）：每人在每个店各自一个达成档
    ps_sales = defaultdict(Decimal)     # (person, store) -> 业绩
    ps_target = defaultdict(Decimal)    # (person, store) -> 目标
    for (s, d), net in daily_sales.items():
        p = _resolve_duty(duty, s, d, None)
        if p is None:
            continue  # 无当班人：不进聚合，其销售按笔 fallback 计
        ps_sales[(p, s)] += net
    # 当班天数（含零销售当班日）——修 C3：目标从当班表累加，而非『有销售的天数』。
    # 否则零销售当班日不会被计入目标 → 目标偏低 → 达成率虚高 → 提成多发。
    for (s, d) in duty.keys():
        p = _resolve_duty(duty, s, d, None)
        if p is None:
            continue
        tgt = targets.get(s)
        if not tgt:
            missing_target_stores.add(s)
        else:
            ps_target[(p, s)] += tgt / days if days else Decimal(0)

    ps_bucket = {}
    for key, tgt in ps_target.items():
        ps_bucket[key] = achievement_bucket(ps_sales[key] / tgt if tgt else Decimal(0))

    # 6) 逐组算提成：达成档用『该人×该店』的档，门店类别按本笔销售门店
    details = []
    comm_person = defaultdict(Decimal)
    comm_store = defaultdict(Decimal)
    ps_commission = defaultdict(Decimal)  # (person, store) -> 提成

    # 正常销售组：逐行产出 DetailRow（有效计提）。
    # C2：tier/rate 按每行自己的单价算（规格 §2.3「按每笔实际成交判定」）。
    for g in groups.values():
        if not g["sales"]:
            continue
        s0 = g["sales"][0]
        product = products[s0.barcode]
        if product.cost is None:
            warnings.append(f"缺成本: {s0.barcode} {s0.product_name}")
            continue
        store_obj = stores.get(s0.store)
        if store_obj is None:
            warnings.append(f"未知门店: {s0.store}")
            continue
        sp = _resolve_duty(duty, s0.store, s0.sale_date, s0.salesperson)
        bucket = ps_bucket.get((sp, s0.store), "LT_70")
        factor = deduct_factors.get((s0.receipt, s0.barcode), Decimal(1))
        row_tag = "赠送扣除" if factor != Decimal(1) else "有效计提"
        # 逐行：销售（每行按自己的 unit_price 算 tier/rate）
        for s in g["sales"]:
            margin = gross_margin(s.unit_price, product.cost)
            tier = classify_tier(product.category, margin)
            rate = lookup_rate(rate_table, store_obj.store_class, bucket, tier)
            amt = s.amount * factor
            commission = amt * rate
            details.append(DetailRow(s.store, s.sale_date, sp, s.barcode, s.product_name,
                                     tier, store_obj.store_class, bucket, rate, amt,
                                     commission, tag=row_tag,
                                     sales_record_id=getattr(s, "sales_record_id", None)))
            comm_person[sp] += commission
            comm_store[s.store] += commission
            ps_commission[(sp, s.store)] += commission

    # 退货：统一按负数算（ADR-019「谁退扣谁」）——归退货当天当班人+档，amount 负→提成负
    for r in returns:
        product = products[r.barcode]
        store_obj = stores.get(r.store)
        if store_obj is None or product.cost is None:
            warnings.append(f"退货异常(缺数据): {r.barcode} @ {r.store}")
            continue
        margin = gross_margin(r.unit_price, product.cost)
        tier = classify_tier(product.category, margin)
        sp = _resolve_duty(duty, r.store, r.sale_date, r.salesperson)
        bucket = ps_bucket.get((sp, r.store), "LT_70")
        rate = lookup_rate(rate_table, store_obj.store_class, bucket, tier)
        commission = r.amount * rate  # amount 为负 → 提成负
        details.append(DetailRow(r.store, r.sale_date, sp, r.barcode, r.product_name,
                                 tier, store_obj.store_class, bucket, rate, r.amount,
                                 commission, tag="退货",
                                 sales_record_id=getattr(r, "sales_record_id", None)))
        comm_person[sp] += commission
        comm_store[r.store] += commission
        ps_commission[(sp, r.store)] += commission

    for store in sorted(missing_target_stores):
        warnings.append(f"缺月度目标: {store}")

    # 按「人×店」汇总（供结果展示/对账）
    breakdown = {}
    for key, sales in ps_sales.items():
        tgt = ps_target.get(key, Decimal(0))
        ach = sales / tgt if tgt else Decimal(0)
        breakdown[key] = {
            "sales": sales,
            "target": tgt,
            "achievement": ach,
            "bucket": achievement_bucket(ach) if tgt else "LT_70",
            "commission": ps_commission.get(key, Decimal(0)),
        }

    # 剔除行：逐行 0 提成明细（台账全覆盖，ADR-008）
    for ln, tag in excluded:
        sp = ln.salesperson or ln.cashier or ""
        details.append(DetailRow(ln.store, ln.sale_date, sp, ln.barcode, ln.product_name,
                                 "", "", "", Decimal(0), ln.amount, Decimal(0),
                                 tag=tag, sales_record_id=getattr(ln, "sales_record_id", None)))

    return ComputeResult(details=details,
                         commission_by_person=dict(comm_person),
                         commission_by_store=dict(comm_store),
                         breakdown=breakdown,
                         warnings=warnings)


def _resolve_duty(duty, store, d, fallback):
    v = duty.get((store, d))
    if v is None:
        return fallback
    if isinstance(v, list):
        return sorted(v)[0]  # 多人当天：确定性取一人（UI 阶段可人工改）
    return v
