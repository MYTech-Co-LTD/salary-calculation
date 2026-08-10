"""SQLAlchemy 模型与会话（规格 §4 数据模型）。"""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Numeric, Boolean, Date, DateTime, JSON,
    UniqueConstraint, ForeignKey, create_engine, event, Index,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

from backend.app.config import DB_URL

Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)


class Product(Base):
    __tablename__ = "products"
    barcode = Column(String, primary_key=True)
    name = Column(String)
    spec = Column(String)
    category = Column(String)            # 常温奶 | 低温奶
    cost = Column(Numeric, nullable=True)  # 销售成本，匹配不到为 None
    exclude_commission = Column(Boolean, default=False)  # 不计入提成


class Store(Base):
    __tablename__ = "stores"
    name = Column(String, primary_key=True)
    group = Column(String)               # 1组 | 2组 | 3组
    store_class = Column(String)         # A | B | C | D
    supervisor = Column(String, default="")
    exclude_assessment = Column(Boolean, default=False)  # 不参与考核


class MonthlyTarget(Base):
    __tablename__ = "monthly_targets"
    id = Column(Integer, primary_key=True)
    month = Column(String, nullable=False)    # YYYY-MM
    store = Column(String, nullable=False)
    target = Column(Numeric, nullable=False)
    __table_args__ = (UniqueConstraint("month", "store", name="uq_month_store"),)


class SalaryPolicyVersion(Base):
    __tablename__ = "salary_policy_versions"

    id = Column(Integer, primary_key=True)
    version = Column(Integer, nullable=False, unique=True)
    effective_from = Column(Date, nullable=False)
    is_current = Column(Boolean, default=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(String(50))
    content = Column(JSON, nullable=False)
    note = Column(String(200))


engine = create_engine(DB_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _):
    """每个新连接开启 WAL（并发读 + 单写）与 10s busy_timeout，
    消除读写锁竞争导致的卡死（audit R3）。"""
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=10000")
    cur.close()


def init_db():
    Base.metadata.create_all(engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class Month(Base):
    __tablename__ = "months"
    month = Column(String, primary_key=True)     # YYYY-MM
    status = Column(String, default="draft")     # draft | computing | computed
    sales_file = Column(String, nullable=True)   # 上传的销售流水路径
    gifts_file = Column(String, nullable=True)   # 上传的让利明细路径
    results_stale = Column(Boolean, default=True)  # 输入变更后置 true，compute 后置 false
    policy_version_id = Column(Integer, ForeignKey("salary_policy_versions.id"), nullable=True)
    policy_version = relationship("SalaryPolicyVersion")
    current_step = Column(String(20), default="import")  # import/targets/duty/results
    step_data = Column(JSON, default=dict)  # 步骤数据快照
    created_at = Column(DateTime, default=datetime.utcnow)


class Duty(Base):
    __tablename__ = "duties"
    id = Column(Integer, primary_key=True)
    month = Column(String, nullable=False)
    store = Column(String, nullable=False)
    duty_date = Column(Date, nullable=False)
    salesperson = Column(String, nullable=False)  # 确认的当班人
    __table_args__ = (UniqueConstraint("month", "store", "duty_date", name="uq_duty"),)


class Result(Base):
    __tablename__ = "results"
    id = Column(Integer, primary_key=True)
    month = Column(String, nullable=False)
    person = Column(String, nullable=False)
    store = Column(String, nullable=False)
    sales = Column(Numeric, nullable=False)
    target = Column(Numeric, nullable=False)
    achievement = Column(Numeric, nullable=False)
    bucket = Column(String, nullable=False)
    commission = Column(Numeric, nullable=False)
    __table_args__ = (
        UniqueConstraint("month", "person", "store", name="uq_result"),
        Index("idx_results_month", "month"),
    )


class DetailRow(Base):
    """逐笔提成台账（compute 物化，逐行 1:1 对应 SalesRecord）"""
    __tablename__ = "detail_rows"
    id = Column(Integer, primary_key=True)
    month = Column(String, nullable=False, index=True)
    sales_record_id = Column(Integer, index=True)
    person = Column(String, nullable=False)
    store = Column(String, nullable=False)
    sale_date = Column(Date, nullable=False)
    barcode = Column(String)
    product_name = Column(String)
    tier = Column(String)        # 商品档位
    bucket = Column(String)      # 达成档
    rate = Column(Numeric)
    amount = Column(Numeric, nullable=False)
    commission = Column(Numeric, nullable=False)
    tag = Column(String(20), nullable=False)  # 有效计提/退货冲抵/退货未匹配/赠送剔除/不计提成/非乳品
    is_transferred = Column(Boolean, default=False)
    __table_args__ = (
        UniqueConstraint("month", "sales_record_id", name="uq_detail_month_sr"),
        Index("idx_detail_month_person_store", "month", "person", "store"),
    )


class SalesRecord(Base):
    """销售明细记录（持久化）"""
    __tablename__ = "sales_records"
    id = Column(Integer, primary_key=True)
    month = Column(String, nullable=False, index=True)  # YYYY-MM
    receipt = Column(String, nullable=False)  # 小票单号
    src_order = Column(String)  # 源单号（退货时指向原销售小票）
    store = Column(String, nullable=False, index=True)
    sale_date = Column(Date, nullable=False, index=True)
    barcode = Column(String, nullable=False)
    product_name = Column(String)
    qty = Column(Numeric, nullable=False)
    amount = Column(Numeric, nullable=False)
    unit_price = Column(Numeric, nullable=False)
    salesperson = Column(String, default="", index=True)
    cashier = Column(String, default="")  # 收银员
    is_return = Column(Boolean, default=False)
    is_online = Column(Boolean, default=False)
    # 标签：有效/退款/赠送/不计提成
    tag = Column(String(20), nullable=False, default="有效", index=True)
    # 调整相关
    original_store = Column(String)  # 原始门店（调整前）
    original_date = Column(Date)    # 原始日期（调整前）
    transfer_reason = Column(String)  # 调整原因
    extra = Column(JSON)  # 源 Excel 其余字段原样留底
    created_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        UniqueConstraint("month", "receipt", "store", "sale_date", "barcode", "amount", name="uq_sales_record"),
        Index("idx_sales_month_store_date_person", "month", "store", "sale_date", "salesperson"),
    )


class TransferRecord(Base):
    """业绩调整记录"""
    __tablename__ = "transfer_records"
    id = Column(Integer, primary_key=True)
    month = Column(String, nullable=False, index=True)
    salesperson = Column(String, nullable=False)
    from_store = Column(String, nullable=False)
    from_date = Column(Date, nullable=False)
    to_store = Column(String, nullable=False)
    to_date = Column(Date, nullable=False)
    reason = Column(String)  # 调整原因
    created_at = Column(DateTime, default=datetime.utcnow)


class Anomaly(Base):
    """计算预检异常记录"""
    __tablename__ = "anomalies"
    id = Column(Integer, primary_key=True)
    month = Column(String, nullable=False, index=True)  # YYYY-MM
    anomaly_type = Column(String(10), nullable=False)  # 1-7（7=赠送件数不符）
    entity_type = Column(String(50))  # store/product/gift/refund
    entity_id = Column(String(100))  # 门店名/条码等
    description = Column(String(500))
    status = Column(String(20), default="pending")  # pending/ignored/resolved
    resolution = Column(String(200))
    created_at = Column(DateTime, default=datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)


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


def mark_all_months_stale(db):
    """主数据变更：所有月份置 results_stale=True（ADR-014）。

    单 SQL 批量 update；draft 月份本就 stale（status!=computed），无副作用。
    stores/products/import_master 的增删改端点在 commit 前调用。
    """
    db.query(Month).update({Month.results_stale: True})
