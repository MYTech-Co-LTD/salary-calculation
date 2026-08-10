from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from backend.app.db import Product, Store


class AnomalyChecker:
    """异常检查服务，产出待处理的异常列表。"""

    def __init__(self, db: Session, month: str):
        self.db = db
        self.month = month
        self.anomalies: List[Dict[str, Any]] = []

    def check_store_exists(self, store_names: List[str], sales_data: Optional[Dict[str, Any]] = None):
        """异常1: 交易流水门店不存在"""
        existing: Set[str] = {s.name for s in self.db.query(Store).all()}
        for name in store_names:
            if name not in existing:
                # 从销售数据中提取更多信息
                extra_info = ""
                if sales_data and name in sales_data:
                    info = sales_data[name]
                    extra_info = f" | 涉及营业员: {', '.join(info.get('salespersons', []))}"
                    extra_info += f" | 交易笔数: {info.get('count', 0)}"
                self.anomalies.append({
                    "month": self.month,
                    "anomaly_type": "1",
                    "entity_type": "store",
                    "entity_id": name,
                    "description": f"门店「{name}」在门店管理中不存在{extra_info}",
                    "status": "pending",
                })

    def check_product_exists(self, barcodes: List[str], sales_data: Optional[Dict[str, Any]] = None):
        """异常2: 交易流水商品不存在"""
        existing: Set[str] = {p.barcode for p in self.db.query(Product).all()}
        for barcode in barcodes:
            if barcode not in existing:
                # 从销售数据中提取商品名称等信息
                extra_info = ""
                if sales_data and barcode in sales_data:
                    info = sales_data[barcode]
                    if info.get("name"):
                        extra_info += f" | 商品名: {info['name']}"
                    if info.get("category"):
                        extra_info += f" | 类别: {info['category']}"
                    if info.get("count"):
                        extra_info += f" | 交易笔数: {info['count']}"
                self.anomalies.append({
                    "month": self.month,
                    "anomaly_type": "2",
                    "entity_type": "product",
                    "entity_id": barcode,
                    "description": f"条码「{barcode}」在商品档案中不存在{extra_info}",
                    "status": "pending",
                })

    def check_targets(self, stores: List[Dict[str, Any]], target_stores: Set[str]):
        """异常3: 门店无目标且未打不参与考核标签"""
        for s in stores:
            if s.get("exclude_assessment"):
                continue
            if s["name"] not in target_stores:
                # 包含门店组别、类别等信息
                extra_info = ""
                if s.get("group"):
                    extra_info += f" | 组别: {s['group']}"
                if s.get("store_class"):
                    extra_info += f" | 类别: {s['store_class']}"
                self.anomalies.append({
                    "month": self.month,
                    "anomaly_type": "3",
                    "entity_type": "store",
                    "entity_id": s["name"],
                    "description": f"「{s['name']}」无月度目标值{extra_info}",
                    "status": "pending",
                })

    def check_products_complete(self, barcodes: List[str]):
        """异常4: 商品缺类别/成本且未打不计提成"""
        products = (
            self.db.query(Product)
            .filter(Product.barcode.in_(barcodes))
            .all()
        )
        for p in products:
            if p.exclude_commission:
                continue
            missing: List[str] = []
            if not p.category:
                missing.append("类别")
            if p.cost is None:
                missing.append("销售成本")
            if missing:
                # 包含商品名称等已有信息
                extra_info = ""
                if p.name:
                    extra_info += f" | 商品名: {p.name}"
                if p.spec:
                    extra_info += f" | 规格: {p.spec}"
                if p.category:
                    extra_info += f" | 类别: {p.category}"
                if p.cost is not None:
                    extra_info += f" | 成本: {p.cost}"
                self.anomalies.append({
                    "month": self.month,
                    "anomaly_type": "4",
                    "entity_type": "product",
                    "entity_id": p.barcode,
                    "description": f"「{p.barcode}」缺少{'、'.join(missing)}{extra_info}",
                    "status": "pending",
                })

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

    def get_anomalies(self) -> List[Dict[str, Any]]:
        return self.anomalies
