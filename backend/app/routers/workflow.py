import os
import threading
from typing import Any, Dict
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from sqlalchemy.orm import Session

from backend.app.auth import current_user
from backend.app.config import BASE_DIR
from backend.app.db import get_db, Month, User

router = APIRouter(tags=["workflow"])
UPLOAD_DIR = BASE_DIR / "uploads"


def _save_upload(month: str, f: UploadFile, kind: str) -> str:
    suffix = os.path.splitext(f.filename or "u.xlsx")[1] or ".xlsx"
    d = UPLOAD_DIR / month
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{kind}{suffix}"
    with open(path, "wb") as out:
        out.write(f.file.read())
    return str(path)


def _get_month(db, month) -> Month:
    m = db.get(Month, month)
    if m is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "月份不存在")
    return m


@router.post("/months/{month}/import-sales")
def import_sales(month: str, file: UploadFile = File(...),
                 _: User = Depends(current_user), db: Session = Depends(get_db)):
    m = _get_month(db, month)
    m.sales_file = _save_upload(month, file, "sales")
    m.results_stale = True
    db.commit()

    # 同时导入到数据库并打标签
    from backend.app.services.sales_importer import import_sales_to_db
    from salary_engine.importer import load_sales_xlsx, load_gift_keys_xlsx

    sales = load_sales_xlsx(m.sales_file)
    gift_keys = set()
    if m.gifts_file:
        try:
            gift_keys = load_gift_keys_xlsx(m.gifts_file)
        except Exception as e:
            print(f"Warning: Failed to load gift keys: {e}")

    result = import_sales_to_db(db, month, sales, gift_keys)
    return {"sales_file": m.sales_file, **result}


@router.post("/months/{month}/import-gifts")
def import_gifts(month: str, file: UploadFile = File(...),
                 _: User = Depends(current_user), db: Session = Depends(get_db)):
    m = _get_month(db, month)
    m.gifts_file = _save_upload(month, file, "gifts")
    m.results_stale = True
    db.commit()

    # 如果已有销售数据，重新打标签
    if m.sales_file:
        from backend.app.services.sales_importer import import_sales_to_db
        from salary_engine.importer import load_sales_xlsx, load_gift_keys_xlsx

        sales = load_sales_xlsx(m.sales_file)
        gift_keys = set()
        try:
            gift_keys = load_gift_keys_xlsx(m.gifts_file)
        except Exception as e:
            print(f"Warning: Failed to load gift keys: {e}")

        import_sales_to_db(db, month, sales, gift_keys)

    return {"gifts_file": m.gifts_file}


from datetime import date as date_type
from dataclasses import replace
from pydantic import BaseModel
from salary_engine.importer import load_sales_xlsx
from salary_engine.calculator import clean_store
from salary_engine.onduty import infer_duty
from backend.app.db import Duty


class DutyItem(BaseModel):
    store: str
    date: str          # YYYY-MM-DD
    salesperson: str


class DutyBatch(BaseModel):
    items: list[DutyItem]


class GiftDeductionConfirm(BaseModel):
    anomaly_id: int


@router.post("/months/{month}/infer-duty")
def infer(month: str, _: User = Depends(current_user), db: Session = Depends(get_db)):
    m = _get_month(db, month)
    sales = sales_lines_from_db(db, month)
    if not sales:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "尚未导入销售流水")
    duty = infer_duty([s for s in sales if not s.is_return])
    grid = {}
    for (store, d), p in duty.items():
        ds = d.isoformat() if hasattr(d, "isoformat") else str(d)
        grid.setdefault(store, {})[ds] = p if isinstance(p, str) else list(p)
    return grid


@router.put("/months/{month}/duty")
def set_duty(month: str, body: DutyBatch,
             _: User = Depends(current_user), db: Session = Depends(get_db)):
    m = _get_month(db, month)
    db.query(Duty).filter_by(month=month).delete()
    for it in body.items:
        db.add(Duty(month=month, store=it.store,
                    duty_date=date_type.fromisoformat(it.date), salesperson=it.salesperson))
    m.results_stale = True
    db.commit()
    return {"saved": len(body.items)}


@router.post("/months/{month}/gift-deduction/confirm")
def confirm_gift_deduction(month: str, body: GiftDeductionConfirm,
                           _: User = Depends(current_user), db: Session = Depends(get_db)):
    """确认按件数扣除赠送（ADR-023）。从 anomaly 反查 receipt|barcode，算 min(赠送,销售)。"""
    from datetime import datetime
    from salary_engine.importer import load_gift_qty_map_xlsx
    from backend.app.db import GiftDeduction, SalesRecord, Anomaly as AnomalyRow

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
    # 一次遍历同时累加销售件数与金额（仅非退货行）——M2：合并原两次 filter_by 遍历
    sales_q = Decimal(0)
    sales_amt = Decimal(0)
    for r in db.query(SalesRecord).filter_by(month=month, receipt=receipt, barcode=barcode).all():
        if not r.is_return:
            sales_q += Decimal(str(r.qty))
            sales_amt += Decimal(str(r.amount))
    # deduct = min(|gift|, |sales|)，符号跟随 gift（销售赠品正 / 退货赠品负）
    sign = -1 if gift_q < 0 else 1
    deduct_qty = Decimal(sign) * min(abs(gift_q), abs(sales_q))
    # 预览扣除额（均摊）
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


@router.get("/months/{month}/duty")
def get_duty(month: str, _: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.query(Duty).filter_by(month=month).all()
    grid = {}
    for r in rows:
        grid.setdefault(r.store, {})[r.duty_date.isoformat()] = r.salesperson
    return grid


class DutyTransfer(BaseModel):
    from_store: str
    to_store: str
    date: str
    salesperson: str


@router.post("/months/{month}/duty/transfer")
def transfer_duty(
    month: str,
    body: DutyTransfer,
    _: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """拖拽排班：人员转移到另一门店，同时转移销售数据"""
    m = _get_month(db, month)
    from backend.app.db import Duty
    from backend.app.services.sales_importer import transfer_sales

    # 更新排班
    db.query(Duty).filter(
        Duty.month == month,
        Duty.store == body.from_store,
        Duty.duty_date == date_type.fromisoformat(body.date),
        Duty.salesperson == body.salesperson,
    ).delete()
    db.add(Duty(
        month=month,
        store=body.to_store,
        duty_date=date_type.fromisoformat(body.date),
        salesperson=body.salesperson,
    ))
    m.results_stale = True
    db.commit()

    # 同步转移销售数据
    transfer_sales(
        db, month, body.salesperson,
        body.from_store, date_type.fromisoformat(body.date),
        body.to_store, date_type.fromisoformat(body.date),
        reason=f"排班调整：从{body.from_store}转移到{body.to_store}"
    )

    return {"transferred": body.salesperson, "from": body.from_store, "to": body.to_store}

from decimal import Decimal
from collections import defaultdict
from salary_engine.importer import load_gift_keys_xlsx, load_sales_xlsx as _load_sales
from salary_engine.calculator import compute, clean_store as _clean
from salary_engine.onduty import infer_duty as _infer
from backend.app.services.engine_bridge import (
    rates_from_db, products_from_db, stores_from_db, targets_from_db,
    duty_override_from_db, days_in_month, sales_lines_from_db, gift_deduction_from_db,
)
from backend.app.db import (
    Result, DetailRow, SalaryPolicyVersion,
    Anomaly, Store, Product, MonthlyTarget,
)


@router.post("/months/{month}/check-anomalies")
def check_anomalies(
    month: str,
    _: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """预检6类异常并存入数据库"""
    m = _get_month(db, month)
    from backend.app.services.anomaly_checker import AnomalyChecker

    sales = sales_lines_from_db(db, month)
    if not sales:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "尚未导入销售流水")

    checker = AnomalyChecker(db, month)

    # 收集销售数据用于丰富异常信息
    store_sales_data: Dict[str, Any] = {}
    product_sales_data: Dict[str, Any] = {}
    for s in sales:
        store = clean_store(s.store)
        if store not in store_sales_data:
            store_sales_data[store] = {"salespersons": set(), "count": 0}
        store_sales_data[store]["salespersons"].add(s.salesperson)
        store_sales_data[store]["count"] += 1

        if s.barcode:
            if s.barcode not in product_sales_data:
                product_sales_data[s.barcode] = {"name": s.product_name, "count": 0}
            product_sales_data[s.barcode]["count"] += 1

    # 异常1: 门店不存在
    store_names = list({clean_store(s.store) for s in sales})
    checker.check_store_exists(store_names, store_sales_data)

    # 异常2: 商品不存在
    barcodes = list({s.barcode for s in sales if s.barcode})
    checker.check_product_exists(barcodes, product_sales_data)

    # 异常3: 门店无目标
    all_stores = db.query(Store).all()
    targeted = set(
        row[0]
        for row in db.query(MonthlyTarget.store).filter(MonthlyTarget.month == month).all()
    )
    checker.check_targets(
        [
            {"name": s.name, "exclude_assessment": s.exclude_assessment, "group": s.group, "store_class": s.store_class}
            for s in all_stores
        ],
        targeted,
    )

    # 异常4: 商品信息不完整
    checker.check_products_complete(barcodes)

    # 异常7: 赠送件数不符（仅当导入了让利明细）
    if m.gifts_file:
        from salary_engine.importer import load_gift_qty_map_xlsx
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

    # 清除旧异常
    db.query(Anomaly).filter(Anomaly.month == month).delete()

    # 写入新异常
    anomalies = checker.get_anomalies()
    for a in anomalies:
        from datetime import datetime
        db.add(Anomaly(
            month=a["month"],
            anomaly_type=a["anomaly_type"],
            entity_type=a["entity_type"],
            entity_id=a["entity_id"],
            description=a["description"],
            status="pending",
            created_at=datetime.utcnow(),
        ))
    db.commit()
    return {"total": len(anomalies), "anomalies": anomalies}


def _run_compute(db, month: str):
    """组装引擎入参并执行计算（Task 6 export 复用）。"""
    m = _get_month(db, month)
    if not m.sales_file:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "尚未导入销售流水")
    # 销售流水以 SalesRecord 为真值源（C2：调班/编辑后立即对计算可见）
    from backend.app.services.engine_bridge import sales_lines_from_db
    sales = sales_lines_from_db(db, month)
    if not sales:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "尚未导入销售流水")
    # 不计考核店（ADR-017）：销售不过滤（引擎内打标签 commission=0），但排除其当班（不算目标/天数）
    excluded_stores = {s.name for s in db.query(Store).filter(Store.exclude_assessment == True).all()}
    duty_override = {k: v for k, v in duty_override_from_db(db, month).items()
                     if k[0] not in excluded_stores}
    gifts = load_gift_keys_xlsx(m.gifts_file) if m.gifts_file else set()
    gift_deduction = gift_deduction_from_db(db, month)
    # 使用锁定的工资策略版本，若无则用当前激活版本（ADR-009：策略存百分数，边界 ÷100）
    try:
        rate_table = rates_from_db(db, m.policy_version_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    result = compute(
        sales_lines=sales,
        products=products_from_db(db),
        stores=stores_from_db(db),
        targets=targets_from_db(db, month),
        rate_table=rate_table,
        month=month, days=days_in_month(month),
        gift_keys=gifts,
        gift_deduction=gift_deduction,
        duty_override=duty_override,
        excluded_stores=excluded_stores,
    )
    return result


# 单进程内按月加锁，防同一月份并发 /compute 叠加 CPU-bound 工作（治 R3 卡死）。
# 注：仅适用单进程 uvicorn 部署（当前 Docker 单实例）；多进程需换 DB 级锁，超出本任务范围。
_compute_locks: dict[str, threading.Lock] = {}


def _get_lock(month: str) -> threading.Lock:
    """返回某月份专属的 Lock（惰性创建）。

    用 setdefault 保证原子性：check-then-assign 在两个首算线程并发时可能
    各自 new 一个 Lock 并互相覆盖，返回不同实例导致单飞失效。setdefault
    在 CPython GIL 下原子，命中时虽会构造一个被丢弃的 Lock，开销可忽略。
    """
    return _compute_locks.setdefault(month, threading.Lock())


@router.post("/months/{month}/compute")
def do_compute(month: str, _: User = Depends(current_user), db: Session = Depends(get_db)):
    """算一次、物化两表（P4 基础）：
    - 聚合 Result（person×store 一行）
    - 逐行 DetailRow（与 SalesRecord 1:1 的提成台账）
    同事务删除-插入-提交，失败回滚；置 status=computed、results_stale=False；
    policy_version_id 仅首次锁定（None 时才写），重算不覆盖（修 H10）。

    T5.2（治 R3）：按月加进程内锁，第二个并发 /compute 立即返回 409；
    计算前置 status=computing，失败时恢复先前值，避免月份卡死在 computing。
    """
    lock = _get_lock(month)
    if not lock.acquire(blocking=False):
        raise HTTPException(status.HTTP_409_CONFLICT, "该月份正在计算，请稍候")
    try:
        m = db.get(Month, month)
        if m is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "月份不存在")
        if m.status == "computing":
            # 注：若进程被强杀（OOM/SIGKILL），status 会停留在 computing 导致后续 409，需手工 SQL 复位
            raise HTTPException(status.HTTP_409_CONFLICT, "该月份正在计算")
        prev_status = m.status
        m.status = "computing"
        db.commit()
        try:
            result = _run_compute(db, month)
            db.query(Result).filter_by(month=month).delete()
            db.query(DetailRow).filter_by(month=month).delete()
            for (person, store), v in result.breakdown.items():
                db.add(Result(month=month, person=person, store=store,
                              sales=v["sales"], target=v["target"], achievement=v["achievement"],
                              bucket=v["bucket"], commission=v["commission"]))
            # DetailRow 逐笔物化：bulk_insert_mappings 分批写入（C4 性能优化）。
            # 9 万行从逐条 ORM add 的 5-15s 降到 <1s；绕过 ORM 默认值，列全显式传。
            detail_mappings = [{
                "month": month, "sales_record_id": d.sales_record_id, "person": d.salesperson,
                "store": d.store, "sale_date": d.sale_date, "barcode": d.barcode,
                "product_name": d.product_name, "tier": d.tier, "bucket": d.bucket,
                "rate": d.rate, "amount": d.amount, "commission": d.commission,
                "tag": d.tag, "is_transferred": False,
            } for d in result.details]
            BATCH = 5000
            for i in range(0, len(detail_mappings), BATCH):
                db.bulk_insert_mappings(DetailRow, detail_mappings[i:i + BATCH])
            m = db.get(Month, month)
            m.status = "computed"
            m.results_stale = False
            if m.policy_version_id is None:          # 仅首次锁（修 H10），重算不覆盖
                cur = db.query(SalaryPolicyVersion).filter_by(is_current=True).first()
                if cur:
                    m.policy_version_id = cur.id
            db.commit()
        except Exception:
            db.rollback()
            # 恢复 status，避免失败的 compute 把月份卡在 "computing"
            m2 = db.get(Month, month)
            if m2 is not None:
                m2.status = prev_status
                db.commit()
            raise
    finally:
        lock.release()
    # ADR-021：结果已物化，后台预生成导出缓存（独立会话 + stale 守卫），导出时秒开
    threading.Thread(target=_pregen_export_cache, args=(month,), daemon=True).start()
    return {"details": len(result.details), "warnings": result.warnings,
            "total": round(float(sum(result.commission_by_person.values())), 2)}


@router.get("/months/{month}/results")
def results(month: str, _: User = Depends(current_user), db: Session = Depends(get_db)):
    """初版：从 Result 表汇总。Task 6 会扩展（breakdown 排序等）。
    stale=True 表示输入已变更或月份未计算，前端应提示"数据已变更，请重新计算"。
    写侧触发（输入变更 → results_stale=True）在 T5.1 落地。"""
    m = db.get(Month, month)
    rows = db.query(Result).filter_by(month=month).all()
    salary = defaultdict(Decimal)
    breakdown = []
    for r in rows:
        salary[r.person] += r.commission
        breakdown.append({"person": r.person, "store": r.store,
                          "sales": round(float(r.sales), 2),
                          "target": round(float(r.target), 2),
                          "achievement": round(float(r.achievement), 4),
                          "bucket": r.bucket,
                          "commission": round(float(r.commission), 2)})
    salary = sorted(({"person": p, "commission": round(float(c), 2)} for p, c in salary.items()),
                    key=lambda x: x["commission"], reverse=True)
    stale = bool(m is not None and (m.results_stale or m.status != "computed"))
    return {"salary": salary, "breakdown": breakdown, "stale": stale}


def _sales_item(r) -> Dict[str, Any]:
    """把 SalesRecord 映射成前端 SalesItem 全字段字典。
    sales_detail 与 tier_detail 共用（DRY），保证前端抽屉字段契约一致。"""
    return {
        "id": r.id,
        "receipt": r.receipt,
        "src_order": r.src_order,
        "store": r.store,
        "sale_date": r.sale_date.isoformat(),
        "barcode": r.barcode,
        "product_name": r.product_name,
        "qty": float(r.qty),
        "amount": round(float(r.amount), 2),
        "unit_price": round(float(r.unit_price), 2),
        "salesperson": r.salesperson,
        "cashier": r.cashier,
        "is_return": r.is_return,
        "is_online": r.is_online,
        "tag": r.tag,
        "original_store": r.original_store,
        "original_date": r.original_date.isoformat() if r.original_date else None,
        "transfer_reason": r.transfer_reason,
    }


@router.get("/months/{month}/sales-detail")
def sales_detail(month: str, store: str, person: str, date: str,
                 _: User = Depends(current_user), db: Session = Depends(get_db)):
    """获取某人某店某天的销售明细（从数据库查询）"""
    from backend.app.db import SalesRecord
    target_date = date_type.fromisoformat(date)

    records = db.query(SalesRecord).filter(
        SalesRecord.month == month,
        SalesRecord.store == store,
        SalesRecord.salesperson == person,
        SalesRecord.sale_date == target_date,
    ).order_by(SalesRecord.receipt, SalesRecord.id).all()

    items = [_sales_item(r) for r in records]
    return {"items": items}


@router.get("/months/{month}/tier-detail")
def tier_detail(month: str, store: str, person: str, bucket: str,
                _: User = Depends(current_user), db: Session = Depends(get_db)):
    """某人某店某档位的逐笔明细（读物化 DetailRow JOIN SalesRecord，零重算，字段与 sales_detail 一致）。
    tier 仍来自 DetailRow（单一真值源，无需重推导）；JOIN SalesRecord 取前端所需全字段。"""
    from backend.app.db import SalesRecord
    rows = (db.query(DetailRow, SalesRecord)
            .join(SalesRecord, SalesRecord.id == DetailRow.sales_record_id)
            .filter(DetailRow.month == month, DetailRow.store == store,
                    DetailRow.person == person, DetailRow.tier == bucket)
            .all())
    items = [_sales_item(sr) for _, sr in rows]
    return {"items": items}


@router.get("/months/{month}/tier-summary")
def tier_summary(month: str, store: str, person: str,
                 _: User = Depends(current_user), db: Session = Depends(get_db)):
    """某人某店档位汇总（读物化 DetailRow，零重算；rate 已是 SalaryPolicyVersion 口径）。"""
    rows = db.query(DetailRow).filter_by(month=month, store=store, person=person).all()
    if not rows:
        return {"tiers": [], "total_sales": 0, "total_commission": 0, "bucket": "LT_70", "target": 0,
                "monthly_target": 0, "duty_days": 0}
    bucket = rows[0].bucket
    tier_sales = defaultdict(float); tier_comm = defaultdict(float)
    for d in rows:
        tier_sales[d.tier] += float(d.amount); tier_comm[d.tier] += float(d.commission)
    tgt = db.query(MonthlyTarget).filter_by(month=month, store=store).first()
    monthly_target = float(tgt.target) if tgt else 0
    duty_days = db.query(Duty).filter_by(month=month, store=store, salesperson=person).count()
    personal_target = monthly_target / (days_in_month(month) or 30) * duty_days
    tiers = []
    for name in ["常温高毛", "常温低毛", "低温高毛", "低温低毛", "特价"]:
        rate = next((float(d.rate) for d in rows if d.tier == name), 0.0)
        tiers.append({"name": name, "sales": round(tier_sales.get(name, 0), 2), "rate": rate,
                      "rate_percent": f"{rate * 100:.0f}%", "commission": round(tier_comm.get(name, 0), 2)})
    return {"tiers": tiers, "total_sales": round(sum(tier_sales.values()), 2),
            "total_commission": round(sum(tier_comm.values()), 2), "bucket": bucket,
            "target": round(personal_target, 2), "monthly_target": round(monthly_target, 2),
            "duty_days": duty_days}


import tempfile
import os as _os
from fastapi import Response


def _export_cache_path(month: str) -> str:
    """导出缓存路径（ADR-021）。默认 <SALARY_DB 目录>/export_cache/{版本}/salary_{month}.xlsx；
    版本（oss_export.EXPORT_VERSION）变了 → 路径变 → 自动失效旧格式产物。"""
    from backend.app.services import oss_export
    cache_dir = _os.environ.get("SALARY_EXPORT_CACHE_DIR") or _os.path.join(
        _os.path.dirname(_os.environ.get("SALARY_DB", "/data/salary.db")), "export_cache")
    return _os.path.join(cache_dir, oss_export.EXPORT_VERSION, f"salary_{month}.xlsx")


def _build_export_file(db, month: str, path: str) -> None:
    """零重算构建三 sheet 导出文件到 path（ADR-020 逻辑抽出，同步兜底与后台预生成共用）。"""
    from sqlalchemy import text
    from backend.app.db import Result, Store, MonthlyTarget, Duty
    from backend.app.services.engine_bridge import days_in_month
    from backend.app.services.ledger_export import (
        write_salary_export, build_summary_rows, build_duty_grid)

    rows = db.execute(text("""
        SELECT d.person, d.store, d.sale_date, d.barcode, d.product_name,
               d.tier, d.bucket, d.rate, d.amount, d.commission, d.tag,
               s.receipt, s.src_order, s.qty, s.unit_price, s.salesperson, s.cashier,
               s.is_return, s.is_online,
               s.original_store, s.original_date, s.transfer_reason, s.extra
        FROM detail_rows d JOIN sales_records s ON d.sales_record_id = s.id
        WHERE d.month = :m
        ORDER BY d.person, d.store, d.sale_date
    """), {"m": month}).mappings().all()
    ledger = [dict(r) for r in rows]

    result_rows = db.query(Result).filter_by(month=month).all()
    store_map = {s.name: s for s in db.query(Store).all()}
    target_map = {t.store: t.target for t in db.query(MonthlyTarget).filter_by(month=month).all()}
    duty_rows = db.query(Duty).filter_by(month=month).all()
    duty_days_map = {}
    for d in duty_rows:
        duty_days_map[(d.salesperson, d.store)] = duty_days_map.get((d.salesperson, d.store), 0) + 1
    days = days_in_month(month)
    summary = build_summary_rows(result_rows, ledger, store_map, target_map, duty_days_map, days)
    duty_grid = build_duty_grid(duty_rows)
    write_salary_export(month, summary, ledger, duty_grid, days, path)


def _pregen_export_cache(month: str) -> None:
    """compute 后后台预生成导出缓存（ADR-021）。独立会话（请求会话已关）；stale/非 computed 放弃写。"""
    try:
        from backend.app.db import SessionLocal, Month
        db = SessionLocal()
        try:
            m = db.get(Month, month)
            if m is None or m.results_stale or m.status != "computed":
                return
            cache_path = _export_cache_path(month)
            _os.makedirs(_os.path.dirname(cache_path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(suffix=".xlsx", dir=_os.path.dirname(cache_path))
            _os.close(fd)
            try:
                _build_export_file(db, month, tmp)
                _os.replace(tmp, cache_path)
                from backend.app.services import oss_export
                oss_export.ensure_upload(month, cache_path)  # ADR-022：上传 OBS，导出秒开
            finally:
                if _os.path.exists(tmp):
                    _os.remove(tmp)
        finally:
            db.close()
    except Exception:
        import logging
        logging.getLogger("workflow.export_cache").exception("预生成导出缓存失败: %s", month)


_XLSX_MT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/months/{month}/export")
def export(month: str, _: User = Depends(current_user), db: Session = Depends(get_db)):
    """导出三 sheet 工资表（ADR-020/021/022）：缓存优先；OSS 已配 → 上传后 302 签名 URL
    （下载走 OBS 高带宽），未配 → 直连。零重算（治 R1）。"""
    from fastapi.responses import FileResponse, RedirectResponse
    from backend.app.services import oss_export

    m = _get_month(db, month)
    stale = bool(m.results_stale or m.status != "computed")
    cache_path = _export_cache_path(month)
    disp = {"Content-Disposition": f'attachment; filename="salary_{month}.xlsx"'}

    # 缓存命中
    if not stale and _os.path.exists(cache_path):
        if oss_export.is_configured():
            oss_export.ensure_upload(month, cache_path)
            return RedirectResponse(oss_export.presign_url(month), status_code=302)
        return FileResponse(cache_path, media_type=_XLSX_MT, headers=disp)

    # 兜底构建（临时文件建在缓存同目录，保证 os.replace 原子不跨设备 EXDEV）
    tmp_dir = _os.path.dirname(cache_path)
    _os.makedirs(tmp_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(suffix=".xlsx", dir=tmp_dir)
    _os.close(fd)
    try:
        _build_export_file(db, month, tmp_path)
        if not stale:
            _os.replace(tmp_path, cache_path)
            src = cache_path
        else:
            src = tmp_path
        if oss_export.is_configured():
            oss_export.ensure_upload(month, src)
            return RedirectResponse(oss_export.presign_url(month), status_code=302)
        with open(src, "rb") as f:
            data = f.read()
    finally:
        if _os.path.exists(tmp_path):
            _os.remove(tmp_path)
    return Response(content=data, media_type=_XLSX_MT, headers=disp)
