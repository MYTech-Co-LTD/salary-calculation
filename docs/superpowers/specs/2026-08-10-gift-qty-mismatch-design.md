# 赠送件数不符异常 + 确认按件数扣除（ADR-023）

> 2026-08-10。原 `(receipt,barcode)` 命中即整组剔除、无数量意识；6 月数据验证 0 案例但理论有"买多送少"多剔风险，需进异常排查可人工确认按件数扣除。

## 背景

让利明细（gift_keys）与销售流水按 `(receipt,barcode)` 匹配，命中即整组剔除（`calculator.py:67`），**无数量意识**。

6 月真实数据交叉验证（gift_keys 2444 个，100% 命中销售流水）：

| 对比 | 数量 |
|---|---|
| 销售件数 == 赠送件数 | 2444（全部） |
| 销售件数 > 赠送件数（"买多于送"） | 0 |
| 销售件数 < 赠送件数 | 0 |

现状对真实数据**精确**，但理论存在"同组买3送1"会被整组剔除（多剔2件）的静默风险。本设计为该风险提供：**异常排查发现 + 人工确认按件数扣除**。

## 决策（口径，用户已确认 2026-08-10）

| 情形 | 处理 |
|---|---|
| 销售件数 == 赠送件数 | 自动整组剔除（**现状，零变化**） |
| 销售件数 ≠ 赠送件数 | 进异常排查 type "7"，pending 拦 compute（复用前端软门禁） |
| 已确认扣除 | 扣 `min(赠送,销售)` 件；扣除额 = 组总额 × (扣除件数/销售件数)（**均摊**）；剩余计提成 |
| 销售 < 赠送 | 扣到销售件数为止（全剔）+ 标异常原因 |
| 不符但被"忽略" | 维持整组剔除（现状），异常说明告知"忽略将整组剔除" |

金额均摊口径：扣除额 = 组总额 × (扣除件数 / 销售件数)，与单行单价差异无关；件数相等时与"整组剔除"金额完全一致 → 平滑兼容。

## 数据模型

新增表 `GiftDeduction`（**持久化确认结果，不受 anomalies 快照重建影响**）：

```python
class GiftDeduction(Base):
    """赠送件数不符——人工确认扣除记录（compute 读取，影响计算）"""
    __tablename__ = "gift_deductions"
    id = Column(Integer, primary_key=True)
    month = Column(String, nullable=False, index=True)
    receipt = Column(String, nullable=False)
    barcode = Column(String, nullable=False)
    sales_qty = Column(Numeric)        # 销售件数（确认时快照）
    gift_qty = Column(Numeric)         # 让利表赠送件数
    deduct_qty = Column(Numeric)       # 实际扣除件数 = min(|gift|,|sales|)，带符号
    reason = Column(String(300))       # 异常原因
    resolution = Column(String(300))   # 处理情况
    status = Column(String(20), default="confirmed")
    created_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("month", "receipt", "barcode", name="uq_gift_deduction"),)
```

迁移脚本 `migrations/004_gift_deduction.py`（entrypoint 自动跑，ADR-016）。

**不新增"让利明细落库"表**——`gift_keys` 仍从 gifts xlsx 实时解析（与现状一致）；**`importer.load_gift_keys` 不改**（保持返回 set 给 calculator）。让利表件数由 checker 内部独立解析。

## 检测层（anomaly_checker.py）

新增 `check_gift_qty_mismatch(sales, gift_qty_map, confirmed_keys)`：

- `gift_qty_map`：checker 内部解析 gifts xlsx 得 `{(receipt,barcode): 赠送件数(带符号)}`（读"数量"列，聚合同 key 多行，如那 4 个案例 2 行各 1 → 2）
- sales 聚合：`{(receipt,barcode): 销售件数(带符号)}`（从 SalesRecord）
- 对每个在 `gift_qty_map` 的 key：
  - `key in confirmed_keys`（已确认）→ 跳过
  - `sales_qty != gift_qty` → 产异常 `anomaly_type="7"`, `entity_type="gift"`, `entity_id="{receipt}|{barcode}"`, `description="赠送件数不符 | 销售件数:X | 赠送件数:Y | 商品名:Z"`
- `workflow.py:check_anomalies` 调用前：解析 gifts xlsx 为 gift_qty_map、查 GiftDeduction 得 confirmed_keys

`anomaly_type="7"`；前端 `ANOMALY_TYPES` 加 `"7": {label:"赠送件数不符", icon:Gift, color:blue}`；`db.py` 注释 `# 1-6` → `# 1-7`。

## 计算层（calculator.py，口径变更核心）

`compute` 新增参数 `gift_deduction=None`（`{(receipt,barcode): deduct_qty}`）。**`gift_keys` 保持 set 不变。**

步骤 1 命中组分叉（`calculator.py:67` 处）：

```python
if (ln.receipt, ln.barcode) in gift_keys:
    if gift_deduction and (ln.receipt, ln.barcode) in gift_deduction:
        # 已确认按件数扣 → 不整组剔，照常进聚合（步骤2再按均摊扣）
        pass
    else:
        excluded.append((ln, "赠送剔除")); continue      # 现状：整组剔
```

步骤 2 聚合后，对在 gift_deduction 的组应用均摊扣除：

```
对每个 (receipt,barcode) in gift_deduction 且在 groups 的组 g：
    total_qty  = Σ s.qty           # 销售件数（带符号）
    total_amt  = Σ s.amount        # 组总额
    deduct_qty = gift_deduction[key]
    deduct_amt = total_amt × (deduct_qty / total_qty)        # 均摊
    该组计入 daily_sales / 提成的净额 = total_amt − deduct_amt
    该组 DetailRow tag = "赠送扣除"
```

**行级台账（逐行 DetailRow，ADR-003/004 约束）**：该组在 gift_deduction 时，组内各行 tag 标 **"赠送扣除"**；逐行 `commission` 与 `amount` 按均摊系数 `(1 − deduct_qty/total_qty)` 缩减，使 `Σ行amount = 组净额`、`Σ行commission = 组净额 × rate`（与组级均摊一致）。具体逐行分摊实现写 plan 时细化。

件数相等组：不进异常、不在 gift_deduction → 整组剔（现状），**零变化**。

`engine_bridge.py` 加 `gift_deduction_from_db(db, month)` → `{(receipt,barcode): deduct_qty}`；`workflow.py:_run_compute` 装配后传给 `compute`。

## API（后端）

新增 `POST /months/{month}/gift-deduction/confirm`（仿 `PUT /months/{month}/duty`）：

- body: `{receipt, barcode}`
- 服务端：
  - 解析 gifts xlsx 得 gift_qty（带符号）
  - 查 SalesRecord 聚合得 sales_qty（带符号）
  - `deduct_qty = min(|gift_qty|, |sales_qty|)` × (gift_qty 符号)
  - upsert GiftDeduction(month, receipt, barcode, sales_qty, gift_qty, deduct_qty, reason, resolution, status="confirmed")
  - 当月同 `(receipt,barcode)` 的 type "7" Anomaly → status="resolved"
  - `Month.results_stale = True`
- 返回 `{deduct_qty, deduct_amt_preview}`

## 前端（AnomalyPanel.tsx）

- `ANOMALY_TYPES` 加 `"7": {label:"赠送件数不符", icon:<Gift/>, color:"text-blue-600 ..."}`
- `parseDescription` 支持 `| 销售件数 / | 赠送件数 / | 商品名`
- 展开按钮分支 `expandedType === "7"`：`<Button onClick={() => handleConfirmDeduct(item)}>确认扣除</Button>`
- `handleConfirmDeduct`：调新端点 → toast → `load()` → `onResolved()`（仿 `handleDeductSales`，但调 confirm 端点）
- `api.ts`：`workflowApiExtended.confirmGiftDeduction(month, {receipt, barcode})`

## 标签 + 原因 + 处理情况

- `DetailRow.tag` 新增 **"赠送扣除"**（已确认组剩余计提成行）
- `GiftDeduction.reason`：`"销售{X}件/赠送{Y}件，已扣除{N}件"`
- `GiftDeduction.resolution`：`"已确认扣除，扣除额{amt}元"`
- **不改 SalesRecord**（原始留底），扣除只在计算层，台账可追溯

## 测试

- `calculator`：件数相等（无 gift_deduction，整组剔，不变）/ 销售>赠送（gift_deduction 扣，均摊金额正确）/ 销售<赠送（全剔）
- `anomaly_checker`：产出 type "7"；已确认组不重复产出；销售==赠送不产出
- `workflow`：confirm 端点写 GiftDeduction + Anomaly resolved + results_stale；_run_compute 装配 gift_deduction
- `test_workflow.py:392` known_tags 集合加 `"赠送扣除"`

## 不做（YAGNI）

- 不让利明细整体落库（gift_keys 仍从 xlsx 解析）
- 不弹窗让用户输入扣除件数（一键 min，服务端算）
- 不给 SalesRecord 加异常列（用 GiftDeduction 存原因/处理）
- 不做硬门禁（沿用前端软门禁现状）
- 不改 importer.load_gift_keys（保持 set）
