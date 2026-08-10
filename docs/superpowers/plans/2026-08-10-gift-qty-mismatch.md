# 赠送件数不符异常 + 确认按件数扣除 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 销售件数 ≠ 让利表赠送件数时进异常排查，用户确认后 compute 按 `min(赠送,销售)` 件、按件数均摊金额扣除，剩余计提成；件数相等组维持现状。

**Architecture:** 复用现有异常排查体系（AnomalyChecker/Anomaly 表/AnomalyPanel 软门禁）+ 复用 duty_override 的"确认影响计算"正模板——新增独立 `GiftDeduction` 表（不受 anomalies 快照重建影响）+ `compute` 新增 `gift_deduction` 参数。`gift_keys` 保持 set 不变、importer 只新增件数解析函数。详见 ADR-023 与 spec `docs/superpowers/specs/2026-08-10-gift-qty-mismatch-design.md`。

**Tech Stack:** Python（salary_engine 引擎 + FastAPI 后端 + SQLAlchemy）、python-calamine（xlsx）、React/TS 前端、pytest。

## Global Constraints

- 所有交互/注释/文案用**中文**；commit message 用 `feat/fix(scope): 中文`。
- 金额/件数一律 `Decimal`（禁 float）；从 DB Numeric 列读出先 `Decimal(str(x))`。
- 测试用 TDD：先写失败测试 → 跑红 → 实现 → 跑绿 → commit。
- 引擎测试跑 `uv run pytest tests/`；后端测试跑 `uv run pytest backend/tests/`；前端 `cd frontend && npx tsc --noEmit`。
- `anomaly_type` 用字符串 `"7"`；DetailRow tag 新增 `"赠送扣除"`。
- 不改 `load_gift_keys_from_rows`（保持返回 set）；不让利明细整体落库；不弹窗输入件数。

## 接口契约（各任务据此对接）

- `load_gift_qty_map_from_rows(rows) -> dict[tuple[str,str], Decimal]`（T1）—— `{(订单号,条码): 赠送件数(带符号)}`，同 key 多行聚合，缺"数量"列默认每行 1。
- `load_gift_qty_map_xlsx(path) -> dict[tuple[str,str], Decimal]`（T1）
- `compute(..., gift_deduction: dict[tuple[str,str], Decimal] = None)`（T2）—— 值为 deduct_qty（带符号）；命中且在 gift_deduction 的组不整组剔，按 `factor = 1 - deduct_qty/组总qty` 均摊，tag `"赠送扣除"`。
- `GiftDeduction` ORM（T3）：`month,receipt,barcode,sales_qty,gift_qty,deduct_qty,reason,resolution,status,created_at`，`uq(month,receipt,barcode)`。
- `AnomalyChecker.check_gift_qty_mismatch(sales_qty_map, gift_qty_map, confirmed_keys, names)`（T4）—— 产 `anomaly_type="7"`, `entity_type="gift"`, `entity_id="{receipt}|{barcode}"`。
- `gift_deduction_from_db(db, month) -> dict[tuple[str,str], Decimal]`（T5）—— `{(receipt,barcode): deduct_qty}`。
- `POST /months/{month}/gift-deduction/confirm` body `{anomaly_id:int}` -> `{deduct_qty, deduct_amt}`（T7）。

---

### Task 1: importer 新增让利件数解析

**Files:**
- Modify: `salary_engine/importer.py`（末尾追加两个函数）
- Test: `tests/test_importer.py`

**Interfaces:**
- Produces: `load_gift_qty_map_from_rows`, `load_gift_qty_map_xlsx`（见契约）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_importer.py` 末尾：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_importer.py::test_load_gift_qty_map_aggregates_same_key -v`
Expected: FAIL `ImportError: cannot import name 'load_gift_qty_map_from_rows'`

- [ ] **Step 3: 实现**

在 `salary_engine/importer.py` 的 `load_gift_keys_xlsx`（约 line 170）后追加：

```python
def load_gift_qty_map_from_rows(rows):
    """让利明细 → {(订单号, 国际条码): 数量(带符号)}。
    同 key 多行聚加；缺『数量』列时每行按 1 计（容错）。"""
    ii, h = _find_header(rows, "订单号", "国际条码")
    idx = {_norm(c): k for k, c in enumerate(h) if c is not None}
    o, b = _col(idx, "订单号"), _col(idx, "国际条码")
    q_i = idx.get(_norm("数量"))  # 数量列可能缺失 → None
    qm: dict[tuple[str, str], Decimal] = {}
    for r in rows[ii + 1:]:
        if not r or len(r) <= o or r[o] in (None, ""):
            continue
        key = (str(r[o]), str(r[b]))
        qty = _D(r[q_i]) if (q_i is not None and q_i < len(r)) else Decimal(1)
        qm[key] = qm.get(key, Decimal(0)) + qty
    return qm


def load_gift_qty_map_xlsx(path):
    return load_gift_qty_map_from_rows(_xlsx_rows(path))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_importer.py -v -k gift_qty`
Expected: PASS（2 个）

- [ ] **Step 5: commit**

```bash
git add salary_engine/importer.py tests/test_importer.py
git commit -m "feat(importer): 新增 load_gift_qty_map 解析让利明细赠送件数"
```

---

### Task 2: calculator 支持按件数均摊扣除（口径变更核心）

**Files:**
- Modify: `salary_engine/calculator.py:48-49`(签名) `:55-77`(步骤1) `:90-96`(步骤4) `:132-158`(步骤6)
- Test: `tests/test_calculator.py`

**Interfaces:**
- Consumes: `gift_deduction: dict[tuple[str,str], Decimal]`（deduct_qty 带符号）
- Produces: `compute` 新参数；命中且在 gift_deduction 的组 tag=`"赠送扣除"`，逐行 amount/commission 乘 factor

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_calculator.py`（复用 `stores`/`products` fixture 与 `seed_rate_table`，模式同现有 `test_gift_excluded`）：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_calculator.py::test_gift_deduction_partial -v`
Expected: FAIL `TypeError: compute() got an unexpected keyword argument 'gift_deduction'`

- [ ] **Step 3: 实现（4 处改动）**

改动 A — 签名 + 初始化（`calculator.py:48-55`）：

```python
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
```

改动 B — 步骤1 分叉（替换 `calculator.py:67-68` 的命中判定）：

```python
        key = (ln.receipt, ln.barcode)
        if key in gift_keys and key not in gift_deduction:
            excluded.append((ln, "赠送剔除")); continue
```

（原 `if (ln.receipt, ln.barcode) in gift_keys:` 两行删除，换成上面；下方 `product = products.get(...)` 起不变。）

改动 C — 步骤2 聚合后（`calculator.py:82` 循环结束后、步骤3 `duty =` 之前）插入 deduct_factors：

```python
    # 已确认按件数扣除的组：算剩余系数（均摊口径，ADR-023）
    deduct_factors = {}  # (receipt,barcode) -> Decimal 剩余比例
    for dkey, deduct_qty in gift_deduction.items():
        g = groups.get(dkey)
        if not g or not g["sales"]:
            continue
        total_qty = sum((s.qty for s in g["sales"]), Decimal(0))
        if total_qty == 0:
            continue
        deduct_factors[dkey] = Decimal(1) - (Decimal(str(deduct_qty)) / total_qty)
```

改动 D — 步骤4 daily_sales（替换 `calculator.py:91-96`）：

```python
    for g in groups.values():
        if not g["sales"]:
            continue
        s0 = g["sales"][0]
        net = group_net(g)
        factor = deduct_factors.get((s0.receipt, s0.barcode), Decimal(1))
        daily_sales[(s0.store, s0.sale_date)] += net * factor
```

改动 E — 步骤6 正常销售组逐行（替换 `calculator.py:145-158`，从 `bucket = ...` 那行到本循环结束）：

```python
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
```

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `uv run pytest tests/test_calculator.py -v`
Expected: PASS（含新增 2 个 + 现有 `test_gift_excluded` 等全绿）

- [ ] **Step 5: commit**

```bash
git add salary_engine/calculator.py tests/test_calculator.py
git commit -m "feat(calculator): 赠送件数不符按件数均摊扣除（gift_deduction 参数）"
```

---

### Task 3: GiftDeduction 模型 + 迁移脚本

**Files:**
- Modify: `backend/app/db.py`（新增模型 + 改 Anomaly 注释 `:204`）
- Create: `migrations/004_gift_deduction.py`
- Test: `backend/tests/test_workflow.py`（加一个模型测试）

**Interfaces:**
- Produces: `GiftDeduction` ORM（见契约）

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_workflow.py`：

```python
def test_gift_deduction_model_persists(db_session):
    from backend.app.db import GiftDeduction
    db_session.add(GiftDeduction(month="2026-06", receipt="R1", barcode="6920001",
                                 sales_qty=3, gift_qty=1, deduct_qty=1,
                                 reason="销售3件/赠送1件，已扣除1件",
                                 resolution="已确认扣除", status="confirmed"))
    db_session.commit()
    rows = db_session.query(GiftDeduction).filter_by(month="2026-06").all()
    assert len(rows) == 1
    assert rows[0].deduct_qty == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest backend/tests/test_workflow.py::test_gift_deduction_model_persists -v`
Expected: FAIL `ImportError: cannot import name 'GiftDeduction'`

- [ ] **Step 3: 实现模型**

在 `backend/app/db.py` 的 `Anomaly` 类（约 line 211）后追加：

```python
class GiftDeduction(Base):
    """赠送件数不符——人工确认扣除记录（compute 读取，影响计算；ADR-023）"""
    __tablename__ = "gift_deductions"
    id = Column(Integer, primary_key=True)
    month = Column(String, nullable=False, index=True)
    receipt = Column(String, nullable=False)
    barcode = Column(String, nullable=False)
    sales_qty = Column(Numeric)
    gift_qty = Column(Numeric)
    deduct_qty = Column(Numeric)      # 实际扣除件数 = min(|gift|,|sales|)，带符号
    reason = Column(String(300))
    resolution = Column(String(300))
    status = Column(String(20), default="confirmed")
    created_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("month", "receipt", "barcode", name="uq_gift_deduction"),)
```

并改 `Anomaly.anomaly_type` 注释（`db.py:204`）：`# 1-6` → `# 1-7（7=赠送件数不符）`

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest backend/tests/test_workflow.py::test_gift_deduction_model_persists -v`
Expected: PASS（`create_all` 自动建表）

- [ ] **Step 5: 写迁移脚本（生产历史库用，entrypoint 自动跑）**

创建 `migrations/004_gift_deduction.py`：

```python
#!/usr/bin/env python3
"""建 gift_deductions 表（ADR-023 赠送件数不符确认扣除）。
幂等：CREATE TABLE IF NOT EXISTS。生产历史库由 entrypoint 自动跑（ADR-016）；
测试库由 create_all 自动建表，不依赖本脚本。"""
import sqlite3
import sys
import os
from pathlib import Path


def main():
    db_path = (
        sys.argv[1] if len(sys.argv) > 1
        else os.environ.get("SALARY_DB")
        or str(Path(__file__).resolve().parent.parent / "salary.db")
    )
    if not Path(db_path).exists():
        print(f"❌ 数据库不存在: {db_path}")
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS gift_deductions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        month TEXT NOT NULL,
        receipt TEXT NOT NULL,
        barcode TEXT NOT NULL,
        sales_qty NUMERIC,
        gift_qty NUMERIC,
        deduct_qty NUMERIC,
        reason TEXT,
        resolution TEXT,
        status TEXT DEFAULT 'confirmed',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(month, receipt, barcode)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gift_deductions_month ON gift_deductions(month)")
    conn.commit()
    conn.close()
    print("✅ gift_deductions 表就绪")


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: 验证迁移可跑（针对临时库，不碰 salary.db）**

Run: `uv run python -c "import tempfile,os,subprocess,sys; f=tempfile.NamedTemporaryFile(suffix='.db'); f.close(); subprocess.check_call([sys.executable,'migrations/004_gift_deduction.py',f.name]); print('ok')"`
Expected: 打印 `✅ gift_deductions 表就绪` 和 `ok`

- [ ] **Step 7: commit**

```bash
git add backend/app/db.py migrations/004_gift_deduction.py backend/tests/test_workflow.py
git commit -m "feat(db): GiftDeduction 模型 + 004 迁移（赠送件数确认扣除）"
```

---

### Task 4: anomaly_checker 新增 type 7 检测

**Files:**
- Modify: `backend/app/services/anomaly_checker.py`（加方法）
- Test: `backend/tests/test_anomaly_checker.py`（新建）

**Interfaces:**
- Consumes: `sales_qty_map: dict[(str,str), Decimal]`、`gift_qty_map`、`confirmed_keys: set[(str,str)]`、`names: dict[(str,str), str]`
- Produces: `check_gift_qty_mismatch`，产出 `anomaly_type="7"`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_anomaly_checker.py`：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest backend/tests/test_anomaly_checker.py -v`
Expected: FAIL `AttributeError: 'AnomalyChecker' object has no attribute 'check_gift_qty_mismatch'`

- [ ] **Step 3: 实现方法**

在 `backend/app/services/anomaly_checker.py` 的 `check_products_complete` 后、`get_anomalies` 前追加：

```python
    def check_gift_qty_mismatch(self, sales_qty_map, gift_qty_map, confirmed_keys, names):
        """异常7: 赠送件数不符——销售件数 ≠ 让利表赠送件数（仅非退货行对比）"""
        for key, gift_q in gift_qty_map.items():
            if key in confirmed_keys:
                continue
            sales_q = sales_qty_map.get(key, Decimal(0))
            if sales_q == gift_q:
                continue
            receipt, barcode = key
            name = names.get(key, "")
            extra = f" | 单号: {receipt} | 条码: {barcode} | 商品名: {name}"
            extra += f" | 销售件数: {sales_q} | 赠送件数: {gift_q}"
            self.anomalies.append({
                "month": self.month,
                "anomaly_type": "7",
                "entity_type": "gift",
                "entity_id": f"{receipt}|{barcode}",
                "description": f"赠送件数不符{extra}",
                "status": "pending",
            })
```

并在文件顶部导入补 `Decimal`：

```python
from decimal import Decimal
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest backend/tests/test_anomaly_checker.py -v`
Expected: PASS（3 个）

- [ ] **Step 5: commit**

```bash
git add backend/app/services/anomaly_checker.py backend/tests/test_anomaly_checker.py
git commit -m "feat(checker): 新增 type7 赠送件数不符检测"
```

---

### Task 5: engine_bridge 加 gift_deduction_from_db

**Files:**
- Modify: `backend/app/services/engine_bridge.py`（加函数 + 导入）
- Test: `backend/tests/test_engine_bridge.py`

**Interfaces:**
- Produces: `gift_deduction_from_db(db, month) -> dict[tuple[str,str], Decimal]`

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_engine_bridge.py`（若无合适 fixture，照下例用 db_session 直造）：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest backend/tests/test_engine_bridge.py::test_gift_deduction_from_db -v`
Expected: FAIL `ImportError`

- [ ] **Step 3: 实现**

在 `backend/app/services/engine_bridge.py`：导入行（line 8）追加 `GiftDeduction`：

```python
from backend.app.db import Product as ProductRow, Store as StoreRow
from backend.app.db import MonthlyTarget, SalaryPolicyVersion, Duty, SalesRecord, GiftDeduction
```

在 `duty_override_from_db`（line 54-56）后追加：

```python
def gift_deduction_from_db(db, month: str) -> dict:
    """已确认的赠送扣除 → {(receipt,barcode): deduct_qty}（带符号，ADR-023）。"""
    return {(r.receipt, r.barcode): Decimal(str(r.deduct_qty))
            for r in db.query(GiftDeduction).filter_by(month=month).all()}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest backend/tests/test_engine_bridge.py::test_gift_deduction_from_db -v`
Expected: PASS

- [ ] **Step 5: commit**

```bash
git add backend/app/services/engine_bridge.py backend/tests/test_engine_bridge.py
git commit -m "feat(bridge): gift_deduction_from_db 桥接确认扣除件数"
```

---

### Task 6: workflow 接入检测 + compute 装配

**Files:**
- Modify: `backend/app/routers/workflow.py:196-269`(check_anomalies) `:272-303`(_run_compute)
- Test: `backend/tests/test_workflow.py`

**Interfaces:**
- Consumes: `load_gift_qty_map_xlsx`（T1）、`check_gift_qty_mismatch`（T4）、`gift_deduction_from_db`（T5）、`compute(gift_deduction=)`（T2）

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_workflow.py`（用 `_setup_computed_month` 脚手架 + 让利表）。需先有一个让利 xlsx 造数 helper：

```python
def _gifts_xlsx(path, rows):
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["序号", "订单号/小票单号", "国际条码", "数量", "商品名称"])
    for r in rows:
        ws.append(r)
    wb.save(path)


def test_check_anomalies_gift_qty_mismatch(tmp_path, client):
    h = auth_header(client)
    # 复用脚手架建好已算月份（含1笔销售 R001/6920001 qty1）
    _setup_computed_month(tmp_path, client, h)
    # 让利表登记 R001/6920001 赠送2件 → 与销售1件不符 → type7
    g = tmp_path / "gifts.xlsx"
    _gifts_xlsx(g, [["1", "R001", "6920001", "2", "低温奶"]])
    with open(g, "rb") as f:
        client.post("/months/2026-06/import-gifts", headers=h, files={"file": ("gifts.xlsx", f)})
    r = client.post("/months/2026-06/check-anomalies", headers=h)
    types = [a["anomaly_type"] for a in r.json()["anomalies"]]
    assert "7" in types
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest backend/tests/test_workflow.py::test_check_anomalies_gift_qty_mismatch -v`
Expected: FAIL（type "7" 不在 types 中）

- [ ] **Step 3: 改 check_anomalies（在 `# 异常4` 之后、`# 清除旧异常` 之前插入）**

在 `backend/app/routers/workflow.py` 的 `check_anomalies`（`checker.check_products_complete(barcodes)` 之后）插入：

```python
    # 异常7: 赠送件数不符（仅当导入了让利明细）
    if m.gifts_file:
        from salary_engine.importer import load_gift_qty_map_xlsx
        from backend.app.services.engine_bridge import gift_deduction_from_db
        gift_qty_map = load_gift_qty_map_xlsx(m.gifts_file)
        # 销售件数按 (receipt,barcode) 聚合（仅非退货行）
        sales_qty_map: Dict[tuple, Decimal] = {}
        names: Dict[tuple, str] = {}
        for s in sales:
            if s.is_return:
                continue
            k = (s.receipt, s.barcode)
            sales_qty_map[k] = sales_qty_map.get(k, Decimal(0)) + Decimal(str(s.qty))
            names.setdefault(k, s.product_name)
        confirmed_keys = set(gift_deduction_from_db(db, month).keys())
        checker.check_gift_qty_mismatch(sales_qty_map, gift_qty_map, confirmed_keys, names)
```

（`Decimal` 已在 workflow.py 导入；若未导入，顶部补 `from decimal import Decimal`。）

- [ ] **Step 4: 改 _run_compute 装配 gift_deduction**

在 `_run_compute`（`gifts = load_gift_keys_xlsx(...)` 之后、`result = compute(` 之前）插入并改 compute 调用：

```python
    from backend.app.services.engine_bridge import gift_deduction_from_db
    gift_deduction = gift_deduction_from_db(db, month)
```

并给 `compute(...)` 调用（line 292-302）追加参数：

```python
        gift_keys=gifts,
        gift_deduction=gift_deduction,
        duty_override=duty_override,
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest backend/tests/test_workflow.py::test_check_anomalies_gift_qty_mismatch -v`
Expected: PASS

- [ ] **Step 6: commit**

```bash
git add backend/app/routers/workflow.py backend/tests/test_workflow.py
git commit -m "feat(workflow): check-anomalies 产 type7 + compute 装配 gift_deduction"
```

---

### Task 7: 确认扣除端点

**Files:**
- Modify: `backend/app/routers/workflow.py`（加端点）、`backend/app/schemas.py`（加 schema，若存在）
- Test: `backend/tests/test_workflow.py`

**Interfaces:**
- Produces: `POST /months/{month}/gift-deduction/confirm` `{anomaly_id}` -> `{deduct_qty, deduct_amt}`

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_workflow.py`（接 Task 6 的场景，确认后异常 resolved + GiftDeduction 落库 + compute 用扣除）：

```python
def test_confirm_gift_deduction(tmp_path, client, db_session):
    from backend.app.db import GiftDeduction, Anomaly
    h = auth_header(client)
    _setup_computed_month(tmp_path, client, h)
    g = tmp_path / "gifts.xlsx"
    _gifts_xlsx(g, [["1", "R001", "6920001", "2", "低温奶"]])
    with open(g, "rb") as f:
        client.post("/months/2026-06/import-gifts", headers=h, files={"file": ("gifts.xlsx", f)})
    client.post("/months/2026-06/check-anomalies", headers=h)
    anom = db_session.query(Anomaly).filter_by(month="2026-06", anomaly_type="7").one()
    r = client.post("/months/2026-06/gift-deduction/confirm", headers=h, json={"anomaly_id": anom.id})
    assert r.status_code == 200
    assert r.json()["deduct_qty"] == 1  # min(销售1, 赠送2)
    gd = db_session.query(GiftDeduction).filter_by(month="2026-06").one()
    assert gd.deduct_qty == 1
    assert anom.status == "resolved" or db_session.get(Anomaly, anom.id).status == "resolved"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest backend/tests/test_workflow.py::test_confirm_gift_deduction -v`
Expected: FAIL（404 无端点）

- [ ] **Step 3: 实现 schema + 端点**

在 `backend/app/routers/workflow.py`（与 `DutyBatch` 同区，约 line 97 后）加 schema：

```python
class GiftDeductionConfirm(BaseModel):
    anomaly_id: int
```

在 `set_duty`（约 line 124）后加端点：

```python
@router.post("/months/{month}/gift-deduction/confirm")
def confirm_gift_deduction(month: str, body: GiftDeductionConfirm,
                           _: User = Depends(current_user), db: Session = Depends(get_db)):
    """确认按件数扣除赠送（ADR-023）。从 anomaly 反查 receipt|barcode，算 min(赠送,销售)。"""
    from datetime import datetime
    from salary_engine.importer import load_gift_keys_xlsx, load_gift_qty_map_xlsx
    from backend.app.db import GiftDeduction, SalesRecord, Anomaly as AnomalyRow
    from backend.app.services.engine_bridge import sales_lines_from_db

    m = _get_month(db, month)
    anom = db.get(AnomalyRow, body.anomaly_id)
    if not anom or anom.month != month or anom.anomaly_type != "7":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "异常不存在或类型不符")
    if "|" not in (anom.entity_id or ""):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "异常 entity_id 格式错误")
    receipt, barcode = anom.entity_id.split("|", 1)

    # 让利件数
    gift_q = Decimal(0)
    if m.gifts_file:
        gift_q = load_gift_qty_map_xlsx(m.gifts_file).get((receipt, barcode), Decimal(0))
    # 销售件数（仅非退货，带符号）
    sales_q = Decimal(0)
    for r in db.query(SalesRecord).filter_by(month=month, receipt=receipt, barcode=barcode).all():
        if not r.is_return:
            sales_q += Decimal(str(r.qty))
    # deduct = min(|gift|, |sales|)，符号跟随 gift（销售赠品正 / 退货赠品负）
    sign = -1 if gift_q < 0 else 1
    deduct_qty = Decimal(sign) * min(abs(gift_q), abs(sales_q))
    # 预览扣除额（均摊）
    sales_amt = sum((Decimal(str(r.amount)) for r in db.query(SalesRecord)
                     .filter_by(month=month, receipt=receipt, barcode=barcode).all()
                     if not r.is_return), Decimal(0))
    deduct_amt = sales_amt * (deduct_qty / sales_q) if sales_q else Decimal(0)

    reason = f"销售{sales_q}件/赠送{gift_q}件，已扣除{deduct_qty}件"
    resolution = f"已确认扣除，扣除额{deduct_amt:.2f}元"
    db.query(GiftDeduction).filter_by(month=month, receipt=receipt, barcode=barcode).delete()
    db.add(GiftDeduction(month=month, receipt=receipt, barcode=barcode,
                         sales_qty=sales_q, gift_qty=gift_q, deduct_qty=deduct_qty,
                         reason=reason, resolution=resolution, status="confirmed"))
    anom.status = "resolved"
    anom.resolution = resolution
    anom.resolved_at = datetime.utcnow()
    m.results_stale = True
    db.commit()
    return {"deduct_qty": deduct_qty, "deduct_amt": deduct_amt}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest backend/tests/test_workflow.py::test_confirm_gift_deduction -v`
Expected: PASS

- [ ] **Step 5: commit**

```bash
git add backend/app/routers/workflow.py backend/tests/test_workflow.py
git commit -m "feat(workflow): POST gift-deduction/confirm 确认按件数扣除"
```

---

### Task 8: 前端异常面板 type 7 + 确认按钮

**Files:**
- Modify: `frontend/src/pages/steps/AnomalyPanel.tsx`、`frontend/src/api.ts`

**Interfaces:**
- Consumes: `POST /months/{month}/gift-deduction/confirm {anomaly_id}`

- [ ] **Step 1: api.ts 加封装**

在 `frontend/src/api.ts` 的 `workflowApiExtended`（约 line 221）加：

```ts
  confirmGiftDeduction: (month: string, anomalyId: number) =>
    http.post<{ deduct_qty: number; deduct_amt: number }>(
      `/months/${month}/gift-deduction/confirm`, { anomaly_id: anomalyId }).then(r => r.data),
```

并在 `AnomalyPanel.tsx` 顶部 import 补 `workflowApiExtended`：

```ts
import { anomalyApi, storesApi, productsApi, targetsApi, workflowApiExtended, type Anomaly } from "../../api";
```

- [ ] **Step 2: ANOMALY_TYPES 加 type 7 + parseDescription 加字段**

`AnomalyPanel.tsx` 的 `ANOMALY_TYPES`（line 29）加一项（"5" 后）：

```ts
  "7": { label: "赠送件数不符", icon: <Gift className="w-5 h-5" />, color: "text-blue-600 bg-blue-50 border-blue-200" },
```

`parseDescription`（line 41）patterns 数组追加：

```ts
    { key: "receipt", regex: /单号:\s*([^|]+)/ },
    { key: "barcode", regex: /条码:\s*([^|]+)/ },
    { key: "sales_qty", regex: /销售件数:\s*([^|]+)/ },
    { key: "gift_qty", regex: /赠送件数:\s*([^|]+)/ },
```

- [ ] **Step 3: 加 handler + 详情展示 + 按钮**

在 `handleDeductSales`（line 259）后加：

```ts
  // 类型7: 确认按件数扣除
  const handleConfirmDeduct = async (item: Anomaly) => {
    try {
      const r = await workflowApiExtended.confirmGiftDeduction(month, item.id);
      toast.success(`已确认扣除 ${r.deduct_qty} 件`);
      load();
      onResolved();
    } catch {
      toast.error("确认扣除失败");
    }
  };
```

详情展示区（line 325-334 的 `flex-wrap` 块）在末尾追加 type7 专用字段：

```tsx
                        {info.sales_qty && <span className="text-xs text-zinc-500">销售件数: {info.sales_qty}</span>}
                        {info.gift_qty && <span className="text-xs text-zinc-500">赠送件数: {info.gift_qty}</span>}
```

按钮分支（line 388 的 `)}` 前、type6 块之后）加：

```tsx
                      {/* 类型7: 确认扣除 / 忽略 */}
                      {expandedType === "7" && (
                        <>
                          <Button size="sm" variant="outline" onClick={() => handleIgnore(item.id)}>
                            <X className="w-3 h-3 mr-1" />忽略
                          </Button>
                          <Button size="sm" onClick={() => handleConfirmDeduct(item)}>
                            <Gift className="w-3 h-3 mr-1" />确认扣除
                          </Button>
                        </>
                      )}
```

- [ ] **Step 4: 类型检查通过**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无错误

- [ ] **Step 5: commit**

```bash
git add frontend/src/api.ts frontend/src/pages/steps/AnomalyPanel.tsx
git commit -m "feat(frontend): 异常面板 type7 赠送件数不符 + 确认扣除按钮"
```

---

### Task 9: 测试收尾 + 全量验证

**Files:**
- Modify: `backend/tests/test_workflow.py:392`（known_tags）

- [ ] **Step 1: known_tags 加"赠送扣除"**

`backend/tests/test_workflow.py:392` 改为：

```python
    known_tags = {"有效计提", "退货冲抵", "退货未匹配", "赠送剔除", "赠送扣除", "不计提成", "非乳品"}
```

- [ ] **Step 2: 全量后端 + 引擎测试**

Run: `uv run pytest backend/tests/ tests/ -q`
Expected: 全绿

- [ ] **Step 3: 前端类型检查 + 构建**

Run: `cd frontend && npx tsc --noEmit && npm run build`
Expected: 无错误

- [ ] **Step 4: 交付前真实 6 月数据前后对比（手测，记录到记忆）**

导入 6 月真实销售 + 让利 → check-anomalies：应**0 个 type7**（6 月销售件数全等于赠送件数）。人工造一组不符（改让利表某行数量）→ 出现 type7 → 点"确认扣除" → compute → 该组台账出现"赠送扣除"行、金额按均摊缩减。

- [ ] **Step 5: commit**

```bash
git add backend/tests/test_workflow.py
git commit -m "test(workflow): known_tags 加 赠送扣除"
```

---

## Self-Review（写完自检）

**1. Spec coverage：** spec 各节 → 任务映射：
- 数据模型 GiftDeduction → T3 ✓
- 检测层 check_gift_qty_mismatch → T4 + T6 装配 ✓
- 计算层（步骤1分叉/factor/步骤4/6）→ T2 ✓
- 让利件数解析（不落库）→ T1 ✓
- engine_bridge → T5 ✓
- confirm 端点 → T7 ✓
- 前端 type7 + 按钮 → T8 ✓
- 标签"赠送扣除" → T2 产出 + T9 测试 ✓
- 迁移脚本 → T3 ✓
- 行级台账均摊 → T2 步骤6 逐行 `amt = s.amount * factor` ✓
- 不做项（不落库/不弹窗/不改importer.load_gift_keys）→ 全程遵守 ✓

**2. Placeholder scan：** 无 TBD/TODO；每个代码步骤含完整代码。✓

**3. Type consistency：** `gift_deduction` 在 T2/T5/T6 均为 `dict[(str,str), Decimal]`；`deduct_qty` 带符号一致；`entity_id` 格式 `"{receipt}|{barcode}"` 在 T4/T7 一致；`load_gift_qty_map_xlsx` 在 T1 定义、T6/T7 调用一致。✓

## 执行选择

Plan complete and saved to `docs/superpowers/plans/2026-08-10-gift-qty-mismatch.md`。两种执行方式：

**1. Subagent-Driven（推荐）** — 每个任务派新 subagent，任务间我复审，快速迭代
**2. Inline Execution** — 本会话内按 executing-plans 批量执行，带检查点

选哪种？
