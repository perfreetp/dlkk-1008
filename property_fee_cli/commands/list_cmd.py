import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from typing import Optional

from ..database import get_connection
from ..utils import mask_phone, mask_name, should_hide_sensitive, format_money

console = Console()


@click.group(help="查看和管理住户欠费信息")
def list_cmd():
    pass


@list_cmd.command("arrears", help="查看欠费明细，支持按楼栋筛选")
@click.option("--building", "-b", "building", help="按楼栋筛选，支持模糊匹配")
@click.option("--unit", "-u", "unit", help="按单元筛选")
@click.option("--floor", "-f", "floor", type=int, help="按楼层筛选")
@click.option("--room", "-r", "room", help="按房号筛选，支持模糊匹配")
@click.option("--owner", "-o", "owner", help="按业主姓名筛选，支持模糊匹配")
@click.option("--status", "-s", "status", type=click.Choice(["未缴", "部分缴纳", "已缴", "承诺付款", "减免"]), help="按缴费状态筛选")
@click.option("--period", "-p", "period", help="按账期筛选")
@click.option("--min-amount", type=float, help="最低欠费金额")
@click.option("--max-amount", type=float, help="最高欠费金额")
@click.option("--unpaid-only", is_flag=True, help="仅显示未缴清的欠费")
@click.option("--hide-sensitive/--show-sensitive", default=None, help="是否隐藏敏感信息（手机号、姓名）")
@click.option("--detail", "-d", is_flag=True, help="显示详细信息")
@click.option("--limit", "-n", type=int, help="限制显示条数")
@click.option("--order-by", type=click.Choice(["amount", "due_date", "building", "room"]), default="building", help="排序方式")
def list_arrears(building, unit, floor, room, owner, status, period, min_amount, max_amount,
                 unpaid_only, hide_sensitive, detail, limit, order_by):
    hide = hide_sensitive if hide_sensitive is not None else should_hide_sensitive()

    sql_parts = [
        "SELECT a.id, a.household_id, a.fee_period, a.fee_type, a.base_amount, a.late_fee,",
        "       a.total_amount, a.due_date, a.paid_amount, a.discount_amount, a.status, a.remark,",
        "       h.room_no, h.building, h.unit, h.floor, h.owner_name, h.phone, h.area",
        "FROM arrears a JOIN households h ON a.household_id = h.id",
        "WHERE 1=1",
    ]
    params = []

    if building:
        sql_parts.append("AND h.building LIKE ?")
        params.append(f"%{building}%")
    if unit:
        sql_parts.append("AND h.unit = ?")
        params.append(unit)
    if floor:
        sql_parts.append("AND h.floor = ?")
        params.append(floor)
    if room:
        sql_parts.append("AND h.room_no LIKE ?")
        params.append(f"%{room}%")
    if owner:
        sql_parts.append("AND h.owner_name LIKE ?")
        params.append(f"%{owner}%")
    if status:
        sql_parts.append("AND a.status = ?")
        params.append(status)
    if period:
        sql_parts.append("AND a.fee_period LIKE ?")
        params.append(f"%{period}%")
    if min_amount:
        sql_parts.append("AND (a.total_amount - a.paid_amount - a.discount_amount) >= ?")
        params.append(min_amount)
    if max_amount:
        sql_parts.append("AND (a.total_amount - a.paid_amount - a.discount_amount) <= ?")
        params.append(max_amount)
    if unpaid_only:
        sql_parts.append("AND (a.total_amount - a.paid_amount - a.discount_amount) > 0")

    order_map = {
        "amount": "(a.total_amount - a.paid_amount - a.discount_amount) DESC",
        "due_date": "a.due_date ASC",
        "building": "h.building ASC, h.unit ASC, h.floor ASC, h.room_no ASC",
        "room": "h.room_no ASC",
    }
    sql_parts.append(f"ORDER BY {order_map[order_by]}")

    if limit:
        sql_parts.append(f"LIMIT {int(limit)}")

    conn = get_connection()
    rows = conn.execute(" ".join(sql_parts), params).fetchall()
    conn.close()

    if not rows:
        console.print("[yellow]未找到匹配的欠费记录[/yellow]")
        return

    total_unpaid = sum((r["total_amount"] - r["paid_amount"] - r["discount_amount"]) for r in rows)
    total_base = sum(r["base_amount"] for r in rows)
    total_late = sum(r["late_fee"] for r in rows)

    if detail:
        for r in rows:
            display_name = mask_name(r["owner_name"]) if hide else r["owner_name"]
            display_phone = mask_phone(r["phone"]) if hide else (r["phone"] or "-")
            unpaid = r["total_amount"] - r["paid_amount"] - r["discount_amount"]

            content = f"""
[bold cyan]房号:[/bold cyan] {r['room_no']}  ({r['building']} {r['unit'] or ''} {str(r['floor']) + '层' if r['floor'] else ''})
[bold magenta]业主:[/bold magenta] {display_name}  [bold yellow]电话:[/bold yellow] {display_phone}
[bold green]账期:[/bold green] {r['fee_period']}  [bold]类型:[/bold] {r['fee_type']}
[bold blue]本金:[/bold blue] {format_money(r['base_amount'])}元  [bold red]滞纳金:[/bold red] {format_money(r['late_fee'])}元
[bold]已缴:[/bold] {format_money(r['paid_amount'])}元  [bold]减免:[/bold] {format_money(r['discount_amount'])}元
[bold]应缴总额:[/bold] {format_money(r['total_amount'])}元  [bold red]尚欠:[/bold red] [bold underline]{format_money(unpaid)}元[/bold underline]
[bold]应缴日:[/bold] {r['due_date']}  [bold]状态:[/bold] {r['status']}
[bold]面积:[/bold] {r['area']}㎡  [bold]备注:[/bold] {r['remark'] or '-'}"
"""
            console.print(Panel(content.strip(), border_style="blue", expand=False))
            console.print()
    else:
        table = Table(title=f"欠费明细（共 {len(rows)} 条，尚欠合计 {format_money(total_unpaid)} 元）")
        table.add_column("房号", style="cyan", no_wrap=True)
        table.add_column("楼栋", style="green")
        table.add_column("业主", style="magenta")
        table.add_column("电话", style="yellow")
        table.add_column("账期", style="white")
        table.add_column("本金", justify="right", style="blue")
        table.add_column("滞纳金", justify="right", style="red")
        table.add_column("尚欠", justify="right", style="bold red")
        table.add_column("应缴日", style="white")
        table.add_column("状态", style="white")

        for r in rows:
            unpaid = r["total_amount"] - r["paid_amount"] - r["discount_amount"]
            display_name = mask_name(r["owner_name"]) if hide else r["owner_name"]
            display_phone = mask_phone(r["phone"]) if hide else (r["phone"] or "-")
            status_style = {
                "未缴": "red",
                "部分缴纳": "yellow",
                "已缴": "green",
                "承诺付款": "blue",
                "减免": "magenta",
            }.get(r["status"], "white")
            table.add_row(
                r["room_no"], r["building"], display_name, display_phone,
                r["fee_period"],
                format_money(r["base_amount"]),
                format_money(r["late_fee"]),
                format_money(unpaid),
                r["due_date"],
                f"[{status_style}]{r['status']}[/{status_style}]",
            )
        console.print(table)

    console.print(f"\n[dim]统计：本金合计 {format_money(total_base)} 元，滞纳金合计 {format_money(total_late)} 元[/dim]")


@list_cmd.command("households", help="查看住户列表")
@click.option("--building", "-b", help="按楼栋筛选")
@click.option("--has-arrears", is_flag=True, help="仅显示有欠费的住户")
@click.option("--hide-sensitive/--show-sensitive", default=None)
def list_households(building, has_arrears, hide_sensitive):
    hide = hide_sensitive if hide_sensitive is not None else should_hide_sensitive()
    conn = get_connection()
    sql = """
        SELECT h.*,
               COALESCE(SUM(a.total_amount - a.paid_amount - a.discount_amount), 0) as total_unpaid,
               COUNT(a.id) as arrear_count
        FROM households h
        LEFT JOIN arrears a ON h.id = a.household_id AND (a.total_amount - a.paid_amount - a.discount_amount) > 0
        WHERE 1=1
    """
    params = []
    if building:
        sql += " AND h.building LIKE ?"
        params.append(f"%{building}%")
    sql += " GROUP BY h.id"
    if has_arrears:
        sql += " HAVING total_unpaid > 0"
    sql += " ORDER BY h.building, h.room_no"
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    if not rows:
        console.print("[yellow]未找到住户记录[/yellow]")
        return

    table = Table(title=f"住户列表（共 {len(rows)} 户）")
    table.add_column("房号", style="cyan")
    table.add_column("楼栋", style="green")
    table.add_column("业主", style="magenta")
    table.add_column("电话", style="yellow")
    table.add_column("面积", justify="right")
    table.add_column("欠费笔数", justify="right")
    table.add_column("欠费总额", justify="right", style="bold red")
    for r in rows:
        display_name = mask_name(r["owner_name"]) if hide else r["owner_name"]
        display_phone = mask_phone(r["phone"]) if hide else (r["phone"] or "-")
        table.add_row(
            r["room_no"], r["building"], display_name, display_phone,
            f"{r['area']}㎡" if r["area"] else "-",
            str(r["arrear_count"]),
            format_money(r["total_unpaid"]) if r["total_unpaid"] > 0 else "-",
        )
    console.print(table)


@list_cmd.command("edit-phone", help="编辑住户联系方式")
@click.argument("room_no")
@click.option("--phone", "-p", required=True, help="新的手机号码")
@click.option("--name", "-n", help="更新业主姓名")
def edit_phone(room_no: str, phone: str, name: Optional[str]):
    conn = get_connection()
    row = conn.execute("SELECT * FROM households WHERE room_no = ?", (room_no,)).fetchone()
    if not row:
        console.print(f"[red]未找到房号 {room_no} 的住户[/red]")
        conn.close()
        return

    display_old = mask_phone(row["phone"]) if should_hide_sensitive() else (row["phone"] or "-")
    display_new = mask_phone(phone) if should_hide_sensitive() else phone

    console.print(f"[cyan]房号:[/cyan] {room_no}")
    console.print(f"[yellow]原电话:[/yellow] {display_old}  →  [green]新电话:[/green] {display_new}")
    if name:
        console.print(f"[magenta]原姓名:[/magenta] {row['owner_name']}  →  [green]新姓名:[/green] {name}")

    if click.confirm("确认修改？"):
        if name:
            conn.execute("UPDATE households SET phone = ?, owner_name = ?, updated_at = datetime('now','localtime') WHERE room_no = ?",
                         (phone, name, room_no))
        else:
            conn.execute("UPDATE households SET phone = ?, updated_at = datetime('now','localtime') WHERE room_no = ?",
                         (phone, room_no))
        conn.commit()
        console.print("[green]联系方式已更新[/green]")
    conn.close()


@list_cmd.command("buildings", help="查看楼栋汇总")
def list_buildings():
    conn = get_connection()
    rows = conn.execute("""
        SELECT h.building,
               COUNT(DISTINCT h.id) as household_count,
               COUNT(DISTINCT CASE WHEN (a.total_amount - a.paid_amount - a.discount_amount) > 0 THEN h.id END) as arrear_hh_count,
               COUNT(a.id) as arrear_count,
               COALESCE(SUM(a.base_amount), 0) as total_base,
               COALESCE(SUM(a.late_fee), 0) as total_late,
               COALESCE(SUM(a.total_amount - a.paid_amount - a.discount_amount), 0) as total_unpaid
        FROM households h
        LEFT JOIN arrears a ON h.id = a.household_id AND (a.total_amount - a.paid_amount - a.discount_amount) > 0
        GROUP BY h.building
        ORDER BY h.building
    """).fetchall()
    conn.close()

    if not rows:
        console.print("[yellow]暂无数据[/yellow]")
        return

    table = Table(title="楼栋汇总")
    table.add_column("楼栋", style="green")
    table.add_column("总户数", justify="right", style="blue")
    table.add_column("欠费户数", justify="right", style="yellow")
    table.add_column("欠费笔数", justify="right", style="white")
    table.add_column("欠费本金", justify="right", style="cyan")
    table.add_column("滞纳金", justify="right", style="red")
    table.add_column("欠费总额", justify="right", style="bold red")
    table.add_column("欠费占比", justify="right")

    for r in rows:
        ratio = f"{r['arrear_hh_count'] / r['household_count'] * 100:.1f}%" if r["household_count"] else "-"
        table.add_row(
            r["building"],
            str(r["household_count"]),
            str(r["arrear_hh_count"]),
            str(r["arrear_count"]),
            format_money(r["total_base"]),
            format_money(r["total_late"]),
            format_money(r["total_unpaid"]),
            ratio,
        )
    console.print(table)
