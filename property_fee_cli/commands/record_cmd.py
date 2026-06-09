import click
from datetime import datetime, date
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from typing import Optional

from ..database import get_connection
from ..utils import (
    mask_phone, mask_name, should_hide_sensitive, format_money, parse_date,
    calc_unpaid, calc_total_owed, determine_status, UNPAID_SQL_EXPR,
)

console = Console()


@click.group(help="记录催缴沟通：电话登记、承诺付款、减免记录、缴费登记")
def record_cmd():
    pass


def _refresh_arrear_status(conn, arrear_id: int):
    """减免/缴费后，重新计算total_amount和status，确保口径一致"""
    row = conn.execute("SELECT * FROM arrears WHERE id = ?", (arrear_id,)).fetchone()
    if not row:
        return
    base = float(row["base_amount"] or 0)
    late = float(row["late_fee"] or 0)
    paid = float(row["paid_amount"] or 0)
    disc = float(row["discount_amount"] or 0)

    new_total = calc_total_owed(base, late, disc)
    unpaid = calc_unpaid(base, late, paid, disc)

    has_commit = conn.execute("""
        SELECT 1 FROM call_records
        WHERE arrear_id = ? AND commitment_date IS NOT NULL AND commitment_date != ''
        LIMIT 1
    """, (arrear_id,)).fetchone() is not None

    new_status = determine_status(unpaid, has_commit and unpaid > 0)
    if disc > 0 and unpaid == 0:
        new_status = "已缴"
    elif disc > 0 and 0 < unpaid <= 0.01:
        new_status = "已缴"

    conn.execute("""
        UPDATE arrears SET
            total_amount = ?,
            original_total = COALESCE(NULLIF(original_total,0), ?),
            status = ?,
            updated_at = datetime('now','localtime')
        WHERE id = ?
    """, (new_total, (base + late), new_status, arrear_id))
    return unpaid, new_total, new_status


@record_cmd.command("call", help="登记电话沟通记录")
@click.option("--room", "-r", "room_no", required=True, help="房号")
@click.option("--arrear-id", type=int, help="关联欠费ID")
@click.option("--contact", "-c", help="通话对象")
@click.option("--call-time", help="通话时间 YYYY-MM-DD HH:MM")
@click.option("--result", "-s", required=True, type=click.Choice([
    "已联系/承诺付款", "已联系/待考虑", "已联系/拒缴",
    "已联系/停机空号", "无人接听", "关机",
    "号码错误", "其他"
]), help="通话结果")
@click.option("--commitment-date", help="承诺付款日期")
@click.option("--commitment-amount", type=float, help="承诺付款金额")
@click.option("--remark", "-m", help="备注")
def record_call(room_no, arrear_id, contact, call_time, result, commitment_date, commitment_amount, remark):
    conn = get_connection()
    hh = conn.execute("SELECT * FROM households WHERE room_no = ?", (room_no,)).fetchone()
    if not hh:
        console.print(f"[red]未找到房号 {room_no}[/red]")
        conn.close()
        return
    household_id = hh["id"]
    hide = should_hide_sensitive()

    if arrear_id:
        ar = conn.execute("SELECT * FROM arrears WHERE id = ? AND household_id = ?", (arrear_id, household_id)).fetchone()
        if not ar:
            console.print(f"[red]欠费ID {arrear_id} 不存在[/red]")
            conn.close()
            return
    else:
        ar = conn.execute(f"""
            SELECT * FROM arrears
            WHERE household_id = ? AND {UNPAID_SQL_EXPR} > 0
            ORDER BY due_date ASC LIMIT 1
        """, (household_id,)).fetchone()

    arrear_id_val = ar["id"] if ar else None
    display_name = mask_name(hh["owner_name"]) if hide else hh["owner_name"]
    display_phone = mask_phone(hh["phone"]) if hide else (hh["phone"] or "-")
    actual_call_time = call_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    unpaid_str = "-"
    if ar:
        unpaid = calc_unpaid(ar["base_amount"], ar["late_fee"], ar["paid_amount"], ar["discount_amount"])
        unpaid_str = f"{format_money(unpaid)}元 ({ar['fee_period']})"

    content = f"""
[cyan]房号:[/cyan] {room_no}  [magenta]业主:[/magenta] {display_name}
[yellow]电话:[/yellow] {display_phone}  [white]通话时间:[/white] {actual_call_time}
[bold]通话结果:[/bold] {result}
[bold]关联欠费:[/bold] {f'ID={arrear_id_val} {unpaid_str}' if ar else '无'}
[bold]承诺付款日:[/bold] {commitment_date or '-'}  [bold]承诺金额:[/bold] {format_money(commitment_amount) if commitment_amount else '-'}
[bold]备注:[/bold] {remark or '-'}
"""
    console.print(Panel(content.strip(), title="通话记录确认", border_style="blue"))
    if not click.confirm("确认登记？"):
        conn.close()
        return

    try:
        conn.execute("""
            INSERT INTO call_records
            (household_id, arrear_id, contact_person, call_time, call_result,
             commitment_date, commitment_amount, remark)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (household_id, arrear_id_val, contact or display_name, actual_call_time,
              result, commitment_date or None,
              commitment_amount if commitment_amount else 0, remark or None))

        if arrear_id_val and (result == "已联系/承诺付款" or commitment_date):
            _refresh_arrear_status(conn, arrear_id_val)

        conn.commit()
        console.print("[green]通话记录已登记[/green]")
    except Exception as e:
        conn.rollback()
        console.print(f"[red]登记失败: {str(e)}[/red]")
    finally:
        conn.close()


@record_cmd.command("calls", help="查看电话沟通记录")
@click.option("--room", "-r", "room_no", help="按房号筛选")
@click.option("--building", "-b", help="按楼栋筛选")
@click.option("--result", help="按通话结果筛选")
@click.option("--from-date", help="起始日期")
@click.option("--to-date", help="结束日期")
@click.option("--has-commitment", is_flag=True, help="仅显示有承诺付款")
@click.option("--limit", "-n", type=int, default=50)
def list_calls(room_no, building, result, from_date, to_date, has_commitment, limit):
    conn = get_connection()
    sql = """
        SELECT cr.*, h.room_no, h.building, h.owner_name, h.phone,
               a.fee_period, a.base_amount, a.late_fee, a.paid_amount, a.discount_amount
        FROM call_records cr
        JOIN households h ON cr.household_id = h.id
        LEFT JOIN arrears a ON cr.arrear_id = a.id
        WHERE 1=1
    """
    params = []
    if room_no:
        sql += " AND h.room_no LIKE ?"
        params.append(f"%{room_no}%")
    if building:
        sql += " AND h.building LIKE ?"
        params.append(f"%{building}%")
    if result:
        sql += " AND cr.call_result LIKE ?"
        params.append(f"%{result}%")
    if from_date:
        sql += " AND DATE(cr.call_time) >= ?"
        params.append(from_date)
    if to_date:
        sql += " AND DATE(cr.call_time) <= ?"
        params.append(to_date)
    if has_commitment:
        sql += " AND cr.commitment_date IS NOT NULL AND cr.commitment_date != ''"
    sql += " ORDER BY cr.call_time DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    if not rows:
        console.print("[yellow]未找到通话记录[/yellow]")
        return
    hide = should_hide_sensitive()
    table = Table(title=f"沟通记录（共 {len(rows)} 条）")
    table.add_column("时间", style="white", no_wrap=True)
    table.add_column("房号", style="cyan")
    table.add_column("业主", style="magenta")
    table.add_column("电话", style="yellow")
    table.add_column("结果", style="white")
    table.add_column("账期")
    table.add_column("承诺日期", style="blue")
    table.add_column("承诺金额", justify="right", style="green")
    table.add_column("备注", style="white")
    for r in rows:
        display_name = mask_name(r["owner_name"]) if hide else r["owner_name"]
        display_phone = mask_phone(r["phone"]) if hide else (r["phone"] or "-")
        result_style = {
            "已联系/承诺付款": "green",
            "已联系/待考虑": "yellow",
            "已联系/拒缴": "red",
        }.get(r["call_result"], "white")
        table.add_row(
            r["call_time"], r["room_no"], display_name, display_phone,
            f"[{result_style}]{r['call_result']}[/{result_style}]",
            r["fee_period"] or "-",
            r["commitment_date"] or "-",
            format_money(r["commitment_amount"]) if r["commitment_amount"] > 0 else "-",
            (r["remark"] or "-")[:20],
        )
    console.print(table)


@record_cmd.command("commitment", help="标记承诺付款")
@click.argument("arrear_id", type=int)
@click.option("--date", "commit_date", required=True, help="承诺付款日期")
@click.option("--amount", type=float, help="承诺付款金额，默认全额尚欠")
@click.option("--remark", help="备注")
def mark_commitment(arrear_id, commit_date, amount, remark):
    try:
        cd = parse_date(commit_date).strftime("%Y-%m-%d")
    except ValueError as e:
        console.print(f"[red]{str(e)}[/red]")
        return
    conn = get_connection()
    ar = conn.execute(f"""
        SELECT a.*, h.room_no, h.owner_name, h.phone,
               {UNPAID_SQL_EXPR} as unpaid_calc
        FROM arrears a JOIN households h ON a.household_id = h.id
        WHERE a.id = ?
    """, (arrear_id,)).fetchone()
    if not ar:
        console.print(f"[red]欠费ID {arrear_id} 不存在[/red]")
        conn.close()
        return
    hide = should_hide_sensitive()
    unpaid = float(ar["unpaid_calc"] or 0)
    actual_amount = amount if amount else unpaid
    actual_amount = min(actual_amount, unpaid)
    display_name = mask_name(ar["owner_name"]) if hide else ar["owner_name"]
    display_phone = mask_phone(ar["phone"]) if hide else (ar["phone"] or "-")
    console.print(f"[cyan]欠费ID:[/cyan] {arrear_id}  {ar['room_no']}")
    console.print(f"[magenta]业主:[/magenta] {display_name}  [yellow]电话:[/yellow] {display_phone}")
    console.print(f"[white]账期:[/white] {ar['fee_period']}  [blue]尚欠:[/blue] {format_money(unpaid)}元")
    console.print(f"[green]承诺日期:[/green] {cd}  [bold]承诺金额:[/bold] {format_money(actual_amount)}元")
    if remark:
        console.print(f"[dim]备注: {remark}[/dim]")
    if not click.confirm("确认标记？"):
        conn.close()
        return
    try:
        conn.execute("""
            INSERT INTO call_records
            (household_id, arrear_id, contact_person, call_time, call_result,
             commitment_date, commitment_amount, remark)
            VALUES (?, ?, ?, ?, '已联系/承诺付款', ?, ?, ?)
        """, (ar["household_id"], arrear_id, display_name,
              datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              cd, actual_amount, remark or "系统标记承诺付款"))
        _refresh_arrear_status(conn, arrear_id)
        conn.commit()
        console.print("[green]已标记承诺付款[/green]")
    except Exception as e:
        conn.rollback()
        console.print(f"[red]操作失败: {str(e)}[/red]")
    finally:
        conn.close()


@record_cmd.command("discount", help="登记费用减免")
@click.argument("arrear_id", type=int)
@click.option("--amount", "-a", type=float, required=True, help="减免金额（正数）")
@click.option("--reason", "-r", required=True, type=click.Choice([
    "业主投诉处理", "物业服务瑕疵", "特殊困难照顾",
    "预缴优惠", "长期空置", "系统计费错误", "其他"
]), help="减免原因")
@click.option("--approved-by", help="审批人")
@click.option("--remark", help="详细说明")
def record_discount(arrear_id, amount, reason, approved_by, remark):
    if amount <= 0:
        console.print("[red]减免金额必须大于0[/red]")
        return
    conn = get_connection()
    ar = conn.execute(f"""
        SELECT a.*, h.room_no, h.owner_name, h.id as household_id,
               {UNPAID_SQL_EXPR} as unpaid_calc
        FROM arrears a JOIN households h ON a.household_id = h.id
        WHERE a.id = ?
    """, (arrear_id,)).fetchone()
    if not ar:
        console.print(f"[red]欠费ID {arrear_id} 不存在[/red]")
        conn.close()
        return
    hide = should_hide_sensitive()
    unpaid = float(ar["unpaid_calc"] or 0)
    if amount > unpaid + 0.01:
        console.print(f"[red]减免({format_money(amount)})超过尚欠({format_money(unpaid)})[/red]")
        if not click.confirm("按全额尚欠减免？"):
            conn.close()
            return
        amount = unpaid
    display_name = mask_name(ar["owner_name"]) if hide else ar["owner_name"]
    new_disc = float(ar["discount_amount"] or 0) + amount
    new_unpaid = calc_unpaid(ar["base_amount"], ar["late_fee"], ar["paid_amount"], new_disc)
    new_total = calc_total_owed(ar["base_amount"], ar["late_fee"], new_disc)
    new_status = "已缴" if new_unpaid <= 0.01 else ("减免" if ar["status"] != "承诺付款" else "承诺付款")
    content = f"""
[cyan]欠费ID:[/cyan] {arrear_id}  [bold]{ar['room_no']}[/bold]
[magenta]业主:[/magenta] {display_name}  [white]账期:[/white] {ar['fee_period']}
[blue]原尚欠:[/blue] {format_money(unpaid)}元
[bold red]本次减免:[/bold red] {format_money(amount)}元  [dim]原因: {reason}[/dim]
[yellow]累计减免:[/yellow] {format_money(new_disc)}元
[green]减免后应缴总额:[/green] {format_money(new_total)}元  [bold]尚欠: {format_money(new_unpaid)}元  状态: {new_status}[/bold]
[dim]审批人: {approved_by or '-'}  说明: {remark or '-'}[/dim]
"""
    console.print(Panel(content.strip(), title="减免确认", border_style="magenta"))
    if not click.confirm("确认登记减免？"):
        conn.close()
        return
    try:
        approved_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("""
            INSERT INTO discount_records
            (arrear_id, household_id, discount_amount, discount_reason, approved_by, approved_at, remark)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (arrear_id, ar["household_id"], amount, reason, approved_by, approved_at, remark or None))
        conn.execute("""
            UPDATE arrears SET discount_amount = discount_amount + ?, updated_at = datetime('now','localtime')
            WHERE id = ?
        """, (amount, arrear_id))
        _refresh_arrear_status(conn, arrear_id)
        conn.commit()
        console.print(f"[green]已登记减免 {format_money(amount)} 元，尚余 {format_money(new_unpaid)} 元[/green]")
    except Exception as e:
        conn.rollback()
        console.print(f"[red]登记失败: {str(e)}[/red]")
    finally:
        conn.close()


@record_cmd.command("discounts", help="查看减免记录")
@click.option("--room", "-r", "room_no", help="按房号筛选")
@click.option("--building", "-b", help="按楼栋筛选")
@click.option("--reason", help="按减免原因筛选")
@click.option("--limit", "-n", type=int, default=50)
def list_discounts(room_no, building, reason, limit):
    conn = get_connection()
    sql = """
        SELECT dr.*, h.room_no, h.building, h.owner_name, a.fee_period
        FROM discount_records dr
        JOIN households h ON dr.household_id = h.id
        LEFT JOIN arrears a ON dr.arrear_id = a.id
        WHERE 1=1
    """
    params = []
    if room_no:
        sql += " AND h.room_no LIKE ?"
        params.append(f"%{room_no}%")
    if building:
        sql += " AND h.building LIKE ?"
        params.append(f"%{building}%")
    if reason:
        sql += " AND dr.discount_reason LIKE ?"
        params.append(f"%{reason}%")
    sql += " ORDER BY dr.created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    total_amount = sum(r["discount_amount"] for r in rows)
    conn.close()
    if not rows:
        console.print("[yellow]未找到减免记录[/yellow]")
        return
    hide = should_hide_sensitive()
    table = Table(title=f"减免记录（共 {len(rows)} 条，合计 {format_money(total_amount)} 元）")
    table.add_column("时间", style="white")
    table.add_column("房号", style="cyan")
    table.add_column("业主", style="magenta")
    table.add_column("账期", style="white")
    table.add_column("减免原因", style="yellow")
    table.add_column("金额", justify="right", style="bold red")
    table.add_column("审批人", style="blue")
    for r in rows:
        display_name = mask_name(r["owner_name"]) if hide else r["owner_name"]
        table.add_row(
            r["approved_at"] or r["created_at"],
            r["room_no"], display_name, r["fee_period"] or "-",
            r["discount_reason"], format_money(r["discount_amount"]),
            r["approved_by"] or "-",
        )
    console.print(table)


@record_cmd.command("payment", help="登记实际缴费（写入支付明细表）")
@click.argument("arrear_id", type=int)
@click.option("--amount", "-a", type=float, required=True, help="实际缴纳金额")
@click.option("--pay-date", help="缴费日期，默认今天")
@click.option("--method", type=click.Choice(["现金", "银行转账", "微信", "支付宝", "POS机", "其他"]), default="其他")
@click.option("--operator", help="经办人")
@click.option("--remark", help="备注")
def record_payment(arrear_id, amount, pay_date, method, operator, remark):
    if amount <= 0:
        console.print("[red]缴费金额必须大于0[/red]")
        return
    conn = get_connection()
    ar = conn.execute(f"""
        SELECT a.*, h.room_no, h.owner_name, h.id as household_id,
               {UNPAID_SQL_EXPR} as unpaid_calc
        FROM arrears a JOIN households h ON a.household_id = h.id
        WHERE a.id = ?
    """, (arrear_id,)).fetchone()
    if not ar:
        console.print(f"[red]欠费ID {arrear_id} 不存在[/red]")
        conn.close()
        return
    hide = should_hide_sensitive()
    unpaid = float(ar["unpaid_calc"] or 0)
    if amount > unpaid + 0.01:
        console.print(f"[red]缴费({format_money(amount)})超过尚欠({format_money(unpaid)})[/red]")
        if not click.confirm("按全额缴纳处理？"):
            conn.close()
            return
        amount = unpaid
    display_name = mask_name(ar["owner_name"]) if hide else ar["owner_name"]
    new_paid = float(ar["paid_amount"] or 0) + amount
    new_unpaid = calc_unpaid(ar["base_amount"], ar["late_fee"], new_paid, ar["discount_amount"])
    actual_pay_date = pay_date or date.today().strftime("%Y-%m-%d")
    new_status = "已缴" if new_unpaid <= 0.01 else "部分缴纳"
    content = f"""
[cyan]欠费ID:[/cyan] {arrear_id}  [bold]{ar['room_no']}[/bold]
[magenta]业主:[/magenta] {display_name}  [white]账期:[/white] {ar['fee_period']}
[blue]原尚欠:[/blue] {format_money(unpaid)}元  (本金:{format_money(ar['base_amount'])} 滞纳金:{format_money(ar['late_fee'])} 已缴:{format_money(ar['paid_amount'])} 减免:{format_money(ar['discount_amount'])})
[bold green]本次缴纳:[/bold green] {format_money(amount)}元  [dim]方式:{method} 日期:{actual_pay_date} 经办人:{operator or '-'}[/dim]
[yellow]缴后已缴:[/yellow] {format_money(new_paid)}元
[bold]尚余: {format_money(new_unpaid)}元  状态: {new_status}[/bold]
[dim]备注: {remark or '-'}[/dim]
"""
    console.print(Panel(content.strip(), title="缴费登记确认", border_style="green"))
    if not click.confirm("确认登记缴费？"):
        conn.close()
        return
    try:
        conn.execute("""
            INSERT INTO payment_records
            (arrear_id, household_id, amount, pay_method, pay_date, operator, remark)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (arrear_id, ar["household_id"], amount, method, actual_pay_date, operator or None, remark or None))
        conn.execute("""
            UPDATE arrears SET paid_amount = paid_amount + ?,
                remark = COALESCE(NULLIF(?, ''), remark),
                updated_at = datetime('now','localtime')
            WHERE id = ?
        """, (amount, remark or None, arrear_id))
        _, _, new_st = _refresh_arrear_status(conn, arrear_id)
        conn.commit()
        console.print(f"[green]已登记缴费 {format_money(amount)} 元，余额 {format_money(new_unpaid)} 元，状态 {new_st}[/green]")
    except Exception as e:
        conn.rollback()
        console.print(f"[red]登记失败: {str(e)}[/red]")
    finally:
        conn.close()


@record_cmd.command("payments", help="查看缴费明细记录")
@click.option("--room", "-r", "room_no", help="按房号筛选")
@click.option("--arrear-id", type=int, help="按欠费ID筛选")
@click.option("--from-date", help="起始日期")
@click.option("--to-date", help="结束日期")
@click.option("--limit", "-n", type=int, default=50)
def list_payments(room_no, arrear_id, from_date, to_date, limit):
    conn = get_connection()
    sql = """
        SELECT pr.*, h.room_no, h.building, h.owner_name, a.fee_period
        FROM payment_records pr
        JOIN households h ON pr.household_id = h.id
        LEFT JOIN arrears a ON pr.arrear_id = a.id
        WHERE 1=1
    """
    params = []
    if room_no:
        sql += " AND h.room_no LIKE ?"
        params.append(f"%{room_no}%")
    if arrear_id:
        sql += " AND pr.arrear_id = ?"
        params.append(arrear_id)
    if from_date:
        sql += " AND DATE(pr.pay_date) >= ?"
        params.append(from_date)
    if to_date:
        sql += " AND DATE(pr.pay_date) <= ?"
        params.append(to_date)
    sql += " ORDER BY pr.created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    total = sum(r["amount"] for r in rows)
    conn.close()
    if not rows:
        console.print("[yellow]未找到缴费记录[/yellow]")
        return
    hide = should_hide_sensitive()
    table = Table(title=f"缴费明细（共 {len(rows)} 条，合计 {format_money(total)} 元）")
    table.add_column("时间", style="white")
    table.add_column("房号", style="cyan")
    table.add_column("业主", style="magenta")
    table.add_column("账期")
    table.add_column("金额", justify="right", style="bold green")
    table.add_column("方式", style="blue")
    table.add_column("缴费日")
    table.add_column("经办人")
    for r in rows:
        display_name = mask_name(r["owner_name"]) if hide else r["owner_name"]
        table.add_row(
            r["created_at"], r["room_no"], display_name, r["fee_period"] or "-",
            format_money(r["amount"]), r["pay_method"] or "-",
            r["pay_date"] or "-", r["operator"] or "-",
        )
    console.print(table)
