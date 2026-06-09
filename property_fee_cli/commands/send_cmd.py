import click
import time
import random
from datetime import datetime, date
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn
from typing import Optional

from ..database import get_connection
from ..utils import (
    mask_phone, mask_name, should_hide_sensitive, format_money,
    is_holiday, next_workday, get_config
)

console = Console()


def _mock_send_sms(phone: str, content: str) -> tuple[bool, str]:
    """模拟短信发送。实际使用时替换为真实短信API调用。"""
    time.sleep(0.05)
    rand = random.random()
    if rand < 0.08:
        return False, "模拟失败: 网关超时"
    elif rand < 0.12:
        return False, "模拟失败: 号码格式错误"
    return True, "发送成功"


def _is_send_allowed_now() -> tuple[bool, str]:
    """检查当前时段是否允许发送（避让节假日+时段限制）"""
    today = date.today()
    hour_start = int(get_config("sms_send_hour_start", "9"))
    hour_end = int(get_config("sms_send_hour_end", "20"))

    if is_holiday(today):
        next_wd = next_workday(today)
        return False, f"今日是节假日/周末，建议推迟至 {next_wd} 发送"

    now_hour = datetime.now().hour
    if now_hour < hour_start:
        return False, f"当前时间早于允许发送时间 {hour_start}:00"
    if now_hour >= hour_end:
        return False, f"当前时间晚于允许发送时间 {hour_end}:00"

    return True, "可以发送"


@click.group(help="批量发送催缴提醒，支持失败重试")
def send_cmd():
    pass


@send_cmd.command("all", help="批量发送队列中所有待发送的通知")
@click.option("--batch-size", type=int, default=100, help="每批发送数量")
@click.option("--interval", type=float, default=0.1, help="每条发送间隔秒数")
@click.option("--force", is_flag=True, help="强制发送（忽略节假日和时段限制）")
@click.option("--dry-run", is_flag=True, help="仅模拟发送，不更新状态")
@click.option("--retry-failed", is_flag=True, help="同时重试之前发送失败的通知")
def send_all(batch_size, interval, force, dry_run, retry_failed):
    if not force:
        allowed, reason = _is_send_allowed_now()
        if not allowed:
            console.print(f"[yellow]{reason}[/yellow]")
            if not click.confirm("是否继续强制发送？"):
                return

    conn = get_connection()
    status_filter = ("'待发送'",)
    if retry_failed:
        status_filter = ("'待发送', '发送失败'")

    total_row = conn.execute(f"""
        SELECT COUNT(*) as cnt FROM notice_records
        WHERE status IN ({','.join(status_filter)}) AND retry_count < max_retry
    """).fetchone()
    total = total_row["cnt"]

    if total == 0:
        console.print("[yellow]没有可发送的通知（队列为空或已超过最大重试次数）[/yellow]")
        conn.close()
        return

    console.print(f"[bold]待发送通知: {total} 条[/bold]")
    if not click.confirm("确认开始发送？"):
        conn.close()
        return

    rows = conn.execute(f"""
        SELECT nr.*, h.room_no, h.owner_name, h.phone as h_phone
        FROM notice_records nr JOIN households h ON nr.household_id = h.id
        WHERE nr.status IN ({','.join(status_filter)}) AND nr.retry_count < nr.max_retry
        ORDER BY nr.created_at ASC
    """).fetchall()

    success_count = 0
    fail_count = 0
    skip_count = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("发送中...", total=len(rows))

        for r in rows:
            progress.update(task, description=f"发送 {r['room_no']}...")

            if r["retry_count"] >= r["max_retry"]:
                skip_count += 1
                progress.advance(task)
                continue

            nr_id = r["id"]
            phone = r["phone"] or r["h_phone"]
            content = r["content"]

            if not phone:
                if not dry_run:
                    conn.execute("""
                        UPDATE notice_records SET status = '发送失败', error_msg = '无手机号',
                            retry_count = retry_count + 1
                        WHERE id = ?
                    """, (nr_id,))
                fail_count += 1
                progress.advance(task)
                continue

            ok, msg = _mock_send_sms(phone, content)

            if not dry_run:
                if ok:
                    conn.execute("""
                        UPDATE notice_records SET status = '已发送', sent_at = datetime('now','localtime'),
                            error_msg = NULL, retry_count = retry_count + 1
                        WHERE id = ?
                    """, (nr_id,))
                    success_count += 1
                else:
                    new_retry = r["retry_count"] + 1
                    new_status = "发送失败" if new_retry >= r["max_retry"] else "发送失败"
                    conn.execute("""
                        UPDATE notice_records SET status = ?, error_msg = ?,
                            retry_count = ?, sent_at = datetime('now','localtime')
                        WHERE id = ?
                    """, (new_status, msg, new_retry, nr_id))
                    fail_count += 1
            else:
                if ok:
                    success_count += 1
                else:
                    fail_count += 1

            time.sleep(interval)
            progress.advance(task)

    if not dry_run:
        conn.commit()
    conn.close()

    console.print("\n[bold]===== 发送结果汇总 =====[/bold]")
    console.print(f"  [green]成功: {success_count} 条[/green]")
    console.print(f"  [red]失败: {fail_count} 条[/red]")
    if skip_count > 0:
        console.print(f"  [yellow]跳过(超重试): {skip_count} 条[/yellow]")
    if dry_run:
        console.print(f"  [yellow]* 这是dry-run模式，未实际更新数据库[/yellow]")


@send_cmd.command("retry", help="重试发送失败的通知")
@click.option("--record-id", type=int, help="仅重试指定ID的通知")
@click.option("--all", "retry_all", is_flag=True, help="重试所有失败的通知")
@click.option("--max-retry", type=int, help="覆盖最大重试次数限制")
@click.option("--force", is_flag=True, help="忽略时段限制")
@click.option("--dry-run", is_flag=True)
def retry_failed(record_id, retry_all, max_retry, force, dry_run):
    if not record_id and not retry_all:
        console.print("[red]请指定 --record-id 或 --all[/red]")
        return

    if not force:
        allowed, reason = _is_send_allowed_now()
        if not allowed:
            console.print(f"[yellow]{reason}[/yellow]")
            if not click.confirm("是否继续发送？"):
                return

    conn = get_connection()

    if record_id:
        row = conn.execute("""
            SELECT nr.*, h.room_no, h.owner_name, h.phone as h_phone
            FROM notice_records nr JOIN households h ON nr.household_id = h.id
            WHERE nr.id = ?
        """, (record_id,)).fetchone()
        if not row:
            console.print(f"[red]通知ID {record_id} 不存在[/red]")
            conn.close()
            return
        rows = [row]
    else:
        rows = conn.execute("""
            SELECT nr.*, h.room_no, h.owner_name, h.phone as h_phone
            FROM notice_records nr JOIN households h ON nr.household_id = h.id
            WHERE nr.status = '发送失败'
        """).fetchall()

    if not rows:
        console.print("[yellow]没有可重试的失败通知[/yellow]")
        conn.close()
        return

    console.print(f"[bold]待重试: {len(rows)} 条[/bold]")
    if not click.confirm("确认重试？"):
        conn.close()
        return

    success = 0
    fail = 0

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), TaskProgressColumn(), console=console,
    ) as progress:
        task = progress.add_task("重试中...", total=len(rows))
        for r in rows:
            progress.update(task, description=f"重试 {r['room_no']}...")
            phone = r["phone"] or r["h_phone"]
            effective_max = max_retry if max_retry else r["max_retry"]
            current_retry = r["retry_count"]

            if current_retry >= effective_max and not max_retry:
                fail += 1
                if not dry_run:
                    conn.execute("UPDATE notice_records SET error_msg = '超过最大重试次数' WHERE id = ?", (r["id"],))
                progress.advance(task)
                continue

            ok, msg = _mock_send_sms(phone, r["content"])
            new_retry = current_retry + 1
            if not dry_run:
                if ok:
                    conn.execute("""
                        UPDATE notice_records SET status = '已发送',
                            sent_at = datetime('now','localtime'), error_msg = NULL,
                            retry_count = ?, max_retry = ?
                        WHERE id = ?
                    """, (new_retry, effective_max, r["id"]))
                    success += 1
                else:
                    new_status = "发送失败"
                    conn.execute("""
                        UPDATE notice_records SET status = ?, error_msg = ?,
                            retry_count = ?, max_retry = ?, sent_at = datetime('now','localtime')
                        WHERE id = ?
                    """, (new_status, msg, new_retry, effective_max, r["id"]))
                    fail += 1
            else:
                if ok:
                    success += 1
                else:
                    fail += 1
            time.sleep(0.05)
            progress.advance(task)

    if not dry_run:
        conn.commit()
    conn.close()

    console.print(f"\n[green]成功: {success}[/green], [red]失败: {fail}[/red]")


@send_cmd.command("status", help="查看发送统计")
def send_status():
    conn = get_connection()
    by_status = conn.execute("""
        SELECT status, COUNT(*) as cnt,
               COALESCE(SUM(CASE WHEN sent_at IS NOT NULL THEN 1 END), 0) as sent_cnt
        FROM notice_records GROUP BY status
    """).fetchall()

    by_date = conn.execute("""
        SELECT DATE(sent_at) as d, status, COUNT(*) as cnt
        FROM notice_records
        WHERE sent_at IS NOT NULL
        GROUP BY DATE(sent_at), status
        ORDER BY d DESC LIMIT 10
    """).fetchall()

    fail_detail = conn.execute("""
        SELECT error_msg, COUNT(*) as cnt
        FROM notice_records
        WHERE status = '发送失败' AND error_msg IS NOT NULL
        GROUP BY error_msg ORDER BY cnt DESC LIMIT 10
    """).fetchall()
    conn.close()

    console.print("[bold]===== 发送状态统计 =====[/bold]")
    total = 0
    for s in by_status:
        total += s["cnt"]
        style = {"待发送": "cyan", "发送中": "yellow", "已发送": "green", "发送失败": "red"}.get(s["status"], "white")
        console.print(f"  [{style}]{s['status']}: {s['cnt']} 条[/{style}]")
    console.print(f"  [bold]合计: {total} 条[/bold]\n")

    if by_date:
        console.print("[bold]===== 近期发送趋势 =====[/bold]")
        table = Table()
        table.add_column("日期", style="white")
        table.add_column("状态", style="white")
        table.add_column("数量", justify="right")
        for d in by_date:
            style = {"已发送": "green", "发送失败": "red"}.get(d["status"], "white")
            table.add_row(d["d"] or "-", f"[{style}]{d['status']}[/{style}]", str(d["cnt"]))
        console.print(table)

    if fail_detail:
        console.print("\n[bold]===== 失败原因TOP10 =====[/bold]")
        table = Table()
        table.add_column("失败原因", style="red")
        table.add_column("次数", justify="right")
        for f in fail_detail:
            table.add_row(f["error_msg"], str(f["cnt"]))
        console.print(table)


@send_cmd.command("config", help="查看/设置发送配置")
@click.option("--hour-start", type=int, help="允许发送的起始小时")
@click.option("--hour-end", type=int, help="允许发送的结束小时")
@click.option("--hide-sensitive", type=click.Choice(["on", "off"]), help="敏感信息隐藏开关")
def send_config(hour_start, hour_end, hide_sensitive):
    from ..utils import set_config as _set_config

    if hour_start is not None:
        if hour_start < 0 or hour_start > 23:
            console.print("[red]起始小时应在0-23之间[/red]")
            return
        _set_config("sms_send_hour_start", str(hour_start))
        console.print(f"[green]发送起始时间已设置为: {hour_start}:00[/green]")

    if hour_end is not None:
        if hour_end < 1 or hour_end > 24:
            console.print("[red]结束小时应在1-24之间[/red]")
            return
        _set_config("sms_send_hour_end", str(hour_end))
        console.print(f"[green]发送结束时间已设置为: {hour_end}:00[/green]")

    if hide_sensitive is not None:
        val = "1" if hide_sensitive == "on" else "0"
        _set_config("hide_sensitive", val)
        console.print(f"[green]敏感信息隐藏: {hide_sensitive}[/green]")

    if hour_start is None and hour_end is None and hide_sensitive is None:
        console.print("[bold]当前发送配置:[/bold]")
        console.print(f"  发送时段: {get_config('sms_send_hour_start', '9')}:00 - {get_config('sms_send_hour_end', '20')}:00")
        console.print(f"  敏感信息隐藏: {'开启' if should_hide_sensitive() else '关闭'}")
        allowed, reason = _is_send_allowed_now()
        console.print(f"  当前状态: {'[green]可发送[/green]' if allowed else f'[yellow]{reason}[/yellow]'}")


@send_cmd.command("test", help="测试发送一条短信")
@click.option("--phone", "-p", required=True, help="测试手机号")
@click.option("--template-id", type=int, help="使用模板ID")
def send_test(phone, template_id):
    conn = get_connection()
    if template_id:
        tpl = conn.execute("SELECT * FROM notice_templates WHERE id = ?", (template_id,)).fetchone()
    else:
        tpl = conn.execute("SELECT * FROM notice_templates WHERE is_default = 1").fetchone()
    conn.close()

    if not tpl:
        console.print("[red]未找到模板[/red]")
        return

    test_vars = {
        "owner_name": mask_name("测试用户") if should_hide_sensitive() else "测试用户",
        "room_no": "测试房号",
        "building": "测试楼栋",
        "phone": mask_phone(phone) if should_hide_sensitive() else phone,
        "fee_period": "2026-01",
        "fee_type": "物业费",
        "base_amount": "100.00",
        "late_fee": "5.00",
        "total_amount": "105.00",
        "unpaid_amount": "105.00",
        "due_date": "2026-01-15",
        "company_name": get_config("company_name", "XX物业"),
        "service_phone": get_config("service_phone", "400-123-4567"),
    }
    content = tpl["content"].format(**test_vars)
    console.print(f"[bold]模板:[/bold] {tpl['name']}\n")
    console.print(Panel(content, title=f"测试内容 → {mask_phone(phone) if should_hide_sensitive() else phone}", border_style="yellow"))

    if click.confirm("\n确认发送测试短信？"):
        ok, msg = _mock_send_sms(phone, content)
        if ok:
            console.print(f"[green]{msg}[/green]")
        else:
            console.print(f"[red]{msg}[/red]")
