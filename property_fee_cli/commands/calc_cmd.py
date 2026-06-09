import click
from datetime import date, datetime
from rich.console import Console
from rich.table import Table
from typing import Optional

from ..database import get_connection
from ..utils import parse_date, format_money, is_holiday, count_workdays, get_config, set_config, UNPAID_SQL_EXPR

console = Console()


@click.group(help="计算滞纳金和费用相关操作")
def calc_cmd():
    pass


@calc_cmd.command("late-fee", help="生成/更新滞纳金（自动避让节假日）")
@click.option("--building", "-b", help="按楼栋筛选")
@click.option("--room", "-r", help="按房号筛选")
@click.option("--period", "-p", help="按账期筛选")
@click.option("--rate", type=float, help="日滞纳金费率，如0.0005表示万分之五")
@click.option("--grace-days", type=int, help="宽限期天数，宽限期内不计滞纳金")
@click.option("--skip-holidays/--include-holidays", default=True, help="是否跳过节假日（含周末）计算")
@click.option("--to-date", help="计算截止日期，默认今天")
@click.option("--dry-run", is_flag=True, help="仅预览不写入数据库")
def calc_late_fee(building, room, period, rate, grace_days, skip_holidays, to_date, dry_run):
    cfg_rate = float(get_config("late_fee_rate", "0.0005"))
    cfg_grace = int(get_config("grace_days", "30"))
    effective_rate = rate if rate is not None else cfg_rate
    effective_grace = grace_days if grace_days is not None else cfg_grace

    try:
        end_date = parse_date(to_date) if to_date else date.today()
    except ValueError as e:
        console.print(f"[red]{str(e)}[/red]")
        return

    conn = get_connection()
    sql = f"""
        SELECT a.id, a.household_id, a.fee_period, a.fee_type, a.base_amount,
               a.late_fee as current_late_fee, a.due_date, a.status,
               a.paid_amount, a.discount_amount,
               h.room_no, h.building, h.owner_name
        FROM arrears a JOIN households h ON a.household_id = h.id
        WHERE {UNPAID_SQL_EXPR} > 0
    """
    params = []
    if building:
        sql += " AND h.building LIKE ?"
        params.append(f"%{building}%")
    if room:
        sql += " AND h.room_no LIKE ?"
        params.append(f"%{room}%")
    if period:
        sql += " AND a.fee_period LIKE ?"
        params.append(f"%{period}%")
    sql += " ORDER BY a.due_date ASC"

    rows = conn.execute(sql, params).fetchall()
    if not rows:
        console.print("[yellow]没有符合条件的欠费记录[/yellow]")
        conn.close()
        return

    results = []
    total_old_late = 0
    total_new_late = 0
    total_diff = 0

    for r in rows:
        try:
            due = parse_date(r["due_date"])
        except ValueError:
            continue

        start_date = due
        if effective_grace > 0:
            from datetime import timedelta
            start_date = due + timedelta(days=effective_grace)

        if start_date >= end_date:
            continue

        if skip_holidays:
            overdue_days = count_workdays(start_date, end_date)
        else:
            overdue_days = (end_date - start_date).days

        if overdue_days <= 0:
            continue

        base = r["base_amount"]
        old_late = r["current_late_fee"] or 0
        new_late = round(base * effective_rate * overdue_days, 2)
        diff = round(new_late - old_late, 2)

        total_old_late += old_late
        total_new_late += new_late
        total_diff += diff

        results.append({
            "id": r["id"],
            "room_no": r["room_no"],
            "building": r["building"],
            "owner": r["owner_name"],
            "period": r["fee_period"],
            "base_amount": base,
            "due_date": r["due_date"],
            "overdue_days": overdue_days,
            "old_late": old_late,
            "new_late": new_late,
            "diff": diff,
        })

    if not results:
        console.print("[yellow]没有符合条件的逾期记录（可能尚在宽限期内）[/yellow]")
        console.print(f"[dim]日费率: {effective_rate*100:.4f}%，宽限期: {effective_grace}天，节假日避让: {'是' if skip_holidays else '否'}，截止日: {end_date}[/dim]")
        conn.close()
        return

    table = Table(title=f"滞纳金计算结果（共 {len(results)} 条，费率 {effective_rate*100:.4f}%/日）")
    table.add_column("房号", style="cyan")
    table.add_column("账期", style="white")
    table.add_column("本金", justify="right", style="blue")
    table.add_column("到期日", style="yellow")
    table.add_column("逾期天数", justify="right", style="white")
    table.add_column("原滞纳金", justify="right", style="white")
    table.add_column("新滞纳金", justify="right", style="red")
    table.add_column("差额", justify="right", style="bold red")

    for r in results:
        diff_style = "green" if r["diff"] < 0 else "bold red"
        table.add_row(
            r["room_no"], r["period"], format_money(r["base_amount"]), r["due_date"],
            str(r["overdue_days"]),
            format_money(r["old_late"]),
            format_money(r["new_late"]),
            f"[{diff_style}]{format_money(r['diff'])}[/{diff_style}]",
        )
    console.print(table)

    console.print(f"\n[dim]原滞纳金合计: {format_money(total_old_late)} 元[/dim]")
    console.print(f"[bold]新滞纳金合计: {format_money(total_new_late)} 元[/bold]")
    diff_style = "green" if total_diff < 0 else "bold red"
    console.print(f"[{diff_style}]差额合计: {format_money(total_diff)} 元[/{diff_style}]")

    if dry_run:
        console.print("\n[yellow]已执行dry-run，未写入数据库[/yellow]")
        conn.close()
        return

    if not click.confirm(f"\n确认更新 {len(results)} 条记录的滞纳金？"):
        conn.close()
        return

    try:
        from .record_cmd import _refresh_arrear_status, _sync_batch_after_change
        for r in results:
            conn.execute("""
                UPDATE arrears SET
                    late_fee = ?,
                    updated_at = datetime('now','localtime')
                WHERE id = ?
            """, (r["new_late"], r["id"]))
            _refresh_arrear_status(conn, r["id"])
            _sync_batch_after_change(conn, r["id"], "", f"滞纳金更新{round(r['diff'],2)}")
            if abs(r["diff"]) > 0.005:
                try:
                    from .archive_cmd import _write_adjustment_if_archived
                    _write_adjustment_if_archived(
                        conn, r["id"], "滞纳金调整", r["diff"],
                        f"逾期{r['overdue_days']}天,费率{effective_rate*100:.4f}%/日,原{format_money(r['old_late'])}→新{format_money(r['new_late'])}",
                        "系统计算"
                    )
                except Exception:
                    pass
        conn.commit()
        console.print("[green]滞纳金已更新[/green]")
    except Exception as e:
        conn.rollback()
        console.print(f"[red]更新失败: {str(e)}[/red]")
    finally:
        conn.close()


@calc_cmd.command("config", help="查看/设置滞纳金计算参数")
@click.option("--rate", type=float, help="设置日滞纳金费率")
@click.option("--grace-days", type=int, help="设置宽限期天数")
def calc_config(rate: Optional[float], grace_days: Optional[int]):
    if rate is not None:
        if rate < 0 or rate > 0.01:
            console.print("[red]费率应在0到0.01（1%/日）之间[/red]")
            return
        set_config("late_fee_rate", str(rate))
        console.print(f"[green]日滞纳金费率已设置为: {rate*100:.4f}%[/green]")

    if grace_days is not None:
        if grace_days < 0:
            console.print("[red]宽限期天数不能为负数[/red]")
            return
        set_config("grace_days", str(grace_days))
        console.print(f"[green]宽限期已设置为: {grace_days} 天[/green]")

    if rate is None and grace_days is None:
        console.print("[bold]当前滞纳金配置:[/bold]")
        console.print(f"  日滞纳金费率: {float(get_config('late_fee_rate', '0.0005'))*100:.4f}%")
        console.print(f"  宽限期: {get_config('grace_days', '30')} 天")


@calc_cmd.command("holiday-check", help="验证日期是否为节假日/工作日")
@click.argument("date_str")
def holiday_check(date_str: str):
    try:
        d = parse_date(date_str)
    except ValueError as e:
        console.print(f"[red]{str(e)}[/red]")
        return

    holiday = is_holiday(d)
    weekday_names = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    conn = get_connection()
    row = conn.execute("SELECT holiday_name FROM holidays WHERE holiday_date = ?",
                       (d.strftime("%Y-%m-%d"),)).fetchone()
    conn.close()

    console.print(f"[bold]日期:[/bold] {d.strftime('%Y-%m-%d')} ({weekday_names[d.weekday()]})")
    if holiday:
        name = row["holiday_name"] if row else "周末"
        console.print(f"[red]节假日/休息日: 是 ({name})[/red]")
    else:
        console.print(f"[green]工作日: 是[/green]")
