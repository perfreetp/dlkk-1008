import sys
sys.path.insert(0, '.')
from property_fee_cli.database import get_connection

conn = get_connection()
try:
    print("=== Pending Payments for 2-1-101 (ID 7/8/9) ===")
    rows = conn.execute("SELECT id, amount, match_status, matched_arrear_id, matched_room, split_to_ids FROM pending_payments WHERE id IN (7,8,9) ORDER BY id").fetchall()
    for r in rows:
        print(dict(r))

    print("\n=== 检查异常表是否有数据 ===")
    cnt = conn.execute("SELECT COUNT(*) FROM audit_exceptions").fetchone()[0]
    print(f"异常记录数: {cnt}")
finally:
    conn.close()
