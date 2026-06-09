import click
import csv
import os
from datetime import datetime, date
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from typing import Optional

from ..database import get_connection
from ..utils import mask_phone, mask_name, should_hide_sensitive, format_money, calc_unpaid, UNPAID_SQL_EXPR

console = Console()


@click.group(help="催缴报表：导出台账、楼栋汇总、历史查询")
def report_cmd():
    pass


@report_cmd.command("ledger", help="导出催缴台账（CSV）")
@click.argument("output", type=click.Path(writable=True), default="催缴台账.csv")
@click.option("--building", "-b", help="按楼栋筛选")
@click.option("--room", "-r", help="按房号筛选")
@click.option("--status", "-s", type=click.Choice(["未缴", "部分缴纳", "已缴", "承诺付款", "减免"]), help="按状态筛选")
@click.option("--period", "-p", help="按账期筛选")
@click.option("--unpaid-only", is_flag=True, help="仅导出未缴清的")
@click.option("--hide-sensitive/--show-sensitive", default=None)
@click.option("--encoding", default="utf-8-sig", help="CSV编码")
def export_ledger(output, building, room, status, period, unpaid_only, hide_sensitive, encoding):
    hide = hide_sensitive if hide_sensitive is not None else should_hide_sensitive()
    conn = get_connection()

    sql = """
        SELECT
            h.room_no, h.building, h.unit, h.floor, h.owner_name, h.phone, h.area, h.property_type,
            a.id as arrear_id, a.fee_period, a.fee_type, a.base_amount, a.late_fee,
            a.total_amount, a.due_date, a.paid_amount, a.discount_amount, a.status, a.remark,
            COALESCE(nr.last_sent, '') as last_sent,
            COALESCE(nr.send_count, 0) as send_count,
            COALESCE(cr.last_call, '') as last_call,
            COALESCE(cr.call_count, 0) as call_count,
            COALESCE(cr.commitment_date, '') as commitment_date,
            COALESCE(cr.commitment_amount, 0) as commitment_amount,
            COALESCE(dr.discount_total, 0) as discount_total
        FROM arrears a
        JOIN households h ON a.household_id = h.id
        LEFT JOIN (
            SELECT arrear_id, MAX(sent_at) as last_sent, COUNT(*) as send_count
            FROM notice_records WHERE status = '已发送' GROUP BY arrear_id
        ) nr ON a.id = nr.arrear_id
        LEFT JOIN (
            SELECT arrear_id, MAX(call_time) as last_call, COUNT(*) as call_count,
                   MAX(commitment_date) as commitment_date,
                   MAX(commitment_amount) as commitment_amount
            FROM call_records GROUP BY arrear_id
        ) cr ON a.id = cr.arrear_id
        LEFT JOIN (
            SELECT arrear_id, SUM(discount_amount) as discount_total
            FROM discount_records GROUP BY arrear_id
        ) dr ON a.id = dr.arrear_id
        WHERE 1=1
    """
    params = []
    if building:
        sql += " AND h.building LIKE ?"
        params.append(f"%{building}%")
    if room:
        sql += " AND h.room_no LIKE ?"
        params.append(f"%{room}%")
    if status:
        sql += " AND a.status = ?"
        params.append(status)
    if period:
        sql += " AND a.fee_period LIKE ?"
        params.append(f"%{period}%")
    if unpaid_only:
        sql += f" AND {UNPAID_SQL_EXPR} > 0"
    sql += " ORDER BY h.building, h.room_no, a.fee_period"

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    if not rows:
        console.print("[yellow]无数据可导出[/yellow]")
        return

    headers = [
        "房号", "楼栋", "单元", "楼层", "业主姓名", "联系电话", "建筑面积(㎡)", "物业类型",
        "欠费ID", "账期", "费用类型", "本金", "滞纳金", "应缴总额",
        "应缴日期", "已缴金额", "已减免金额", "当前状态", "尚欠金额",
        "最后短信时间", "短信次数", "最后电话时间", "电话次数",
        "承诺付款日", "承诺金额", "备注"
    ]

    with open(output, "w", encoding=encoding, newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)

        for r in rows:
            unpaid = calc_unpaid(r["base_amount"], r["late_fee"], r["paid_amount"], r["discount_amount"])
            name = mask_name(r["owner_name"]) if hide else r["owner_name"]
            phone = mask_phone(r["phone"]) if hide else (r["phone"] or "")
            writer.writerow([
                r["room_no"], r["building"], r["unit"] or "", r["floor"] or "",
                name, phone, r["area"] or "", r["property_type"] or "",
                r["arrear_id"], r["fee_period"], r["fee_type"],
                f"{r['base_amount']:.2f}", f"{r['late_fee']:.2f}", f"{r['total_amount']:.2f}",
                r["due_date"], f"{r['paid_amount']:.2f}", f"{r['discount_amount']:.2f}",
                r["status"], f"{unpaid:.2f}",
                r["last_sent"] or "", r["send_count"],
                r["last_call"] or "", r["call_count"],
                r["commitment_date"] or "", f"{r['commitment_amount']:.2f}",
                r["remark"] or "",
            ])

    total_unpaid = sum(calc_unpaid(r["base_amount"], r["late_fee"], r["paid_amount"], r["discount_amount"]) for r in rows)
    console.print(f"[green]已导出 {len(rows)} 条记录到 {output}[/green]")
    console.print(f"[dim]尚欠合计: {format_money(total_unpaid)} 元，敏感信息隐藏: {'是' if hide else '否'}[/dim]")


@report_cmd.command("building", help="生成楼栋催缴汇总表")
@click.option("--output", "-o", type=click.Path(writable=True), help="导出CSV路径")
@click.option("--encoding", default="utf-8-sig")
def building_summary(output, encoding):
    conn = get_connection()
    rows = conn.execute(f"""
        SELECT
            h.building,
            COUNT(DISTINCT h.id) as total_households,
            COUNT(DISTINCT CASE WHEN {UNPAID_SQL_EXPR} > 0 THEN h.id END) as arrear_households,
            COUNT(DISTINCT a.id) as total_arrears,
            COUNT(DISTINCT CASE WHEN a.status = '承诺付款' THEN a.id END) as commitment_count,
            COUNT(DISTINCT CASE WHEN a.status = '减免' THEN a.id END) as discount_count,
            COALESCE(SUM(a.base_amount), 0) as total_base,
            COALESCE(SUM(a.late_fee), 0) as total_late,
            COALESCE(SUM(CASE WHEN {UNPAID_SQL_EXPR} > 0
                THEN a.base_amount END), 0) as unpaid_base,
            COALESCE(SUM(CASE WHEN {UNPAID_SQL_EXPR} > 0
                THEN a.late_fee END), 0) as unpaid_late,
            COALESCE(SUM({UNPAID_SQL_EXPR}), 0) as total_unpaid,
            COALESCE(SUM(a.paid_amount), 0) as total_paid,
            COALESCE(SUM(a.discount_amount), 0) as total_discount,
            COALESCE(ns.send_count, 0) as notice_sent,
            COALESCE(cr.call_count, 0) as call_made,
            COALESCE(cm.commitment_total, 0) as commitment_total_amount
        FROM households h
        LEFT JOIN arrears a ON h.id = a.household_id
        LEFT JOIN (
            SELECT h2.building, COUNT(*) as send_count
            FROM notice_records nr JOIN households h2 ON nr.household_id = h2.id
            WHERE nr.status = '已发送' GROUP BY h2.building
        ) ns ON h.building = ns.building
        LEFT JOIN (
            SELECT h2.building, COUNT(*) as call_count
            FROM call_records cr JOIN households h2 ON cr.household_id = h2.id
            GROUP BY h2.building
        ) cr ON h.building = cr.building
        LEFT JOIN (
            SELECT h2.building, SUM(cr2.commitment_amount) as commitment_total
            FROM call_records cr2 JOIN households h2 ON cr2.household_id = h2.id
            WHERE cr2.commitment_amount > 0 GROUP BY h2.building
        ) cm ON h.building = cm.building
        GROUP BY h.building
        ORDER BY h.building
    """).fetchall()
    conn.close()

    if not rows:
        console.print("[yellow]暂无数据[/yellow]")
        return

    table = Table(title="楼栋催缴汇总")
    table.add_column("楼栋", style="green")
    table.add_column("总户数", justify="right", style="blue")
    table.add_column("欠费户数", justify="right", style="yellow")
    table.add_column("欠费占比", justify="right")
    table.add_column("欠费本金", justify="right", style="cyan")
    table.add_column("欠费滞纳金", justify="right", style="red")
    table.add_column("欠费总额", justify="right", style="bold red")
    table.add_column("承诺付款", justify="right", style="magenta")
    table.add_column("减免金额", justify="right", style="magenta")
    table.add_column("已发通知", justify="right", style="white")
    table.add_column("电话沟通", justify="right", style="white")

    totals = {"hh": 0, "arrear": 0, "base": 0, "late": 0, "unpaid": 0, "commit": 0, "disc": 0, "notice": 0, "call": 0}
    for r in rows:
        ratio = f"{r['arrear_households'] / r['total_households'] * 100:.1f}%" if r["total_households"] else "-"
        totals["hh"] += r["total_households"]
        totals["arrear"] += r["arrear_households"]
        totals["base"] += r["unpaid_base"] or 0
        totals["late"] += r["unpaid_late"] or 0
        totals["unpaid"] += r["total_unpaid"] or 0
        totals["commit"] += r["commitment_total_amount"] or 0
        totals["disc"] += r["total_discount"] or 0
        totals["notice"] += r["notice_sent"] or 0
        totals["call"] += r["call_made"] or 0
        table.add_row(
            r["building"],
            str(r["total_households"]), str(r["arrear_households"]), ratio,
            format_money(r["unpaid_base"] or 0), format_money(r["unpaid_late"] or 0),
            format_money(r["total_unpaid"] or 0),
            format_money(r["commitment_total_amount"] or 0),
            format_money(r["total_discount"] or 0),
            str(r["notice_sent"] or 0), str(r["call_made"] or 0),
        )

    total_ratio = f"{totals['arrear'] / totals['hh'] * 100:.1f}%" if totals["hh"] else "-"
    table.add_row(
        "[bold]合计[/bold]", f"[bold]{totals['hh']}[/bold]", f"[bold]{totals['arrear']}[/bold]",
        f"[bold]{total_ratio}[/bold]",
        f"[bold]{format_money(totals['base'])}[/bold]",
        f"[bold]{format_money(totals['late'])}[/bold]",
        f"[bold red]{format_money(totals['unpaid'])}[/bold red]",
        f"[bold]{format_money(totals['commit'])}[/bold]",
        f"[bold]{format_money(totals['disc'])}[/bold]",
        f"[bold]{totals['notice']}[/bold]", f"[bold]{totals['call']}[/bold]",
    )
    console.print(table)

    if output:
        with open(output, "w", encoding=encoding, newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "楼栋", "总户数", "欠费户数", "欠费占比(%)",
                "欠费本金", "欠费滞纳金", "欠费总额",
                "承诺金额", "已减免", "已发通知数", "电话沟通数"
            ])
            for r in rows:
                ratio = round(r["arrear_households"] / r["total_households"] * 100, 1) if r["total_households"] else 0
                writer.writerow([
                    r["building"], r["total_households"], r["arrear_households"], ratio,
                    round(r["unpaid_base"] or 0, 2), round(r["unpaid_late"] or 0, 2),
                    round(r["total_unpaid"] or 0, 2),
                    round(r["commitment_total_amount"] or 0, 2),
                    round(r["total_discount"] or 0, 2),
                    r["notice_sent"] or 0, r["call_made"] or 0,
                ])
        console.print(f"\n[green]汇总表已导出到 {output}[/green]")


@report_cmd.command("history", help="查询历史催缴记录（完整时间线：短信/电话/承诺/减免/缴费）")
@click.option("--room", "-r", "room_no", help="按房号查询")
@click.option("--building", "-b", help="按楼栋查询")
@click.option("--from-date", help="起始日期 YYYY-MM-DD")
@click.option("--to-date", help="结束日期 YYYY-MM-DD")
@click.option("--type", "rtype", type=click.Choice(["短信", "电话", "减免", "缴费", "全部"]), default="全部", help="记录类型")
@click.option("--output", "-o", type=click.Path(writable=True), help="导出CSV")
@click.option("--limit", "-n", type=int, default=200, help="显示条数")
@click.option("--hide-sensitive/--show-sensitive", default=None)
def query_history(room_no, building, from_date, to_date, rtype, output, limit, hide_sensitive):
    hide = hide_sensitive if hide_sensitive is not None else should_hide_sensitive()
    conn = get_connection()

    events: list = []
    room_where = ""
    bld_where = ""
    params_common: list = []
    if room_no:
        room_where = " AND h.room_no LIKE ?"
        params_common.append(f"%{room_no}%")
    if building:
        bld_where = " AND h.building LIKE ?"
        params_common.append(f"%{building}%")
    date_where_from = ""
    date_where_to = ""
    if from_date:
        date_where_from = " AND DATE({timecol}) >= ?"
    if to_date:
        date_where_to = " AND DATE({timecol}) <= ?"

    def _limit_date(sql: str, timecol: str) -> str:
        s = sql.replace("{timecol}", timecol)
        return s

    if rtype in ("短信", "全部"):
        sql = f"""
            SELECT '短信' as event_type, nr.created_at as event_time, h.room_no, h.building,
                   h.owner_name, nr.phone, nr.channel, nr.status,
                   nr.content as detail, nr.sent_at,
                   '' as amount, '' as amount_change,
                   nr.is_mock as is_mock, '' as extra
            FROM notice_records nr JOIN households h ON nr.household_id = h.id
            WHERE 1=1 {room_where} {bld_where}
            {_limit_date(date_where_from, 'nr.created_at') if from_date else ''}
            {_limit_date(date_where_to, 'nr.created_at') if to_date else ''}
            ORDER BY nr.created_at DESC LIMIT ?
        """
        params = params_common[:]
        if from_date: params.append(from_date)
        if to_date: params.append(to_date)
        params.append(limit)
        for r in conn.execute(sql, params).fetchall():
            events.append(dict(r))

    if rtype in ("电话", "全部"):
        sql = f"""
            SELECT '电话' as event_type, cr.call_time as event_time, h.room_no, h.building,
                   h.owner_name, h.phone, '电话' as channel, cr.call_result as status,
                   COALESCE(cr.remark, '') as detail, '' as sent_at,
                   '' as amount, '' as amount_change,
                   NULL as is_mock,
                   CASE WHEN cr.commitment_date IS NOT NULL AND cr.commitment_date != ''
                        THEN '承诺:'||cr.commitment_date||
                             CASE WHEN cr.commitment_amount > 0 THEN ' '||printf('%.2f',cr.commitment_amount)||'元' ELSE '' END
                        ELSE '' END as extra
            FROM call_records cr JOIN households h ON cr.household_id = h.id
            WHERE 1=1 {room_where} {bld_where}
            {_limit_date(date_where_from, 'cr.call_time') if from_date else ''}
            {_limit_date(date_where_to, 'cr.call_time') if to_date else ''}
            ORDER BY cr.call_time DESC LIMIT ?
        """
        params = params_common[:]
        if from_date: params.append(from_date)
        if to_date: params.append(to_date)
        params.append(limit)
        for r in conn.execute(sql, params).fetchall():
            events.append(dict(r))

    if rtype in ("减免", "全部"):
        sql = f"""
            SELECT '费用减免' as event_type, dr.created_at as event_time, h.room_no, h.building,
                   h.owner_name, h.phone, '减免' as channel, '已审批' as status,
                   COALESCE(dr.discount_reason,'') || ' ' || COALESCE(dr.remark,'') as detail,
                   COALESCE(dr.approved_at,'') as sent_at,
                   printf('%.2f', dr.discount_amount) as amount,
                   printf('%.2f', -dr.discount_amount) as amount_change,
                   NULL as is_mock,
                   '审批人:'||COALESCE(dr.approved_by,'-') as extra
            FROM discount_records dr JOIN households h ON dr.household_id = h.id
            WHERE 1=1 {room_where} {bld_where}
            {_limit_date(date_where_from, 'dr.created_at') if from_date else ''}
            {_limit_date(date_where_to, 'dr.created_at') if to_date else ''}
            ORDER BY dr.created_at DESC LIMIT ?
        """
        params = params_common[:]
        if from_date: params.append(from_date)
        if to_date: params.append(to_date)
        params.append(limit)
        for r in conn.execute(sql, params).fetchall():
            events.append(dict(r))

    if rtype in ("缴费", "全部"):
        sql = f"""
            SELECT '缴费记录' as event_type, pr.created_at as event_time, h.room_no, h.building,
                   h.owner_name, h.phone, COALESCE(pr.pay_method,'银行') as channel, '已登记' as status,
                   COALESCE(pr.remark,'') as detail,
                   COALESCE(pr.pay_date,'') as sent_at,
                   printf('%.2f', pr.amount) as amount,
                   printf('%.2f', -pr.amount) as amount_change,
                   NULL as is_mock,
                   '经办人:'||COALESCE(pr.operator,'-') as extra
            FROM payment_records pr JOIN households h ON pr.household_id = h.id
            WHERE 1=1 {room_where} {bld_where}
            {_limit_date(date_where_from, 'pr.created_at') if from_date else ''}
            {_limit_date(date_where_to, 'pr.created_at') if to_date else ''}
            ORDER BY pr.created_at DESC LIMIT ?
        """
        params = params_common[:]
        if from_date: params.append(from_date)
        if to_date: params.append(to_date)
        params.append(limit)
        for r in conn.execute(sql, params).fetchall():
            events.append(dict(r))

    if rtype == "全部":
        sql = f"""
            SELECT '欠费产生' as event_type, a.created_at as event_time, h.room_no, h.building,
                   h.owner_name, h.phone, a.fee_type as channel, a.status as status,
                   '账期:'||a.fee_period||' 本金:'||printf('%.2f',a.base_amount) as detail,
                   a.due_date as sent_at,
                   printf('%.2f', a.base_amount + COALESCE(a.late_fee,0)) as amount,
                   printf('%.2f', a.base_amount + COALESCE(a.late_fee,0)) as amount_change,
                   NULL as is_mock,
                   '应缴日:'||COALESCE(a.due_date,'') as extra
            FROM arrears a JOIN households h ON a.household_id = h.id
            WHERE 1=1 {room_where} {bld_where}
            {_limit_date(date_where_from, 'a.created_at') if from_date else ''}
            {_limit_date(date_where_to, 'a.created_at') if to_date else ''}
            ORDER BY a.created_at DESC LIMIT ?
        """
        params = params_common[:]
        if from_date: params.append(from_date)
        if to_date: params.append(to_date)
        params.append(limit)
        for r in conn.execute(sql, params).fetchall():
            events.append(dict(r))

    conn.close()

    events.sort(key=lambda r: r["event_time"] or "", reverse=True)
    events = events[:limit]

    if not events:
        console.print("[yellow]未找到历史记录[/yellow]")
        return

    table = Table(title=f"催缴历史记录（完整时间线，共 {len(events)} 条）")
    table.add_column("时间", style="white", no_wrap=True)
    table.add_column("类型", style="cyan")
    table.add_column("金额变化", justify="right", style="yellow")
    table.add_column("真实/模拟", style="white")
    table.add_column("房号", style="green")
    table.add_column("业主", style="magenta")
    table.add_column("渠道", style="white")
    table.add_column("状态/结果", style="white")
    table.add_column("详情", style="white", max_width=40)
    table.add_column("补充", style="dim")

    type_colors = {
        "欠费产生": "red", "短信": "cyan", "电话": "blue",
        "费用减免": "magenta", "缴费记录": "green",
    }

    for r in events:
        name = mask_name(r["owner_name"]) if hide else r["owner_name"]
        phone_str = r.get("phone") or ""
        if hide and phone_str:
            phone_str = mask_phone(phone_str)
        detail = (r.get("detail") or "")[:36]
        if r.get("extra"):
            detail += " | " + str(r["extra"])[:20]
        rtype_style = type_colors.get(r["event_type"], "white")
        status = r.get("status") or "-"
        status_style = {"已发送": "green", "发送失败": "red", "已联系/承诺付款": "green",
                        "已审批": "magenta", "已登记": "green"}.get(status, "white")
        is_mock = r.get("is_mock")
        if is_mock is None:
            mock_label = "-"
            mock_style = "dim"
        else:
            mock_label = "模拟" if is_mock else "真实"
            mock_style = "yellow" if is_mock else "green"
        amt_change = r.get("amount_change") or ""
        amt_color = "green" if amt_change and str(amt_change).startswith("-") else ("red" if amt_change else "white")
        table.add_row(
            r["event_time"],
            f"[{rtype_style}]{r['event_type']}[/{rtype_style}]",
            f"[{amt_color}]{amt_change}[/{amt_color}]" if amt_change else "-",
            f"[{mock_style}]{mock_label}[/{mock_style}]",
            r["room_no"], name,
            r.get("channel") or "-",
            f"[{status_style}]{status}[/{status_style}]",
            detail + ("..." if len((r.get("detail") or "")) > 36 else ""),
            (r.get("sent_at") or "")[:10] if r.get("sent_at") else "",
        )
    console.print(table)

    if output:
        with open(output, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["时间", "事件类型", "金额", "金额变化", "真实/模拟", "房号", "楼栋", "业主",
                             "联系方式", "渠道", "状态", "详情", "补充信息"])
            for r in events:
                name = mask_name(r["owner_name"]) if hide else r["owner_name"]
                phone_str = r.get("phone") or ""
                if hide and phone_str:
                    phone_str = mask_phone(phone_str)
                is_mock = r.get("is_mock")
                mock_txt = "" if is_mock is None else ("模拟" if is_mock else "真实")
                writer.writerow([
                    r["event_time"], r["event_type"],
                    r.get("amount") or "", r.get("amount_change") or "",
                    mock_txt, r["room_no"], r.get("building") or "", name, phone_str,
                    r.get("channel") or "", r.get("status") or "",
                    r.get("detail") or "", r.get("extra") or "",
                ])
        console.print(f"[green]✓ 已导出到 {output}[/green]")


@report_cmd.command("dashboard", help="催缴工作总览仪表盘")
def dashboard():
    conn = get_connection()

    overview = conn.execute(f"""
        SELECT
            COUNT(DISTINCT h.id) as total_hh,
            COUNT(DISTINCT CASE WHEN {UNPAID_SQL_EXPR} > 0 THEN h.id END) as arrear_hh,
            COUNT(DISTINCT a.id) as total_arrears,
            COUNT(DISTINCT CASE WHEN a.status = '承诺付款' THEN a.id END) as cnt_commitment,
            COUNT(DISTINCT CASE WHEN a.status = '部分缴纳' THEN a.id END) as cnt_partial,
            COUNT(DISTINCT CASE WHEN a.status = '减免' THEN a.id END) as cnt_discount,
            COUNT(DISTINCT CASE WHEN a.status = '已缴' THEN a.id END) as cnt_paid,
            COALESCE(SUM(a.base_amount), 0) as sum_base,
            COALESCE(SUM(a.late_fee), 0) as sum_late,
            COALESCE(SUM(CASE WHEN {UNPAID_SQL_EXPR} > 0
                THEN {UNPAID_SQL_EXPR} END), 0) as sum_unpaid,
            COALESCE(SUM(a.paid_amount), 0) as sum_paid,
            COALESCE(SUM(a.discount_amount), 0) as sum_discount
        FROM households h LEFT JOIN arrears a ON h.id = a.household_id
    """).fetchone()

    notice_stats = conn.execute("""
        SELECT status, COUNT(*) as cnt FROM notice_records GROUP BY status
    """).fetchall()

    call_stats = conn.execute("""
        SELECT call_result, COUNT(*) as cnt FROM call_records GROUP BY call_result
    """).fetchall()

    top_arrears = conn.execute(f"""
        SELECT h.room_no, h.building, h.owner_name, h.phone,
               SUM({UNPAID_SQL_EXPR}) as total_unpaid,
               COUNT(a.id) as cnt
        FROM arrears a JOIN households h ON a.household_id = h.id
        WHERE {UNPAID_SQL_EXPR} > 0
        GROUP BY h.id
        ORDER BY total_unpaid DESC LIMIT 10
    """).fetchall()

    conn.close()

    hide = should_hide_sensitive()
    o = overview

    console.print("[bold magenta]===== 催缴工作总览 =====[/bold magenta]\n")

    col1 = Table(show_header=False, show_lines=False, box=None, padding=(0, 2))
    col1.add_column(style="bold")
    col1.add_column()
    col1.add_row("总户数", f"[blue]{o['total_hh']}[/blue] 户")
    col1.add_row("欠费户数", f"[yellow]{o['arrear_hh']}[/yellow] 户")
    ratio = f"{o['arrear_hh'] / o['total_hh'] * 100:.1f}%" if o['total_hh'] else "-"
    col1.add_row("欠费占比", ratio)
    col1.add_row("欠费笔数", f"{o['total_arrears']} 笔")

    col2 = Table(show_header=False, show_lines=False, box=None, padding=(0, 2))
    col2.add_column(style="bold")
    col2.add_column()
    col2.add_row("欠费本金", f"[cyan]{format_money(o['sum_base'])}[/cyan] 元")
    col2.add_row("滞纳金合计", f"[red]{format_money(o['sum_late'])}[/red] 元")
    col2.add_row("欠费总额", f"[bold red]{format_money(o['sum_unpaid'])}[/bold red] 元")
    col2.add_row("已缴合计", f"[green]{format_money(o['sum_paid'])}[/green] 元")
    col2.add_row("减免合计", f"[magenta]{format_money(o['sum_discount'])}[/magenta] 元")

    grid = Table.grid(padding=2)
    grid.add_column()
    grid.add_column()
    grid.add_row(col1, col2)
    console.print(grid)

    console.print("\n[bold]📱 短信发送统计:[/bold]")
    if notice_stats:
        table = Table(show_header=False, show_lines=False, box=None)
        for n in notice_stats:
            style = {"待发送": "cyan", "已发送": "green", "发送失败": "red"}.get(n["status"], "white")
            table.add_column()
            table.add_row(f"[{style}]{n['status']}: {n['cnt']}[/{style}]")
        console.print(table)
    else:
        console.print("  [dim]暂无数据[/dim]")

    console.print("\n[bold]📞 电话沟通统计:[/bold]")
    if call_stats:
        table = Table()
        table.add_column("沟通结果")
        table.add_column("次数", justify="right")
        for c in call_stats:
            style = {"已联系/承诺付款": "green", "已联系/拒缴": "red"}.get(c["call_result"], "white")
            table.add_row(f"[{style}]{c['call_result']}[/{style}]", str(c["cnt"]))
        console.print(table)
    else:
        console.print("  [dim]暂无数据[/dim]")

    if top_arrears:
        console.print("\n[bold]💰 欠费金额 TOP10:[/bold]")
        table = Table()
        table.add_column("排名", justify="right")
        table.add_column("房号", style="cyan")
        table.add_column("楼栋", style="green")
        table.add_column("业主", style="magenta")
        table.add_column("欠费笔数", justify="right")
        table.add_column("欠费总额", justify="right", style="bold red")
        for i, r in enumerate(top_arrears, 1):
            name = mask_name(r["owner_name"]) if hide else r["owner_name"]
            table.add_row(f"#{i}", r["room_no"], r["building"], name,
                          str(r["cnt"]), format_money(r["total_unpaid"]))
        console.print(table)


@report_cmd.command("file", help="单户催缴档案（按房号输出完整时间线）")
@click.argument("room")
@click.option("--output", "-o", type=click.Path(writable=True), help="导出CSV路径")
@click.option("--hide-sensitive/--show-sensitive", default=None)
@click.option("--encoding", default="utf-8-sig")
def household_file(room, output, hide_sensitive, encoding):
    hide = hide_sensitive if hide_sensitive is not None else should_hide_sensitive()
    conn = get_connection()

    hh = conn.execute("SELECT * FROM households WHERE room_no = ?", (room,)).fetchone()
    if not hh:
        conn.close()
        console.print(f"[red]未找到房号 [{room}] 的业主档案[/red]")
        return

    household_id = hh["id"]
    arrears_list = conn.execute(f"""
        SELECT a.*, {UNPAID_SQL_EXPR} as unpaid_amount
        FROM arrears a WHERE a.household_id = ? ORDER BY a.fee_period
    """, (household_id,)).fetchall()

    notice_list = conn.execute("""
        SELECT nr.*, a.fee_period FROM notice_records nr
        LEFT JOIN arrears a ON nr.arrear_id = a.id
        WHERE nr.household_id = ? ORDER BY nr.created_at
    """, (household_id,)).fetchall()

    call_list = conn.execute("""
        SELECT cr.*, a.fee_period FROM call_records cr
        LEFT JOIN arrears a ON cr.arrear_id = a.id
        WHERE cr.household_id = ? ORDER BY cr.call_time
    """, (household_id,)).fetchall()

    discount_list = conn.execute("""
        SELECT dr.*, a.fee_period FROM discount_records dr
        LEFT JOIN arrears a ON dr.arrear_id = a.id
        WHERE dr.household_id = ? ORDER BY dr.created_at
    """, (household_id,)).fetchall()

    payment_list = conn.execute("""
        SELECT pr.*, a.fee_period FROM payment_records pr
        LEFT JOIN arrears a ON pr.arrear_id = a.id
        WHERE pr.household_id = ? ORDER BY pr.created_at
    """, (household_id,)).fetchall()

    import json
    pending_info_by_id = {}
    for p in payment_list:
        if p.get("pending_id"):
            if p["pending_id"] not in pending_info_by_id:
                pp = conn.execute("""
                    SELECT pp.*, h.room_no FROM pending_payments pp
                    LEFT JOIN households h ON pp.matched_household_id = h.id
                    WHERE pp.id = ?
                """, (p["pending_id"],)).fetchone()
                info = {"pp": dict(pp) if pp else None, "splits": [], "matched_fee_period": "", "peer_count": 0}
                if pp:
                    if pp.get("split_to_ids"):
                        try:
                            splits = json.loads(pp["split_to_ids"])
                            for s in splits:
                                ar = conn.execute("SELECT fee_period FROM arrears WHERE id = ?", (s["arrear_id"],)).fetchone()
                                if ar:
                                    info["splits"].append({"fee_period": ar["fee_period"], "amount": s["amount"]})
                        except Exception:
                            pass
                    elif pp.get("matched_arrear_id"):
                        ar = conn.execute("SELECT fee_period FROM arrears WHERE id = ?", (pp["matched_arrear_id"],)).fetchone()
                        if ar:
                            info["matched_fee_period"] = ar["fee_period"]
                        peers = conn.execute("SELECT COUNT(*) FROM pending_payments WHERE matched_arrear_id = ? AND id != ? AND matched_arrear_id IS NOT NULL",
                                             (pp["matched_arrear_id"] or -1, pp["id"])).fetchone()[0]
                        info["peer_count"] = peers
                pending_info_by_id[p["pending_id"]] = info

    conn.close()

    total_base = sum(r["base_amount"] for r in arrears_list)
    total_late = sum(r["late_fee"] for r in arrears_list)
    total_paid = sum(r["paid_amount"] for r in arrears_list)
    total_discount = sum(r["discount_amount"] for r in arrears_list)
    total_unpaid = sum(calc_unpaid(r["base_amount"], r["late_fee"], r["paid_amount"], r["discount_amount"]) for r in arrears_list)

    owner_name = mask_name(hh["owner_name"]) if hide else hh["owner_name"]
    phone = mask_phone(hh["phone"]) if hide else (hh["phone"] or "-")

    console.print(f"[bold magenta]===== 单户催缴档案：{hh['room_no']} =====[/bold magenta]\n")

    header_table = Table(show_header=False, show_lines=False, box=None, padding=(0, 3))
    header_table.add_column(style="bold cyan")
    header_table.add_column()
    header_table.add_column(style="bold cyan")
    header_table.add_column()
    header_table.add_row("房号", f"[green]{hh['room_no']}[/green]", "楼栋", hh["building"] or "-")
    header_table.add_row("业主", owner_name, "联系电话", phone)
    header_table.add_row("建筑面积", f"{hh['area'] or 0:.2f} ㎡", "物业类型", hh["property_type"] or "-")
    if hh["unit"]:
        header_table.add_row("单元", str(hh["unit"]), "楼层", str(hh["floor"]) if hh["floor"] else "-")
    console.print(header_table)

    console.print("\n[bold]📊 欠费汇总:[/bold]")
    sum_table = Table(show_header=False, show_lines=False, box=None, padding=(0, 3))
    sum_table.add_column(style="bold")
    sum_table.add_column()
    sum_table.add_column(style="bold")
    sum_table.add_column()
    sum_table.add_row("总本金", f"[cyan]{format_money(total_base)} 元[/cyan]",
                      "总滞纳金", f"[red]{format_money(total_late)} 元[/red]")
    sum_table.add_row("总已缴", f"[green]{format_money(total_paid)} 元[/green]",
                      "总减免", f"[magenta]{format_money(total_discount)} 元[/magenta]")
    sum_table.add_row("[bold red]总尚欠[/bold red]",
                      f"[bold red]{format_money(total_unpaid)} 元[/bold red]",
                      "欠费笔数", f"{len(arrears_list)} 笔")
    console.print(sum_table)

    if arrears_list:
        console.print("\n[bold]📋 各账期明细:[/bold]")
        period_table = Table()
        period_table.add_column("账期", style="cyan")
        period_table.add_column("费用类型")
        period_table.add_column("本金", justify="right", style="cyan")
        period_table.add_column("滞纳金", justify="right", style="red")
        period_table.add_column("已缴", justify="right", style="green")
        period_table.add_column("减免", justify="right", style="magenta")
        period_table.add_column("尚欠", justify="right", style="bold red")
        period_table.add_column("应缴日")
        period_table.add_column("状态", style="yellow")
        for a in arrears_list:
            unpaid = calc_unpaid(a["base_amount"], a["late_fee"], a["paid_amount"], a["discount_amount"])
            period_table.add_row(
                a["fee_period"], a["fee_type"] or "-",
                format_money(a["base_amount"]), format_money(a["late_fee"]),
                format_money(a["paid_amount"]), format_money(a["discount_amount"]),
                format_money(unpaid), a["due_date"], a["status"] or "-"
            )
        console.print(period_table)

    events = []
    for a in arrears_list:
        unpaid = calc_unpaid(a["base_amount"], a["late_fee"], a["paid_amount"], a["discount_amount"])
        detail = f"账期:{a['fee_period']} 本金:{format_money(a['base_amount'])}元 应缴日:{a['due_date']}"
        amount_change = f"+{format_money(a['base_amount'] + a['late_fee'])}"
        events.append({
            "time": a["created_at"],
            "type": "欠费产生",
            "type_style": "bold red",
            "detail": detail,
            "amount_change": amount_change,
            "export_detail": detail,
            "export_amount": a["base_amount"] + a["late_fee"],
            "fee_period": a["fee_period"],
        })

    for n in notice_list:
        mock_label = "模拟" if n["is_mock"] else "真实"
        content_preview = (n["content"] or "")[:30]
        phone_display = mask_phone(n["phone"]) if hide else (n["phone"] or "-")
        detail = f"状态:{n['status']} [{mock_label}] 手机号:{phone_display} 内容:{content_preview}"
        events.append({
            "time": n["created_at"],
            "type": "短信通知",
            "type_style": "cyan",
            "detail": detail,
            "amount_change": "-",
            "export_detail": f"状态:{n['status']} 是否模拟:{mock_label} 手机号:{n['phone'] or ''} 内容:{n['content'] or ''}",
            "export_amount": "",
            "fee_period": n["fee_period"] or "",
            "mock": mock_label,
        })

    for c in call_list:
        detail_parts = [f"结果:{c['call_result'] or '-'}"]
        if c["commitment_date"]:
            detail_parts.append(f"承诺日:{c['commitment_date']}")
        if c["commitment_amount"] and c["commitment_amount"] > 0:
            detail_parts.append(f"承诺金额:{format_money(c['commitment_amount'])}元")
        if c["remark"]:
            detail_parts.append(f"备注:{c['remark'][:20]}")
        detail = " ".join(detail_parts)
        events.append({
            "time": c["call_time"],
            "type": "电话沟通",
            "type_style": "blue",
            "detail": detail,
            "amount_change": "-",
            "export_detail": f"结果:{c['call_result'] or ''} 承诺日期:{c['commitment_date'] or ''} 承诺金额:{c['commitment_amount'] or 0} 备注:{c['remark'] or ''}",
            "export_amount": "",
            "fee_period": c["fee_period"] or "",
            "mock": "-",
        })

    for d in discount_list:
        detail_parts = [f"金额:{format_money(d['discount_amount'])}元"]
        if d["discount_reason"]:
            detail_parts.append(f"原因:{d['discount_reason']}")
        if d["approved_by"]:
            detail_parts.append(f"审批人:{d['approved_by']}")
        detail = " ".join(detail_parts)
        events.append({
            "time": d["created_at"],
            "type": "费用减免",
            "type_style": "magenta",
            "detail": detail,
            "amount_change": f"-{format_money(d['discount_amount'])}",
            "export_detail": f"减免金额:{d['discount_amount']} 原因:{d['discount_reason'] or ''} 审批人:{d['approved_by'] or ''} 备注:{d['remark'] or ''}",
            "export_amount": -float(d["discount_amount"] or 0),
            "fee_period": d["fee_period"] or "",
            "mock": "-",
        })

    import json
    for p in payment_list:
        detail_parts = [f"金额:{format_money(p['amount'])}元"]
        if p["pay_method"]:
            detail_parts.append(f"方式:{p['pay_method']}")
        if p["pay_date"]:
            detail_parts.append(f"日期:{p['pay_date']}")
        if p["operator"]:
            detail_parts.append(f"经办人:{p['operator']}")
        related_periods = []
        if p.get("pending_id") and p["pending_id"] in pending_info_by_id:
            info = pending_info_by_id[p["pending_id"]]
            pp = info["pp"]
            if pp:
                detail_parts.append(f"流水#{pp['id']}")
                if pp.get("payer_name"):
                    detail_parts.append(f"付款人:{pp['payer_name']}")
            if info["splits"]:
                related_periods = [f"{s['fee_period']}({format_money(s['amount'])})" for s in info["splits"]]
            elif info["matched_fee_period"] and pp:
                related_periods = [f"{info['matched_fee_period']}({format_money(float(pp.get('amount') or 0))})"]
            if info["peer_count"] > 0:
                detail_parts.append(f"[与{info['peer_count']}笔流水合缴]")
        detail = " ".join(detail_parts)
        events.append({
            "time": p["created_at"],
            "type": "缴费记录",
            "type_style": "green",
            "detail": detail,
            "amount_change": f"-{format_money(p['amount'])}",
            "related_periods": " / ".join(related_periods),
            "export_detail": f"缴费金额:{p['amount']} 方式:{p['pay_method'] or ''} 缴费日期:{p['pay_date'] or ''} 经办人:{p['operator'] or ''} 备注:{p['remark'] or ''} 关联账期:{';'.join(related_periods)}",
            "export_amount": -float(p["amount"] or 0),
            "fee_period": p["fee_period"] or "",
            "mock": "-",
        })

    events.sort(key=lambda e: e["time"] or "")

    console.print(f"\n[bold]⏰ 事件时间线（共 {len(events)} 条）:[/bold]")
    timeline = Table()
    timeline.add_column("时间", style="white", no_wrap=True)
    timeline.add_column("类型", style="white")
    timeline.add_column("详情", style="white")
    timeline.add_column("关联账期/拆分", style="cyan", max_width=30)
    timeline.add_column("金额变化", justify="right", style="white")
    for e in events:
        timeline.add_row(
            e["time"],
            f"[{e['type_style']}]{e['type']}[/{e['type_style']}]",
            e["detail"],
            e.get("related_periods", "") or "-",
            e["amount_change"],
        )
    console.print(timeline)

    if output:
        with open(output, "w", encoding=encoding, newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["=== 单户催缴档案 ==="])
            writer.writerow(["房号", "楼栋", "单元", "楼层", "业主姓名", "联系电话", "建筑面积(㎡)", "物业类型"])
            writer.writerow([
                hh["room_no"], hh["building"] or "", hh["unit"] or "", hh["floor"] or "",
                owner_name, phone, hh["area"] or "", hh["property_type"] or ""
            ])
            writer.writerow([])
            writer.writerow(["=== 欠费汇总 ==="])
            writer.writerow(["总本金", "总滞纳金", "总已缴", "总减免", "总尚欠", "欠费笔数"])
            writer.writerow([
                f"{total_base:.2f}", f"{total_late:.2f}", f"{total_paid:.2f}",
                f"{total_discount:.2f}", f"{total_unpaid:.2f}", len(arrears_list)
            ])
            writer.writerow([])
            writer.writerow(["=== 账期明细 ==="])
            writer.writerow([
                "账期", "费用类型", "本金", "滞纳金", "已缴", "减免", "尚欠", "应缴日", "状态"
            ])
            for a in arrears_list:
                unpaid = calc_unpaid(a["base_amount"], a["late_fee"], a["paid_amount"], a["discount_amount"])
                writer.writerow([
                    a["fee_period"], a["fee_type"] or "",
                    f"{a['base_amount']:.2f}", f"{a['late_fee']:.2f}",
                    f"{a['paid_amount']:.2f}", f"{a['discount_amount']:.2f}",
                    f"{unpaid:.2f}", a["due_date"], a["status"] or ""
                ])
            writer.writerow([])
            writer.writerow(["=== 事件时间线 ==="])
            writer.writerow([
                "时间", "类型", "真实/模拟", "关联账期", "详情", "金额变化"
            ])
            for e in events:
                writer.writerow([
                    e["time"], e["type"], e.get("mock", "-"),
                    e.get("fee_period", ""), e["export_detail"], e["export_amount"]
                ])
        console.print(f"\n[green]单户催缴档案已导出到 {output}[/green]")


@report_cmd.command("monthly", help="月结账期报表：原始金额 + 调整金额 = 最终余额")
@click.option("--period", "-p", required=True, help="账期，如 2026-05")
@click.option("--building", "-b", default=None, help="按楼栋筛选")
@click.option("--output", "-o", type=click.Path(writable=True), help="导出CSV")
@click.option("--encoding", default="utf-8-sig")
def report_monthly(period: str, building: Optional[str], output: Optional[str], encoding: str):
    conn = get_connection()
    try:
        snap_rows = conn.execute("""
            SELECT * FROM settlement_snapshots
            WHERE period = ? AND COALESCE(building,'') LIKE COALESCE(?, '')
            ORDER BY snapshot_time DESC
        """, (period, building or "%")).fetchall()
        if not snap_rows:
            console.print(f"[yellow]账期 {period}{(' '+building) if building else ''} 尚无归档快照，请先 archive create[/yellow]")
            return
        snap = snap_rows[0]
        snap_id = snap["id"]
        items = conn.execute("""
            SELECT si.*, a.base_amount as cur_base, a.late_fee as cur_late,
                   a.paid_amount as cur_paid, a.discount_amount as cur_disc,
                   a.status as cur_status,
                   (COALESCE(a.base_amount,0)+COALESCE(a.late_fee,0)-COALESCE(a.paid_amount,0)-COALESCE(a.discount_amount,0)) as cur_unpaid,
                   h.building
            FROM settlement_items si
            LEFT JOIN arrears a ON si.arrear_id = a.id
            LEFT JOIN households h ON si.household_id = h.id
            WHERE si.snapshot_id = ?
            ORDER BY h.building, si.room_no, si.fee_period
        """, (snap_id,)).fetchall()

        ar_adj = conn.execute("""
            SELECT ar.arrear_id,
                   SUM(CASE WHEN ar.adjust_type='实际缴费' THEN ar.adjust_amount ELSE 0 END) as pay_adj,
                   SUM(CASE WHEN ar.adjust_type='费用减免' THEN ar.adjust_amount ELSE 0 END) as disc_adj,
                   SUM(CASE WHEN ar.adjust_type='滞纳金调整' THEN ar.adjust_amount ELSE 0 END) as late_adj,
                   SUM(ar.adjust_amount) as total_adj,
                   COUNT(*) as adj_count
            FROM adjustment_records ar
            WHERE ar.snapshot_id = ?
            GROUP BY ar.arrear_id
        """, (snap_id,)).fetchall()
        adj_map = {r["arrear_id"]: dict(r) for r in ar_adj}

        console.print(Panel(
            f"[bold]归档编号:[/bold] {snap['snapshot_no']}    [bold]账期:[/bold] {period}    [bold]楼栋:[/bold] {building or '全部'}\n"
            f"[bold]归档时间:[/bold] {snap['snapshot_time']}    [bold]操作人:[/bold] {snap['created_by']}\n"
            f"[bold]归档住户:[/bold] {snap['total_households']} 户    [bold]欠费笔数:[/bold] {snap['total_arrears']} 笔\n"
            f"[bold]尚欠合计(归档):[/bold] {format_money(snap['snap_unpaid_amount'])} 元    [bold]备注:[/bold] {snap['description'] or '-'}",
            title=f"月结账期报表：{period}", style="cyan",
        ))

        t = Table(title=f"明细（共{len(items)}条，显示前30条）")
        t.add_column("楼栋", style="magenta")
        t.add_column("房号", style="cyan")
        t.add_column("业主", style="white")
        t.add_column("账期", style="white")
        t.add_column("归档尚欠", justify="right", style="blue")
        t.add_column("缴费调整", justify="right", style="green")
        t.add_column("减免调整", justify="right", style="magenta")
        t.add_column("滞纳金调整", justify="right", style="yellow")
        t.add_column("最终余额", justify="right", style="bold red")
        t.add_column("当前状态", style="white")

        csv_rows = []
        sum_snap = 0.0
        sum_pay_adj = 0.0
        sum_disc_adj = 0.0
        sum_late_adj = 0.0
        sum_final = 0.0
        for it in items:
            arid = it["arrear_id"]
            adj = adj_map.get(arid, {"pay_adj": 0, "disc_adj": 0, "late_adj": 0, "total_adj": 0})
            snap_u = float(it["unpaid_amount"] or 0)
            cur_u = float(it["cur_unpaid"] or 0) if it["cur_unpaid"] is not None else snap_u
            pay_a = float(adj["pay_adj"] or 0)
            disc_a = float(adj["disc_adj"] or 0)
            late_a = float(adj["late_adj"] or 0)
            final = round(snap_u + pay_a + disc_a + late_a, 2)
            sum_snap += snap_u
            sum_pay_adj += pay_a
            sum_disc_adj += disc_a
            sum_late_adj += late_a
            sum_final += final

            t.add_row(
                it["building"] or "-", it["room_no"], it["owner_name"] or "", it["fee_period"],
                format_money(snap_u), format_money(-pay_a), format_money(-disc_a),
                format_money(late_a), format_money(final),
                (it["cur_status"] or it["status"] or "")[:8],
            )
            csv_rows.append({
                "楼栋": it["building"] or "", "房号": it["room_no"], "业主": it["owner_name"] or "",
                "账期": it["fee_period"],
                "归档本金": f"{it['base_amount']:.2f}", "归档滞纳金": f"{it['late_fee']:.2f}",
                "归档已缴": f"{it['paid_amount']:.2f}", "归档减免": f"{it['discount_amount']:.2f}",
                "归档尚欠": f"{snap_u:.2f}",
                "缴费调整": f"{-pay_a:.2f}", "减免调整": f"{-disc_a:.2f}", "滞纳金调整": f"{late_a:.2f}",
                "最终余额": f"{final:.2f}", "当前余额": f"{cur_u:.2f}",
                "归档状态": it["status"] or "", "当前状态": it["cur_status"] or "",
            })
        console.print(t)
        if len(items) > 30:
            console.print(f"[dim]剩余{len(items)-30}条，请加 --output 导出完整数据[/dim]")

        footer = Table(show_header=False, box=None, padding=(0, 3))
        footer.add_column(style="bold")
        footer.add_column()
        footer.add_row("归档尚欠合计", format_money(sum_snap) + " 元")
        footer.add_row("缴费调整合计", f"[green]{format_money(-sum_pay_adj)}[/green] 元")
        footer.add_row("减免调整合计", f"[magenta]{format_money(-sum_disc_adj)}[/magenta] 元")
        footer.add_row("滞纳金调整合计", f"[yellow]{format_money(sum_late_adj)}[/yellow] 元")
        footer.add_row("最终余额合计", f"[bold red]{format_money(sum_final)}[/bold red] 元")
        console.print()
        console.print(footer)

        if output:
            with open(output, "w", encoding=encoding, newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
                writer.writeheader()
                writer.writerows(csv_rows)
            console.print(f"\n[green]月报已导出: {output} ({len(csv_rows)}条)[/green]")
    finally:
        conn.close()

