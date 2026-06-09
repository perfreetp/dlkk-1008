import click
import csv
import json
from typing import List, Dict, Any, Optional, Tuple
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from difflib import SequenceMatcher

from ..database import get_connection
from ..utils import parse_date, calc_unpaid, UNPAID_SQL_EXPR, mask_phone

console = Console()

BANK_REQUIRED_COLS = ["amount"]
BANK_OPTIONAL_COLS = [
    "trans_no", "trans_date", "payer_name", "payer_phone",
    "payer_account", "remark"
]

MATCH_SCORE_ROOM_EXACT = 50
MATCH_SCORE_ROOM_PARTIAL = 25
MATCH_SCORE_NAME_EXACT = 30
MATCH_SCORE_NAME_PARTIAL = 15
MATCH_SCORE_PHONE_EXACT = 25
MATCH_SCORE_PHONE_LAST4 = 10
MATCH_SCORE_AMOUNT_EXACT = 40
MATCH_SCORE_AMOUNT_CLOSE = 20


@click.group(help="银行流水导入和自动匹配")
def bank_cmd():
    pass


def _score_match(
    payer_name: str, payer_phone: str, amount: float, remark: str,
    hh: Dict[str, Any], arrears_list: List[Dict[str, Any]]
) -> Tuple[int, Optional[int], Optional[str]]:
    score = 0
    best_arrear = None
    best_unpaid = 0.0

    room = hh["room_no"]
    owner = hh["owner_name"] or ""
    phone = hh["phone"] or ""

    if room and (room in (remark or "")):
        score += MATCH_SCORE_ROOM_EXACT
    elif room and len(room) >= 3:
        r_short = room.replace("-", "").replace("栋", "").replace("单元", "")
        rem_short = (remark or "").replace("-", "").replace("栋", "").replace("单元", "")
        if r_short in rem_short:
            score += MATCH_SCORE_ROOM_PARTIAL

    if payer_name and owner == payer_name:
        score += MATCH_SCORE_NAME_EXACT
    elif payer_name and owner:
        s = SequenceMatcher(None, payer_name, owner).ratio()
        if s >= 0.8:
            score += MATCH_SCORE_NAME_PARTIAL

    if payer_phone and phone == payer_phone:
        score += MATCH_SCORE_PHONE_EXACT
    elif payer_phone and phone and payer_phone[-4:] == phone[-4:]:
        score += MATCH_SCORE_PHONE_LAST4

    close_arrear = None
    exact_arrear = None
    for ar in arrears_list:
        unpaid = calc_unpaid(ar["base_amount"], ar["late_fee"], ar["paid_amount"], ar["discount_amount"])
        if unpaid <= 0:
            continue
        diff = abs(unpaid - amount)
        if diff < 0.01:
            exact_arrear = ar
            best_unpaid = unpaid
            break
        if diff <= 5.0 and (close_arrear is None or diff < abs(calc_unpaid(close_arrear["base_amount"], close_arrear["late_fee"], close_arrear["paid_amount"], close_arrear["discount_amount"]) - amount)):
            close_arrear = ar
            best_unpaid = unpaid

    if exact_arrear:
        score += MATCH_SCORE_AMOUNT_EXACT
        best_arrear = exact_arrear["id"]
    elif close_arrear:
        score += MATCH_SCORE_AMOUNT_CLOSE
        best_arrear = close_arrear["id"]

    return score, best_arrear, room


def _auto_match(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        households = [dict(r) for r in conn.execute("SELECT * FROM households").fetchall()]
        arrears_rows = conn.execute(f"""
            SELECT a.*, h.room_no, h.owner_name, h.phone,
                   {UNPAID_SQL_EXPR} as unpaid_calc
            FROM arrears a JOIN households h ON a.household_id = h.id
            WHERE {UNPAID_SQL_EXPR} > 0
        """).fetchall()
        arrears_by_hh: Dict[int, List[Dict[str, Any]]] = {}
        for ar in arrears_rows:
            arrears_by_hh.setdefault(ar["household_id"], []).append(dict(ar))

        for row in rows:
            payer_name = row.get("payer_name") or ""
            payer_phone = row.get("payer_phone") or ""
            remark = row.get("remark") or ""
            amount = float(row.get("amount") or 0)

            best_score = 0
            best_hh = None
            best_arrear = None
            best_room = None

            for hh in households:
                arr_list = arrears_by_hh.get(hh["id"], [])
                if not arr_list:
                    continue
                score, arrear_id, room = _score_match(
                    payer_name, payer_phone, amount, remark, hh, arr_list
                )
                if score > best_score:
                    best_score = score
                    best_hh = hh["id"]
                    best_arrear = arrear_id
                    best_room = hh["room_no"]

            row["match_score"] = best_score
            row["matched_household_id"] = best_hh
            row["matched_arrear_id"] = best_arrear
            row["matched_room"] = best_room
            if best_score >= 60 and best_arrear:
                row["match_status"] = "已匹配(待确认)"
            elif best_score >= 35:
                row["match_status"] = "建议人工核对"
            else:
                row["match_status"] = "未匹配"

        return rows
    finally:
        conn.close()


@bank_cmd.command("import", help="导入银行流水CSV并自动匹配欠费")
@click.argument("filepath", type=click.Path(exists=True, readable=True))
@click.option("--encoding", default="utf-8-sig", help="CSV文件编码，默认utf-8-sig")
@click.option("--delimiter", default=",", help="CSV分隔符，默认逗号")
@click.option("--source-name", default=None, help="流水来源备注，默认用文件名")
def bank_import(filepath: str, encoding: str, delimiter: str, source_name: Optional[str]):
    with open(filepath, "r", encoding=encoding, newline="") as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        headers = reader.fieldnames or []
        if "amount" not in headers:
            console.print("[red]错误：CSV缺少必需列: amount[/red]")
            console.print("推荐列名: amount, trans_no, trans_date, payer_name, payer_phone, payer_account, remark")
            return
        rows_raw = list(reader)

    cleaned: List[Dict[str, Any]] = []
    for i, row in enumerate(rows_raw, start=1):
        try:
            amt_str = (row.get("amount") or "0").replace(",", "").strip()
            amt = float(amt_str) if amt_str else 0.0
            if amt <= 0:
                continue
            obj = {
                "trans_no": (row.get("trans_no") or "").strip(),
                "trans_date": (row.get("trans_date") or "").strip(),
                "payer_name": (row.get("payer_name") or "").strip(),
                "payer_phone": (row.get("payer_phone") or "").strip(),
                "payer_account": (row.get("payer_account") or "").strip(),
                "amount": round(amt, 2),
                "remark": (row.get("remark") or row.get("摘要") or row.get("备注") or "").strip(),
                "raw_data": json.dumps(row, ensure_ascii=False),
                "source_file": source_name or filepath.split("/")[-1].split("\\")[-1],
            }
            if obj["trans_date"]:
                try:
                    obj["trans_date"] = parse_date(obj["trans_date"]).strftime("%Y-%m-%d")
                except Exception:
                    pass
            cleaned.append(obj)
        except Exception as e:
            console.print(f"[yellow]第{i}行解析失败: {e}[/yellow]")

    if not cleaned:
        console.print("[yellow]没有有效流水记录[/yellow]")
        return

    matched = _auto_match(cleaned)

    stat_match = sum(1 for r in matched if r["match_status"] == "已匹配(待确认)")
    stat_suggest = sum(1 for r in matched if r["match_status"] == "建议人工核对")
    stat_none = sum(1 for r in matched if r["match_status"] == "未匹配")
    total_amount = sum(r["amount"] for r in matched)
    matched_amount = sum(r["amount"] for r in matched if r["match_status"] == "已匹配(待确认)")

    conn = get_connection()
    try:
        for r in matched:
            conn.execute("""
                INSERT INTO pending_payments
                (trans_no, trans_date, payer_name, payer_phone, payer_account,
                 amount, remark, match_status, match_score,
                 matched_household_id, matched_arrear_id, matched_room,
                 raw_data, source_file)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                r.get("trans_no"), r.get("trans_date"), r.get("payer_name"),
                r.get("payer_phone"), r.get("payer_account"), r["amount"],
                r.get("remark"), r["match_status"], r["match_score"],
                r.get("matched_household_id"), r.get("matched_arrear_id"),
                r.get("matched_room"), r.get("raw_data"), r.get("source_file"),
            ))
        conn.commit()
    finally:
        conn.close()

    console.print(Panel(
        f"导入总笔数: {len(matched)} 笔   金额合计: {total_amount:,.2f} 元\n"
        f"[green]自动匹配成功: {stat_match} 笔[/green]  ({matched_amount:,.2f} 元)\n"
        f"[yellow]建议人工核对: {stat_suggest} 笔[/yellow]\n"
        f"[red]无法匹配: {stat_none} 笔[/red]",
        title="银行流水导入结果", style="cyan",
    ))

    table = Table(title=f"匹配结果（显示最近15条）")
    table.add_column("流水日期", style="white")
    table.add_column("付款人", style="magenta")
    table.add_column("金额(元)", justify="right", style="green")
    table.add_column("匹配状态", style="yellow")
    table.add_column("匹配分", justify="right")
    table.add_column("匹配房号", style="cyan")
    for r in matched[-15:]:
        status_color = {
            "已匹配(待确认)": "green",
            "建议人工核对": "yellow",
            "未匹配": "red",
        }.get(r["match_status"], "white")
        table.add_row(
            r.get("trans_date") or "-",
            r.get("payer_name") or "-",
            f"{r['amount']:,.2f}",
            f"[{status_color}]{r['match_status']}[/{status_color}]",
            str(r["match_score"]),
            r.get("matched_room") or "-",
        )
    console.print(table)
    console.print(f"使用 [cyan]pfee bank list[/cyan] 查看全部待确认列表")
    console.print(f"使用 [cyan]pfee bank confirm <pending_id>[/cyan] 确认单条缴费")
    console.print(f"使用 [cyan]pfee bank confirm-all[/cyan] 批量确认所有已匹配记录")


@bank_cmd.command("list", help="查看待确认缴费列表")
@click.option("--status", default=None, help="按匹配状态筛选: 已匹配(待确认)/建议人工核对/未匹配/已确认/已拒绝")
@click.option("--room", default=None, help="按房号筛选")
@click.option("--limit", default=50, help="显示条数，默认50")
def bank_list(status: Optional[str], room: Optional[str], limit: int):
    conn = get_connection()
    try:
        sql = "SELECT * FROM pending_payments WHERE 1=1"
        params: List[Any] = []
        if status:
            sql += " AND match_status LIKE ?"
            params.append(f"%{status}%")
        if room:
            sql += " AND matched_room LIKE ?"
            params.append(f"%{room}%")
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()

        if not rows:
            console.print("[yellow]没有待确认的缴费记录[/yellow]")
            return

        table = Table(title=f"待确认缴费列表（共{len(rows)}条）")
        table.add_column("ID", style="cyan")
        table.add_column("流水日期", style="white")
        table.add_column("付款人/账号", style="magenta")
        table.add_column("金额(元)", justify="right", style="green")
        table.add_column("状态", style="yellow")
        table.add_column("匹配分", justify="right")
        table.add_column("匹配欠费", style="blue")
        table.add_column("备注", style="white")

        for r in rows:
            color = {"已匹配": "green", "建议人工": "yellow", "未匹配": "red",
                     "已确认": "green", "已拒绝": "strike"}
            sc = "white"
            for k, v in color.items():
                if k in (r["match_status"] or ""):
                    sc = v
                    break
            arrear_info = "-"
            if r["matched_arrear_id"]:
                ar = conn.execute(f"""
                    SELECT a.fee_period, h.room_no, {UNPAID_SQL_EXPR} as unpaid_calc
                    FROM arrears a JOIN households h ON a.household_id = h.id
                    WHERE a.id = ?
                """, (r["matched_arrear_id"],)).fetchone()
                if ar:
                    arrear_info = f"{ar['room_no']} {ar['fee_period']} 尚欠{ar['unpaid_calc']:,.2f}"
            payer = r["payer_name"] or "-"
            if r["payer_account"]:
                payer += f" ({r['payer_account'][-4:] if len(r['payer_account']) > 4 else r['payer_account']})"
            table.add_row(
                str(r["id"]),
                r["trans_date"] or "-",
                payer,
                f"{r['amount']:,.2f}",
                f"[{sc}]{r['match_status']}[/{sc}]",
                str(r["match_score"] or 0),
                arrear_info,
                (r["remark"] or "")[:16],
            )
        console.print(table)

        total_pending = conn.execute("SELECT COUNT(*) as c, COALESCE(SUM(amount),0) as s FROM pending_payments WHERE match_status='已匹配(待确认)'").fetchone()
        if total_pending and total_pending["c"]:
            console.print(f"[yellow]待确认: {total_pending['c']} 笔，合计 {total_pending['s']:,.2f} 元[/yellow]")
    finally:
        conn.close()


def _apply_payment(conn, pending_id: int, operator: str = "银行流水导入") -> bool:
    pp = conn.execute("SELECT * FROM pending_payments WHERE id = ?", (pending_id,)).fetchone()
    if not pp:
        console.print(f"[red]待确认记录ID {pending_id} 不存在[/red]")
        return False
    if pp["match_status"] == "已确认":
        console.print(f"[yellow]记录 {pending_id} 已确认过，跳过[/yellow]")
        return False
    if not pp["matched_arrear_id"]:
        console.print(f"[red]记录 {pending_id} 没有匹配到欠费，请先使用 bank match 重新匹配[/red]")
        return False

    arrear = conn.execute(f"""
        SELECT a.*, h.room_no, {UNPAID_SQL_EXPR} as unpaid_calc
        FROM arrears a JOIN households h ON a.household_id = h.id
        WHERE a.id = ?
    """, (pp["matched_arrear_id"],)).fetchone()
    if not arrear:
        console.print(f"[red]欠费ID {pp['matched_arrear_id']} 不存在[/red]")
        return False

    amount = pp["amount"] or 0
    if amount > arrear["unpaid_calc"] + 0.01:
        console.print(f"[yellow]⚠ 流水金额 {amount:,.2f} 大于尚欠 {arrear['unpaid_calc']:,.2f}，按实缴 {arrear['unpaid_calc']:,.2f} 写入，剩余 {amount-arrear['unpaid_calc']:,.2f} 需财务单独处理[/yellow]")
        amount = arrear["unpaid_calc"]

    from .record_cmd import _refresh_arrear_status

    pay_date = pp["trans_date"] or parse_date(pp["created_at"]).strftime("%Y-%m-%d") if pp["created_at"] else None
    try:
        pay_date = parse_date(pay_date).strftime("%Y-%m-%d")
    except Exception:
        pay_date = None

    conn.execute("""
        INSERT INTO payment_records
        (arrear_id, household_id, amount, pay_method, pay_date, operator, remark)
        VALUES (?,?,?,?,?,?,?)
    """, (
        arrear["id"], arrear["household_id"], amount,
        "银行转账", pay_date, operator,
        f"银行流水#{pending_id} {pp['payer_name'] or ''} 原备注:{pp['remark'] or ''}"[:200],
    ))

    new_paid = (arrear["paid_amount"] or 0) + amount
    conn.execute("UPDATE arrears SET paid_amount = ?, updated_at = datetime('now','localtime') WHERE id = ?",
                 (new_paid, arrear["id"]))
    _refresh_arrear_status(conn, arrear["id"])

    conn.execute("""
        UPDATE pending_payments
        SET match_status = '已确认', operator = ?, updated_at = datetime('now','localtime')
        WHERE id = ?
    """, (operator, pending_id))

    for bm in conn.execute("SELECT * FROM batch_members WHERE arrear_id = ?", (arrear["id"],)).fetchall():
        conn.execute("""
            UPDATE batch_members SET
                repayment_status = CASE WHEN repayment_status = '未回款' THEN '部分回款' ELSE repayment_status END,
                repayment_amount = COALESCE(repayment_amount,0) + ?
            WHERE id = ?
        """, (amount, bm["id"]))
        unpaid_check = conn.execute(f"SELECT {UNPAID_SQL_EXPR} as u FROM arrears WHERE id = ?", (arrear["id"],)).fetchone()
        if unpaid_check and unpaid_check["u"] < 0.01:
            conn.execute("UPDATE batch_members SET repayment_status = '全部回款' WHERE id = ?", (bm["id"],))

    console.print(f"[green]✓ 已确认: {arrear['room_no']} {arrear['fee_period']} 缴费 {amount:,.2f} 元，尚余 {calc_unpaid(arrear['base_amount'], arrear['late_fee'], new_paid, arrear['discount_amount']):,.2f} 元[/green]")
    return True


@bank_cmd.command("confirm", help="确认单条待确认缴费，写入缴费明细")
@click.argument("pending_id", type=int)
@click.option("--operator", default="银行流水导入", help="经办人")
def bank_confirm(pending_id: int, operator: str):
    conn = get_connection()
    try:
        ok = _apply_payment(conn, pending_id, operator)
        if ok:
            conn.commit()
    except Exception as e:
        conn.rollback()
        console.print(f"[red]确认失败: {e}[/red]")
    finally:
        conn.close()


@bank_cmd.command("confirm-all", help="批量确认所有【已匹配(待确认)】的记录")
@click.option("--operator", default="银行流水导入", help="经办人")
def bank_confirm_all(operator: str):
    conn = get_connection()
    try:
        rows = conn.execute("SELECT id FROM pending_payments WHERE match_status='已匹配(待确认)' ORDER BY id").fetchall()
        if not rows:
            console.print("[yellow]没有待确认的匹配记录[/yellow]")
            return
        if not click.confirm(f"即将批量确认 {len(rows)} 条缴费记录，继续？"):
            return

        succ = 0
        fail = 0
        total_amt = 0.0
        for pp in rows:
            before = conn.total_changes
            try:
                ok = _apply_payment(conn, pp["id"], operator)
                if ok:
                    row = conn.execute("SELECT amount FROM pending_payments WHERE id = ?", (pp["id"],)).fetchone()
                    total_amt += row["amount"] or 0
                    succ += 1
            except Exception as e:
                console.print(f"[red]ID {pp['id']} 失败: {e}[/red]")
                fail += 1
        conn.commit()
        console.print(Panel(
            f"[green]成功: {succ} 笔[/green]   合计金额: {total_amt:,.2f} 元\n[red]失败: {fail} 笔[/red]",
            title="批量确认完成", style="cyan",
        ))
    finally:
        conn.close()


@bank_cmd.command("match", help="手动为某条流水重新指定匹配欠费")
@click.argument("pending_id", type=int)
@click.option("--arrear-id", required=True, type=int, help="欠费ID")
@click.option("--operator", default="财务人工匹配", help="经办人")
def bank_match(pending_id: int, arrear_id: int, operator: str):
    conn = get_connection()
    try:
        ar = conn.execute(f"""
            SELECT a.*, h.room_no, {UNPAID_SQL_EXPR} as unpaid_calc
            FROM arrears a JOIN households h ON a.household_id = h.id
            WHERE a.id = ?
        """, (arrear_id,)).fetchone()
        if not ar:
            console.print(f"[red]欠费ID {arrear_id} 不存在[/red]")
            return
        conn.execute("""
            UPDATE pending_payments SET
                match_status = '已匹配(待确认)', match_score = 99,
                matched_household_id = ?, matched_arrear_id = ?, matched_room = ?,
                operator = ?, updated_at = datetime('now','localtime')
            WHERE id = ?
        """, (ar["household_id"], arrear_id, ar["room_no"], operator, pending_id))
        conn.commit()
        console.print(f"[green]✓ 匹配成功: 流水{pending_id} → {ar['room_no']} {ar['fee_period']} 尚欠{ar['unpaid_calc']:,.2f}元，等待确认[/green]")
    finally:
        conn.close()


@bank_cmd.command("reject", help="拒绝某条流水（标记为已拒绝，不写入缴费）")
@click.argument("pending_id", type=int)
@click.option("--reason", default=None, help="拒绝原因")
@click.option("--operator", default="财务审核", help="经办人")
def bank_reject(pending_id: int, reason: Optional[str], operator: str):
    conn = get_connection()
    try:
        remark = f"拒绝原因：{reason}" if reason else "财务标记拒绝"
        conn.execute("""
            UPDATE pending_payments SET match_status='已拒绝', remark=COALESCE(remark||' | ','')||?, operator=?, updated_at=datetime('now','localtime') WHERE id=?
        """, (remark, operator, pending_id))
        conn.commit()
        console.print(f"[green]✓ 已标记流水 {pending_id} 为已拒绝[/green]")
    finally:
        conn.close()


@bank_cmd.command("template", help="生成银行流水CSV导入模板")
@click.argument("output", type=click.Path(writable=True), default="bank_statement_template.csv")
def bank_template(output: str):
    headers = ["trans_no", "trans_date", "payer_name", "payer_phone", "payer_account", "amount", "remark"]
    sample = [
        ["20260610001", "2026-06-10", "张伟", "13800138001", "6222****8888", "151.25", "物业费1-1-101 2026年1月"],
        ["20260610002", "2026-06-10", "李娜", "", "6225****6666", "301.25", "1-1-202 物管费"],
        ["20260610003", "2026-06-10", "王强", "13900139002", "", "523.80", "3-2-503"],
    ]
    with open(output, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(sample)
    console.print(f"[green]✓ 模板已生成: {output}[/green]")
    console.print("说明：amount 必填；匹配算法会根据 payer_name / payer_phone / remark 里的房号 + 金额，自动匹配欠费记录")
