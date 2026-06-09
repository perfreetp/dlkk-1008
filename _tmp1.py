import sys
sys.path.insert(0, '.')
from property_fee_cli.database import get_connection

conn = get_connection()
try:
    rows = conn.execute("""
        SELECT a.id, h.room_no, a.fee_period, a.base_amount, a.late_fee,
               a.paid_amount, a.discount_amount,
               (a.base_amount+a.late_fee-a.paid_amount-a.discount_amount) as u
        FROM arrears a JOIN households h ON a.household_id=h.id
        WHERE h.room_no='2-1-101' ORDER BY a.fee_period
    """).fetchall()
    print("--- 2-1-101 欠费明细 ---")
    for r in rows:
        print(dict(r))
finally:
    conn.close()
