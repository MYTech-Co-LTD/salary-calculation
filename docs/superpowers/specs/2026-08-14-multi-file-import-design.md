# 多文件导入：导入会话 + 预览确认（ADR-025）

> 2026-08-14。背景：销售流水/让利明细目前每月单文件上传即导入（multipart 或 ADR-024 OBS 直传），一个月拆多文件、补传、跨月文件需手工合并。
> 用户已确认四个场景全选（一月多文件 / 补传增量 / 让利多文件 / 跨月一起传）、数据质量要「导入前预览确认」、行级问题走现有异常检查、「全量替换（文件集合 = 当月完整数据）」、跨月自动建月。

## 决策

新增 **ImportSession + ImportFile 两张表**，导入改为两阶段会话流程：

```
上传（多文件，逐文件解析校验） → 预览报告（按月分组，人工确认） → 落库（按月 DELETE+insert）
```

- 合并语义 = **全量替换**：一次会话内某月的文件集合 = 该月完整数据。补传 = 老文件+新文件一起重选。
- 让利明细**仍不落库**（沿用 ADR-023 决策），keys/qty map 每次从 active 文件现场解析。
- 旧单文件端点（import-sales / import-gifts / upload-ticket / import-oss）**原样保留**（脚本兼容 + 回退），UI 全面切会话流程。

## 1. 数据模型

```python
class ImportSession(Base):
    __tablename__ = "import_sessions"
    id            # String PK，uuid4().hex
    month_context # 创建时的月份上下文（让利文件归属 + 默认月）
    status        # pending | confirmed | expired
    report        # JSON（nullable）：最新预览报告，按月分组
    created_at / confirmed_at

class ImportFile(Base):
    __tablename__ = "import_files"
    id          # Integer PK
    session_id  # FK -> import_sessions.id
    kind        # "sales" | "gifts"
    filename    # 原始文件名
    path        # staging 落盘绝对路径
    status      # ok | bad（bad 附 error 原因，不参与导入/确认）
    error       # String，nullable，bad 时的解析失败原因
    rows        # 解析出行数（sales=总行数；gifts=key 数）
    months      # JSON，nullable：sales=按 sale_date 路由到的月份列表；gifts=[month_context]
    active      # Boolean：confirm 后是否属于某月「现行数据集」的成员
```

- **新表 create_all 即可建**（踩坑记忆只限加列；加新表安全，生产启动自动建）。
- `Month.sales_file / gifts_file` 单路径字段：**读方业务逻辑全部废弃**，新增 helper：

```python
def active_files(db, month: str, kind: str) -> list[str]:
    """某月现行数据集的物理文件路径列表（最近一次 confirm 的 active 文件）。"""
```

  - 读方包括：`_apply_gifts_import` 的 sales 重导入（`load_sales_xlsx(m.sales_file)`）、anomaly check 的 `load_gift_qty_map_xlsx(m.gifts_file)`、`gift-deduction/confirm` 的 qty map 读取、compute 的 `if not m.sales_file` 前置守卫、月份详情 UI 的文件展示（`ImportStep` 完成态判断 / `MonthWorkspace` 门禁）——全部切 `active_files`/「最近 confirm 会话存在性」口径。
  - 会话 confirm **不回填** Month 字段（旧字段保留历史值，兼容旧单文件端点继续写）。
  - 让利 keys/gift qty map 现场解析：遍历该月 active gifts 文件，合并 `load_gift_keys_xlsx` / `load_gift_qty_map_xlsx` 结果，**按 (receipt, barcode) 键去重、后写覆盖**（与 sales 去重口径一致）。

## 2. 流程与 API

```
① POST   /imports/sessions  {month_context}                    → 创建会话
② POST   /imports/sessions/{id}/tickets  {kind, ext}           → {url, key}（OBS 预签名 PUT）
   浏览器 XHR PUT → POST /imports/sessions/{id}/files {kind, key}
   或回退：POST /imports/sessions/{id}/files-multipart {kind, file}
   后端 fetch 到 staging/{session_id}/ 并即时解析：
   - sales：按行内 sale_date 路由月份；统计总行数/跳过行/跨文件重复键
   - gifts：归属 month_context；统计 key 数
   - 解析失败 → 该文件标 bad + error 原因，不影响其他文件
③ GET    /imports/sessions/{id}                                 → 会话 + 预览报告
④ POST   /imports/sessions/{id}/confirm                         → 落库（幂等护栏见 §3）
⑤ DELETE /imports/sessions/{id}/files/{fid}                     → 移除误传/bad 文件
```

- 全部端点 `Depends(current_user)`（同 workflow 路由）。
- **session 级 ticket**：key = `salary/uploads/{session_id}/{kind}-{uuid4().hex}{ext}`（`oss_upload.upload_key/key_matches` 泛化为 scope 参数；旧端点传 month 行为不变）。files 端点校验 key 前缀 = 本会话 id，防借端点拉桶内任意对象（沿用 ADR-024 安全原则）。
- 文件永远物理留在 `uploads/staging/{session_id}/`（确认后不搬移），台账靠 `ImportFile` 表；清理只删 pending/expired 会话的目录（见 §4）。

### confirm 原子动作（单事务，失败整体回滚）

涉及月集合 = sales 文件路由月 ∪ {month_context | 会话含 gifts 文件}。对每个月 M：

1. M 不存在 → 自动建月（`Month(month=M, status="draft")`）。
2. **sales 数据源**：
   - 会话有 sales 文件路由到 M → 文件集合的解析行（会话文件 confirm 后 active=1，旧 sales active=0）；
   - 否则（M == month_context 仅有 gifts 变更）→ 沿用 M 现有 active sales 文件（active 不变，**不删 sales**，仅重打 tag）。
3. **gift_keys**：合并 M 的 post-flip active gifts 文件（会话 gifts → active=1、旧 gifts active=0）。
4. DELETE M 的 SalesRecord → 内存去重（后写覆盖，键同 ADR-024）→ 批插（复用 `import_sales_to_db` 语义）。
5. `results_stale = True`；Anomaly 表不动（沿用现有重导语义，下次 check 重算）。

> 语义护栏：**只替换会话含 kind 的月**。某月若会话既无 sales 文件也无 gifts 变更 → 完全不动。

## 3. 预览报告与全量替换护栏

report JSON 结构（GET session 返回）：

```json
{
  "months": {
    "2026-08": {
      "sales_files": [{"id": 1, "filename": "a.xlsx", "rows": 50000}],
      "total_rows": 98765, "dup_rows": 12, "skipped_rows": 3,
      "existing_rows": 102400, "replace_rows": 98765
    }
  },
  "gifts": {"month": "2026-08", "files": [{"id": 2, "filename": "g.xlsx", "rows": 345}], "key_count": 340, "dup_keys": 5}
}
```

- 每涉及月显示「现有 N 行 → 替换后 M 行」；**M 明显小于 N（如 < 50%）→ 前端红字警示**（防补传只带新文件忘带老文件、整月被换薄）。
- 跨文件重复键 = 会话内同月 sales 文件之间唯一键重叠计数（confirm 时后写覆盖，报告告知丢多少行）。
- bad 文件独立列出（红字 + error 原因），不计入统计。

## 4. 错误处理与生命周期

- 会话含 bad 文件 → **confirm 拒绝 409**（须先 DELETE 移除；宁可多一步，不默默漏数据）。
- 会话 `status != pending`（已确认/已过期）→ confirm 409。
- 会话不存在 → 404；文件不属于该会话 → 404。
- 新建会话时：同用户 pending 超 **24h** 的旧会话 → 标 expired，并清理其 staging 目录（`shutil.rmtree`，容错）。confirmed 会话目录**永不清**（台账溯源）。
- 旧四端点原样保留：脚本/回退路径继续可用；UI 不再调用。

## 5. 前端（ImportStep 重构）

- 两个 DropZone（销售/让利）改 `multiple` 多选；文件**依次** ticket → XHR PUT（进度条）→ 入会话；整体进度 = 「第 i/n 个 + 当前文件百分比」。
- 报告面板（会话态）：文件清单（状态/行数/bad 红字原因 + 移除按钮）+ 按月分组统计 + 「现有 N 行 → 替换后 M 行」对比与红字警示。
- 「确认导入」按钮 → 成功 toast（中文）+ 步骤完成态（`monthStepApi.update(month, "import", {import: true})`）。
- 回退链不变：ticket 失败（未配 OBS/网络）→ multipart 入会话。
- 进入导入页时若已有本文件的 pending 会话 → 恢复展示（会话 id 存组件 state / 页面刷新后不恢复，首次实现不做持久化恢复，YAGNI）。
- `api.ts`：`importsApi` 封装（createSession / getTicket / addFile / addFileMultipart / getSession / confirmSession / removeFile）。

## 6. 测试

后端（`backend/tests/test_import_sessions.py` 新建 + 既有文件回归）：

- 会话创建；ticket key 前缀 = session_id 且 kind/ext 校验（复用 oss_upload 泛化后的 key_matches）。
- 加文件：sales 跨月路由正确性（一个月文件 → 多个 month）；行数/跳过行/跨文件重复键统计正确。
- bad 文件：解析失败标 bad + error，其他文件不受影响；**含 bad → confirm 409**；移除后可 confirm。
- confirm 多月：自动建月、DELETE+insert、内存去重后写覆盖、results_stale、active 翻转（旧→0 新→1）。
- confirm 仅 gifts 变更（无 sales 文件）：sales 不删、用新 gift_keys 重打 tag。
- confirm 幂等护栏：status≠pending → 409；会话过期 → 409。
- gift qty map 读方（anomaly check / gift-deduction）切 active_files 后行为回归。
- 旧四端点回归不动。
- 生命周期：超 24h pending 新建会话时被标 expired + staging 清理；confirmed 目录保留。

前端：`tsc --noEmit` + `vite build`（vitest 不可用，已知坑）。

## 不做（YAGNI）

- 不做追加模式（用户选全量替换）；不做会话断点恢复 UI（刷新丢会话，重新上传即可）。
- 不做让利明细整体落库（沿用 ADR-023）。
- 不做跨会话的重复提醒（同一文件传进两个会话 → 各自 confirm 后靠「替换行数对比」护栏兜底）。
- 不做异步导入/状态机（沿用 ADR-024 决策：同步落库 + OBS 直传已达标）。