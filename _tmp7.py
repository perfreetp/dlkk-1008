import sys
sys.path.insert(0, '.')
from property_fee_cli.database import get_connection
from property_fee_cli.commands.notice_cmd import _build_template_vars, _render_template
from property_fee_cli.utils import calc_unpaid, UNPAID_SQL_EXPR

conn = get_connection()
try:
    r = conn.execute("SELECT a.id, h.room_no, h.building, a.fee_period, a.base_amount, a.late_fee, a.discount_amount, a.paid_amount, a.status, h.owner_name, h.phone, a.fee_type, a.due_date FROM arrears a JOIN households h ON a.household_id=h.id WHERE a.id=9").fetchone()
    d = dict(r)
    d['unpaid'] = calc_unpaid(d['base_amount'], d['late_fee'], d['paid_amount'], d['discount_amount'])
    print(f"=== arrear_id=9 ===")
    print(f"  房号={d['room_no']}, 账期={d['fee_period']}")
    print(f"  本金={d['base_amount']}, 滞纳金={d['late_fee']}, 已缴={d['paid_amount']}, 减免={d['discount_amount']}")
    print(f"  calc_unpaid = {d['unpaid']}")

    vars = _build_template_vars(dict(r), hide_sensitive=False)
    print(f"\n=== 模板变量 ===")
    print(f"  total_amount(应缴总额) = {vars['total_amount']}")
    print(f"  unpaid_amount(尚欠) = {vars['unpaid_amount']}")

    tpl = conn.execute("SELECT id, name, content, is_default FROM notice_templates WHERE is_default=1 LIMIT 1").fetchone()
    print(f"\n=== 默认模板 ===")
    print(f"  模板名: {tpl['name']}")
    print(f"  模板文本: {tpl['content']}")

    rendered = _render_template(tpl['content'], vars)
    print(f"\n=== 渲染后 ===")
    print(f"  {rendered}")
    print(f"\n=== 关键检查：正文里出现的金额是尚欠360还是应缴560？ ===")
    if '360' in rendered:
        print("  ✅ PASS：正文显示尚欠360元（部分缴费后正确口径）")
    elif '560' in rendered:
        print("  ❌ FAIL：正文显示560元，还是用的应缴总额（bug未修复）")
    else:
        print("  ⚠️ 无法判断，检查金额格式")
finally:
    conn.close()
