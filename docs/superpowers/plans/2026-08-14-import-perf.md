# 导入提速（去重直插 + OBS 中转上传）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 销售流水导入 60-90s → ~15-20s：落库 upsert 改内存去重直插（42s→8s），上传走 OBS 预签名直传绕开源站带宽。

**Architecture:** Part A 纯性能——`import_sales_to_db` 弃 `on_conflict_do_update`，单遍组装 dict 去重（后写覆盖前写，唯一键同现有 uq_sales_record）+ Core insert 批插；Part C 数据流——前端 ticket→XHR PUT 直传 OBS→后端内网拉回复用现有导入，未配 OBS 回退旧 multipart 端点（保留不删）。

**Tech Stack:** FastAPI + SQLAlchemy（SQLite）/ boto3 presign（复用 `oss_export._clients()` 双 endpoint）/ React+TS（XHR 上传进度）。

**Spec:** `docs/superpowers/specs/2026-08-14-import-perf-design.md`（ADR-024）

## Global Constraints

- 注释/UI 文案一律中文，风格贴既有代码
- 旧端点 `POST /months/{m}/import-sales`、`import-gifts` **行为不变、保留**（回退路径）
- `import_sales_to_db` 返回值语义不变：`{"total": 文件行数, "db_count": 去重后库行数}`
- 同文件重复行语义不变：唯一键 (month, receipt, store, sale_date, barcode, amount) 后写覆盖前写
- OSS 未配置（env 缺 OSS_*）时：ticket 端点 503，前端回退 multipart，全链路不报错
- 跑 pytest 用项目 uv venv（`.venv`，python 3.12）；前端验证 `tsc --noEmit` + `vite build`（vitest 不可用，已知坑）

---

### Task 1: 落库提速——内存去重 + 纯批插

**Files:**
- Modify: `backend/app/services/sales_importer.py:41-96`（插入路径；`_bulk_upsert` 删除）
- Test: `backend/tests/test_import_master.py`（追加 1 个测试）

**Interfaces:**
- Consumes: `salary_engine.models.SalesLine`、`_determine_tag`、`clean_store`（均现有）
- Produces: `import_sales_to_db(db, month, sales, gift_keys) -> {"total": int, "db_count": int}` 签名与语义不变（Task 3 的公共 helper 依赖它）

- [ ] **Step 1: 写特征测试（锁定"后写覆盖前写"的字段值，现有 upsert 也应通过）**

在 `backend/tests/test_import_master.py` 末尾追加（`_line` 构造器在同文件 `:40` 附近，先读一眼确认字段）：

```python
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
```

注意：若 `_line` 生成的两条 SalesLine 唯一键不完全相同（如 amount 随 i 变），改为手工构造两条同键 SalesLine。

- [ ] **Step 2: 跑测试确认现状通过（这是重构的特征测试，不要求先红）**

Run: `uv run pytest backend/tests/test_import_master.py -v`
Expected: 全 PASS

- [ ] **Step 3: 重写插入路径**

`sales_importer.py` 中 `import_sales_to_db` 的插入段（`BATCH = 1000` 到 `_bulk_upsert` 调用，`:41-70`）替换为：

```python
    # ADR-024 Part A：内存 dict 去重（同文件重复键后写覆盖前写，语义同原 on_conflict
    # upsert 的 set_）+ Core insert 批插。弃 on_conflict：10 万行实测 42s → 8s。
    dedup: dict[tuple, dict] = {}
    for s in sales:
        tag = _determine_tag(s, products, gift_keys)
        cleaned_store = clean_store(s.store)
        v = dict(
            month=month,
            receipt=s.receipt,
            src_order=s.src_order,
            store=cleaned_store,
            sale_date=s.sale_date,
            barcode=s.barcode,
            product_name=s.product_name,
            qty=s.qty,
            amount=s.amount,
            unit_price=s.unit_price,
            salesperson=s.salesperson,
            cashier=s.cashier,
            is_return=s.is_return,
            is_online=s.is_online,
            tag=tag,
            original_store=cleaned_store,
            original_date=s.sale_date,
            extra=(s.raw or None),  # 源 Excel 全字段留底；空则存 None（T6.2）
        )
        key = (month, s.receipt, cleaned_store, s.sale_date, s.barcode, s.amount)
        dedup[key] = v  # 后写覆盖前写
    values = list(dedup.values())
    BATCH = 1000
    for i in range(0, len(values), BATCH):
        db.execute(SalesRecord.__table__.insert(), values[i:i + BATCH])
```

同时：删除 `_bulk_upsert` 函数（`:79-96`，已无调用方）；删除顶部 `from sqlalchemy.dialects.sqlite import insert as sqlite_insert`（已不用）。

- [ ] **Step 4: 全量回归**

Run: `uv run pytest backend/tests/ -v`
Expected: 全 PASS（重点 `test_import_sales_bulk_and_reimport_clears_stale`、`test_import_sales_bulk_within_file_duplicates`、`test_import_sales_persists_raw_to_extra`、`test_workflow.py` 导入相关）

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sales_importer.py backend/tests/test_import_master.py
git commit -m "perf(importer): 内存去重+纯批插，10万行落库 42s→8s（ADR-024 A）"
```

---

### Task 2: OSS 上传服务（presign PUT + 内网拉取）

**Files:**
- Create: `backend/app/services/oss_upload.py`
- Test: `backend/tests/test_oss_upload.py`

**Interfaces:**
- Consumes: `backend/app/services/oss_export.py` 的 `_clients()`（返回 (内网 upload, 公网 presign) 两个 boto3 client）、`_env(k, default)`、`is_configured()`
- Produces（Task 3 依赖，签名精确如下）:
  - `is_configured() -> bool`
  - `upload_key(month: str, kind: str) -> str`（如 `salary/uploads/2026-07/sales-{32位hex}.xlsx`，前缀随 env `OSS_PREFIX`）
  - `key_matches(key: str, month: str, kind: str) -> bool`
  - `presign_put(key: str, expires: int = 600) -> str`
  - `fetch_to_file(key: str, local_path: str) -> None`（失败抛异常；**finally 总删对象**——中转用完即弃，失败重试意味着整段重来）

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_oss_upload.py
"""OSS 上传中转服务（ADR-024 Part C）：预签名 PUT + 内网拉取。"""
import pytest


@pytest.fixture
def oss_env(monkeypatch):
    monkeypatch.setenv("OSS_BUCKET", "test-bucket")
    monkeypatch.setenv("OSS_ACCESS_KEY", "ak")
    monkeypatch.setenv("OSS_SECRET_KEY", "sk")
    monkeypatch.setenv("OSS_ENDPOINT_INTERNAL", "http://internal.example")
    monkeypatch.setenv("OSS_ENDPOINT_PUBLIC", "https://pub.example")


def test_is_configured(oss_env):
    from backend.app.services import oss_upload
    assert oss_upload.is_configured() is True


def test_is_configured_false_without_env(monkeypatch):
    from backend.app.services import oss_upload
    for k in ("OSS_BUCKET", "OSS_ACCESS_KEY", "OSS_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert oss_upload.is_configured() is False


def test_upload_key_and_match(oss_env):
    from backend.app.services import oss_upload
    key = oss_upload.upload_key("2026-07", "sales")
    assert key.startswith("salary/uploads/2026-07/sales-")
    assert key.endswith(".xlsx")
    assert oss_upload.key_matches(key, "2026-07", "sales") is True
    # key 与路径参数不一致 / 非本服务签发 → 拒绝
    assert oss_upload.key_matches(key, "2026-06", "sales") is False
    assert oss_upload.key_matches(key, "2026-07", "gifts") is False
    assert oss_upload.key_matches("salary/v3/2026-06.xlsx", "2026-07", "sales") is False


def test_presign_put_url_shape(oss_env):
    from backend.app.services import oss_upload
    url = oss_upload.presign_put("salary/uploads/2026-07/sales-abc.xlsx")
    # 公网 virtual-host：bucket 拼进域名，path 是 key
    assert url.startswith("https://test-bucket.pub.example/")
    assert "salary/uploads/2026-07/sales-abc.xlsx" in url
    assert "X-Amz-Signature" in url


def test_fetch_to_file_writes_and_deletes(oss_env, tmp_path, monkeypatch):
    from backend.app.services import oss_upload

    deleted = []

    class _Body:
        def iter_chunks(self, size):
            yield b"hello "
            yield b"xlsx"

    class _FakeUpload:
        def get_object(self, Bucket, Key):
            assert Bucket == "test-bucket" and Key == "salary/uploads/k.xlsx"
            return {"Body": _Body()}

        def delete_object(self, Bucket, Key):
            deleted.append(Key)

    monkeypatch.setattr(oss_upload, "_clients", lambda: (_FakeUpload(), None))
    p = tmp_path / "out.xlsx"
    oss_upload.fetch_to_file("salary/uploads/k.xlsx", str(p))
    assert p.read_bytes() == b"hello xlsx"
    assert deleted == ["salary/uploads/k.xlsx"]  # 用完即删


def test_fetch_to_file_deletes_even_on_failure(oss_env, tmp_path, monkeypatch):
    from backend.app.services import oss_upload

    deleted = []

    class _BrokenUpload:
        def get_object(self, Bucket, Key):
            raise RuntimeError("网络断")

        def delete_object(self, Bucket, Key):
            deleted.append(Key)

    monkeypatch.setattr(oss_upload, "_clients", lambda: (_BrokenUpload(), None))
    with pytest.raises(RuntimeError):
        oss_upload.fetch_to_file("salary/uploads/k.xlsx", str(tmp_path / "o.xlsx"))
    assert deleted == ["salary/uploads/k.xlsx"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest backend/tests/test_oss_upload.py -v`
Expected: FAIL（`ModuleNotFoundError: backend.app.services.oss_upload`）

- [ ] **Step 3: 实现服务**

```python
# backend/app/services/oss_upload.py
"""导入文件经 OBS 中转上传（ADR-024 Part C）。

与导出下载（ADR-022 oss_export）对称：浏览器拿公网预签名 URL 直传 PUT，
后端从云内网拉回本地 uploads 目录，拉取后即删对象（中转用完即弃）。
"""
import re
from uuid import uuid4

from backend.app.services.oss_export import _clients, _env, is_configured  # noqa: F401 (re-export)

__all__ = ["is_configured", "upload_key", "key_matches", "presign_put", "fetch_to_file"]


def upload_key(month: str, kind: str) -> str:
    """生成一次性上传 key：{prefix}uploads/{month}/{kind}-{uuid}.xlsx"""
    return f"{_env('OSS_PREFIX', 'salary/')}uploads/{month}/{kind}-{uuid4().hex}.xlsx"


def key_matches(key: str, month: str, kind: str) -> bool:
    """校验 import-oss 收到的 key 确为本服务签发的当月当类型上传 key。

    防登录用户借端点拉桶内任意对象（如导出文件）。"""
    prefix = f"{_env('OSS_PREFIX', 'salary/')}uploads/{month}/{kind}-"
    if not key.startswith(prefix):
        return False
    return bool(re.fullmatch(r"[0-9a-f]{32}\.xlsx", key[len(prefix):]))


def presign_put(key: str, expires: int = 600) -> str:
    """公网 virtual-host 预签名 PUT URL（默认 10 分钟有效）。"""
    _, presign = _clients()
    return presign.generate_presigned_url(
        "put_object", Params={"Bucket": _env("OSS_BUCKET"), "Key": key}, ExpiresIn=expires)


def fetch_to_file(key: str, local_path: str) -> None:
    """内网拉取对象写本地文件。finally 总删对象：中转用完即弃，
    失败也不留垃圾（重试 = 前端重新走 ticket 整段上传）。"""
    upload, _ = _clients()
    bucket = _env("OSS_BUCKET")
    try:
        resp = upload.get_object(Bucket=bucket, Key=key)
        with open(local_path, "wb") as out:
            for chunk in resp["Body"].iter_chunks(1024 * 1024):
                out.write(chunk)
    finally:
        try:
            upload.delete_object(Bucket=bucket, Key=key)
        except Exception:
            pass  # 删除失败不掩盖主流程异常
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest backend/tests/test_oss_upload.py -v`
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/oss_upload.py backend/tests/test_oss_upload.py
git commit -m "feat(oss): 导入中转服务——预签名PUT+内网拉取+key校验（ADR-024 C）"
```

---

### Task 3: 端点 upload-ticket + import-oss（抽公共导入逻辑）

**Files:**
- Modify: `backend/app/routers/workflow.py:32-78`（两个旧端点改调公共 helper；追加两个新端点）
- Test: `backend/tests/test_workflow.py`（追加）

**Interfaces:**
- Consumes: Task 2 的 `oss_upload.{is_configured, upload_key, key_matches, presign_put, fetch_to_file}`；现有 `_get_month`、`UPLOAD_DIR`
- Produces:
  - `POST /months/{m}/upload-ticket` body `{"kind": "sales"|"gifts"}` → `200 {"url": str, "key": str}` / `503`（OSS 未配）/ `400`（kind 非法）
  - `POST /months/{m}/import-oss` body `{"kind": "sales"|"gifts", "key": str}` → 与旧 multipart 端点同构返回（sales: `{"sales_file", "total", "db_count"}`；gifts: `{"gifts_file"}`）/ `400`（key 非法或拉取失败）
  - 模块内私有 helper `_apply_sales_import(db, m, path) -> dict`、`_apply_gifts_import(db, m, path) -> None`（旧端点与新端点共用）

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_workflow.py`（复用文件顶部 `auth_header`/`_sales_xlsx`）：

```python
def _mk_month_with_sales(tmp_path, client, h):
    client.post("/months", headers=h, json={"month": "2026-06"})
    s = tmp_path / "sales.xlsx"; _sales_xlsx(s)
    with open(s, "rb") as f:
        client.post("/months/2026-06/import-sales", headers=h,
                    files={"file": ("sales.xlsx", f)})


def test_upload_ticket_requires_oss(client, monkeypatch):
    """未配 OSS → 503（前端据此回退 multipart）"""
    h = auth_header(client)
    client.post("/months", headers=h, json={"month": "2026-06"})
    from backend.app.services import oss_upload
    monkeypatch.setattr(oss_upload, "is_configured", lambda: False)
    r = client.post("/months/2026-06/upload-ticket", headers=h, json={"kind": "sales"})
    assert r.status_code == 503


def test_upload_ticket_returns_url_and_key(client, monkeypatch):
    h = auth_header(client)
    client.post("/months", headers=h, json={"month": "2026-06"})
    from backend.app.services import oss_upload
    monkeypatch.setattr(oss_upload, "is_configured", lambda: True)
    monkeypatch.setattr(oss_upload, "presign_put", lambda key, expires=600: f"https://fake/{key}")
    r = client.post("/months/2026-06/upload-ticket", headers=h, json={"kind": "sales"})
    assert r.status_code == 200
    body = r.json()
    assert body["url"] == f"https://fake/{body['key']}"
    assert oss_upload.key_matches(body["key"], "2026-06", "sales")
    # kind 非法 → 400
    r2 = client.post("/months/2026-06/upload-ticket", headers=h, json={"kind": "other"})
    assert r2.status_code == 400


def test_import_oss_rejects_bad_key(client, monkeypatch, tmp_path):
    h = auth_header(client)
    client.post("/months", headers=h, json={"month": "2026-06"})
    from backend.app.services import oss_upload
    fetched = []
    monkeypatch.setattr(oss_upload, "fetch_to_file",
                        lambda key, path: fetched.append(key))
    # 非 32hex 的 key / 跨月 key / 桶内其他对象 → 400 且不触发拉取
    for key in ("salary/uploads/2026-06/sales-not-a-uuid.xlsx",
                "salary/uploads/2026-07/sales-" + "0" * 32 + ".xlsx",
                "salary/v3/2026-06.xlsx"):
        r = client.post("/months/2026-06/import-oss", headers=h,
                        json={"kind": "sales", "key": key})
        assert r.status_code == 400, key
    assert fetched == []


def test_import_oss_sales_full_path(client, monkeypatch, tmp_path, db_session):
    """import-oss 成功路径：内网拉回 → 行为与旧 multipart 导入等同"""
    h = auth_header(client)
    client.post("/months", headers=h, json={"month": "2026-06"})
    s = tmp_path / "via_oss.xlsx"; _sales_xlsx(s)

    from backend.app.services import oss_upload
    def _fake_fetch(key, path):
        import shutil; shutil.copy(s, path)
    monkeypatch.setattr(oss_upload, "fetch_to_file", _fake_fetch)

    key = oss_upload.upload_key("2026-06", "sales")
    r = client.post("/months/2026-06/import-oss", headers=h,
                    json={"kind": "sales", "key": key})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1 and body["db_count"] == 1
    m = client.get("/months/2026-06", headers=h).json()
    assert m["sales_file"] and m["sales_file"].endswith(".xlsx")
    from backend.app.db import SalesRecord
    assert db_session.query(SalesRecord).filter_by(month="2026-06").count() == 1
    # results_stale 语义不在此重复断言：新端点与旧 multipart 走同一 _apply_sales_import，
    # stale 已由 test_input_changes_mark_month_stale 覆盖。
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest backend/tests/test_workflow.py -k "upload_ticket or import_oss" -v`
Expected: FAIL（404 Not Found，端点不存在）

- [ ] **Step 3: 实现端点**

`workflow.py` 改造：

(a) 旧两个端点体抽成模块级 helper（放在 `_get_month` 之后）：

```python
def _apply_sales_import(db, m: Month, path: str) -> dict:
    """销售文件已落盘后的公共导入逻辑（multipart 与 OBS 中转共用，ADR-024）。"""
    m.sales_file = path
    m.results_stale = True
    db.commit()

    from backend.app.services.sales_importer import import_sales_to_db
    from salary_engine.importer import load_sales_xlsx, load_gift_keys_xlsx

    sales = load_sales_xlsx(path)
    gift_keys = set()
    if m.gifts_file:
        try:
            gift_keys = load_gift_keys_xlsx(m.gifts_file)
        except Exception as e:
            print(f"Warning: Failed to load gift keys: {e}")

    return import_sales_to_db(db, m.month, sales, gift_keys)


def _apply_gifts_import(db, m: Month, path: str) -> None:
    """让利文件已落盘后的公共导入逻辑（multipart 与 OBS 中转共用，ADR-024）。"""
    m.gifts_file = path
    m.results_stale = True
    db.commit()

    if m.sales_file:
        from backend.app.services.sales_importer import import_sales_to_db
        from salary_engine.importer import load_sales_xlsx, load_gift_keys_xlsx

        sales = load_sales_xlsx(m.sales_file)
        gift_keys = set()
        try:
            gift_keys = load_gift_keys_xlsx(path)
        except Exception as e:
            print(f"Warning: Failed to load gift keys: {e}")

        import_sales_to_db(db, m.month, sales, gift_keys)
```

(b) 旧端点改为调 helper（`_save_upload` 保留）：

```python
@router.post("/months/{month}/import-sales")
def import_sales(month: str, file: UploadFile = File(...),
                 _: User = Depends(current_user), db: Session = Depends(get_db)):
    m = _get_month(db, month)
    result = _apply_sales_import(db, m, _save_upload(month, file, "sales"))
    return {"sales_file": m.sales_file, **result}


@router.post("/months/{month}/import-gifts")
def import_gifts(month: str, file: UploadFile = File(...),
                 _: User = Depends(current_user), db: Session = Depends(get_db)):
    m = _get_month(db, month)
    _apply_gifts_import(db, m, _save_upload(month, file, "gifts"))
    return {"gifts_file": m.gifts_file}
```

(c) 追加新端点（schema 类放文件里其他 BaseModel 附近）：

```python
class UploadTicketReq(BaseModel):
    kind: str  # "sales" | "gifts"


class ImportOssReq(BaseModel):
    kind: str
    key: str


@router.post("/months/{month}/upload-ticket")
def upload_ticket(month: str, body: UploadTicketReq,
                  _: User = Depends(current_user), db: Session = Depends(get_db)):
    """签发 OBS 预签名直传 ticket（ADR-024）。未配 OBS → 503，前端回退 multipart。"""
    _get_month(db, month)
    if body.kind not in ("sales", "gifts"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "kind 必须是 sales 或 gifts")
    from backend.app.services import oss_upload
    if not oss_upload.is_configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "对象存储未配置，请直传")
    key = oss_upload.upload_key(month, body.kind)
    return {"url": oss_upload.presign_put(key), "key": key}


@router.post("/months/{month}/import-oss")
def import_oss(month: str, body: ImportOssReq,
               _: User = Depends(current_user), db: Session = Depends(get_db)):
    """OBS 中转导入：内网拉回文件 → 复用与 multipart 相同的导入逻辑（ADR-024）。"""
    m = _get_month(db, month)
    if body.kind not in ("sales", "gifts"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "kind 必须是 sales 或 gifts")
    from backend.app.services import oss_upload
    # key 必须是本服务签发的当月当类型 key——防借端点拉桶内任意对象
    if not oss_upload.key_matches(body.key, month, body.kind):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "非法的上传 key")
    d = UPLOAD_DIR / month
    d.mkdir(parents=True, exist_ok=True)
    path = str(d / f"{body.kind}.xlsx")
    try:
        oss_upload.fetch_to_file(body.key, path)
    except Exception as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"从对象存储拉取失败，请重新上传: {e}")
    if body.kind == "sales":
        result = _apply_sales_import(db, m, path)
        return {"sales_file": m.sales_file, **result}
    _apply_gifts_import(db, m, path)
    return {"gifts_file": m.gifts_file}
```

注意：新端点用 `from backend.app.services import oss_upload` 后以 `oss_upload.xxx` 调用——测试 monkeypatch 的是 `oss_upload` 模块属性，这样 patch 才生效。

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `uv run pytest backend/tests/test_workflow.py -k "upload_ticket or import_oss" -v && uv run pytest backend/tests/ -q`
Expected: 新测试 PASS；全量无回归（旧 import 端点行为不变）

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/workflow.py backend/tests/test_workflow.py
git commit -m "feat(workflow): upload-ticket/import-oss 端点，multipart 抽公共 helper（ADR-024 C）"
```

---

### Task 4: 前端——OSS 直传 + 进度条 + 分阶段报错

**Files:**
- Modify: `frontend/src/api.ts:118-126`（multipart 保留）、`:252-258`（workflowApiExtended 追加）
- Modify: `frontend/src/pages/steps/ImportStep.tsx`（upload 流程重写）

**Interfaces:**
- Consumes: Task 3 端点；旧 `workflowApi.importSales/importGifts`（回退用，不删）
- Produces:
  - `workflowApiExtended.getUploadTicket(month, kind): Promise<{url: string, key: string}>`
  - `workflowApiExtended.importFromOss(month, kind, key): Promise<any>`
  - `putFileToOss(url, file, onProgress?): Promise<void>`（模块级导出，XHR）

- [ ] **Step 1: api.ts 追加封装**

`workflowApiExtended`（`api.ts:252`）追加：

```ts
  // —— OBS 中转上传（ADR-024）——
  getUploadTicket: (month: string, kind: "sales" | "gifts") =>
    http.post<{ url: string; key: string }>(`/months/${month}/upload-ticket`, { kind }).then(r => r.data),
  importFromOss: (month: string, kind: "sales" | "gifts", key: string) =>
    http.post(`/months/${month}/import-oss`, { kind, key }).then(r => r.data),
};

/** XHR PUT 直传 OBS（fetch 无上传进度故用 XHR）。 */
export function putFileToOss(url: string, file: File, onProgress?: (pct: number) => void): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(Math.round((e.loaded / e.total) * 100));
    };
    xhr.onload = () => (xhr.status >= 200 && xhr.status < 300 ? resolve() : reject(new Error(`PUT ${xhr.status}`)));
    xhr.onerror = () => reject(new Error("网络错误"));
    xhr.send(file);
  });
}
```

- [ ] **Step 2: ImportStep.tsx 重写 upload 流程**

(a) import 行改：`import { workflowApi, workflowApiExtended, putFileToOss, salaryPolicyApi, monthStepApi, monthsApi } from "../../api";`

(b) `uploading` state 扩展：

```tsx
const [uploading, setUploading] = useState<{ kind: string; progress: number } | null>(null);
```

(c) `upload` 函数（替换 `ImportStep.tsx:83-103` 整个函数）：

```tsx
  const legacyUpload = (kind: "sales" | "gifts", file: File) => {
    setUploading({ kind, progress: 0 });
    return (kind === "sales" ? workflowApi.importSales : workflowApi.importGifts)(month, file)
      .then(() => markDone(kind))
      .catch(() => toast.error("上传失败，请检查网络后重试"));
  };

  const markDone = (kind: "sales" | "gifts") => {
    kind === "sales" ? setSales(true) : setGifts(true);
    toast.success(`${kind === "sales" ? "销售流水" : "让利明细"}上传成功`);
    const newSales = kind === "sales" ? true : sales;
    const newGifts = kind === "gifts" ? true : gifts;
    if (newSales && newGifts) {
      monthStepApi.update(month, "import", { import: true }).catch(() => {});
    }
  };

  const upload = async (kind: "sales" | "gifts", file: File) => {
    setUploading({ kind, progress: 0 });
    let ticket: { url: string; key: string };
    try {
      ticket = await workflowApiExtended.getUploadTicket(month, kind);
    } catch {
      // OBS 通道不可用（未配置/网络）→ 回退旧 multipart 直传
      return legacyUpload(kind, file);
    }
    try {
      await putFileToOss(ticket.url, file, (p) => setUploading({ kind, progress: p }));
      await workflowApiExtended.importFromOss(month, kind, ticket.key);
      markDone(kind);
    } catch (e: any) {
      const detail = e?.response?.data?.detail;
      toast.error(detail ? String(detail) : "上传失败，请重新选择文件重试");
    } finally {
      setUploading(null);
    }
  };
```

注意：`legacyUpload` 的 `finally` 需补 `setUploading(null)`——把 catch 后加 `.finally(() => setUploading(null))`。

(d) DropZone 的 `loading` 判断与文案改进度显示。DropZone props `loading?: boolean` 改 `progress?: number | null`，label 处：

```tsx
{progress != null ? `上传中 ${progress}%` : label}
```

调用处 `loading={uploading === "sales"}` 改 `progress={uploading?.kind === "sales" ? uploading.progress : null}`（gifts 同理）。

- [ ] **Step 3: 类型检查 + 构建**

```bash
cd frontend && npx tsc --noEmit && npm run build
```
Expected: 均无错误

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api.ts frontend/src/pages/steps/ImportStep.tsx
git commit -m "feat(import): OBS 直传+进度条+分阶段报错，OBS 不可用回退 multipart（ADR-024 C）"
```

---

### Task 5: 桶 CORS 合并脚本 + 收尾验证

**Files:**
- Create: `scripts/update_oss_cors.py`
- Verify: 全量测试 + 文档状态

**Interfaces:**
- Consumes: `oss_export._env`；服务器 `.env` 的 OSS_* 变量
- Produces: 幂等运维脚本——在桶现有 CORS 规则上**合并追加** PUT 规则（不覆盖 ADR-022 的 GET 规则）

- [ ] **Step 1: 写脚本**

```python
# scripts/update_oss_cors.py
"""桶 CORS 合并追加 PUT 规则（导入直传，ADR-024）。幂等，不动现有规则。

用法（部署机 /opt/salary 下，env 已含 OSS_*）：
    docker exec openship-salary-calculation-backend python scripts/update_oss_cors.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import boto3
from botocore.config import Config

from backend.app.services.oss_export import _env

bucket = _env("OSS_BUCKET")
client = boto3.client(
    "s3",
    endpoint_url=_env("OSS_ENDPOINT_PUBLIC", "https://xinan1.zos.ctyun.cn"),
    aws_access_key_id=_env("OSS_ACCESS_KEY"),
    aws_secret_access_key=_env("OSS_SECRET_KEY"),
    region_name=_env("OSS_REGION", "xinan1"),
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)

try:
    rules = client.get_bucket_cors(Bucket=bucket)["CORSRules"]
except client.exceptions.NoSuchCORSConfiguration:
    rules = []

if any("PUT" in r.get("AllowedMethods", []) for r in rules):
    print("PUT 规则已存在，跳过")
else:
    rules.append({
        "AllowedMethods": ["PUT"],
        "AllowedOrigins": ["*"],
        "AllowedHeaders": ["*"],
        "ExposeHeaders": ["ETag"],
        "MaxAgeSeconds": 3000,
    })
    client.put_bucket_cors(Bucket=bucket, CORSConfiguration={"CORSRules": rules})
    print(f"已向桶 {bucket} 追加 PUT CORS 规则（现有 {len(rules) - 1} 条保留）")
```

- [ ] **Step 2: 本地全量回归 + 前端构建**

```bash
uv run pytest backend/tests/ tests/ -q && cd frontend && npx tsc --noEmit && npm run build
```
Expected: 后端全 PASS；前端零错误

- [ ] **Step 3: 更新进度账本与 ADR 状态**

- `docs/ARCHITECTURE.md` ADR-024 标题已 ✅（无需改）
- `.superpowers/sdd/progress.md` 追加本轮记录（T1-T5 + 部署注意事项：**部署后需在服务器跑一次 `scripts/update_oss_cors.py`**，否则浏览器直传 PUT 会被桶 CORS 拒）

- [ ] **Step 4: Commit**

```bash
git add scripts/update_oss_cors.py .superpowers/sdd/progress.md
git commit -m "chore(oss): 桶CORS合并脚本（追加PUT，幂等）+ 收尾（ADR-024）"
```

---

## 部署与验证（实现完成后、push 后执行）

1. push main → CI（Test & Deploy）→ openship 部署
2. 服务器跑 `docker exec openship-salary-calculation-backend python scripts/update_oss_cors.py`（一次性）
3. 生产 UI 手测：导入 7 月销售 28MB——应显示进度条、总耗时 ~15-20s；导入手测后查 `docker logs` 确认走的 `import-oss`
4. 观察桶 `salary/uploads/` 前缀下无残留对象（fetch_to_file 用完即删）
