import click
import time
import random
import hashlib
import urllib.request
import urllib.parse
import json as _json
from datetime import datetime, date
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn
from typing import Optional

from ..database import get_connection
from ..utils import (
    mask_phone, mask_name, should_hide_sensitive, format_money,
    is_holiday, next_workday, get_config, set_config,
    is_sms_real_configured, calc_unpaid,
)

console = Console()


def _call_real_sms_api(cfg: dict, phone: str, content: str) -> tuple[bool, str, str]:
    """调用真实短信网关。返回(成功,消息,短信ID)"""
    try:
        provider = cfg["sms_provider"]
        url = cfg["sms_api_url"]
        app_key = cfg["sms_app_key"]
        app_secret = cfg["sms_app_secret"]
        sign = cfg["sms_signature"] or ""
        tpl_code = cfg["sms_template_code"] or ""

        ts = str(int(time.time()))
        sig_str = f"{app_key}{ts}{app_secret}"
        sig = hashlib.md5(sig_str.encode("utf-8")).hexdigest()

        payload = urllib.parse.urlencode({
            "provider": provider,
            "appkey": app_key,
            "timestamp": ts,
            "sign": sig,
            "signature": sign,
            "template_code": tpl_code,
            "phone": phone,
            "content": content,
        }).encode("utf-8")

        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")

        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="ignore")

        try:
            data = _json.loads(body)
            ok = str(data.get("code", "1")) == "0" or str(data.get("success", False)).lower() == "true"
            msg = data.get("message", data.get("msg", body[:200]))
            msg_id = str(data.get("msgid", data.get("msg_id", data.get("bizId", ""))))
            return ok, str(msg), msg_id
        except _json.JSONDecodeError:
            return resp.status == 200, body[:200], ""

    except Exception as e:
        return False, f"接口异常: {str(e)[:200]}", ""


def _mock_send_sms(phone: str, content: str) -> tuple[bool, str, str]:
    """模拟发送，用于未配置真实服务时，返回带MOCK前缀的msgid"""
    time.sleep(0.02)
    rand = random.random()
    msg_id = f"MOCK{int(time.time()*1000)}{random.randint(1000,9999)}"
    if rand < 0.08:
        return False, "模拟失败: 网关超时", msg_id
    elif rand < 0.12:
        return False, "模拟失败: 号码格式错误", msg_id
    return True, "模拟发送成功", msg_id


def _is_send_allowed_now() -> tuple[bool, str]:
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


@click.group(help="批量发送催缴提醒，支持失败重试与真实短信配置")
def send_cmd():
    pass


@send_cmd.group("sms-config", help="真实短信服务配置")
def sms_config_group():
    pass


@sms_config_group.command("set", help="设置短信服务参数")
@click.option("--provider", type=click.Choice(["aliyun", "tencent", "huawei", "cloopen", "custom"]), help="短信供应商")
@click.option("--signature", "-s", help="短信签名")
@click.option("--api-url", "-u", help="接口地址URL")
@click.option("--app-key", "-k", help="AppKey/AccessKey")
@click.option("--app-secret", "-x", help="AppSecret/SecretKey")
@click.option("--template-code", "-t", help="短信模板编号")
def set_sms_config(provider, signature, api_url, app_key, app_secret, template_code):
    updates = []
    if provider is not None:
        set_config("sms_provider", provider)
        updates.append(f"供应商={provider}")
    if signature is not None:
        set_config("sms_signature", signature)
        updates.append(f"签名={signature}")
    if api_url is not None:
        set_config("sms_api_url", api_url)
        updates.append(f"接口={api_url}")
    if app_key is not None:
        set_config("sms_app_key", app_key)
        updates.append(f"AppKey={app_key[:6]}***")
    if app_secret is not None:
        set_config("sms_app_secret", app_secret)
        updates.append(f"AppSecret=***已保存***")
    if template_code is not None:
        set_config("sms_template_code", template_code)
        updates.append(f"模板编号={template_code}")

    if updates:
        console.print(f"[green]已更新配置:[/green] {', '.join(updates)}")
    else:
        show_sms_config()


@sms_config_group.command("show", help="查看短信服务配置")
def show_sms_config():
    ok, cfg = is_sms_real_configured()
    hide = should_hide_sensitive()
    table = Table(title="短信服务配置")
    table.add_column("配置项", style="bold")
    table.add_column("值")
    table.add_column("说明", style="dim")

    def mask_kv(k, v):
        if not v:
            return "[red](未配置)[/red]"
        if hide and k in ("app_key", "app_secret"):
            return v[:4] + "****" + v[-4:] if len(v) > 8 else "****"
        return v

    table.add_row("状态", f"[{'green' if ok else 'red'}]{'已配置真实服务' if ok else '未配置（将使用模拟发送）'}[/{'green' if ok else 'red'}]", "")
    table.add_row("供应商", mask_kv("provider", cfg["sms_provider"]), "aliyun/tencent/huawei/cloopen/custom")
    table.add_row("短信签名", mask_kv("signature", cfg["sms_signature"]), "显示在短信内容开头【签名】")
    table.add_row("接口地址", mask_kv("url", cfg["sms_api_url"]), "HTTP POST接口")
    table.add_row("AppKey", mask_kv("app_key", cfg["sms_app_key"]), "")
    table.add_row("AppSecret", mask_kv("app_secret", cfg["sms_app_secret"]), "")
    table.add_row("模板编号", mask_kv("tpl", cfg["sms_template_code"]), "部分供应商需要传模板ID")
    console.print(table)
    if not ok:
        console.print("\n[yellow]⚠ 未配置真实短信服务，所有发送将使用模拟模式，历史记录会明确标注【模拟发送】[/yellow]")
        console.print("[dim]使用示例: pfee send sms-config set --provider aliyun --signature 我的物业 --api-url https://... --app-key xxx --app-secret xxx[/dim]")


@sms_config_group.command("test", help="测试真实短信接口连通性")
@click.option("--phone", "-p", required=True, help="测试手机号")
def test_sms_api(phone):
    ok, cfg = is_sms_real_configured()
    if not ok:
        console.print("[red]尚未配置真实短信服务，请先使用 pfee send sms-config set 配置[/red]")
        show_sms_config()
        return

    test_content = f"【{cfg.get('sms_signature') or '测试'}】物业费催缴系统接口测试短信，如收到则配置正常。"
    if click.confirm(f"将通过真实接口向 {mask_phone(phone) if should_hide_sensitive() else phone} 发送测试短信，确认？"):
        with console.status("[bold]正在调用真实接口...[/bold]"):
            ok, msg, msg_id = _call_real_sms_api(cfg, phone, test_content)
        if ok:
            console.print(f"[green]✓ 接口调用成功[/green]，消息ID: {msg_id or '-'}，返回: {msg}")
        else:
            console.print(f"[red]✗ 接口调用失败[/red]，返回: {msg}")


def _dispatch_send(phone: str, content: str) -> tuple[bool, str, str, bool]:
    """统一调度：根据配置决定真实/模拟发送。返回(ok, msg, msg_id, is_mock)"""
    cfg_ok, cfg = is_sms_real_configured()
    if cfg_ok:
        ok, msg, msg_id = _call_real_sms_api(cfg, phone, content)
        return ok, msg, msg_id, False
    else:
        ok, msg, msg_id = _mock_send_sms(phone, content)
        return ok, msg, msg_id, True


@send_cmd.command("all", help="批量发送队列中所有待发送的通知")
@click.option("--batch-size", type=int, default=100, help="每批发送数量")
@click.option("--interval", type=float, default=0.1, help="每条发送间隔秒数")
@click.option("--force", is_flag=True, help="强制发送（忽略节假日和时段限制）")
@click.option("--dry-run", is_flag=True, help="仅模拟发送，不更新状态")
@click.option("--retry-failed", is_flag=True, help="同时重试之前发送失败的通知")
@click.option("--force-mock", is_flag=True, help="强制使用模拟发送（即使配置了真实服务）")
def send_all(batch_size, interval, force, dry_run, retry_failed, force_mock):
    cfg_ok, cfg = is_sms_real_configured()
    mode_label = "[red]【模拟发送】[/red]" if (not cfg_ok or force_mock) else "[green]【真实发送】[/green]"
    console.print(f"[bold]发送模式: {mode_label}[/bold]")
    if force_mock and cfg_ok:
        console.print("[yellow]已指定 --force-mock，将忽略真实配置改用模拟发送[/yellow]")

    if not force:
        allowed, reason = _is_send_allowed_now()
        if not allowed:
            console.print(f"[yellow]{reason}[/yellow]")
            if not click.confirm("是否继续强制发送？"):
                return

    conn = get_connection()
    status_filter = ("'待发送'",)
    if retry_failed:
        status_filter = ("'待发送'", "'发送失败'")

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

    success = 0
    fail = 0
    skip = 0
    real_sent = 0
    mock_sent = 0

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
                skip += 1
                progress.advance(task)
                continue

            nr_id = r["id"]
            phone = r["phone"] or r["h_phone"]
            content = r["content"]

            if not phone:
                if not dry_run:
                    conn.execute("""
                        UPDATE notice_records SET status = '发送失败', error_msg = '无手机号',
                            retry_count = retry_count + 1 WHERE id = ?
                    """, (nr_id,))
                fail += 1
                progress.advance(task)
                continue

            if force_mock:
                ok, msg, msg_id = _mock_send_sms(phone, content)
                is_mock = True
            else:
                ok, msg, msg_id, is_mock = _dispatch_send(phone, content)

            if is_mock:
                mock_sent += 1
            else:
                real_sent += 1

            if not dry_run:
                prov = "mock" if is_mock else (cfg["sms_provider"] or "custom")
                new_retry = r["retry_count"] + 1
                if ok:
                    conn.execute("""
                        UPDATE notice_records SET status = '已发送',
                            sent_at = datetime('now','localtime'),
                            error_msg = NULL, retry_count = ?, max_retry = ?,
                            is_mock = ?, provider = ?, message_id = ?
                        WHERE id = ?
                    """, (new_retry, r["max_retry"], 1 if is_mock else 0, prov, msg_id, nr_id))
                    success += 1
                else:
                    conn.execute("""
                        UPDATE notice_records SET status = '发送失败',
                            error_msg = ?, retry_count = ?, max_retry = ?,
                            sent_at = datetime('now','localtime'),
                            is_mock = ?, provider = ?, message_id = ?
                        WHERE id = ?
                    """, (msg, new_retry, r["max_retry"], 1 if is_mock else 0, prov, msg_id, nr_id))
                    fail += 1
            else:
                if ok:
                    success += 1
                else:
                    fail += 1

            time.sleep(interval)
            progress.advance(task)

    if not dry_run:
        conn.commit()
    conn.close()

    console.print("\n[bold]===== 发送结果汇总 =====[/bold]")
    console.print(f"  发送模式: {mode_label}")
    if not dry_run:
        console.print(f"  真实发送: {real_sent} 条，模拟发送: {mock_sent} 条")
    console.print(f"  [green]成功: {success} 条[/green]")
    console.print(f"  [red]失败: {fail} 条[/red]")
    if skip:
        console.print(f"  [yellow]跳过(超重试): {skip} 条[/yellow]")
    if dry_run:
        console.print(f"  [yellow]* dry-run模式，未实际更新数据库[/yellow]")
    if (not cfg_ok and not force_mock):
        console.print(f"\n[yellow]⚠ 当前为模拟发送模式，短信并未真正送达业主，请配置真实服务后再发送。[/yellow]")


@send_cmd.command("retry", help="重试发送失败的通知")
@click.option("--record-id", type=int, help="仅重试指定ID的通知")
@click.option("--all", "retry_all", is_flag=True, help="重试所有失败的通知")
@click.option("--max-retry", type=int, help="覆盖最大重试次数限制")
@click.option("--force", is_flag=True, help="忽略时段限制")
@click.option("--dry-run", is_flag=True)
@click.option("--force-mock", is_flag=True, help="强制模拟发送")
def retry_failed(record_id, retry_all, max_retry, force, dry_run, force_mock):
    if not record_id and not retry_all:
        console.print("[red]请指定 --record-id 或 --all[/red]")
        return
    cfg_ok, cfg = is_sms_real_configured()
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

    success, fail = 0, 0
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

            if force_mock:
                ok, msg, msg_id = _mock_send_sms(phone, r["content"])
                is_mock = True
            else:
                ok, msg, msg_id, is_mock = _dispatch_send(phone, r["content"])

            prov = "mock" if is_mock else (cfg["sms_provider"] or "custom")
            new_retry = current_retry + 1
            if not dry_run:
                if ok:
                    conn.execute("""
                        UPDATE notice_records SET status = '已发送',
                            sent_at = datetime('now','localtime'), error_msg = NULL,
                            retry_count = ?, max_retry = ?, is_mock = ?, provider = ?, message_id = ?
                        WHERE id = ?
                    """, (new_retry, effective_max, 1 if is_mock else 0, prov, msg_id, r["id"]))
                    success += 1
                else:
                    conn.execute("""
                        UPDATE notice_records SET status = '发送失败', error_msg = ?,
                            retry_count = ?, max_retry = ?, is_mock = ?, provider = ?, message_id = ?,
                            sent_at = datetime('now','localtime')
                        WHERE id = ?
                    """, (msg, new_retry, effective_max, 1 if is_mock else 0, prov, msg_id, r["id"]))
                    fail += 1
            else:
                success += ok
                fail += 0 if ok else 1
            time.sleep(0.02)
            progress.advance(task)

    if not dry_run:
        conn.commit()
    conn.close()
    console.print(f"\n[green]成功: {success}[/green], [red]失败: {fail}[/red]")


@send_cmd.command("status", help="查看发送统计")
def send_status():
    conn = get_connection()
    by_status = conn.execute("""
        SELECT status, is_mock, provider, COUNT(*) as cnt
        FROM notice_records GROUP BY status, is_mock, provider
    """).fetchall()

    counts = {}
    mock_cnt = real_cnt = 0
    for s in by_status:
        key = (s["status"], bool(s["is_mock"]))
        counts[key] = counts.get(key, 0) + s["cnt"]
        if s["status"] == "已发送":
            if s["is_mock"]:
                mock_cnt += s["cnt"]
            else:
                real_cnt += s["cnt"]

    by_date = conn.execute("""
        SELECT DATE(sent_at) as d, status, is_mock, COUNT(*) as cnt
        FROM notice_records
        WHERE sent_at IS NOT NULL
        GROUP BY DATE(sent_at), status, is_mock
        ORDER BY d DESC LIMIT 10
    """).fetchall()

    fail_detail = conn.execute("""
        SELECT error_msg, COUNT(*) as cnt
        FROM notice_records
        WHERE status = '发送失败' AND error_msg IS NOT NULL
        GROUP BY error_msg ORDER BY cnt DESC LIMIT 10
    """).fetchall()
    conn.close()

    cfg_ok, _ = is_sms_real_configured()

    console.print("[bold]===== 发送状态统计 =====[/bold]")
    console.print(f"  服务配置: [{'green' if cfg_ok else 'red'}]{'真实服务' if cfg_ok else '仅模拟发送'}[/{'green' if cfg_ok else 'red'}]")
    console.print(f"  累计真实送达: [green]{real_cnt}[/green] 条，模拟发送: [yellow]{mock_cnt}[/yellow] 条\n")

    table = Table(title="按状态分布")
    table.add_column("状态")
    table.add_column("真实", justify="right", style="green")
    table.add_column("模拟", justify="right", style="yellow")
    table.add_column("合计", justify="right")
    statuses = ["待发送", "发送中", "已发送", "发送失败"]
    for st in statuses:
        rc = counts.get((st, False), 0)
        mc = counts.get((st, True), 0)
        table.add_row(st, str(rc), str(mc), str(rc + mc))
    console.print(table)

    if by_date:
        console.print("\n[bold]===== 近期发送趋势 =====[/bold]")
        table = Table()
        table.add_column("日期")
        table.add_column("状态")
        table.add_column("类型")
        table.add_column("数量", justify="right")
        for d in by_date:
            st_style = {"已发送": "green", "发送失败": "red"}.get(d["status"], "white")
            tp_style = "yellow" if d["is_mock"] else "green"
            table.add_row(
                d["d"] or "-",
                f"[{st_style}]{d['status']}[/{st_style}]",
                f"[{tp_style}]{'模拟' if d['is_mock'] else '真实'}[/{tp_style}]",
                str(d["cnt"]),
            )
        console.print(table)

    if fail_detail:
        console.print("\n[bold]===== 失败原因TOP10 =====[/bold]")
        table = Table()
        table.add_column("失败原因", style="red")
        table.add_column("次数", justify="right")
        for f in fail_detail:
            table.add_row(f["error_msg"], str(f["cnt"]))
        console.print(table)


@send_cmd.command("config", help="查看/设置发送时段和脱敏")
@click.option("--hour-start", type=int, help="允许发送的起始小时")
@click.option("--hour-end", type=int, help="允许发送的结束小时")
@click.option("--hide-sensitive", type=click.Choice(["on", "off"]), help="敏感信息隐藏开关")
def send_config(hour_start, hour_end, hide_sensitive):
    if hour_start is not None:
        if hour_start < 0 or hour_start > 23:
            console.print("[red]起始小时应在0-23之间[/red]")
            return
        set_config("sms_send_hour_start", str(hour_start))
        console.print(f"[green]发送起始时间已设置为: {hour_start}:00[/green]")
    if hour_end is not None:
        if hour_end < 1 or hour_end > 24:
            console.print("[red]结束小时应在1-24之间[/red]")
            return
        set_config("sms_send_hour_end", str(hour_end))
        console.print(f"[green]发送结束时间已设置为: {hour_end}:00[/green]")
    if hide_sensitive is not None:
        val = "1" if hide_sensitive == "on" else "0"
        set_config("hide_sensitive", val)
        console.print(f"[green]敏感信息隐藏: {hide_sensitive}[/green]")
    if hour_start is None and hour_end is None and hide_sensitive is None:
        cfg_ok, _ = is_sms_real_configured()
        console.print("[bold]当前发送配置:[/bold]")
        console.print(f"  短信服务: [{'green' if cfg_ok else 'red'}]{'已配置真实服务' if cfg_ok else '未配置（使用模拟发送）'}[/{'green' if cfg_ok else 'red'}]")
        console.print(f"  发送时段: {get_config('sms_send_hour_start', '9')}:00 - {get_config('sms_send_hour_end', '20')}:00")
        console.print(f"  敏感信息隐藏: {'开启' if should_hide_sensitive() else '关闭'}")
        allowed, reason = _is_send_allowed_now()
        console.print(f"  当前状态: {'[green]可发送[/green]' if allowed else f'[yellow]{reason}[/yellow]'}")


@send_cmd.command("test", help="测试发送一条短信（自动选择真实/模拟）")
@click.option("--phone", "-p", required=True, help="测试手机号")
@click.option("--template-id", type=int, help="使用模板ID")
@click.option("--force-mock", is_flag=True, help="强制模拟")
def send_test(phone, template_id, force_mock):
    conn = get_connection()
    if template_id:
        tpl = conn.execute("SELECT * FROM notice_templates WHERE id = ?", (template_id,)).fetchone()
    else:
        tpl = conn.execute("SELECT * FROM notice_templates WHERE is_default = 1").fetchone()
    conn.close()
    if not tpl:
        console.print("[red]未找到模板[/red]")
        return

    from ..utils import get_sms_signature
    hide = should_hide_sensitive()
    test_vars = {
        "owner_name": mask_name("测试用户") if hide else "测试用户",
        "room_no": "测试房号",
        "building": "测试楼栋",
        "phone": mask_phone(phone) if hide else phone,
        "fee_period": "2026-01",
        "fee_type": "物业费",
        "base_amount": "100.00",
        "late_fee": "5.00",
        "total_amount": "105.00",
        "unpaid_amount": "105.00",
        "due_date": "2026-01-15",
        "signature": get_sms_signature(),
        "company_name": get_config("company_name", "XX物业"),
        "service_phone": get_config("service_phone", "400-123-4567"),
    }
    content = tpl["content"].format(**test_vars)

    cfg_ok, _ = is_sms_real_configured()
    mode = "[red]模拟[/red]" if (not cfg_ok or force_mock) else "[green]真实[/green]"
    console.print(f"[bold]发送模式: {mode}   模板: {tpl['name']}\n[/bold]")
    display_phone = mask_phone(phone) if hide else phone
    console.print(Panel(content, title=f"测试内容 → {display_phone}", border_style="yellow"))

    if click.confirm("\n确认发送测试短信？"):
        if force_mock:
            ok, msg, msg_id = _mock_send_sms(phone, content)
            is_mock = True
        else:
            ok, msg, msg_id, is_mock = _dispatch_send(phone, content)
        tag = "[yellow]【模拟】[/yellow]" if is_mock else "[green]【真实】[/green]"
        if ok:
            console.print(f"[green]✓ 发送成功 {tag}[/green] msg_id={msg_id}，返回: {msg}")
        else:
            console.print(f"[red]✗ 发送失败 {tag}[/red] 返回: {msg}")
