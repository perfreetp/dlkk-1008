import sys
sys.path.insert(0, '.')
from property_fee_cli.database import get_connection
from property_fee_cli.commands.notice_cmd import _build_template_vars
from property_fee_cli.utils import calc_unpaid, UNPAID_SQL_EXPR

conn = get_connection()
try:
    print("=== arrear_id=9 的房号 ===")
    r = conn.execute("SELECT a.id, h.room_no, a.fee_period, a.base_amount, a.late_fee, a.discount_amount, a.paid_amount, a.status, h.owner_name, h.phone FROM arrears a JOIN households h ON a.household_id=h.id WHERE a.id=9").fetchone()
    d = dict(r)
    d['unpaid'] = calc_unpaid(d['base_amount'], d['late_fee'], d['paid_amount'], d['discount_amount'])
    print(f"  {d}")

    print(f"\n=== 测试模板变量 (arrear_id={r['id']}, 房号={r['room_no']}) ===")
    vars = _build_template_vars(conn, dict(r), use_mask=False)
    print(f"  total_amount(应缴总额=本金+滞纳金-减免) = {vars['total_amount']}")
    print(f"  unpaid_amount(尚欠=本金+滞纳金-已缴-减免) = {vars['unpaid_amount']}")

    cur = conn.execute(f"SELECT {UNPAID_SQL_EXPR} AS unpaid FROM arrears a WHERE id=?", (r['id'],))
    print(f"\n  SQL层UNPAID_SQL_EXPR算出来 = {cur.fetchone()[0]}")

    print("\n=== 短信模板表名 ===")
    tables = [x[0] for x in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
    print(f"  有 {[t for t in tables if 'template' in t.lower() or 'notice' in t.lower() or 'sms' in t.lower()]}")

    tpl_table = 'notice_templates' if 'notice_templates' in tables else 'sms_templates'
    tpl = conn.execute(f"SELECT id, template_name, template_text FROM {tpl_table} WHERE id=1").fetchone()
    print(f"\n  默认模板(ID=1): {dict(tpl) if tpl else '未找到'}")
    if tpl:
        print(f"\n  === 模板渲染后(用unpaid_amount) ===")
        from string import Template
        rendered = Template(tpl['template_text']).safe_substitute(**vars)
        print(f"  {rendered}")
finally:
    conn.close()
