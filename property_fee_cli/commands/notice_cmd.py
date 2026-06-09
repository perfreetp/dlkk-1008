import click
from datetime import date
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from typing import Optional, Dict, Any

from ..database import get_connection
from ..utils import (
    mask_phone, mask_name, should_hide_sensitive, format_money,
    is_holiday, next_workday, get_config, get_sms_signature, calc_unpaid,
    UNPAID_SQL_EXPR,
)

console = Console()


@click.group(help="管理催缴通知：模板、预览、生成提醒内容")
def notice_cmd():
    pass


@notice_cmd.group("template", help="短信模板管理")
def template_group():
    pass


@template_group.command("list", help="查看所有短信模板")
def list_templates():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM notice_templates ORDER BY is_default DESC, id ASC").fetchall()
    conn.close()

    if not rows:
        console.print("[yellow]暂无模板[/yellow]")
        return

    for r in rows:
        default_tag = " [green][默认][/green]" if r["is_default"] else ""
        title = f"[bold cyan]{r['name']}[/bold cyan] (ID: {r['id']}){default_tag}"
        console.print(Panel(r["content"], title=title, border_style="blue", expand=False))
        console.print()


@template_group.command("add", help="新增短信模板")
@click.option("--name", "-n", required=True, help="模板名称")
@click.option("--content", "-c", required=True, help="模板内容，可用变量: {owner_name}, {room_no}, {building}, {phone}, {fee_period}, {fee_type}, {base_amount}, {late_fee}, {total_amount}, {due_date}, {unpaid_amount}, {company_name}, {service_phone}")
@click.option("--default", is_flag=True, help="设为默认模板")
def add_template(name: str, content: str, default: bool):
    conn = get_connection()
    try:
        if default:
            conn.execute("UPDATE notice_templates SET is_default = 0")
        conn.execute(
            "INSERT INTO notice_templates (name, content, is_default) VALUES (?, ?, ?)",
            (name, content, 1 if default else 0),
        )
        conn.commit()
        console.print(f"[green]模板 '{name}' 已添加[/green]")
    except Exception as e:
        console.print(f"[red]添加失败: {str(e)}[/red]")
    finally:
        conn.close()


@template_group.command("set-default", help="设置默认模板")
@click.argument("template_id", type=int)
def set_default_template(template_id: int):
    conn = get_connection()
    row = conn.execute("SELECT id FROM notice_templates WHERE id = ?", (template_id,)).fetchone()
    if not row:
        console.print(f"[red]模板ID {template_id} 不存在[/red]")
        conn.close()
        return
    conn.execute("UPDATE notice_templates SET is_default = 0")
    conn.execute("UPDATE notice_templates SET is_default = 1 WHERE id = ?", (template_id,))
    conn.commit()
    conn.close()
    console.print(f"[green]默认模板已设置为 ID={template_id}[/green]")


@template_group.command("delete", help="删除模板")
@click.argument("template_id", type=int)
def delete_template(template_id: int):
    conn = get_connection()
    row = conn.execute("SELECT name, is_default FROM notice_templates WHERE id = ?", (template_id,)).fetchone()
    if not row:
        console.print(f"[red]模板ID {template_id} 不存在[/red]")
        conn.close()
        return
    if row["is_default"]:
        console.print("[red]默认模板不能删除，请先设置其他默认模板[/red]")
        conn.close()
        return
    if click.confirm(f"确认删除模板 '{row['name']}'？"):
        conn.execute("DELETE FROM notice_templates WHERE id = ?", (template_id,))
        conn.commit()
        console.print("[green]模板已删除[/green]")
    conn.close()


def _build_template_vars(arrear_row, hide_sensitive: bool) -> Dict[str, Any]:
    name = arrear_row["owner_name"]
    phone = arrear_row["phone"] or ""
    if hide_sensitive:
        name = mask_name(name)
        phone = mask_phone(phone)
    unpaid = calc_unpaid(arrear_row["base_amount"], arrear_row["late_fee"], arrear_row["paid_amount"], arrear_row["discount_amount"])
    total_owed = round(float(arrear_row["base_amount"] or 0) + float(arrear_row["late_fee"] or 0) - float(arrear_row["discount_amount"] or 0), 2)
    return {
        "owner_name": name,
        "room_no": arrear_row["room_no"],
        "building": arrear_row["building"],
        "phone": phone,
        "fee_period": arrear_row["fee_period"],
        "fee_type": arrear_row["fee_type"],
        "base_amount": format_money(arrear_row["base_amount"]),
        "late_fee": format_money(arrear_row["late_fee"]),
        "total_amount": format_money(total_owed),
        "unpaid_amount": format_money(unpaid),
        "due_date": arrear_row["due_date"],
        "company_name": get_config("company_name", "XX物业服务有限公司"),
        "service_phone": get_config("service_phone", "400-123-4567"),
        "signature": get_sms_signature(),
    }


def _render_template(content: str, vars: Dict[str, Any]) -> str:
    try:
        return content.format(**vars)
    except KeyError as e:
        return f"[模板变量错误: 缺少 {e}]"


@notice_cmd.command("preview", help="预览催缴通知内容")
@click.option("--building", "-b", help="按楼栋筛选")
@click.option("--room", "-r", help="按房号筛选")
@click.option("--period", "-p", help="按账期筛选")
@click.option("--template-id", "-t", type=int, help="使用指定模板ID，默认使用默认模板")
@click.option("--template-name", help="按模板名称选择")
@click.option("--unpaid-only", is_flag=True, default=True, help="仅显示未缴清的欠费")
@click.option("--hide-sensitive/--show-sensitive", default=None)
@click.option("--limit", "-n", type=int, default=5, help="预览条数，默认5条")
@click.option("--avoid-holiday", is_flag=True, default=True, help="发送日期避让节假日")
def preview_notice(building, room, period, template_id, template_name, unpaid_only,
                   hide_sensitive, limit, avoid_holiday):
    hide = hide_sensitive if hide_sensitive is not None else should_hide_sensitive()

    conn = get_connection()
    if template_id:
        tpl = conn.execute("SELECT * FROM notice_templates WHERE id = ?", (template_id,)).fetchone()
    elif template_name:
        tpl = conn.execute("SELECT * FROM notice_templates WHERE name = ?", (template_name,)).fetchone()
    else:
        tpl = conn.execute("SELECT * FROM notice_templates WHERE is_default = 1").fetchone()
        if not tpl:
            tpl = conn.execute("SELECT * FROM notice_templates LIMIT 1").fetchone()

    if not tpl:
        console.print("[red]未找到可用的短信模板[/red]")
        conn.close()
        return

    sql = """
        SELECT a.*, h.room_no, h.building, h.owner_name, h.phone
        FROM arrears a JOIN households h ON a.household_id = h.id
        WHERE 1=1
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
    if unpaid_only:
        sql += f" AND {UNPAID_SQL_EXPR} > 0"
    sql += " ORDER BY h.building, h.room_no LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    if not rows:
        console.print("[yellow]未找到匹配的欠费记录[/yellow]")
        return

    send_date = date.today()
    if avoid_holiday and is_holiday(send_date):
        send_date = next_workday(send_date)

    console.print(f"[bold]使用模板:[/bold] {tpl['name']} (ID: {tpl['id']})")
    if avoid_holiday:
        console.print(f"[bold]计划发送日期:[/bold] {send_date} {'(已避让节假日)' if send_date != date.today() else ''}")
    console.print(f"[bold]敏感信息隐藏:[/bold] {'开启' if hide else '关闭'}\n")

    for r in rows:
        vars = _build_template_vars(r, hide)
        content = _render_template(tpl["content"], vars)
        display_phone = vars["phone"] or "[red]无手机号[/red]"
        title = f"[cyan]{r['room_no']}[/cyan] · {vars['owner_name']} · [yellow]{display_phone}[/yellow]"
        unpaid_show = calc_unpaid(r["base_amount"], r["late_fee"], r["paid_amount"], r["discount_amount"])
        console.print(Panel(content, title=title, border_style="green", expand=False, subtitle=f"账期: {r['fee_period']}  尚欠: {format_money(unpaid_show)}元"))
        console.print()


@notice_cmd.command("generate", help="生成催缴通知记录（写入待发送队列）")
@click.option("--building", "-b", help="按楼栋筛选")
@click.option("--room", "-r", help="按房号筛选")
@click.option("--period", "-p", help="按账期筛选")
@click.option("--template-id", "-t", type=int, help="使用指定模板ID")
@click.option("--min-amount", type=float, help="最低欠费金额")
@click.option("--channel", default="短信", help="发送渠道，默认短信")
@click.option("--max-retry", type=int, default=3, help="失败最大重试次数")
@click.option("--dry-run", is_flag=True, help="仅预览不写入")
@click.option("--batch", "batch_id", type=int, default=None, help="关联到指定批次ID")
def generate_notice(building, room, period, template_id, min_amount, channel, max_retry, dry_run, batch_id):
    conn = get_connection()

    if template_id:
        tpl = conn.execute("SELECT * FROM notice_templates WHERE id = ?", (template_id,)).fetchone()
    else:
        tpl = conn.execute("SELECT * FROM notice_templates WHERE is_default = 1").fetchone()
    if not tpl:
        console.print("[red]未找到短信模板[/red]")
        conn.close()
        return

    if batch_id:
        ba = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if not ba:
            console.print(f"[red]批次ID {batch_id} 不存在[/red]")
            conn.close()
            return
        if ba["status"] and ba["status"] not in ("草稿", "进行中"):
            console.print(f"[yellow]⚠ 批次 {ba['batch_name']} 已是'{ba['status']}'，可能已归档[/yellow]")

    sql = f"""
        SELECT a.*, h.room_no, h.building, h.owner_name, h.phone,
               {UNPAID_SQL_EXPR} as unpaid_calc
        FROM arrears a JOIN households h ON a.household_id = h.id
        WHERE {UNPAID_SQL_EXPR} > 0
          AND h.phone IS NOT NULL AND h.phone != ''
    """
    params = []
    if batch_id:
        sql += " AND a.id IN (SELECT arrear_id FROM batch_members WHERE batch_id = ? AND arrear_id IS NOT NULL)"
        params.append(batch_id)
    if building:
        sql += " AND h.building LIKE ?"
        params.append(f"%{building}%")
    if room:
        sql += " AND h.room_no LIKE ?"
        params.append(f"%{room}%")
    if period:
        sql += " AND a.fee_period LIKE ?"
        params.append(f"%{period}%")
    if min_amount:
        sql += f" AND {UNPAID_SQL_EXPR} >= ?"
        params.append(min_amount)
    sql += " ORDER BY h.building, h.room_no"

    rows = conn.execute(sql, params).fetchall()
    if not rows:
        console.print("[yellow]未找到符合条件的欠费记录[/yellow]")
        conn.close()
        return

    if dry_run:
        console.print(f"[yellow]Dry-run: 将生成 {len(rows)} 条待发送记录[/yellow]")
        table = Table(title="待生成通知预览")
        table.add_column("房号", style="cyan")
        table.add_column("业主", style="magenta")
        table.add_column("手机", style="yellow")
        table.add_column("账期", style="white")
        table.add_column("尚欠", justify="right", style="bold red")
        for r in rows[:20]:
            unpaid = calc_unpaid(r["base_amount"], r["late_fee"], r["paid_amount"], r["discount_amount"])
            phone = mask_phone(r["phone"]) if should_hide_sensitive() else r["phone"]
            name = mask_name(r["owner_name"]) if should_hide_sensitive() else r["owner_name"]
            table.add_row(r["room_no"], name, phone, r["fee_period"], format_money(unpaid))
        if len(rows) > 20:
            table.add_row("...", "...", "...", "...", "...")
        console.print(table)
        conn.close()
        return

    if not click.confirm(f"确认生成 {len(rows)} 条催缴通知？"):
        conn.close()
        return

    generated = 0
    skipped = 0
    from ..utils import is_sms_real_configured
    real_cfg = is_sms_real_configured()
    is_mock_val = 0 if real_cfg else 1
    provider_val = conn.execute("SELECT value FROM config WHERE key='sms_provider'").fetchone()
    provider = (provider_val["value"] if provider_val and provider_val["value"] else "mock") if real_cfg else "mock"

    for r in rows:
        vars = _build_template_vars(r, hide_sensitive=False)
        content = _render_template(tpl["content"], vars)
        try:
            cur = conn.execute("""
                INSERT INTO notice_records
                (arrear_id, household_id, channel, template_name, content, phone, status, retry_count, max_retry, is_mock, provider, batch_id)
                VALUES (?, ?, ?, ?, ?, ?, '待发送', 0, ?, ?, ?, ?)
            """, (r["id"], r["household_id"], channel, tpl["name"], content, r["phone"], max_retry, is_mock_val, provider, batch_id))
            notice_id = cur.lastrowid
            if batch_id:
                conn.execute("""
                    UPDATE batch_members SET notice_status='待发送'
                    WHERE batch_id=? AND arrear_id=?
                """, (batch_id, r["id"]))
            generated += 1
        except Exception as e:
            skipped += 1
            console.print(f"[yellow]跳过 {r['room_no']}: {e}[/yellow]")

    if batch_id:
        from .batch_cmd import _refresh_batch_stats
        _refresh_batch_stats(conn, batch_id)

    conn.commit()
    conn.close()
    console.print(f"[green]已生成 {generated} 条待发送通知[/green]，跳过 {skipped} 条")
    if batch_id:
        console.print(f"批次ID={batch_id} 已关联")


@notice_cmd.command("queue", help="查看待发送队列")
@click.option("--status", "-s", type=click.Choice(["待发送", "发送中", "已发送", "发送失败"]), help="按状态筛选")
@click.option("--limit", "-n", type=int, default=20, help="显示条数")
def notice_queue(status, limit):
    conn = get_connection()
    sql = "SELECT nr.*, h.room_no, h.owner_name FROM notice_records nr JOIN households h ON nr.household_id = h.id"
    params = []
    if status:
        sql += " WHERE nr.status = ?"
        params.append(status)
    sql += " ORDER BY nr.created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()

    counts = conn.execute("""
        SELECT status, COUNT(*) as cnt FROM notice_records GROUP BY status
    """).fetchall()
    conn.close()

    count_info = " | ".join(f"{c['status']}: {c['cnt']}" for c in counts)
    console.print(f"[bold]队列统计:[/bold] {count_info or '空'}\n")

    if not rows:
        console.print("[yellow]队列为空[/yellow]")
        return

    table = Table(title=f"通知队列（显示最近{limit}条）")
    table.add_column("ID", style="white", no_wrap=True)
    table.add_column("房号", style="cyan")
    table.add_column("业主", style="magenta")
    table.add_column("渠道", style="blue")
    table.add_column("模板", style="yellow")
    table.add_column("手机号", style="yellow")
    table.add_column("类型", style="white")
    table.add_column("状态", style="white")
    table.add_column("重试", justify="right")
    table.add_column("发送时间", style="white")

    for r in rows:
        hide = should_hide_sensitive()
        phone = mask_phone(r["phone"]) if hide else (r["phone"] or "-")
        name = mask_name(r["owner_name"]) if hide else r["owner_name"]
        status_style = {"待发送": "cyan", "发送中": "yellow", "已发送": "green", "发送失败": "red"}.get(r["status"], "white")
        is_mock = bool(r["is_mock"]) if r["is_mock"] is not None else True
        type_style = "yellow" if is_mock else "green"
        type_text = "模拟" if is_mock else "真实"
        table.add_row(
            str(r["id"]), r["room_no"], name, r["channel"], r["template_name"] or "-",
            phone, f"[{type_style}]{type_text}[/{type_style}]",
            f"[{status_style}]{r['status']}[/{status_style}]",
            f"{r['retry_count']}/{r['max_retry']}",
            r["sent_at"] or r["created_at"],
        )
    console.print(table)
