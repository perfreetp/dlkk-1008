import sys
sys.path.insert(0, '.')
from property_fee_cli.database import get_connection

conn = get_connection()
try:
    print("=== Batch 2 stats 银行流水入账后 ===")
    b = conn.execute("SELECT id, target_count, sent_count, success_count, fail_count, call_count, promised_count, repaid_count, repaid_amount, status FROM batches WHERE id=2").fetchone()
    for k, v in dict(b).items():
        print(f"  {k}: {v}")

    print("\n=== Batch 2 中 2-1-101两成员更新后状态 ===")
    bm = conn.execute("SELECT id, arrear_id, fee_period, unpaid_amount, repayment_status, repayment_amount FROM batch_members WHERE batch_id=2 AND arrear_id IN (7,21)").fetchall()
    for r in bm:
        print(dict(r))

    print("\n=== 批次1（旧，已关闭）的最终回款状态 ===")
    b1 = conn.execute("SELECT batch_name, target_count, repaid_count, repaid_amount FROM batches WHERE id=1").fetchone()
    print(dict(b1))
finally:
    conn.close()
