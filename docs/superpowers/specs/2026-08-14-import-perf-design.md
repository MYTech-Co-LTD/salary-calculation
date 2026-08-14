# 导入提速：内存去重直插 + OBS 中转上传（ADR-024）

> 2026-08-14。7 月销售 28MB/10.2 万行导入 60-90s → 目标 ~15-20s。用户已确认 A+C 组合。

## 实测基准（生产容器 + 临时库，2026-08-14）

| 阶段 | 现状耗时 | 优化后 |
|---|---|---|
| 网络传输 28MB（用户→源站 multipart） | 15-30s | 数秒（OBS 公网直传） |
| Excel 解析（calamine，10.2 万行） | 5.8s | 5.8s（不动） |
| SQLite 落库（WAL + on_conflict upsert） | 42-47s | **8.2s**（去重 0.7s + 直插 7.5s） |

关键发现：
- `on_conflict_do_update` 逐行索引探测是落库瓶颈；WAL 条件下 47s 甚至慢于默认 journal 36s；`synchronous=NORMAL` 仅省 ~5s → **journal/sync 参数不是杠杆，弃**。
- 上传流量走 OBS 后不过 openship-edge，天然绕开边缘 body 上限（同日踩过 1MB→413 坑）。

## Part A：落库提速（纯性能，不改数据流）

`sales_importer.import_sales_to_db` 重写插入路径：

```python
# 现状：1000/批 on_conflict_do_update（同文件重复行后写胜 via set_）
# 新：单遍组装 + dict 去重 + Core insert 1000/批

dedup: dict[tuple, dict] = {}
for s in sales:
    v = {...同现有字段...}
    key = (month, s.receipt, cleaned_store, s.sale_date, s.barcode, s.amount)
    dedup[key] = v                    # 后写覆盖前写（= on_conflict set_ 语义）
for i in range(0, len(vals), 1000):
    db.execute(SalesRecord.__table__.insert(), vals[i:i+1000])
db.commit()
```

- 唯一键 = 现有 `index_elements`（month/receipt/store/sale_date/barcode/amount），**语义零变化**。
- DELETE-then-insert 全量替换逻辑不变（H4）；`_determine_tag`/`clean_store` 不变。
- 删除 `_bulk_upsert`（无调用方）。
- 回归测试：同文件重复行 → 后写覆盖（qty/tag 取后行）；重导全量替换不变；`db_count == len(dedup)`。
- 兼容性：`transfer_sales` 等读方不受影响；upsert 曾防的"同文件重复"仅 3/102403 行（bench 输出 rows_out=102400），内存覆盖已覆盖。

## Part C：OBS 中转上传（数据流变化，ADR-024）

### 新服务 `backend/app/services/oss_upload.py`

复用 `oss_export` 的 env 与 `_clients()` 模式（import 复用或抽公共，写 plan 时定，倾向直接 from oss_export import _clients）：

```python
def is_configured() -> bool          # 同 oss_export
def presign_put(key: str, expires=600) -> str
    # 公网 virtual-host client.generate_presigned_url("put_object", ...)
def fetch_to_file(key: str, local_path: str) -> None
    # 内网 client.get_object → 流式写 local_path → finally delete_object（防桶膨胀）
KEY_PATTERN = r"^salary/uploads/(?P<month>\d{4}-\d{2})/(?P<kind>sales|gifts)-[0-9a-f]{32}\.xlsx$"
```

### 新端点（workflow.py，均需 current_user）

1. `POST /months/{month}/upload-ticket` body `{kind: "sales"|"gifts"}`
   - 校验 month 存在、kind 合法、`oss_upload.is_configured()`（未配 → 503，前端回退旧路径）
   - `key = salary/uploads/{month}/{kind}-{uuid4().hex}.xlsx`
   - 返回 `{url, key}`（10 分钟有效）
2. `POST /months/{month}/import-oss` body `{kind, key}`
   - **校验 `re.fullmatch(KEY_PATTERN, key)` 且 month/kind 与路径参数一致**（防登录用户拉桶内任意对象，如导出文件）
   - `fetch_to_file(key, uploads/{month}/{kind}.xlsx)`（同现有 `_save_upload` 落点，Month 字段更新/导入逻辑**抽公共函数复用**，不复制粘贴）
   - 失败（对象不存在/网络）→ 400 带原因
   - 返回值与旧 multipart 端点一致
   - 旧 `import-sales`/`import-gifts` multipart 端点**原样保留**（回退路径 + 本地开发未配 OBS）

### 前端 `ImportStep.tsx`

```
upload(kind, file):
  1. POST upload-ticket ──失败(503/网络)──→ 回退旧 multipart 直传
  2. XHR PUT file → url（onprogress 显示进度条，28MB 体验关键；fetch 无上传进度故用 XHR）
  3. POST import-oss {kind, key} ──失败──→ 提示重试（文件已在 OBS，不回退直传造成重复上传）
```

- DropZone loading 态展示进度百分比（`上传中 45%`）。
- `api.ts`：`workflowApiExtended.getUploadTicket(month, kind)` / `importFromOss(month, kind, key)`；multipart 上传函数保留。
- 进度 state：`uploading` 从 `string|null` 扩为 `{kind, progress}`。

### 桶 CORS（部署时一次性运维步骤）

现仅 GET *（ADR-022）。需加：AllowedMethods 加 `PUT`，AllowedOrigins `https://lesson.hookflow.cn`（或 `*`，与现有一致），AllowedHeaders `*`，ExposeHeaders `ETag`。用 boto3 `get_bucket_cors`/`put_bucket_cors` 原地合并，**不覆盖现有 GET 规则**。

### 错误提示（顺带修）

旧 `.catch(() => toast.error("上传失败，请检查文件格式"))` 吞真实错误（413 被报成"格式错误"的教训）。新路径按阶段报：ticket 失败→"无法建立上传通道"；PUT 失败→"上传到对象存储失败，请重试"；import-oss 失败→透出后端 message。

## 测试

- `backend/tests/test_sales_importer.py`（或就近）：同文件重复行后写覆盖；重导替换；计数正确。
- `backend/tests/test_workflow.py`：
  - ticket：未配 OSS → 503；返回 key 匹配 pattern。
  - import-oss：key 非法 → 400（含"合法 key 拉别的对象"用例）；key month/kind 与路径不一致 → 400。
  - import-oss 成功路径 mock `fetch_to_file`（单测不打真 OBS）→ 行为等同旧导入（sales_file 落盘、Month 更新、返回 total/db_count）。
- 前端：vitest 不可用（已知坑），`tsc --noEmit` + `vite build` 验证。

## 不做（YAGNI）

- 不做异步导入/状态机（用户未选 B）
- 不做断点续传/分片（28MB 单 PUT 足够）
- 不改解析层（calamine 5.8s 可接受）
- 不动 PRAGMA（实测收益小）
- 不删 multipart 旧端点
