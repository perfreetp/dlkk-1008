import click
import csv
import os
from datetime import datetime, date
from rich.console import Console
from rich.table import Table
from typing import Optional

from ..database import get_connection
from ..utils import mask_phone, mask_name, should_hide_sensitive, format_money

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
        sql += " AND (a.total_amount - a.paid_amount - a.discount_amount) > 0"
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
            unpaid = r["total_amount"] - r["paid_amount"] - r["discount_amount"]
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

    total_unpaid = sum(r["total_amount"] - r["paid_amount"] - r["discount_amount"] for r in rows)
    console.print(f"[green]已导出 {len(rows)} 条记录到 {output}[/green]")
    console.print(f"[dim]尚欠合计: {format_money(total_unpaid)} 元，敏感信息隐藏: {'是' if hide else '否'}[/dim]")


@report_cmd.command("building", help="生成楼栋催缴汇总表")
@click.option("--output", "-o", type=click.Path(writable=True), help="导出CSV路径")
@click.option("--encoding", default="utf-8-sig")
def building_summary(output, encoding):
    conn = get_connection()
    rows = conn.execute("""
        SELECT
            h.building,
            COUNT(DISTINCT h.id) as total_households,
            COUNT(DISTINCT CASE WHEN (a.total_amount - a.paid_amount - a.discount_amount) > 0 THEN h.id END) as arrear_households,
            COUNT(DISTINCT a.id) as total_arrears,
            COUNT(DISTINCT CASE WHEN a.status = '承诺付款' THEN a.id END) as commitment_count,
            COUNT(DISTINCT CASE WHEN a.status = '减免' THEN a.id END) as discount_count,
            COALESCE(SUM(a.base_amount), 0) as total_base,
            COALESCE(SUM(a.late_fee), 0) as total_late,
            COALESCE(SUM(CASE WHEN (a.total_amount - a.paid_amount - a.discount_amount) > 0
                THEN a.base_amount END), 0) as unpaid_base,
            COALESCE(SUM(CASE WHEN (a.total_amount - a.paid_amount - a.discount_amount) > 0
                THEN a.late_fee END), 0) as unpaid_late,
            COALESCE(SUM(a.total_amount - a.paid_amount - a.discount_amount), 0) as total_unpaid,
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


@report_cmd.command("history", help="查询历史催缴记录")
@click.option("--room", "-r", "room_no", help="按房号查询")
@click.option("--building", "-b", help="按楼栋查询")
@click.option("--from-date", help="起始日期 YYYY-MM-DD")
@click.option("--to-date", help="结束日期 YYYY-MM-DD")
@click.option("--type", "rtype", type=click.Choice(["短信", "电话", "全部"]), default="全部", help="记录类型")
@click.option("--output", "-o", type=click.Path(writable=True), help="导出CSV")
@click.option("--limit", "-n", type=int, default=100, help="显示条数")
@click.option("--hide-sensitive/--show-sensitive", default=None)
def query_history(room_no, building, from_date, to_date, rtype, output, limit, hide_sensitive):
    hide = hide_sensitive if hide_sensitive is not None else should_hide_sensitive()
    conn = get_connection()

    notice_rows, call_rows = [], []

    if rtype in ("短信", "全部"):
        sql = """
            SELECT '短信' as rtype, nr.created_at as event_time, h.room_no, h.building,
                   h.owner_name, nr.phone, nr.channel, nr.status,
                   nr.content as detail, nr.sent_at, '' as extra1, '' as extra2
            FROM notice_records nr JOIN households h ON nr.household_id = h.id
            WHERE 1=1
        """
        params = []
        if room_no:
            sql += " AND h.room_no LIKE ?"
            params.append(f"%{room_no}%")
        if building:
            sql += " AND h.building LIKE ?"
            params.append(f"%{building}%")
        if from_date:
            sql += " AND DATE(nr.created_at) >= ?"
            params.append(from_date)
        if to_date:
            sql += " AND DATE(nr.created_at) <= ?"
            params.append(to_date)
        sql += " ORDER BY nr.created_at DESC LIMIT ?"
        params.append(limit)
        notice_rows = conn.execute(sql, params).fetchall()

    if rtype in ("电话", "全部"):
        sql = """
            SELECT '电话' as rtype, cr.call_time as event_time, h.room_no, h.building,
                   h.owner_name, h.phone, '电话' as channel, cr.call_result as status,
                   cr.remark as detail, '' as sent_at,
                   COALESCE(cr.commitment_date, '') as extra1,
                   CASE WHEN cr.commitment_amount > 0 THEN printf('%.2f', cr.commitment_amount) ELSE '' END as extra2
            FROM call_records cr JOIN households h ON cr.household_id = h.id
            WHERE 1=1
        """
        params = []
        if room_no:
            sql += " AND h.room_no LIKE ?"
            params.append(f"%{room_no}%")
        if building:
            sql += " AND h.building LIKE ?"
            params.append(f"%{building}%")
        if from_date:
            sql += " AND DATE(cr.call_time) >= ?"
            params.append(from_date)
        if to_date:
            sql += " AND DATE(cr.call_time) <= ?"
            params.append(to_date)
        sql += " ORDER BY cr.call_time DESC LIMIT ?"
        params.append(limit)
        call_rows = conn.execute(sql, params).fetchall()

    conn.close()

    all_rows = list(notice_rows) + list(call_rows)
    all_rows.sort(key=lambda r: r["event_time"], reverse=True)
    all_rows = all_rows[:limit]

    if not all_rows:
        console.print("[yellow]未找到历史记录[/yellow]")
        return

    table = Table(title=f"催缴历史记录（共 {len(all_rows)} 条）")
    table.add_column("时间", style="white", no_wrap=True)
    table.add_column("类型", style="cyan")
    table.add_column("房号", style="green")
    table.add_column("业主", style="magenta")
    table.add_column("联系方式", style="yellow")
    table.add_column("状态/结果", style="white")
    table.add_column("详情", style="white")

    for r in all_rows:
        name = mask_name(r["owner_name"]) if hide else r["owner_name"]
        phone = mask_phone(r["phone"]) if hide else (r["phone"] or "-")
        detail = (r["detail"] or "")[:40]
        if r["rtype"] == "电话" and r["extra1"]:
            detail += f" | 承诺:{r['extra1']}"
            if r["extra2"]:
                detail += f" {r['extra2']}元"
        rtype_style = "cyan" if r["rtype"] == "短信" else "blue"
        status_style = {"已发送": "green", "发送失败": "red"}.get(r["status"], "white")
        table.add_row(
            r["event_time"], f"[{rtype_style}]{r['rtype']}[/{rtype_style}]",
            r["room_no"], name, phone,
            f"[{status_style}]{r['status']}[/{status_style}]",
            detail + ("..." if len(r["detail"] or "") > 40 else ""),
        )
    console.print(table)

    if output:
        with open(output, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["时间", "类型", "房号", "楼栋", "业主", "联系方式", "状态", "详情", "承诺日期", "承诺金额"])
            for r in all_rows:
                name = mask_name(r["owner_name"]) if hide else r["owner_name"]
                phone = mask_phone(r["phone"]) if hide else (r["phone"] or "")
                writer.writerow([
                    r["event_time"], r["rtype"], r["room_no"], r["building"], name, phone,
                    r["status"], r["detail"] or "", r["extra1"] or "", r["extra2"] or "",
                ])
        console.print(f"\n[green]历史记录已导出到 {output}[/green]")


@report_cmd.command("dashboard", help="催缴工作总览仪表盘")
def dashboard():
    conn = get_connection()

    overview = conn.execute("""
        SELECT
            COUNT(DISTINCT h.id) as total_hh,
            COUNT(DISTINCT CASE WHEN (a.total_amount - a.paid_amount - a.discount_amount) > 0 THEN h.id END) as arrear_hh,
            COUNT(DISTINCT a.id) as total_arrears,
            COUNT(DISTINCT CASE WHEN a.status = '承诺付款' THEN a.id END) as cnt_commitment,
            COUNT(DISTINCT CASE WHEN a.status = '部分缴纳' THEN a.id END) as cnt_partial,
            COUNT(DISTINCT CASE WHEN a.status = '减免' THEN a.id END) as cnt_discount,
            COUNT(DISTINCT CASE WHEN a.status = '已缴' THEN a.id END) as cnt_paid,
            COALESCE(SUM(a.base_amount), 0) as sum_base,
            COALESCE(SUM(a.late_fee), 0) as sum_late,
            COALESCE(SUM(CASE WHEN (a.total_amount - a.paid_amount - a.discount_amount) > 0
                THEN (a.total_amount - a.paid_amount - a.discount_amount) END), 0) as sum_unpaid,
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

    top_arrears = conn.execute("""
        SELECT h.room_no, h.building, h.owner_name, h.phone,
               SUM(a.total_amount - a.paid_amount - a.discount_amount) as total_unpaid,
               COUNT(a.id) as cnt
        FROM arrears a JOIN households h ON a.household_id = h.id
        WHERE (a.total_amount - a.paid_amount - a.discount_amount) > 0
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
