import click
import csv
from datetime import datetime
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from typing import Optional, List, Dict, Any

from ..database import get_connection
from ..utils import calc_unpaid, format_money, UNPAID_SQL_EXPR

console = Console()


@click.group(help="月结归档与月报 - 生成账期结算快照、归档后调整留痕、导出月报")
def archive_cmd():
    pass


def _find_latest_snapshot_for_arrear(conn, arrear_id: int) -> Optional[Dict[str, Any]]:
    """找到某欠费记录对应的最近归档快照"""
    row = conn.execute("""
        SELECT si.snapshot_id, ss.period, ss.snapshot_no,
               si.base_amount as snap_base, si.late_fee as snap_late,
               si.paid_amount as snap_paid, si.discount_amount as snap_disc,
               si.unpaid_amount as snap_unpaid
        FROM settlement_items si
        JOIN settlement_snapshots ss ON si.snapshot_id = ss.id
        WHERE si.arrear_id = ?
        ORDER BY ss.snapshot_time DESC LIMIT 1
    """, (arrear_id,)).fetchone()
    return dict(row) if row else None


def _write_adjustment_if_archived(conn, arrear_id: int, adjust_type: str,
                                  adjust_amount: float, remark: str,
                                  operator: str) -> Optional[int]:
    """若欠费已归档，写调整记录，返回adjustment_id；未归档返回None"""
    if not arrear_id:
        return None
    snap = _find_latest_snapshot_for_arrear(conn, arrear_id)
    if not snap:
        return None
    ar = conn.execute(f"""
        SELECT a.*, {UNPAID_SQL_EXPR} as u
        FROM arrears a WHERE a.id = ?
    """, (arrear_id,)).fetchone()
    if not ar:
        return None
    cur = conn.execute("""
        INSERT INTO adjustment_records
        (snapshot_id, arrear_id, household_id, adjust_type, adjust_amount,
         original_unpaid, final_unpaid, operator, remark)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (
        snap["snapshot_id"], arrear_id, ar["household_id"], adjust_type,
        adjust_amount, snap["snap_unpaid"],
        float(ar["u"] or 0), operator, remark,
    ))
    return cur.lastrowid


@archive_cmd.command("create", help="按月/楼栋生成账期结算快照（月结归档）")
@click.option("--period", "-p", required=True, help="账期，如 2026-05 或 2026-Q2")
@click.option("--building", "-b", default=None, help="按楼栋筛选，默认全部楼栋")
@click.option("--description", default=None, help="归档备注")
@click.option("--operator", default="财务月结", help="操作人")
def archive_create(period: str, building: Optional[str], description: Optional[str], operator: str):
    conn = get_connection()
    try:
        existing = conn.execute("""
            SELECT id FROM settlement_snapshots WHERE period = ? AND COALESCE(building,'') = COALESCE(?, '')
        """, (period, building or "")).fetchone()
        if existing and not click.confirm(f"{period}{(' ['+building+']') if building else ''} 已有归档，覆盖重建？"):
            return

        where_sql = ""
        params: List[Any] = []
        if building:
            where_sql = " WHERE h.building = ?"
            params.append(building)
        items_sql = f"""
            SELECT a.id, a.household_id, a.fee_period, a.fee_type,
                   a.base_amount, a.late_fee, a.paid_amount, a.discount_amount,
                   a.status, {UNPAID_SQL_EXPR} as unpaid_calc,
                   h.room_no, h.owner_name, h.building
            FROM arrears a JOIN households h ON a.household_id = h.id
            {where_sql}
            ORDER BY h.building, h.room_no, a.fee_period
        """
        rows = conn.execute(items_sql, params).fetchall()
        if not rows:
            console.print("[yellow]没有符合条件的欠费记录[/yellow]")
            return

        snap_no = f"SNAP-{period}-{datetime.now().strftime('%m%d%H%M')}"
        if building:
            snap_no += f"-{building}"

        tot_base = sum(float(r["base_amount"] or 0) for r in rows)
        tot_late = sum(float(r["late_fee"] or 0) for r in rows)
        tot_paid = sum(float(r["paid_amount"] or 0) for r in rows)
        tot_disc = sum(float(r["discount_amount"] or 0) for r in rows)
        tot_unpaid = sum(float(r["unpaid_calc"] or 0) for r in rows)
        hh_set = set(r["household_id"] for r in rows)

        if existing:
            conn.execute("DELETE FROM settlement_items WHERE snapshot_id = ?", (existing["id"],))
            conn.execute("DELETE FROM settlement_snapshots WHERE id = ?", (existing["id"],))

        cur = conn.execute("""
            INSERT INTO settlement_snapshots
            (snapshot_no, period, building, description, created_by,
             total_households, total_arrears, snap_base_amount, snap_late_fee,
             snap_paid_amount, snap_discount_amount, snap_unpaid_amount)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            snap_no, period, building, description, operator,
            len(hh_set), len(rows), round(tot_base, 2), round(tot_late, 2),
            round(tot_paid, 2), round(tot_disc, 2), round(tot_unpaid, 2),
        ))
        snap_id = cur.lastrowid

        for r in rows:
            conn.execute("""
                INSERT INTO settlement_items
                (snapshot_id, arrear_id, household_id, room_no, owner_name,
                 fee_period, base_amount, late_fee, paid_amount, discount_amount,
                 unpaid_amount, status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                snap_id, r["id"], r["household_id"], r["room_no"], r["owner_name"] or "",
                r["fee_period"], float(r["base_amount"] or 0), float(r["late_fee"] or 0),
                float(r["paid_amount"] or 0), float(r["discount_amount"] or 0),
                float(r["unpaid_calc"] or 0), r["status"] or "",
            ))

        conn.commit()
        console.print(Panel(
            f"[bold]归档编号:[/bold] {snap_no}\n"
            f"[bold]账期/楼栋:[/bold] {period}{(' / '+building) if building else ' / 全部楼栋'}\n"
            f"[bold]覆盖住户:[/bold] {len(hh_set)} 户    [bold]欠费记录:[/bold] {len(rows)} 条\n"
            f"本金合计: {format_money(tot_base)} 元    滞纳金合计: {format_money(tot_late)} 元\n"
            f"已缴合计: {format_money(tot_paid)} 元    减免合计: {format_money(tot_disc)} 元\n"
            f"[bold red]尚欠合计: {format_money(tot_unpaid)} 元[/bold red]\n"
            f"操作人: {operator}    时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            title="月结归档快照生成成功", style="green",
        ))
    except Exception as e:
        conn.rollback()
        console.print(f"[red]归档失败: {e}[/red]")
    finally:
        conn.close()


@archive_cmd.command("list", help="列出所有月结归档快照")
@click.option("--period", "-p", default=None, help="按账期筛选")
@click.option("--limit", default=20, help="显示条数")
def archive_list(period: Optional[str], limit: int):
    conn = get_connection()
    try:
        sql = "SELECT * FROM settlement_snapshots WHERE 1=1"
        params: List[Any] = []
        if period:
            sql += " AND period LIKE ?"
            params.append(f"%{period}%")
        sql += " ORDER BY snapshot_time DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        if not rows:
            console.print("[yellow]暂无归档快照[/yellow]")
            return
        t = Table(title=f"月结归档列表（共{len(rows)}条）")
        t.add_column("ID", style="dim")
        t.add_column("归档编号", style="cyan")
        t.add_column("账期", style="white")
        t.add_column("楼栋", style="magenta")
        t.add_column("户数", justify="right")
        t.add_column("欠费数", justify="right")
        t.add_column("尚欠合计(元)", justify="right", style="bold red")
        t.add_column("操作人", style="white")
        t.add_column("归档时间", style="yellow")
        for r in rows:
            t.add_row(
                str(r["id"]), r["snapshot_no"], r["period"],
                r["building"] or "全部", str(r["total_households"]),
                str(r["total_arrears"]), format_money(r["snap_unpaid_amount"] or 0),
                r["created_by"] or "-", r["snapshot_time"] or "-",
            )
        console.print(t)
    finally:
        conn.close()


@archive_cmd.command("show", help="查看归档快照详情")
@click.argument("snapshot_id", type=int)
@click.option("--output", "-o", default=None, help="导出归档明细到CSV")
def archive_show(snapshot_id: int, output: Optional[str]):
    conn = get_connection()
    try:
        snap = conn.execute("SELECT * FROM settlement_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
        if not snap:
            console.print(f"[red]快照ID {snapshot_id} 不存在[/red]")
            return
        items = conn.execute("""
            SELECT si.*, a.status as current_status,
                   a.base_amount as cur_base, a.late_fee as cur_late,
                   a.paid_amount as cur_paid, a.discount_amount as cur_disc,
                   (COALESCE(a.base_amount,0)+COALESCE(a.late_fee,0)-COALESCE(a.paid_amount,0)-COALESCE(a.discount_amount,0)) as cur_unpaid
            FROM settlement_items si
            LEFT JOIN arrears a ON si.arrear_id = a.id
            WHERE si.snapshot_id = ?
            ORDER BY si.room_no, si.fee_period
        """, (snapshot_id,)).fetchall()

        console.print(Panel(
            f"编号: {snap['snapshot_no']}    账期: {snap['period']}    楼栋: {snap['building'] or '全部'}\n"
            f"本金: {format_money(snap['snap_base_amount'])}  滞纳金: {format_money(snap['snap_late_fee'])}\n"
            f"已缴: {format_money(snap['snap_paid_amount'])}  减免: {format_money(snap['snap_discount_amount'])}\n"
            f"[bold]归档尚欠: {format_money(snap['snap_unpaid_amount'])}[/bold]   备注: {snap['description'] or '-'}",
            title=f"归档快照 #{snapshot_id}", style="cyan",
        ))

        t = Table(title=f"明细（共{len(items)}条，显示前30条）")
        t.add_column("房号", style="cyan")
        t.add_column("业主", style="white")
        t.add_column("账期", style="white")
        t.add_column("本金", justify="right", style="blue")
        t.add_column("滞纳金", justify="right", style="yellow")
        t.add_column("已缴", justify="right", style="green")
        t.add_column("减免", justify="right", style="magenta")
        t.add_column("归档尚欠", justify="right", style="bold red")
        t.add_column("当前尚欠", justify="right")
        t.add_column("差额", justify="right")
        csv_rows = []
        for it in items:
            snap_u = float(it["unpaid_amount"] or 0)
            cur_u = float(it["cur_unpaid"] or 0)
            diff = round(cur_u - snap_u, 2)
            diff_col = f"[green]{format_money(diff)}[/green]" if diff < 0 else (f"[yellow]{format_money(diff)}[/yellow]" if diff > 0 else "-")
            t.add_row(
                it["room_no"], it["owner_name"] or "", it["fee_period"],
                format_money(it["base_amount"]), format_money(it["late_fee"]),
                format_money(it["paid_amount"]), format_money(it["discount_amount"]),
                format_money(snap_u), format_money(cur_u), diff_col,
            )
            csv_rows.append({
                "房号": it["room_no"], "业主": it["owner_name"] or "",
                "账期": it["fee_period"],
                "本金": f"{it['base_amount']:.2f}", "滞纳金": f"{it['late_fee']:.2f}",
                "已缴": f"{it['paid_amount']:.2f}", "减免": f"{it['discount_amount']:.2f}",
                "归档尚欠": f"{snap_u:.2f}", "当前尚欠": f"{cur_u:.2f}",
                "差额(当前-归档)": f"{diff:.2f}",
                "归档状态": it["status"] or "",
                "当前状态": it["current_status"] or "",
            })
        console.print(t)
        if len(items) > 30:
            console.print(f"[dim]...剩余{len(items)-30}条，完整数据请加 --output 导出[/dim]")
        if output:
            with open(output, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
                writer.writeheader()
                writer.writerows(csv_rows)
            console.print(f"[green]✓ 归档明细已导出: {output} ({len(csv_rows)}条)[/green]")
    finally:
        conn.close()


@archive_cmd.command("adjustments", help="查看归档后的调整记录")
@click.option("--snapshot-id", type=int, default=None, help="按快照ID筛选")
@click.option("--room", default=None, help="按房号筛选")
@click.option("--type", "atype", default=None, help="调整类型: 缴费/减免/滞纳金/其他")
@click.option("--limit", default=50, help="显示条数")
def archive_adjustments(snapshot_id: Optional[int], room: Optional[str], atype: Optional[str], limit: int):
    conn = get_connection()
    try:
        sql = """
            SELECT ar.*, h.room_no, ss.snapshot_no, ss.period
            FROM adjustment_records ar
            LEFT JOIN households h ON ar.household_id = h.id
            LEFT JOIN settlement_snapshots ss ON ar.snapshot_id = ss.id
            WHERE 1=1
        """
        params: List[Any] = []
        if snapshot_id:
            sql += " AND ar.snapshot_id = ?"
            params.append(snapshot_id)
        if room:
            sql += " AND h.room_no LIKE ?"
            params.append(f"%{room}%")
        if atype:
            sql += " AND ar.adjust_type LIKE ?"
            params.append(f"%{atype}%")
        sql += " ORDER BY ar.created_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        if not rows:
            console.print("[yellow]暂无调整记录[/yellow]")
            return
        t = Table(title=f"归档后调整记录（共{len(rows)}条）")
        t.add_column("ID", style="dim")
        t.add_column("快照", style="cyan")
        t.add_column("账期", style="white")
        t.add_column("房号", style="cyan")
        t.add_column("类型", style="magenta")
        t.add_column("调整额(元)", justify="right", style="yellow")
        t.add_column("原尚欠", justify="right")
        t.add_column("新尚欠", justify="right", style="bold red")
        t.add_column("操作人", style="white")
        t.add_column("时间", style="yellow")
        t.add_column("备注", style="dim", max_width=20)
        for r in rows:
            t.add_row(
                str(r["id"]), r["snapshot_no"] or "-", r["period"] or "-",
                r["room_no"] or "-", r["adjust_type"],
                format_money(r["adjust_amount"] or 0),
                format_money(r["original_unpaid"] or 0),
                format_money(r["final_unpaid"] or 0),
                r["operator"] or "-", (r["created_at"] or "")[:16],
                (r["remark"] or "")[:20],
            )
        console.print(t)
    finally:
        conn.close()
