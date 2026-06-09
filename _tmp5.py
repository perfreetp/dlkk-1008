import sys
sys.path.insert(0, '.')
from property_fee_cli.database import get_connection
from property_fee_cli.commands.notice_cmd import _build_template_vars
from property_fee_cli.utils import calc_unpaid, UNPAID_SQL_EXPR

conn = get_connection()
try:
    print("=== 1-2-202 的欠费记录 ===")
    rows = conn.execute("SELECT a.id, h.room_no, a.fee_period, a.base_amount, a.late_fee, a.discount_amount, a.paid_amount, a.status, h.owner_name, h.phone FROM arrears a JOIN households h ON a.household_id=h.id WHERE h.room_no='1-2-202'").fetchall()
    for r in rows:
        d = dict(r)
        d['unpaid'] = calc_unpaid(d['base_amount'], d['late_fee'], d['paid_amount'], d['discount_amount'])
        print(f"  {d}")

    if rows:
        r = rows[0]
        print(f"\n=== 测试模板变量 (arrear_id={r['id']}) ===")
        vars = _build_template_vars(conn, dict(r), use_mask=False)
        print(f"  total_amount(应缴总额=本金+滞纳金-减免) = {vars['total_amount']}")
        print(f"  unpaid_amount(尚欠=本金+滞纳金-已缴-减免) = {vars['unpaid_amount']}")
        print(f"  短信模板默认变量显示金额的是哪个？让我渲染一下默认模板看最终结果。")

        from rich.markup import escape
        cur = conn.execute(f"SELECT {UNPAID_SQL_EXPR} AS unpaid FROM arrears a WHERE id=?", (r['id'],))
        print(f"\n  SQL层UNPAID_SQL_EXPR算出来 = {cur.fetchone()[0]}")

    print("\n=== 默认短信模板内容 ===")
    tpl = conn.execute("SELECT template_text FROM sms_templates WHERE id=1").fetchone()
    print(f"  {tpl[0]}")
finally:
    conn.close()
