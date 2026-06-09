import click
import csv
from rich.console import Console
from rich.table import Table
from typing import List, Dict, Any
from collections import OrderedDict

from ..database import get_connection
from ..utils import calc_unpaid, format_money, UNPAID_SQL_EXPR

console = Console()


@click.group(help="数据校验命令组 - 检查欠费数据异常")
def check_cmd():
    pass


ANOMALY_TYPES = OrderedDict([
    ("negative_unpaid", "尚欠为负"),
    ("discount_exceed", "减免超过应缴"),
    ("paid_but_unpaid", "已缴但余额"),
    ("unpaid_but_clear", "未缴但已清"),
    ("total_mismatch", "总额不一致"),
    ("over_paid", "已缴+减免>原应缴"),
    ("promise_no_contact", "承诺未联系"),
])

AUTO_FIXABLE = {"paid_but_unpaid", "unpaid_but_clear", "total_mismatch"}


def _raw_unpaid(base: float, late: float, paid: float, discount: float) -> float:
    return round(float(base or 0) + float(late or 0) - float(paid or 0) - float(discount or 0), 2)


def _build_base_table(title: str) -> Table:
    t = Table(title=title)
    t.add_column("ID", style="dim", no_wrap=True)
    t.add_column("房号", style="cyan")
    t.add_column("业主", style="white")
    t.add_column("账期", style="white")
    t.add_column("本金", justify="right", style="blue")
    t.add_column("滞纳金", justify="right", style="yellow")
    t.add_column("已缴", justify="right", style="green")
    t.add_column("减免", justify="right", style="magenta")
    t.add_column("总额", justify="right", style="white")
    t.add_column("状态", style="white")
    return t


def _add_row(t: Table, r: Any, extra: str = "") -> None:
    t.add_row(
        str(r["id"]), r["room_no"], r["owner_name"] or "",
        r["fee_period"], format_money(r["base_amount"]), format_money(r["late_fee"]),
        format_money(r["paid_amount"]), format_money(r["discount_amount"]),
        format_money(r["total_amount"]), r["status"], extra)


@check_cmd.command("audit", help="扫描所有欠费记录，检查数据异常")
@click.option("--output", "-o", "output_file", help="导出异常清单到CSV文件")
@click.option("--auto-fix", is_flag=True, help="自动修复第3/4/5类异常")
def audit(output_file: str, auto_fix: bool):
    conn = get_connection()

    sql = f"""
        SELECT a.id, a.household_id, a.fee_period, a.fee_type,
               a.base_amount, a.late_fee, a.total_amount,
               a.paid_amount, a.discount_amount, a.status,
               h.room_no, h.owner_name, h.building
        FROM arrears a JOIN households h ON a.household_id = h.id
        ORDER BY h.building, h.room_no, a.fee_period
    """
    rows = conn.execute(sql).fetchall()

    if not rows:
        console.print("[yellow]没有欠费记录[/yellow]")
        conn.close()
        return

    anomalies: Dict[str, List[Any]] = {k: [] for k in ANOMALY_TYPES}

    for r in rows:
        base = float(r["base_amount"] or 0)
        late = float(r["late_fee"] or 0)
        paid = float(r["paid_amount"] or 0)
        discount = float(r["discount_amount"] or 0)
        total = float(r["total_amount"] or 0)
        status = r["status"] or ""
        unpaid = calc_unpaid(base, late, paid, discount)
        raw_unpaid = round(base + late - paid - discount, 2)
        expected_total = round(base + late - discount, 2)

        if raw_unpaid < -0.01:
            anomalies["negative_unpaid"].append(r)

        if discount > base + late + 0.01:
            anomalies["discount_exceed"].append(r)

        if status == "已缴" and unpaid > 0.01:
            anomalies["paid_but_unpaid"].append(r)

        if status != "已缴" and unpaid <= 0.01:
            anomalies["unpaid_but_clear"].append(r)

        if abs(total - expected_total) > 0.01:
            anomalies["total_mismatch"].append(r)

        if paid + discount > base + late + 0.01:
            anomalies["over_paid"].append(r)

        if status == "承诺付款":
            cr = conn.execute(
                "SELECT id FROM call_records WHERE arrear_id = ? AND (commitment_date IS NOT NULL OR commitment_amount > 0 OR call_result LIKE ?)",
                (r["id"], "%承诺%")
            ).fetchone()
            if not cr:
                cr2 = conn.execute(
                    "SELECT id FROM call_records WHERE household_id = ? AND (commitment_date IS NOT NULL OR commitment_amount > 0 OR call_result LIKE ?)",
                    (r["household_id"], "%承诺%")
                ).fetchone()
                if not cr2:
                    anomalies["promise_no_contact"].append(r)

    conn.close()

    total_anomaly_count = sum(len(v) for v in anomalies.values())
    console.print(f"[bold]扫描完成:[/bold] 共 {len(rows)} 条欠费记录，发现 [red]{total_anomaly_count}[/red] 条异常")

    if total_anomaly_count == 0:
        console.print("[green]数据校验通过，未发现异常[/green]")
        return

    summary_rows = []
    csv_rows = []

    for key, label in ANOMALY_TYPES.items():
        records = anomalies[key]
        if not records:
            continue
        console.print()
        table_title = f"【{label}】共 {len(records)} 条"
        t = _build_base_table(table_title)
        t.add_column("说明", style="bold red")

        for r in records:
            base = float(r["base_amount"] or 0)
            late = float(r["late_fee"] or 0)
            paid = float(r["paid_amount"] or 0)
            discount = float(r["discount_amount"] or 0)
            total = float(r["total_amount"] or 0)
            unpaid = calc_unpaid(base, late, paid, discount)
            raw_unpaid = round(base + late - paid - discount, 2)
            expected_total = round(base + late - discount, 2)

            extra = ""
            if key == "negative_unpaid":
                extra = f"尚欠={format_money(raw_unpaid)}"
            elif key == "discount_exceed":
                extra = f"减免{format_money(discount)} > 应缴{format_money(base + late)}"
            elif key == "paid_but_unpaid":
                extra = f"尚欠={format_money(unpaid)}"
            elif key == "unpaid_but_clear":
                extra = f"尚欠={format_money(unpaid)}"
            elif key == "total_mismatch":
                extra = f"总额{format_money(total)} ≠ 应为{format_money(expected_total)}"
            elif key == "over_paid":
                extra = f"已缴+减免={format_money(paid + discount)} > 原应缴{format_money(base + late)}"
            elif key == "promise_no_contact":
                extra = "无承诺通话记录"

            _add_row(t, r, extra)

            csv_rows.append({
                "异常类型": label,
                "ID": r["id"],
                "房号": r["room_no"],
                "业主": r["owner_name"] or "",
                "账期": r["fee_period"],
                "本金": r["base_amount"],
                "滞纳金": r["late_fee"],
                "已缴": r["paid_amount"],
                "减免": r["discount_amount"],
                "总额": r["total_amount"],
                "状态": r["status"],
                "说明": extra,
            })

            fixable_mark = " [green]✓可修复[/green]" if key in AUTO_FIXABLE else ""
            summary_rows.append((label, len(records), key in AUTO_FIXABLE))

        console.print(t)

    console.print()
    summary_table = Table(title="异常汇总")
    summary_table.add_column("异常类型", style="bold")
    summary_table.add_column("数量", justify="right", style="white")
    summary_table.add_column("可自动修复", justify="center")
    total_fixable = 0
    for label, count, fixable in summary_rows:
        mark = "[green]是[/green]" if fixable else "[dim]否[/dim]"
        if fixable:
            total_fixable += count
        summary_table.add_row(label, str(count), mark)
    summary_table.add_row("─" * 20, "─" * 6, "─" * 10, style="dim")
    summary_table.add_row("[bold]合计[/bold]", f"[bold red]{total_anomaly_count}[/bold red]", f"[bold green]{total_fixable}[/bold green]")
    console.print(summary_table)

    if output_file:
        try:
            with open(output_file, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=[
                    "异常类型", "ID", "房号", "业主", "账期",
                    "本金", "滞纳金", "已缴", "减免", "总额", "状态", "说明"
                ])
                writer.writeheader()
                writer.writerows(csv_rows)
            console.print(f"\n[green]异常清单已导出: {output_file}[/green]")
        except Exception as e:
            console.print(f"[red]导出CSV失败: {str(e)}[/red]")

    if auto_fix and total_fixable > 0:
        console.print(f"\n[bold yellow]开始自动修复 {total_fixable} 条异常...[/bold yellow]")
        conn = get_connection()
        fixed_count = 0

        try:
            for r in anomalies["paid_but_unpaid"]:
                base = float(r["base_amount"] or 0)
                late = float(r["late_fee"] or 0)
                paid = float(r["paid_amount"] or 0)
                discount = float(r["discount_amount"] or 0)
                unpaid = calc_unpaid(base, late, paid, discount)
                new_status = "承诺付款" if unpaid > 0.01 else "未缴"
                cr = conn.execute(
                    "SELECT id FROM call_records WHERE arrear_id = ? AND (commitment_date IS NOT NULL OR commitment_amount > 0)",
                    (r["id"],)
                ).fetchone()
                if not cr:
                    cr = conn.execute(
                        "SELECT id FROM call_records WHERE household_id = ? AND (commitment_date IS NOT NULL OR commitment_amount > 0)",
                        (r["household_id"],)
                    ).fetchone()
                if cr:
                    new_status = "承诺付款"
                conn.execute(
                    "UPDATE arrears SET status = ?, updated_at = datetime('now','localtime') WHERE id = ?",
                    (new_status, r["id"])
                )
                fixed_count += 1

            for r in anomalies["unpaid_but_clear"]:
                conn.execute(
                    "UPDATE arrears SET status = '已缴', updated_at = datetime('now','localtime') WHERE id = ?",
                    (r["id"],)
                )
                fixed_count += 1

            for r in anomalies["total_mismatch"]:
                base = float(r["base_amount"] or 0)
                late = float(r["late_fee"] or 0)
                discount = float(r["discount_amount"] or 0)
                expected_total = round(base + late - discount, 2)
                conn.execute(
                    "UPDATE arrears SET total_amount = ?, updated_at = datetime('now','localtime') WHERE id = ?",
                    (expected_total, r["id"])
                )
                fixed_count += 1

            conn.commit()
            console.print(f"[green]自动修复完成，共修复 {fixed_count} 条记录[/green]")
        except Exception as e:
            conn.rollback()
            console.print(f"[red]修复失败: {str(e)}[/red]")
        finally:
            conn.close()
    elif auto_fix and total_fixable == 0:
        console.print("\n[dim]没有可自动修复的异常[/dim]")
