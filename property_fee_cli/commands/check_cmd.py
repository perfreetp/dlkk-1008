import click
import csv
from datetime import datetime
from rich.console import Console
from rich.table import Table
from typing import List, Dict, Any, Optional
from collections import OrderedDict

from ..database import get_connection
from ..utils import calc_unpaid, format_money, UNPAID_SQL_EXPR

console = Console()


@click.group(help="数据校验命令组 - 检查欠费数据异常并标记处理")
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

HANDLE_STATUS_OPTIONS = ["待处理", "已确认", "已修复", "暂缓处理"]


def _build_base_table(title: str, show_handle_cols: bool = False) -> Table:
    t = Table(title=title)
    t.add_column("异常ID", style="dim", no_wrap=True)
    t.add_column("欠费ID", style="white", no_wrap=True)
    t.add_column("房号", style="cyan")
    t.add_column("业主", style="white")
    t.add_column("账期", style="white")
    t.add_column("本金", justify="right", style="blue")
    t.add_column("滞纳金", justify="right", style="yellow")
    t.add_column("已缴", justify="right", style="green")
    t.add_column("减免", justify="right", style="magenta")
    t.add_column("尚欠", justify="right", style="bold red")
    t.add_column("状态", style="white")
    t.add_column("说明", style="bold red")
    if show_handle_cols:
        t.add_column("处理状态", style="yellow")
        t.add_column("处理备注", style="dim", max_width=20)
        t.add_column("处理人/时间", style="dim", max_width=20)
        t.add_column("复核人", style="dim", max_width=12)
        t.add_column("复核时间", style="dim", max_width=16)
    return t


@check_cmd.command("audit", help="扫描所有欠费记录，检查数据异常并持久化")
@click.option("--output", "-o", "output_file", help="导出异常清单到CSV文件")
@click.option("--auto-fix", is_flag=True, help="自动修复第3/4/5类异常")
@click.option("--reset-handle", is_flag=True, help="重置已标记的处理状态为待处理")
@click.option("--show-handled", is_flag=True, help="终端显示时包含已确认/已修复的记录")
@click.option("--operator", default="系统扫描", help="扫描操作人")
def audit(output_file: Optional[str], auto_fix: bool, reset_handle: bool, show_handled: bool, operator: str):
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

    anomalies_all: List[Dict[str, Any]] = []
    scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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
        diff_amt = round(total - expected_total, 2)

        hit_list = []
        if raw_unpaid < -0.01:
            hit_list.append(("negative_unpaid", f"尚欠={format_money(raw_unpaid)}", raw_unpaid))
        if discount > base + late + 0.01:
            hit_list.append(("discount_exceed", f"减免{format_money(discount)} > 应缴{format_money(base+late)}", round(discount - base - late, 2)))
        if status == "已缴" and unpaid > 0.01:
            hit_list.append(("paid_but_unpaid", f"尚欠={format_money(unpaid)}", unpaid))
        if status != "已缴" and unpaid <= 0.01:
            hit_list.append(("unpaid_but_clear", f"尚欠={format_money(unpaid)}", -unpaid))
        if abs(total - expected_total) > 0.01:
            hit_list.append(("total_mismatch", f"总额{format_money(total)} ≠ 应为{format_money(expected_total)}", diff_amt))
        if paid + discount > base + late + 0.01:
            hit_list.append(("over_paid", f"已缴+减免={format_money(paid+discount)} > 原应缴{format_money(base+late)}", round(paid + discount - base - late, 2)))
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
                    hit_list.append(("promise_no_contact", "无承诺通话记录", 0.0))

        for ex_type, desc, diff in hit_list:
            anomalies_all.append({
                "check_time": scan_time,
                "exception_type": ex_type,
                "arrear_id": r["id"],
                "room_no": r["room_no"],
                "fee_period": r["fee_period"],
                "description": desc,
                "base_amount": base,
                "late_fee": late,
                "paid_amount": paid,
                "discount_amount": discount,
                "unpaid_amount": unpaid,
                "diff_amount": round(diff, 2),
                "owner_name": r["owner_name"] or "",
                "old_status": r["status"] or "",
            })

    for ae in anomalies_all:
        existing = conn.execute("""
            SELECT id, handle_status, handle_remark, handled_by, handled_at, reviewer, reviewed_at
            FROM audit_exceptions
            WHERE arrear_id = ? AND exception_type = ? AND handle_status != '已修复'
            ORDER BY id DESC LIMIT 1
        """, (ae["arrear_id"], ae["exception_type"])).fetchone()

        if reset_handle:
            conn.execute("DELETE FROM audit_exceptions WHERE arrear_id = ? AND exception_type = ?",
                         (ae["arrear_id"], ae["exception_type"]))
            existing = None

        if existing:
            ae["handle_status"] = existing["handle_status"] or "待处理"
            ae["handle_remark"] = existing["handle_remark"] or ""
            ae["handled_by"] = existing["handled_by"] or ""
            ae["handled_at"] = existing["handled_at"] or ""
            ae["reviewer"] = existing["reviewer"] or ""
            ae["reviewed_at"] = existing["reviewed_at"] or ""
            conn.execute("""
                UPDATE audit_exceptions SET
                    check_time = ?, description = ?,
                    base_amount = ?, late_fee = ?, paid_amount = ?, discount_amount = ?,
                    unpaid_amount = ?, diff_amount = ?
                WHERE id = ?
            """, (scan_time, ae["description"],
                  ae["base_amount"], ae["late_fee"], ae["paid_amount"], ae["discount_amount"],
                  ae["unpaid_amount"], ae["diff_amount"], existing["id"]))
            ae["exception_id"] = existing["id"]
        else:
            cur = conn.execute("""
                INSERT INTO audit_exceptions
                (check_time, exception_type, arrear_id, room_no, fee_period, description,
                 base_amount, late_fee, paid_amount, discount_amount, unpaid_amount, diff_amount,
                 handle_status, handle_remark)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?, '待处理', '')
            """, (
                scan_time, ae["exception_type"], ae["arrear_id"], ae["room_no"], ae["fee_period"],
                ae["description"], ae["base_amount"], ae["late_fee"], ae["paid_amount"],
                ae["discount_amount"], ae["unpaid_amount"], ae["diff_amount"],
            ))
            ae["exception_id"] = cur.lastrowid
            ae["handle_status"] = "待处理"
            ae["handle_remark"] = ""
            ae["handled_by"] = ""
            ae["handled_at"] = ""
            ae["reviewer"] = ""
            ae["reviewed_at"] = ""

    conn.commit()
    conn.close()

    total_anomaly_count = len(anomalies_all)
    display_anomalies = [a for a in anomalies_all if show_handled or a["handle_status"] == "待处理"]

    console.print(f"[bold]扫描完成:[/bold] 共 {len(rows)} 条欠费记录，发现 [red]{total_anomaly_count}[/red] 条异常（含历史），当前待处理 [yellow]{sum(1 for a in anomalies_all if a['handle_status']=='待处理')}[/yellow] 条")

    if total_anomaly_count == 0:
        console.print("[green]数据校验通过，未发现异常[/green]")
        return

    grouped: Dict[str, List[Dict[str, Any]]] = {k: [] for k in ANOMALY_TYPES}
    for a in anomalies_all:
        grouped.setdefault(a["exception_type"], []).append(a)

    summary_rows = []
    csv_rows = []

    for key, label in ANOMALY_TYPES.items():
        records = grouped.get(key, [])
        if not records:
            continue
        show_recs = [r for r in records if show_handled or r["handle_status"] == "待处理"]
        if not show_recs:
            summary_rows.append((label, len(records), key in AUTO_FIXABLE, 0, 0))
            continue
        console.print()
        t = _build_base_table(f"【{label}】共 {len(records)} 条 （显示{len(show_recs)}条待处理）", show_handle_cols=True)
        fixable_mark = " [green]✓可修复[/green]" if key in AUTO_FIXABLE else ""
        handled_count = sum(1 for r in records if r["handle_status"] != "待处理")
        summary_rows.append((label, len(records), key in AUTO_FIXABLE, handled_count, len(records) - handled_count))

        for r in show_recs:
            status_style = {"待处理": "yellow", "已确认": "blue", "已修复": "green", "暂缓处理": "dim"}.get(r["handle_status"], "white")
            h_info = f"{r.get('handled_by','')} {r.get('handled_at','')[:10]}".strip()
            t.add_row(
                str(r["exception_id"]), str(r["arrear_id"]), r["room_no"], r["owner_name"],
                r["fee_period"], format_money(r["base_amount"]), format_money(r["late_fee"]),
                format_money(r["paid_amount"]), format_money(r["discount_amount"]),
                format_money(r["unpaid_amount"]), r.get("old_status") or "-",
                r["description"],
                f"[{status_style}]{r['handle_status']}[/{status_style}]",
                r.get("handle_remark") or "-",
                h_info or "-",
                r.get("reviewer") or "-",
                (r.get("reviewed_at") or "")[:16] or "-",
            )
        console.print(t)

        for r in records:
            csv_rows.append({
                "异常ID": r["exception_id"],
                "异常类型": ANOMALY_TYPES.get(r["exception_type"], r["exception_type"]),
                "欠费ID": r["arrear_id"],
                "房号": r["room_no"],
                "业主": r["owner_name"],
                "账期": r["fee_period"],
                "本金": f"{r['base_amount']:.2f}",
                "滞纳金": f"{r['late_fee']:.2f}",
                "已缴": f"{r['paid_amount']:.2f}",
                "减免": f"{r['discount_amount']:.2f}",
                "尚欠": f"{r['unpaid_amount']:.2f}",
                "差额": f"{r['diff_amount']:.2f}",
                "原状态": r.get("old_status", ""),
                "说明": r["description"],
                "扫描时间": r["check_time"],
                "处理状态": r["handle_status"],
                "处理备注": r.get("handle_remark") or "",
                "处理人": r.get("handled_by") or "",
                "处理时间": r.get("handled_at") or "",
                "复核人": r.get("reviewer") or "",
                "复核时间": r.get("reviewed_at") or "",
            })

    console.print()
    summary_table = Table(title="异常汇总（明细计数自动一致）")
    summary_table.add_column("异常类型", style="bold")
    summary_table.add_column("总数", justify="right", style="white")
    summary_table.add_column("可自动修复", justify="center")
    summary_table.add_column("已处理", justify="right", style="green")
    summary_table.add_column("待处理", justify="right", style="yellow")
    total_fixable = 0
    total_handled = 0
    total_pending = 0
    for label, count, fixable, handled, pending in summary_rows:
        mark = "[green]是[/green]" if fixable else "[dim]否[/dim]"
        if fixable:
            total_fixable += count
        total_handled += handled
        total_pending += pending
        summary_table.add_row(label, str(count), mark, str(handled), str(pending))
    summary_table.add_row("─" * 18, "─" * 5, "─" * 8, "─" * 5, "─" * 5, style="dim")
    summary_table.add_row("[bold]合计[/bold]",
                          f"[bold red]{total_anomaly_count}[/bold red]",
                          f"[bold green]{total_fixable}[/bold green]",
                          f"[bold green]{total_handled}[/bold green]",
                          f"[bold yellow]{total_pending}[/bold yellow]")
    console.print(summary_table)
    if total_anomaly_count != sum(x[1] for x in summary_rows):
        console.print(f"[red]⚠ 汇总与明细不一致：总数{total_anomaly_count} vs 分项合计{sum(x[1] for x in summary_rows)}[/red]")
    else:
        console.print("[dim]✓ 汇总与明细计数一致[/dim]")

    if output_file:
        try:
            with open(output_file, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=[
                    "异常ID", "异常类型", "欠费ID", "房号", "业主", "账期",
                    "本金", "滞纳金", "已缴", "减免", "尚欠", "差额", "原状态",
                    "说明", "扫描时间", "处理状态", "处理备注", "处理人", "处理时间",
                    "复核人", "复核时间"
                ])
                writer.writeheader()
                writer.writerows(csv_rows)
            console.print(f"\n[green]异常清单已导出: {output_file} (共{len(csv_rows)}条，含处理状态)[/green]")
        except Exception as e:
            console.print(f"[red]导出CSV失败: {str(e)}[/red]")

    if auto_fix and total_fixable > 0:
        console.print(f"\n[bold yellow]开始自动修复 {total_fixable} 条异常...[/bold yellow]")
        conn = get_connection()
        fixed_count = 0
        now_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            for a in anomalies_all:
                if a["exception_type"] not in AUTO_FIXABLE:
                    continue
                arrear_id = a["arrear_id"]
                r = conn.execute("SELECT * FROM arrears WHERE id = ?", (arrear_id,)).fetchone()
                if not r:
                    continue
                base = float(r["base_amount"] or 0)
                late = float(r["late_fee"] or 0)
                paid = float(r["paid_amount"] or 0)
                discount = float(r["discount_amount"] or 0)
                unpaid = calc_unpaid(base, late, paid, discount)

                if a["exception_type"] == "paid_but_unpaid":
                    new_status = "承诺付款" if unpaid > 0.01 else "未缴"
                    cr = conn.execute(
                        "SELECT id FROM call_records WHERE arrear_id = ? AND (commitment_date IS NOT NULL OR commitment_amount > 0)",
                        (arrear_id,)
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
                        (new_status, arrear_id)
                    )
                elif a["exception_type"] == "unpaid_but_clear":
                    conn.execute(
                        "UPDATE arrears SET status = '已缴', updated_at = datetime('now','localtime') WHERE id = ?",
                        (arrear_id,)
                    )
                elif a["exception_type"] == "total_mismatch":
                    expected_total = round(base + late - discount, 2)
                    conn.execute(
                        "UPDATE arrears SET total_amount = ?, updated_at = datetime('now','localtime') WHERE id = ?",
                        (expected_total, arrear_id)
                    )
                fixed_count += 1
                conn.execute("""
                    UPDATE audit_exceptions SET
                        handle_status = '已修复',
                        handle_remark = COALESCE(handle_remark||' | ','')||'系统自动修复',
                        handled_by = ?,
                        handled_at = ?
                    WHERE id = ?
                """, (operator, now_time, a["exception_id"]))
            conn.commit()
            console.print(f"[green]自动修复完成，共修复 {fixed_count} 条记录，异常表已标记为已修复[/green]")
        except Exception as e:
            conn.rollback()
            console.print(f"[red]修复失败: {str(e)}[/red]")
        finally:
            conn.close()
    elif auto_fix and total_fixable == 0:
        console.print("\n[dim]没有可自动修复的异常[/dim]")


@check_cmd.command("mark", help="标记异常处理状态：待处理/已确认/已修复/暂缓处理")
@click.option("--exception-id", "exception_id", type=int, default=None, help="按异常ID标记")
@click.option("--arrear-id", "arrear_id", type=int, default=None, help="按欠费ID批量标记该欠费的所有异常")
@click.option("--all-of-type", "all_type", type=click.Choice(list(ANOMALY_TYPES.keys()) + list(ANOMALY_TYPES.values()) + ["all"]), default=None,
              help="按异常类型批量标记（或'all'全部）")
@click.option("--status", required=True, type=click.Choice(HANDLE_STATUS_OPTIONS), help="目标处理状态")
@click.option("--remark", default=None, help="处理备注")
@click.option("--operator", default="财务", help="操作人")
@click.option("--review/--no-review", "review", default=False, help="是否同时标记为已复核")
@click.option("--reviewer", default="财务主管", help="复核人姓名，默认'财务主管'")
def check_mark(exception_id, arrear_id, all_type, status: str, remark: Optional[str], operator: str, review: bool, reviewer: str):
    if exception_id is None and arrear_id is None and all_type is None:
        console.print("[red]请指定 --exception-id、--arrear-id 或 --all-of-type 其中之一[/red]")
        return
    conn = get_connection()
    try:
        sql_parts: List[str] = []
        params: List[Any] = []
        if exception_id is not None:
            sql_parts.append("id = ?")
            params.append(exception_id)
        if arrear_id is not None:
            sql_parts.append("arrear_id = ?")
            params.append(arrear_id)
        if all_type is not None:
            if all_type == "all":
                pass
            elif all_type in ANOMALY_TYPES:
                sql_parts.append("exception_type = ?")
                params.append(all_type)
            else:
                key_by_label = {v: k for k, v in ANOMALY_TYPES.items()}
                if all_type in key_by_label:
                    sql_parts.append("exception_type = ?")
                    params.append(key_by_label[all_type])
        where_sql = (" WHERE " + " AND ".join(sql_parts)) if sql_parts else ""
        select_sql = f"SELECT id, exception_type, arrear_id, room_no, handle_status FROM audit_exceptions{where_sql}"
        rows = conn.execute(select_sql, params).fetchall()
        if not rows:
            console.print("[yellow]未匹配到任何异常记录[/yellow]")
            return
        confirm_msg = f"确认将 {len(rows)} 条异常标记为 [{status}] ？"
        if review:
            confirm_msg += f"\n同时标记为已复核，复核人：{reviewer}"
        if not click.confirm(confirm_msg):
            return
        now_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        count_ok = 0
        for r in rows:
            update_parts = ["handle_status = ?", "handled_by = ?", "handled_at = ?"]
            update_params: List[Any] = [status, operator, now_time]
            if remark:
                update_parts.insert(1, "handle_remark = COALESCE(NULLIF(handle_remark,'') || ' | ','') || ?")
                update_params.insert(1, remark)
            if review:
                update_parts.append("reviewer = ?")
                update_params.append(reviewer)
                update_parts.append("reviewed_at = ?")
                update_params.append(now_time)
            update_sql = f"UPDATE audit_exceptions SET {', '.join(update_parts)} WHERE id = ?"
            update_params.append(r["id"])
            conn.execute(update_sql, update_params)
            count_ok += 1
        conn.commit()
        msg = f"[green]✓ 已更新 {count_ok} 条记录的处理状态为 '{status}'[/green]"
        if review:
            msg += f" [blue]（同时已复核，复核人：{reviewer}）[/blue]"
        console.print(msg)
        table = Table(title=f"标记明细（前{min(10, count_ok)}条）")
        table.add_column("异常ID", style="dim")
        table.add_column("欠费ID", style="white")
        table.add_column("房号", style="cyan")
        table.add_column("异常类型", style="magenta")
        table.add_column("原状态", style="yellow")
        table.add_column("→ 新状态", style="green")
        if review:
            table.add_column("复核人", style="blue")
            table.add_column("复核时间", style="dim")
        for r in rows[:10]:
            row_data = [str(r["id"]), str(r["arrear_id"]), r["room_no"],
                        ANOMALY_TYPES.get(r["exception_type"], r["exception_type"]),
                        r["handle_status"] or "待处理", status]
            if review:
                row_data.extend([reviewer, now_time])
            table.add_row(*row_data)
        console.print(table)
    except Exception as e:
        conn.rollback()
        console.print(f"[red]标记失败: {str(e)}[/red]")
    finally:
        conn.close()


@check_cmd.command("list", help="查询已扫描的异常记录，支持按处理状态筛选")
@click.option("--status", type=click.Choice(HANDLE_STATUS_OPTIONS + ["全部"]), default="全部", help="处理状态筛选")
@click.option("--room", default=None, help="按房号模糊匹配")
@click.option("--type", "atype", default=None, help="按异常类型筛选（名称或key）")
@click.option("--output", "-o", default=None, help="导出到CSV")
@click.option("--limit", type=int, default=100, help="显示条数")
def check_list(status: str, room: Optional[str], atype: Optional[str], output: Optional[str], limit: int):
    conn = get_connection()
    try:
        sql = "SELECT * FROM audit_exceptions WHERE 1=1"
        params: List[Any] = []
        if status != "全部":
            sql += " AND handle_status = ?"
            params.append(status)
        if room:
            sql += " AND room_no LIKE ?"
            params.append(f"%{room}%")
        if atype:
            if atype in ANOMALY_TYPES:
                sql += " AND exception_type = ?"
                params.append(atype)
            else:
                key_by_label = {v: k for k, v in ANOMALY_TYPES.items()}
                if atype in key_by_label:
                    sql += " AND exception_type = ?"
                    params.append(key_by_label[atype])
        sql += " ORDER BY check_time DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        if not rows:
            console.print("[yellow]未找到异常记录[/yellow]")
            return
        t = _build_base_table(f"异常记录（共 {len(rows)} 条，显示最多{limit}条）", show_handle_cols=True)
        csv_rows = []
        for r in rows:
            status_style = {"待处理": "yellow", "已确认": "blue", "已修复": "green", "暂缓处理": "dim"}.get(r["handle_status"] or "待处理", "white")
            h_info = f"{r['handled_by'] or ''} {r['handled_at'] or ''}"[:20].strip()
            t.add_row(
                str(r["id"]), str(r["arrear_id"] or "-"), r["room_no"] or "-",
                "", r["fee_period"] or "",
                format_money(r["base_amount"] or 0), format_money(r["late_fee"] or 0),
                format_money(r["paid_amount"] or 0), format_money(r["discount_amount"] or 0),
                format_money(r["unpaid_amount"] or 0),
                ANOMALY_TYPES.get(r["exception_type"], r["exception_type"]),
                r["description"] or "",
                f"[{status_style}]{r['handle_status'] or '待处理'}[/{status_style}]",
                r["handle_remark"] or "-", h_info or "-",
                r["reviewer"] or "-",
                (r["reviewed_at"] or "")[:16] or "-",
            )
            csv_rows.append({
                "异常ID": r["id"],
                "异常类型": ANOMALY_TYPES.get(r["exception_type"], r["exception_type"]),
                "欠费ID": r["arrear_id"],
                "房号": r["room_no"],
                "账期": r["fee_period"],
                "说明": r["description"],
                "尚欠": f"{r['unpaid_amount'] or 0:.2f}",
                "扫描时间": r["check_time"],
                "处理状态": r["handle_status"] or "待处理",
                "处理备注": r["handle_remark"] or "",
                "处理人": r["handled_by"] or "",
                "处理时间": r["handled_at"] or "",
                "复核人": r["reviewer"] or "",
                "复核时间": r["reviewed_at"] or "",
            })
        console.print(t)
        if output:
            with open(output, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
                writer.writeheader()
                writer.writerows(csv_rows)
            console.print(f"[green]✓ 已导出 {len(csv_rows)} 条到 {output}[/green]")
    finally:
        conn.close()


@check_cmd.command("export-summary", help="按楼栋+处理状态导出异常汇总台账")
@click.option("--output", "-o", "output_file", required=True, help="导出CSV路径（必填）")
@click.option("--period", "-p", default=None, help="按异常记录check_time的年月筛选，格式如2026-05")
@click.option("--operator", default="系统", help="导出操作人/备注，默认'系统'")
def export_summary(output_file: str, period: Optional[str], operator: str):
    import os

    conn = get_connection()
    try:
        where_parts = []
        params: List[Any] = []
        if period:
            where_parts.append("strftime('%Y-%m', ae.check_time) = ?")
            params.append(period)

        where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

        detail_sql = f"""
            SELECT ae.id AS exception_id,
                   COALESCE(h.building, '未知') AS building,
                   ae.room_no,
                   COALESCE(h.owner_name, '') AS owner_name,
                   ae.fee_period,
                   ae.exception_type,
                   COALESCE(ae.handle_status, '待处理') AS handle_status,
                   COALESCE(ae.handled_by, '') AS handled_by,
                   COALESCE(ae.handled_at, '') AS handled_at,
                   COALESCE(ae.handle_remark, '') AS handle_remark,
                   COALESCE(ae.reviewer, '') AS reviewer,
                   COALESCE(ae.reviewed_at, '') AS reviewed_at,
                   ae.check_time
            FROM audit_exceptions ae
            LEFT JOIN arrears ar ON ae.arrear_id = ar.id
            LEFT JOIN households h ON (h.room_no = ae.room_no OR h.id = ar.household_id)
            {where_sql}
            ORDER BY COALESCE(h.building, '未知'), ae.room_no, ae.fee_period, ae.id
        """
        detail_rows = conn.execute(detail_sql, params).fetchall()

        if not detail_rows:
            console.print("[yellow]未找到任何异常记录[/yellow]")
            return

        building_stats: Dict[str, Dict[str, Any]] = {}
        last_scan_per_building: Dict[str, str] = {}

        for r in detail_rows:
            b = r["building"] or "未知"
            if b not in building_stats:
                building_stats[b] = {
                    "total": 0,
                    "待处理": 0,
                    "已确认": 0,
                    "已修复": 0,
                    "暂缓处理": 0,
                    "reviewed": 0,
                    "not_reviewed": 0,
                }
            building_stats[b]["total"] += 1
            status = r["handle_status"] or "待处理"
            if status in building_stats[b]:
                building_stats[b][status] += 1
            if r["reviewer"] and r["reviewed_at"]:
                building_stats[b]["reviewed"] += 1
            else:
                building_stats[b]["not_reviewed"] += 1
            ct = r["check_time"] or ""
            if ct:
                if b not in last_scan_per_building or ct > last_scan_per_building[b]:
                    last_scan_per_building[b] = ct

        summary_rows = []
        sorted_buildings = sorted(building_stats.keys())
        for b in sorted_buildings:
            s = building_stats[b]
            total = s["total"]
            pending = s["待处理"]
            pending_ratio = f"{(pending / total * 100):.2f}%" if total > 0 else "0.00%"
            reviewed = s["reviewed"]
            not_reviewed = s["not_reviewed"]
            review_rate = f"{(reviewed / total * 100):.2f}%" if total > 0 else "0.00%"
            summary_rows.append({
                "楼栋": b,
                "异常总数": total,
                "待处理": pending,
                "已确认": s["已确认"],
                "已修复": s["已修复"],
                "暂缓处理": s["暂缓处理"],
                "待处理占比": pending_ratio,
                "已复核数": reviewed,
                "未复核数": not_reviewed,
                "复核率": review_rate,
                "最后扫描时间": last_scan_per_building.get(b, ""),
            })

        with open(output_file, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "楼栋", "异常总数", "待处理", "已确认", "已修复", "暂缓处理",
                "待处理占比", "已复核数", "未复核数", "复核率", "最后扫描时间"
            ])
            writer.writeheader()
            writer.writerows(summary_rows)
        console.print(f"[green]✓ 汇总台账已导出: {output_file}（共 {len(summary_rows)} 栋楼）[/green]")

        base, ext = os.path.splitext(output_file)
        detail_file = f"{base}_detail{ext}"

        with open(detail_file, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "异常ID", "楼栋", "房号", "业主", "账期", "异常类型",
                "处理状态", "处理人", "处理时间", "处理备注", "复核人", "复核时间"
            ])
            writer.writeheader()
            for r in detail_rows:
                writer.writerow({
                    "异常ID": r["exception_id"],
                    "楼栋": r["building"],
                    "房号": r["room_no"] or "",
                    "业主": r["owner_name"] or "",
                    "账期": r["fee_period"] or "",
                    "异常类型": ANOMALY_TYPES.get(r["exception_type"], r["exception_type"]),
                    "处理状态": r["handle_status"] or "待处理",
                    "处理人": r["handled_by"] or "",
                    "处理时间": r["handled_at"] or "",
                    "处理备注": r["handle_remark"] or "",
                    "复核人": r["reviewer"] or "",
                    "复核时间": r["reviewed_at"] or "",
                })
        console.print(f"[green]✓ 明细数据已导出: {detail_file}（共 {len(detail_rows)} 条）[/green]")

        grand_total = sum(s["异常总数"] for s in summary_rows)
        grand_reviewed = sum(s["已复核数"] for s in summary_rows)
        grand_review_rate = f"{(grand_reviewed / grand_total * 100):.2f}%" if grand_total > 0 else "0.00%"
        console.print(f"[dim]导出操作人: {operator} | 异常总计: {grand_total} | 总复核率: {grand_review_rate}[/dim]")

    except Exception as e:
        console.print(f"[red]导出失败: {str(e)}[/red]")
    finally:
        conn.close()
