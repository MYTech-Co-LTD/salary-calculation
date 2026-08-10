import openpyxl


def auth_header(client):
    t = client.post("/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    return {"Authorization": f"Bearer {t}"}


def _sales_xlsx(path):
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["序号", "机构名称", "小票单号", "销售时间", "上传时间", "销售方式", "商品编码",
               "收银员名称", "国际条码", "数量", "销售金额", "销售单价", "商品名称",
               "订单渠道", "源单号"])
    ws.append(["1", "福景店", "R001", "2026-06-01 10:00", "", "销售", "", "高睿",
               "6920001", "1", "3", "3", "低温奶", "线下", ""])
    wb.save(path)


def _policy_content():
    """构造工资策略内容（commission_rates 存百分数，ADR-009）。"""
    from salary_engine.rates import _RATES, _TIERS
    cr = {}
    for cls, by_bucket in _RATES.items():
        for bucket, vals in by_bucket.items():
            for tier, val in zip(_TIERS, vals):
                cr.setdefault(cls, {}).setdefault(bucket, {})[tier] = str(val)
    return {"margin_rules": {}, "commission_rates": cr}


def _setup_computed_month(tmp_path, client, h):
    """建一个已计算完成的月份（建月→激活策略→门店/商品/目标→导入销售→排班→compute），
    返回 month 字符串。供多个 /compute 相关测试复用，消除三处脚手架重复。"""
    client.post("/months", headers=h, json={"month": "2026-06"})
    # 创建并激活工资策略（compute 读 SalaryPolicyVersion，策略存百分数；ADR-009）
    client.post("/salary-policies", headers=h, json={
        "effective_from": "2026-01-01", "content": _policy_content()})
    client.put("/stores/福景店", headers=h, json={"name": "福景店", "group": "1组", "store_class": "A"})
    client.put("/products/6920001", headers=h, json={
        "barcode": "6920001", "name": "低温奶", "spec": "200ml", "category": "低温奶", "cost": "2"})
    client.put("/months/2026-06/targets", headers=h, json={"items": [{"store": "福景店", "target": "3"}]})
    s = tmp_path / "sales.xlsx"; _sales_xlsx(s)
    with open(s, "rb") as f:
        client.post("/months/2026-06/import-sales", headers=h, files={"file": ("sales.xlsx", f)})
    client.post("/months/2026-06/infer-duty", headers=h)
    client.put("/months/2026-06/duty", headers=h, json={
        "items": [{"store": "福景店", "date": "2026-06-01", "salesperson": "高睿"}]})
    client.post("/months/2026-06/compute", headers=h)
    return "2026-06"


def test_import_sales_and_gifts(tmp_path, client):
    h = auth_header(client)
    client.post("/months", headers=h, json={"month": "2026-06"})
    s = tmp_path / "sales.xlsx"; _sales_xlsx(s)
    with open(s, "rb") as f:
        r = client.post("/months/2026-06/import-sales", headers=h,
                        files={"file": ("sales.xlsx", f)})
    assert r.status_code == 200
    m = client.get("/months/2026-06", headers=h).json()
    assert m["sales_file"] and m["sales_file"].endswith(".xlsx")


def test_infer_and_confirm_duty(tmp_path, client):
    h = auth_header(client)
    client.post("/months", headers=h, json={"month": "2026-06"})
    client.put("/stores/福景店", headers=h, json={"name": "福景店", "group": "1组", "store_class": "A"})
    s = tmp_path / "sales.xlsx"; _sales_xlsx(s)
    with open(s, "rb") as f:
        client.post("/months/2026-06/import-sales", headers=h, files={"file": ("sales.xlsx", f)})
    grid = client.post("/months/2026-06/infer-duty", headers=h).json()
    assert "福景店" in grid and "2026-06-01" in grid["福景店"]
    # 确认
    r = client.put("/months/2026-06/duty", headers=h, json={
        "items": [{"store": "福景店", "date": "2026-06-01", "salesperson": "高睿"}]})
    assert r.status_code == 200
    got = client.get("/months/2026-06/duty", headers=h).json()
    assert got["福景店"]["2026-06-01"] == "高睿"


def test_compute_and_result(tmp_path, client):
    h = auth_header(client)
    _setup_computed_month(tmp_path, client, h)
    res = client.get("/months/2026-06/results", headers=h).json()
    # 目标3/天0.1，卖3→达成3000%→GE_100；A低温高毛(单价3成本2=33%>15%)13%→0.39
    assert any(x["person"] == "高睿" and abs(x["commission"] - 0.39) < 0.01 for x in res["salary"])


def test_excluded_store_not_in_results_but_in_ledger(tmp_path, client, db_session):
    """不计考核的店：不进 results 汇总，但台账 DetailRow 含它（标不计考核、0 提成）。ADR-017"""
    from backend.app.db import Store, DetailRow
    h = auth_header(client)
    _setup_computed_month(tmp_path, client, h)  # 福景店 computed（高睿@福景店）
    st = db_session.get(Store, "福景店")
    st.exclude_assessment = True
    db_session.commit()
    client.post("/months/2026-06/compute", headers=h)
    res = client.get("/months/2026-06/results", headers=h).json()
    stores = {x["store"] for x in res["breakdown"]}
    assert "福景店" not in stores, "不计考核的店不进 results 汇总"
    # 台账（DetailRow）含福景店，标"不计考核"，commission=0
    rows = db_session.query(DetailRow).filter_by(month="2026-06", store="福景店").all()
    assert rows and all(r.tag == "不计考核" and r.commission == 0 for r in rows), \
        "不计考核店的行应在台账（DetailRow），标不计考核且 0 提成"


def test_results_and_export(tmp_path, client):
    h = auth_header(client)
    _setup_computed_month(tmp_path, client, h)  # 复用：已算好 2026-06
    res = client.get("/months/2026-06/results", headers=h).json()
    assert res["salary"] and res["breakdown"]
    exp = client.get("/months/2026-06/export", headers=h)
    assert exp.status_code == 200
    ct = exp.headers.get("content-type", "")
    assert "spreadsheet" in ct or ct.startswith("application/vnd")


def test_results_stale_flag(tmp_path, client, db_session):
    """T4.3：/results 必须返回 stale 标志，让前端判断是否提示"数据已变更，请重新计算"。
    - 已计算 & 未变更 → stale=False
    - results_stale=True → stale=True（模拟 T5.1 的输入变更触发）
    - 未计算的草稿月份（status=draft, results_stale=True）→ stale=True
    """
    from backend.app.db import Month

    h = auth_header(client)
    _setup_computed_month(tmp_path, client, h)  # 建 2026-06 已计算月份

    # 已计算 & 未变更 → stale=False
    r = client.get("/months/2026-06/results", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["stale"] is False

    # 模拟 T5.1 的输入变更触发：直接翻 results_stale（写侧逻辑在 T5.1 落地）
    db_session.get(Month, "2026-06").results_stale = True
    db_session.commit()
    r2 = client.get("/months/2026-06/results", headers=h)
    assert r2.status_code == 200, r2.text
    assert r2.json()["stale"] is True

    # 未计算的草稿月份（默认 status=draft、results_stale=True）→ stale=True
    client.post("/months", headers=h, json={"month": "2026-07"})
    r3 = client.get("/months/2026-07/results", headers=h)
    assert r3.status_code == 200, r3.text
    assert r3.json()["stale"] is True


def test_compute_materializes_result_and_detail(tmp_path, client, db_session):
    """T4.1：/compute 必须同事务物化 Result（聚合）+ DetailRow（逐行），Σ 提成相等，
    status=computed、results_stale=False、policy_version_id 仅首次锁定。"""
    from decimal import Decimal
    from backend.app.db import Result, DetailRow, Month

    h = auth_header(client)
    _setup_computed_month(tmp_path, client, h)

    # Result 与 DetailRow 均落库
    assert db_session.query(Result).filter_by(month="2026-06").count() > 0
    assert db_session.query(DetailRow).filter_by(month="2026-06").count() > 0

    # Σ 不变量：逐行台账提成 == 聚合表提成
    dsum = sum((d.commission for d in db_session.query(DetailRow).filter_by(month="2026-06").all()),
               Decimal(0))
    rsum = sum((x.commission for x in db_session.query(Result).filter_by(month="2026-06").all()),
               Decimal(0))
    assert dsum == rsum, f"Σ mismatch: detail={dsum} result={rsum}"

    # 状态与首次锁定策略
    m = db_session.get(Month, "2026-06")
    assert m.status == "computed"
    assert m.results_stale is False
    assert m.policy_version_id is not None


def test_tier_summary_does_not_recompute(tmp_path, client, monkeypatch):
    """T4.2：tier_summary 必须读物化 DetailRow，零 _run_compute 调用（治 R1/R2 + C1）。"""
    from unittest.mock import MagicMock
    import backend.app.routers.workflow as workflow

    h = auth_header(client)
    _setup_computed_month(tmp_path, client, h)

    spy = MagicMock()
    monkeypatch.setattr(workflow, "_run_compute", spy)

    r = client.get("/months/2026-06/tier-summary", headers=h,
                   params={"store": "福景店", "person": "高睿"})
    assert r.status_code == 200, r.text
    assert spy.call_count == 0, "tier_summary 不应触发全量重算（应读物化 DetailRow）"
    data = r.json()
    assert isinstance(data.get("tiers"), list)
    # 与 compute 一致：A 类 GE_100 低温高毛 13% × 3 = 0.39
    assert abs(data["total_commission"] - 0.39) < 0.01, data
    # rate 已是 SalaryPolicyVersion 口径（13% 物化），不应再读 legacy RateVersion
    assert any(t["name"] == "低温高毛" and abs(t["rate"] - 0.13) < 0.001 for t in data["tiers"])


def test_tier_detail_does_not_recompute(tmp_path, client, monkeypatch):
    """T4.2：tier_detail 必须读物化 DetailRow，零 _run_compute 调用。"""
    from unittest.mock import MagicMock
    import backend.app.routers.workflow as workflow

    h = auth_header(client)
    _setup_computed_month(tmp_path, client, h)

    spy = MagicMock()
    monkeypatch.setattr(workflow, "_run_compute", spy)

    r = client.get("/months/2026-06/tier-detail", headers=h,
                   params={"store": "福景店", "person": "高睿", "bucket": "低温高毛"})
    assert r.status_code == 200, r.text
    assert spy.call_count == 0, "tier_detail 不应触发全量重算（应读物化 DetailRow）"
    items = r.json()["items"]
    assert items, "应当返回低温高毛档位的明细行"
    # 前端契约：tier_detail 必须返回完整 SalesItem（与 sales_detail 一致），而非 SLIM 子集
    it = items[0]
    expected_keys = {"id", "receipt", "src_order", "store", "sale_date", "barcode",
                     "product_name", "qty", "amount", "unit_price", "salesperson",
                     "cashier", "is_return", "is_online", "tag", "original_store",
                     "original_date", "transfer_reason"}
    assert expected_keys.issubset(it.keys()), f"缺字段: {expected_keys - set(it.keys())}"
    # 关键字段不再是 SLIM 形态独有：receipt/qty/unit_price 来自 JOIN 的 SalesRecord
    assert it["receipt"] == "R001"
    assert it["qty"] == 1.0
    assert it["unit_price"] == 3.0
    assert it["barcode"] == "6920001"


def test_input_changes_mark_month_stale(client, db_session, tmp_path):
    """T5.1：每个输入变更端点必须把 Month.results_stale 置 True（写侧），
    让 /results 的 stale 标志提示前端"数据已变更，请重新计算"。
    覆盖：set_duty / transfer_duty / set_targets / import_sales / import_gifts。
    salary_policies.activate 不在内 —— 已计算月份策略锁定，新策略不改其冻结结果（T4.1/H10）。"""
    from backend.app.db import Month

    h = auth_header(client)
    _setup_computed_month(tmp_path, client, h)  # 建 2026-06 已计算月份

    def _reset_stale():
        db_session.get(Month, "2026-06").results_stale = False
        db_session.commit()

    def _assert_stale(label):
        db_session.expire_all()
        m = db_session.get(Month, "2026-06")
        assert m.results_stale is True, f"{label} 后 results_stale 应为 True"

    # set_duty (PUT /months/{m}/duty)
    _reset_stale()
    r = client.put("/months/2026-06/duty", headers=h, json={
        "items": [{"store": "福景店", "date": "2026-06-01", "salesperson": "高睿"}]})
    assert r.status_code == 200, r.text
    _assert_stale("set_duty")

    # transfer_duty (POST /months/{m}/duty/transfer)
    _reset_stale()
    r = client.post("/months/2026-06/duty/transfer", headers=h, json={
        "from_store": "福景店", "to_store": "新店",
        "date": "2026-06-01", "salesperson": "高睿"})
    assert r.status_code == 200, r.text
    _assert_stale("transfer_duty")

    # set_targets (PUT /months/{m}/targets)
    _reset_stale()
    r = client.put("/months/2026-06/targets", headers=h, json={
        "items": [{"store": "福景店", "target": "5"}]})
    assert r.status_code == 200, r.text
    _assert_stale("set_targets")

    # import_sales (POST /months/{m}/import-sales)
    _reset_stale()
    s = tmp_path / "sales2.xlsx"; _sales_xlsx(s)
    with open(s, "rb") as f:
        r = client.post("/months/2026-06/import-sales", headers=h,
                        files={"file": ("sales.xlsx", f)})
    assert r.status_code == 200, r.text
    _assert_stale("import_sales")

    # import_gifts (POST /months/{m}/import-gifts)
    _reset_stale()
    g = tmp_path / "gifts.xlsx"
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["订单号", "国际条码"])
    ws.append(["R001", "6920001"])
    wb.save(g)
    with open(g, "rb") as f:
        r = client.post("/months/2026-06/import-gifts", headers=h,
                        files={"file": ("gifts.xlsx", f)})
    assert r.status_code == 200, r.text
    _assert_stale("import_gifts")


def test_compute_single_flight(tmp_path, client, db_session):
    """T5.2（治 R3）：do_compute 必须按月加锁；同一月份已有计算进行中时，
    第二次 /compute 应立即返回 409，而不是叠加 CPU-bound 工作。
    模拟方式：测试线程直接持有该月的锁，再调 /compute 应得 409。"""
    import backend.app.routers.workflow as wf

    h = auth_header(client)
    _setup_computed_month(tmp_path, client, h)   # 确保月份存在且已计算

    # 模拟另一个正在进行的 compute：测试线程持有该月的锁
    lock = wf._get_lock("2026-06")
    # setdefault 保证同月份两次取锁是同一实例（消除 check-then-act 竞态）
    assert wf._get_lock("2026-06") is lock
    got = lock.acquire(blocking=False)
    assert got
    try:
        r = client.post("/months/2026-06/compute", headers=h)
        assert r.status_code == 409, f"并发计算应返回 409，实际 {r.status_code}: {r.text}"
    finally:
        lock.release()


def test_compute_restores_status_on_failure(tmp_path, client, db_session, monkeypatch):
    """T5.2：计算失败时必须把 status 从 "computing" 恢复到先前值，避免月份卡死。"""
    import pytest
    from backend.app.db import Month
    import backend.app.routers.workflow as wf

    h = auth_header(client)
    _setup_computed_month(tmp_path, client, h)

    # 先记下先前状态（应为 computed）
    db_session.expire_all()
    prev = db_session.get(Month, "2026-06").status
    assert prev == "computed"

    # 让 _run_compute 抛异常，模拟计算失败
    def _boom(db, month):
        raise RuntimeError("engine exploded")
    monkeypatch.setattr(wf, "_run_compute", _boom)

    # Starlette TestClient 默认会把服务端异常原样抛出
    with pytest.raises(RuntimeError):
        client.post("/months/2026-06/compute", headers=h)

    # 状态应恢复成 computed，而非卡在 computing
    db_session.expire_all()
    m = db_session.get(Month, "2026-06")
    assert m.status == prev, f"失败后 status 应恢复为 {prev}，实际 {m.status}"
    assert m.status != "computing", "失败后不应卡在 computing"


def test_export_reads_materialized_no_recompute(tmp_path, client, monkeypatch):
    """T7.1：/export 必须读物化 DetailRow JOIN SalesRecord，零 _run_compute 调用（治 R1）。
    每条导入记录 = 台账一行；去向标签 + 提成金额从物化表直接读取。"""
    from unittest.mock import MagicMock
    import backend.app.routers.workflow as workflow

    h = auth_header(client)
    _setup_computed_month(tmp_path, client, h)

    spy = MagicMock()
    monkeypatch.setattr(workflow, "_run_compute", spy)

    r = client.get("/months/2026-06/export", headers=h)
    assert r.status_code == 200, r.text
    assert spy.call_count == 0, "export 不应触发全量重算（应读物化 DetailRow JOIN SalesRecord）"
    ct = r.headers.get("content-type", "")
    assert "spreadsheet" in ct or ct.startswith("application/vnd"), f"bad ct: {ct}"
    assert len(r.content) > 0, "导出体不应为空"


def test_export_ledger_has_tags_and_fields(tmp_path, client):
    """T7.1：导出台账每条记录一行，含去向标签 + 提成金额 + 全字段（含源 extra）。
    解析 xlsx，校验表头与至少一行数据带已知 fate 标签。"""
    import io
    import openpyxl

    h = auth_header(client)
    _setup_computed_month(tmp_path, client, h)

    r = client.get("/months/2026-06/export", headers=h)
    assert r.status_code == 200, r.text

    wb = openpyxl.load_workbook(io.BytesIO(r.content), read_only=True)
    assert "提成台账-2026-06" in wb.sheetnames, f"sheet names: {wb.sheetnames}"
    ws = wb["提成台账-2026-06"]
    rows = list(ws.iter_rows(values_only=True))
    assert len(rows) >= 2, f"台账至少表头 + 1 行数据，实际 {len(rows)}"
    headers = list(rows[0])
    header_set = set(headers)
    # 关键列必须存在
    for required in ("去向标签", "提成金额", "商品档位", "达成档", "提成比例",
                     "是否调班", "源字段"):
        assert required in header_set, f"缺少列 {required}: {headers}"

    tag_idx = headers.index("去向标签")
    comm_idx = headers.index("提成金额")
    data_rows = rows[1:]
    tags = {row[tag_idx] for row in data_rows if row[tag_idx]}
    known_tags = {"有效计提", "退货冲抵", "退货未匹配", "赠送剔除", "赠送扣除", "不计提成", "非乳品"}
    assert tags & known_tags, f"无已知 fate 标签: {tags}"
    # 至少一行有非空提成金额
    assert any(row[comm_idx] not in (None, 0, 0.0) for row in data_rows), "无提成金额"


def test_export_ledger_shows_transfer(tmp_path, client, db_session):
    """T7.1：是否调班 列由 SalesRecord.original_store 派生（不依赖 DetailRow.is_transferred 占位）。
    直接把一条 SalesRecord 的 original_store/original_date 改非空（等价于 transfer_sales 的落库效果），
    再导出，台账应至少一行显示 是否调班=="是" 且 原门店 非空。"""
    import io
    from datetime import date as _date
    import openpyxl
    from backend.app.db import SalesRecord

    h = auth_header(client)
    _setup_computed_month(tmp_path, client, h)

    # 取一条已物化的 SalesRecord，直接打上调班标记（模拟 transfer_sales 的写库效果）
    rec = db_session.query(SalesRecord).filter_by(month="2026-06").first()
    assert rec is not None, "应有已导入的销售记录"
    rec.original_store = "老门店"
    rec.original_date = _date(2026, 5, 31)
    db_session.commit()

    r = client.get("/months/2026-06/export", headers=h)
    assert r.status_code == 200, r.text

    wb = openpyxl.load_workbook(io.BytesIO(r.content), read_only=True)
    ws = wb["提成台账-2026-06"]
    rows = list(ws.iter_rows(values_only=True))
    headers = list(rows[0])
    transfer_idx = headers.index("是否调班")
    orig_store_idx = headers.index("原门店")

    # 至少一行：是否调班=="是" 且 原门店=="老门店"
    matched = [row for row in rows[1:]
               if row[transfer_idx] == "是" and row[orig_store_idx] == "老门店"]
    assert matched, "调班行未在台账正确渲染（是否调班=是 + 原门店 populated）"


def test_compute_recompute_preserves_policy_lock(tmp_path, client, db_session):
    """H10：首次 /compute 锁定 policy_version_id；策略变更后再次 /compute 不应覆盖锁定。
    若 do_compute 里 `if m.policy_version_id is None` 守卫被删除，本测试必须失败。"""
    from backend.app.db import Month

    h = auth_header(client)
    _setup_computed_month(tmp_path, client, h)

    # 首次 compute 后，月份已锁定到 v1
    db_session.expire_all()
    m = db_session.get(Month, "2026-06")
    v1_id = m.policy_version_id
    assert v1_id is not None, "首次 compute 应锁定 policy_version_id"

    # 新建第二个工资策略（/salary-policies 会把 is_current 切到新版本，即 v2）
    r2 = client.post("/salary-policies", headers=h, json={
        "effective_from": "2026-07-01", "content": _policy_content()})
    assert r2.status_code == 201
    v2_id = r2.json()["id"]
    assert v2_id != v1_id

    # 再次 /compute：守卫 `if m.policy_version_id is None` 应使其跳过覆盖，仍保持 v1
    r = client.post("/months/2026-06/compute", headers=h)
    assert r.status_code == 200

    db_session.expire_all()
    m = db_session.get(Month, "2026-06")
    assert m.policy_version_id == v1_id, (
        f"重算不应覆盖已锁定的 policy_version_id: expected v1={v1_id}, got {m.policy_version_id}")
    assert m.policy_version_id != v2_id


def test_export_ledger_commission_matches_salary_total(tmp_path, client):
    """H5 / ADR-008 对账闭环（端到端）：导出台账里计提类标签的 commission 之和
    == /results 工资总额之和。守护"Σ 台账 == 工资总额"这一核心承诺不被
    export JOIN 漏行 / DetailRow 物化过滤 / tag 误标悄悄破坏。
    计提类标签：有效计提 / 退货冲抵 / 退货未匹配（赠送剔除/不计提成/非乳品 = 0）。"""
    import io
    import openpyxl

    h = auth_header(client)
    _setup_computed_month(tmp_path, client, h)

    # /results 工资总额
    salary_total = sum(
        x["commission"] for x in
        client.get("/months/2026-06/results", headers=h).json()["salary"]
    )

    # /export 台账：计提类标签行 commission 求和
    r = client.get("/months/2026-06/export", headers=h)
    wb = openpyxl.load_workbook(io.BytesIO(r.content), read_only=True)
    rows = list(wb["提成台账-2026-06"].iter_rows(values_only=True))
    headers = list(rows[0])
    tag_idx = headers.index("去向标签")
    comm_idx = headers.index("提成金额")
    ledger_total = sum(
        (row[comm_idx] or 0) for row in rows[1:]
        if row[tag_idx] in ("有效计提", "退货冲抵", "退货未匹配")
    )

    assert abs(ledger_total - salary_total) < 0.01, (
        f"对账闭环失效: 台账计提Σ={ledger_total} != 工资总额Σ={salary_total}")


def test_export_three_sheets_and_reconciliation(tmp_path, client):
    """ADR-020：/export 三 sheet；Sheet1 Σ提成 == Sheet2 Σ提成 == /results Σ；Sheet3 考勤矩阵正确。"""
    import io
    import openpyxl

    h = auth_header(client)
    _setup_computed_month(tmp_path, client, h)

    salary_total = sum(
        x["commission"] for x in
        client.get("/months/2026-06/results", headers=h).json()["salary"])

    r = client.get("/months/2026-06/export", headers=h)
    assert r.status_code == 200
    wb = openpyxl.load_workbook(io.BytesIO(r.content), read_only=True)
    assert wb.sheetnames == ["计算结果", "提成台账-2026-06", "考勤排版"], wb.sheetnames

    ws1 = wb["计算结果"]
    rows1 = list(ws1.iter_rows(values_only=True))
    h1 = list(rows1[0]); ci = h1.index("提成金额")
    # 末尾一行是"合计"，数据行是 [1:-1]
    sum1 = sum((r[ci] or 0) for r in rows1[1:-1])

    ws2 = wb["提成台账-2026-06"]
    rows2 = list(ws2.iter_rows(values_only=True))
    h2 = list(rows2[0]); c2 = h2.index("提成金额")
    sum2 = sum((r[c2] or 0) for r in rows2[1:])

    assert abs(sum1 - salary_total) < 0.01, f"Sheet1 Σ={sum1} != 工资总额={salary_total}"
    assert abs(sum2 - salary_total) < 0.01, f"Sheet2 Σ={sum2} != 工资总额={salary_total}"

    ws3 = wb["考勤排版"]
    rows3 = list(ws3.iter_rows(values_only=True))
    assert rows3[0][0] == "门店" and rows3[0][1] == "1号"
    assert any(r[0] == "福景店" and r[1] == "高睿" for r in rows3[1:]), "考勤排版缺福景店/高睿"


def test_build_export_file_three_sheets(tmp_path, client, db_session):
    """ADR-020/021：_build_export_file 直接产出 3 sheet（同步兜底与后台预生成共用同一函数）。"""
    from backend.app.routers.workflow import _build_export_file
    h = auth_header(client)
    _setup_computed_month(tmp_path, client, h)
    out = tmp_path / "out.xlsx"
    _build_export_file(db_session, "2026-06", str(out))
    wb = openpyxl.load_workbook(out)
    assert wb.sheetnames == ["计算结果", "提成台账-2026-06", "考勤排版"], wb.sheetnames


def test_export_uses_cache_when_not_stale(tmp_path, client, monkeypatch):
    """ADR-021：非 stale 导出写缓存，二次导出命中缓存不重建（mtime 不变）。"""
    import io
    from backend.app.services import oss_export
    cache_dir = tmp_path / "export_cache"
    monkeypatch.setenv("SALARY_EXPORT_CACHE_DIR", str(cache_dir))
    h = auth_header(client)
    _setup_computed_month(tmp_path, client, h)

    r1 = client.get("/months/2026-06/export", headers=h)
    assert r1.status_code == 200
    cache_file = cache_dir / oss_export.EXPORT_VERSION / "salary_2026-06.xlsx"
    assert cache_file.exists(), "导出应写入缓存"
    mtime1 = cache_file.stat().st_mtime

    r2 = client.get("/months/2026-06/export", headers=h)
    assert r2.status_code == 200
    assert cache_file.stat().st_mtime == mtime1, "二次导出应命中缓存（不重建）"
    wb = openpyxl.load_workbook(io.BytesIO(r2.content), read_only=True)
    assert wb.sheetnames == ["计算结果", "提成台账-2026-06", "考勤排版"]


def test_export_bypasses_cache_when_stale(tmp_path, client, monkeypatch, db_session):
    """ADR-021：stale 时绕过缓存重新生成（不读旧缓存）。"""
    import io
    from backend.app.db import Month
    from backend.app.services import oss_export
    cache_dir = tmp_path / "export_cache"
    monkeypatch.setenv("SALARY_EXPORT_CACHE_DIR", str(cache_dir))
    h = auth_header(client)
    _setup_computed_month(tmp_path, client, h)
    client.get("/months/2026-06/export", headers=h)
    cache_file = cache_dir / oss_export.EXPORT_VERSION / "salary_2026-06.xlsx"
    assert cache_file.exists()

    db_session.get(Month, "2026-06").results_stale = True
    db_session.commit()

    r = client.get("/months/2026-06/export", headers=h)
    assert r.status_code == 200
    wb = openpyxl.load_workbook(io.BytesIO(r.content), read_only=True)
    assert wb.sheetnames == ["计算结果", "提成台账-2026-06", "考勤排版"]


def test_compute_kicks_off_cache_pregen(tmp_path, client, monkeypatch):
    """ADR-021：/compute 成功后触发后台预生成（spy 确认 _pregen_export_cache 被调）。"""
    import time
    import backend.app.routers.workflow as workflow
    calls = []
    monkeypatch.setattr(workflow, "_pregen_export_cache", lambda m: calls.append(m))
    h = auth_header(client)
    _setup_computed_month(tmp_path, client, h)
    for _ in range(50):
        if "2026-06" in calls:
            break
        time.sleep(0.02)
    assert "2026-06" in calls, "compute 后应触发 _pregen_export_cache"


def test_export_redirects_to_oss_when_configured(tmp_path, client, monkeypatch):
    """ADR-022：OSS 配置时 /export 返回 302 跳签名 URL，且触发上传。"""
    from backend.app.services import oss_export
    uploaded = []
    monkeypatch.setattr(oss_export, "is_configured", lambda: True)
    monkeypatch.setattr(oss_export, "ensure_upload",
                        lambda month, p: uploaded.append((month, p)) or True)
    monkeypatch.setattr(oss_export, "presign_url",
                        lambda month, **k: f"https://oss.test/salary_{month}.xlsx")
    h = auth_header(client)
    _setup_computed_month(tmp_path, client, h)

    r = client.get("/months/2026-06/export", headers=h, follow_redirects=False)
    assert r.status_code == 302, f"应 302 跳 OSS，实际 {r.status_code}"
    assert r.headers["location"] == "https://oss.test/salary_2026-06.xlsx", r.headers.get("location")
    assert uploaded, "导出应触发 OSS 上传"


def test_master_data_change_marks_computed_month_stale(tmp_path, client, db_session):
    """H1/ADR-014：改主数据（商品）后，已计算月份 results_stale=True。
    之前 stores/products/import_master 端点全不标 stale → 喂陈旧物化结果。"""
    from backend.app.db import Month

    h = auth_header(client)
    _setup_computed_month(tmp_path, client, h)  # 建 2026-06 computed（stale=False）

    db_session.expire_all()
    assert db_session.get(Month, "2026-06").results_stale is False  # 起点 stale=False

    # 改一商品（PATCH cost：2→3）
    r = client.patch("/products/6920001", headers=h, json={"cost": "3"})
    assert r.status_code == 200, r.text

    db_session.expire_all()
    assert db_session.get(Month, "2026-06").results_stale is True, (
        "改商品后 computed 月份应 results_stale=True（ADR-014）")


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


def test_confirm_gift_deduction(tmp_path, client, db_session):
    from backend.app.db import GiftDeduction, Anomaly, Month, DetailRow
    from decimal import Decimal
    h = auth_header(client)
    _setup_computed_month(tmp_path, client, h)
    # 原始组总额（首次 compute 时 R001/6920001 有效计提）：qty1 × 单价3 = 3
    orig_group_total = sum(
        (d.amount for d in db_session.query(DetailRow).filter_by(
            month="2026-06", barcode="6920001").all()),
        Decimal(0))
    g = tmp_path / "gifts.xlsx"
    _gifts_xlsx(g, [["1", "R001", "6920001", "2", "低温奶"]])
    with open(g, "rb") as f:
        client.post("/months/2026-06/import-gifts", headers=h, files={"file": ("gifts.xlsx", f)})
    client.post("/months/2026-06/check-anomalies", headers=h)
    anom = db_session.query(Anomaly).filter_by(month="2026-06", anomaly_type="7").one()
    r = client.post("/months/2026-06/gift-deduction/confirm", headers=h, json={"anomaly_id": anom.id})
    assert r.status_code == 200
    assert r.json()["deduct_qty"] == 1  # min(销售1, 赠送2)
    assert r.json()["deduct_amt"] > 0     # I3：扣除额必须为正
    gd = db_session.query(GiftDeduction).filter_by(month="2026-06").one()
    assert gd.deduct_qty == 1
    # I3：显式 expire_all 后重查 anomaly 状态（替代原弱断言）
    db_session.expire_all()
    assert db_session.get(Anomaly, anom.id).status == "resolved"
    # I3：确认后必须把月份标 stale，提示前端重算
    assert db_session.get(Month, "2026-06").results_stale is True

    # I2：端到端——确认后重算，该组应出现 tag=="赠送扣除" 的行且金额缩减
    rc = client.post("/months/2026-06/compute", headers=h)
    assert rc.status_code == 200, rc.text
    deducted = db_session.query(DetailRow).filter_by(
        month="2026-06", barcode="6920001", tag="赠送扣除").all()
    assert deducted, "重算后该组应出现 tag==赠送扣除 的明细行"
    deducted_total = sum((d.amount for d in deducted), Decimal(0))
    assert deducted_total < orig_group_total, (
        f"赠送扣除后组金额应缩减: {deducted_total} !< 原始 {orig_group_total}")
