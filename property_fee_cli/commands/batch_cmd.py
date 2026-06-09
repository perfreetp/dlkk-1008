import click
from datetime import datetime
from typing import List, Dict, Any, Optional
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from ..database import get_connection
from ..utils import calc_unpaid, UNPAID_SQL_EXPR

console = Console()


def _gen_batch_no(batch_type: str) -> str:
    now = datetime.now()
    prefix = {"短信": "S", "电话": "C", "律师函前": "L", "综合": "M"}.get(batch_type, "B")
    return f"{prefix}{now.strftime('%Y%m%d%H%M%S')}"


@click.group(help="催缴批次管理（短信/电话/律师函前提醒等）")
def batch_cmd():
    pass


@batch_cmd.command("create", help="创建新的催缴批次")
@click.option("--name", required=True, help="批次名称，如'2026年6月第一轮短信'")
@click.option("--type", "batch_type", default="短信", help="批次类型：短信/电话/律师函前/综合")
@click.option("--desc", "description", default=None, help="批次说明")
@click.option("--from-building", default=None, help="按楼栋筛选欠费（可选）")
@click.option("--from-min-unpaid", default=0.01, type=float, help="只加入尚欠大于该值的欠费")
@click.option("--from-auto", is_flag=True, help="自动加载所有未缴的欠费为本批次成员")
@click.option("--operator", default="系统", help="创建人")
def batch_create(name: str, batch_type: str, description: Optional[str],
                 from_building: Optional[str], from_min_unpaid: float,
                 from_auto: bool, operator: str):
    batch_no = _gen_batch_no(batch_type)
    conn = get_connection()
    try:
        conn.execute("""
            INSERT INTO batches
            (batch_no, batch_name, batch_type, description, created_by)
            VALUES (?,?,?,?,?)
        """, (batch_no, name, batch_type, description, operator))
        batch_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

        member_rows = []
        if from_auto or from_building:
            sql = f"""
                SELECT a.id as arrear_id, h.id as household_id, h.room_no, h.owner_name,
                       {UNPAID_SQL_EXPR} as unpaid_calc
                FROM arrears a JOIN households h ON a.household_id = h.id
                WHERE {UNPAID_SQL_EXPR} >= ?
            """
            params: List[Any] = [from_min_unpaid]
            if from_building:
                sql += " AND h.building = ?"
                params.append(from_building)
            for ar in conn.execute(sql, params).fetchall():
                member_rows.append((
                    batch_id, ar["household_id"], ar["arrear_id"],
                    ar["room_no"], ar["owner_name"], ar["unpaid_calc"] or 0,
                ))
            if member_rows:
                conn.executemany("""
                    INSERT OR IGNORE INTO batch_members
                    (batch_id, household_id, arrear_id, room_no, owner_name, unpaid_amount)
                    VALUES (?,?,?,?,?,?)
                """, member_rows)
                conn.execute("UPDATE batches SET target_count = ?, status = '进行中' WHERE id = ?",
                             (len(member_rows), batch_id))

        conn.commit()
        console.print(Panel(
            f"批次编号: [cyan]{batch_no}[/cyan]\n批次名称: [green]{name}[/green]\n"
            f"批次类型: {batch_type}    创建人: {operator}\n"
            f"[bold]加载成员: {len(member_rows)} 户[/bold]",
            title="批次创建成功", style="green",
        ))
        console.print(f"使用 [cyan]pfee batch add {batch_id} --arrear-id <id>[/cyan] 手动加单户")
        console.print(f"使用 [cyan]pfee notice generate --batch {batch_id}[/cyan] 为该批次生成通知")
        console.print(f"使用 [cyan]pfee send all --batch {batch_id}[/cyan] 按批次发送")
    except Exception as e:
        conn.rollback()
        console.print(f"[red]创建失败: {e}[/red]")
    finally:
        conn.close()


@batch_cmd.command("add", help="向批次添加单条欠费")
@click.argument("batch_id", type=int)
@click.option("--arrear-id", required=True, type=int, help="欠费ID")
def batch_add(batch_id: int, arrear_id: int):
    conn = get_connection()
    try:
        ba = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if not ba:
            console.print(f"[red]批次ID {batch_id} 不存在[/red]")
            return
        ar = conn.execute(f"""
            SELECT a.id as arrear_id, h.id as household_id, h.room_no, h.owner_name,
                   {UNPAID_SQL_EXPR} as unpaid_calc
            FROM arrears a JOIN households h ON a.household_id = h.id
            WHERE a.id = ?
        """, (arrear_id,)).fetchone()
        if not ar:
            console.print(f"[red]欠费ID {arrear_id} 不存在[/red]")
            return
        conn.execute("""
            INSERT OR IGNORE INTO batch_members
            (batch_id, household_id, arrear_id, room_no, owner_name, unpaid_amount)
            VALUES (?,?,?,?,?,?)
        """, (batch_id, ar["household_id"], arrear_id, ar["room_no"], ar["owner_name"], ar["unpaid_calc"] or 0))
        conn.execute("UPDATE batches SET target_count = (SELECT COUNT(*) FROM batch_members WHERE batch_id = ?) WHERE id = ?",
                     (batch_id, batch_id))
        conn.commit()
        console.print(f"[green]✓ 已添加: {ar['room_no']} {ar['unpaid_calc']:,.2f}元 -> 批次 {ba['batch_name']}[/green]")
    finally:
        conn.close()


@batch_cmd.command("remove", help="从批次移除一条欠费")
@click.argument("batch_id", type=int)
@click.option("--arrear-id", required=True, type=int, help="欠费ID")
def batch_remove(batch_id: int, arrear_id: int):
    conn = get_connection()
    try:
        conn.execute("DELETE FROM batch_members WHERE batch_id = ? AND arrear_id = ?", (batch_id, arrear_id))
        conn.execute("UPDATE batches SET target_count = (SELECT COUNT(*) FROM batch_members WHERE batch_id = ?) WHERE id = ?",
                     (batch_id, batch_id))
        conn.commit()
        console.print(f"[green]✓ 已从批次 {batch_id} 移除欠费 {arrear_id}[/green]")
    finally:
        conn.close()


def _refresh_batch_stats(conn, batch_id: int) -> None:
    m = conn.execute("SELECT * FROM batch_members WHERE batch_id = ?", (batch_id,)).fetchall()
    target = len(m)
    sent = sum(1 for r in m if r["notice_status"] and r["notice_status"] != "未发送")
    succ = sum(1 for r in m if r["notice_status"] == "发送成功")
    fail = sum(1 for r in m if r["notice_status"] == "发送失败")
    call = sum(1 for r in m if r["call_status"] and r["call_status"] != "未联系")
    prom = sum(1 for r in m if r["commitment_status"] and r["commitment_status"] != "未承诺")
    repaid_amt = sum(float(r["repayment_amount"] or 0) for r in m)
    repaid_cnt = sum(1 for r in m if (r["repayment_amount"] or 0) > 0)
    conn.execute("""
        UPDATE batches SET
            target_count=?, sent_count=?, success_count=?, fail_count=?,
            call_count=?, promised_count=?, repaid_count=?, repaid_amount=?
        WHERE id=?
    """, (target, sent, succ, fail, call, prom, repaid_cnt, repaid_amt, batch_id))


@batch_cmd.command("list", help="查看所有催缴批次")
@click.option("--status", default=None, help="按状态筛选：草稿/进行中/已关闭")
def batch_list(status: Optional[str]):
    conn = get_connection()
    try:
        sql = "SELECT * FROM batches WHERE 1=1"
        params: List[Any] = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY id DESC"
        rows = conn.execute(sql, params).fetchall()
        if not rows:
            console.print("[yellow]暂无批次记录[/yellow]")
            return

        table = Table(title="催缴批次列表")
        table.add_column("ID", style="cyan")
        table.add_column("批次编号", style="white")
        table.add_column("批次名称", style="magenta")
        table.add_column("类型", style="yellow")
        table.add_column("状态", style="green")
        table.add_column("目标", justify="right")
        table.add_column("发送成功/失败", justify="center")
        table.add_column("已联系", justify="right")
        table.add_column("承诺", justify="right")
        table.add_column("回款(元)", justify="right", style="green")
        table.add_column("创建人", style="white")
        table.add_column("创建时间", style="white")

        for r in rows:
            status_color = {"草稿": "white", "进行中": "cyan", "已关闭": "green"}.get(r["status"] or "草稿", "white")
            table.add_row(
                str(r["id"]), r["batch_no"], r["batch_name"], r["batch_type"] or "-",
                f"[{status_color}]{r['status'] or '草稿'}[/{status_color}]",
                str(r["target_count"] or 0),
                f"{r['success_count'] or 0}/{r['fail_count'] or 0}",
                str(r["call_count"] or 0),
                str(r["promised_count"] or 0),
                f"{r['repaid_amount'] or 0:,.2f}",
                r["created_by"] or "-",
                (r["created_at"] or "")[:16],
            )
        console.print(table)
    finally:
        conn.close()


@batch_cmd.command("members", help="查看批次成员明细")
@click.argument("batch_id", type=int)
@click.option("--status", default=None, help="通知状态筛选")
@click.option("--no-closed", is_flag=True, help="只显示未回款的成员")
def batch_members(batch_id: int, status: Optional[str], no_closed: bool):
    conn = get_connection()
    try:
        _refresh_batch_stats(conn, batch_id)
        conn.commit()
        ba = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if not ba:
            console.print(f"[red]批次ID {batch_id} 不存在[/red]")
            return
        sql = "SELECT bm.* FROM batch_members bm WHERE batch_id = ?"
        params: List[Any] = [batch_id]
        if status:
            sql += " AND bm.notice_status LIKE ?"
            params.append(f"%{status}%")
        if no_closed:
            sql += " AND bm.repayment_status != '全部回款'"
        sql += " ORDER BY bm.id"
        rows = conn.execute(sql, params).fetchall()
        if not rows:
            console.print("[yellow]该批次没有成员[/yellow]")
            return

        table = Table(title=f"批次成员：{ba['batch_name']}（{len(rows)}户）")
        table.add_column("ID", style="cyan")
        table.add_column("房号", style="white")
        table.add_column("业主", style="magenta")
        table.add_column("原尚欠(元)", justify="right", style="yellow")
        table.add_column("通知", style="cyan")
        table.add_column("联系", style="blue")
        table.add_column("承诺", style="green")
        table.add_column("回款", style="green")
        table.add_column("已回款(元)", justify="right", style="green")
        for r in rows:
            table.add_row(
                str(r["id"]), r["room_no"] or "-", r["owner_name"] or "-",
                f"{r['unpaid_amount'] or 0:,.2f}",
                r["notice_status"] or "-",
                r["call_status"] or "-",
                r["commitment_status"] or "-",
                r["repayment_status"] or "-",
                f"{r['repayment_amount'] or 0:,.2f}",
            )
        console.print(table)
    finally:
        conn.close()


@batch_cmd.command("report", help="批次复盘报表：覆盖/发送/联系/回款")
@click.argument("batch_id", type=int)
@click.option("--output", "-o", default=None, help="导出CSV路径")
@click.option("--export-unclosed", default=None, help="额外导出未闭环名单CSV")
def batch_report(batch_id: int, output: Optional[str], export_unclosed: Optional[str]):
    import csv
    conn = get_connection()
    try:
        _refresh_batch_stats(conn, batch_id)
        conn.commit()
        ba = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if not ba:
            console.print(f"[red]批次ID {batch_id} 不存在[/red]")
            return

        rows = conn.execute("SELECT * FROM batch_members WHERE batch_id = ? ORDER BY id", (batch_id,)).fetchall()
        if not rows:
            console.print("[yellow]批次无成员数据[/yellow]")
            return

        notice_succ = sum(1 for r in rows if r["notice_status"] == "发送成功")
        notice_fail = sum(1 for r in rows if r["notice_status"] == "发送失败")
        notice_pending = sum(1 for r in rows if r["notice_status"] in ("待发送", "待发送(重试)"))
        contacted = sum(1 for r in rows if r["call_status"] not in (None, "未联系", "-", ""))
        promised = sum(1 for r in rows if r["commitment_status"] not in (None, "未承诺", "-", ""))
        total_unpaid = sum(float(r["unpaid_amount"] or 0) for r in rows)
        repaid_amt = sum(float(r["repayment_amount"] or 0) for r in rows)
        full_closed = sum(1 for r in rows if r["repayment_status"] == "全部回款")
        partial_closed = sum(1 for r in rows if r["repayment_status"] == "部分回款")
        not_closed = sum(1 for r in rows if r["repayment_status"] in (None, "未回款", "", "-"))

        console.print(Panel(
            f" 批次名称: [bold]{ba['batch_name']}[/bold] ({ba['batch_type']})\n"
            f" 批次编号: {ba['batch_no']}    状态: {ba['status']}\n"
            f"────── 覆盖 ──────\n"
            f" 目标户数: {ba['target_count']}    覆盖欠费本金+滞: {total_unpaid:,.2f} 元\n"
            f"────── 发送结果 ──────\n"
            f" [green]发送成功: {notice_succ}[/green]   [red]发送失败: {notice_fail}[/red]   待发送: {notice_pending}\n"
            f" 发送覆盖率: {notice_succ*100/(ba['target_count'] or 1):.1f}%\n"
            f"────── 联系结果 ──────\n"
            f" 电话已联系: {contacted}    承诺付款: {promised}\n"
            f"────── 回款效果 ──────\n"
            f" [green]全部回款: {full_closed}[/green]   [yellow]部分回款: {partial_closed}[/yellow]   [red]未回款: {not_closed}[/red]\n"
            f" 回款金额: {repaid_amt:,.2f} 元    回款率: {repaid_amt*100/(total_unpaid or 1):.1f}%",
            title=f"批次复盘报表 #{ba['id']}", style="cyan",
        ))

        if output:
            with open(output, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["批次ID", ba["id"]])
                writer.writerow(["批次编号", ba["batch_no"]])
                writer.writerow(["批次名称", ba["batch_name"]])
                writer.writerow(["批次类型", ba["batch_type"]])
                writer.writerow([])
                writer.writerow(["指标", "数值"])
                writer.writerow(["目标户数", ba["target_count"]])
                writer.writerow(["发送成功", notice_succ])
                writer.writerow(["发送失败", notice_fail])
                writer.writerow(["待发送", notice_pending])
                writer.writerow(["已联系", contacted])
                writer.writerow(["承诺付款", promised])
                writer.writerow(["全部回款户数", full_closed])
                writer.writerow(["部分回款户数", partial_closed])
                writer.writerow(["未回款户数", not_closed])
                writer.writerow(["总欠费金额(元)", f"{total_unpaid:.2f}"])
                writer.writerow(["回款金额(元)", f"{repaid_amt:.2f}"])
                writer.writerow([])
                writer.writerow(["成员ID", "房号", "业主", "原尚欠", "通知状态", "联系状态", "承诺状态", "回款状态", "已回款(元)", "备注"])
                for r in rows:
                    writer.writerow([
                        r["id"], r["room_no"], r["owner_name"],
                        f"{r['unpaid_amount'] or 0:.2f}",
                        r["notice_status"] or "", r["call_status"] or "",
                        r["commitment_status"] or "", r["repayment_status"] or "",
                        f"{r['repayment_amount'] or 0:.2f}", r["remark"] or "",
                    ])
            console.print(f"[green]✓ 批次报表已导出: {output}[/green]")

        if export_unclosed:
            unclosed = [r for r in rows if r["repayment_status"] != "全部回款"]
            with open(export_unclosed, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["批次", "房号", "业主", "原尚欠(元)", "当前尚欠(元)", "已回款(元)", "通知状态", "联系状态", "承诺状态", "回款状态"])
                for r in unclosed:
                    cur_unpaid = 0
                    if r["arrear_id"]:
                        x = conn.execute(f"SELECT {UNPAID_SQL_EXPR} as u FROM arrears a WHERE a.id = ?",
                                         (r["arrear_id"],)).fetchone()
                        if x:
                            cur_unpaid = x["u"] or 0
                    writer.writerow([
                        ba["batch_name"], r["room_no"], r["owner_name"],
                        f"{r['unpaid_amount'] or 0:.2f}", f"{cur_unpaid:.2f}",
                        f"{r['repayment_amount'] or 0:.2f}",
                        r["notice_status"] or "", r["call_status"] or "",
                        r["commitment_status"] or "", r["repayment_status"] or "",
                    ])
            console.print(f"[yellow]✓ 未闭环名单已导出: {export_unclosed} ({len(unclosed)}户)[/yellow]")
    finally:
        conn.close()


@batch_cmd.command("close", help="关闭批次（月底复盘归档用）")
@click.argument("batch_id", type=int)
@click.option("--operator", default="系统", help="操作人")
def batch_close(batch_id: int, operator: str):
    conn = get_connection()
    try:
        _refresh_batch_stats(conn, batch_id)
        conn.execute("UPDATE batches SET status='已关闭', closed_at=datetime('now','localtime') WHERE id=?", (batch_id,))
        conn.commit()
        console.print(f"[green]✓ 批次 {batch_id} 已关闭归档[/green]")
    finally:
        conn.close()
