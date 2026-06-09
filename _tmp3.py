import sys
sys.path.insert(0, '.')
from property_fee_cli.database import get_connection

conn = get_connection()
try:
    print("=== Batch 2 stats 初始状态 ===")
    b = conn.execute("SELECT id, batch_name, target_count, repaid_count, repaid_amount, status FROM batches WHERE id=2").fetchone()
    print(dict(b))

    print("\n=== Batch 2 中 2-1-101成员 (arrear_id=7,21) ===")
    bm = conn.execute("SELECT id, arrear_id, room_no, unpaid_amount, repayment_status, repayment_amount FROM batch_members WHERE batch_id=2 AND arrear_id IN (7,21)").fetchall()
    for r in bm:
        print(dict(r))
finally:
    conn.close()
