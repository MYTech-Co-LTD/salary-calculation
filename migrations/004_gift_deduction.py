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
