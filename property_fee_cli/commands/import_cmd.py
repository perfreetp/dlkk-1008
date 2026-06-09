import click
import csv
import os
from datetime import datetime
from rich.console import Console
from rich.table import Table
from typing import List, Dict, Any

from ..database import get_connection
from ..utils import parse_date

console = Console()


@click.group(help="导入住户欠费清单")
def import_cmd():
    pass


@import_cmd.command("arrears", help="从CSV文件导入住户欠费清单")
@click.argument("filepath", type=click.Path(exists=True, readable=True))
@click.option("--dry-run", is_flag=True, help="仅预览不写入数据库")
@click.option("--encoding", default="utf-8-sig", help="CSV文件编码，默认utf-8-sig")
@click.option("--delimiter", default=",", help="CSV分隔符，默认逗号")
def import_arrears(filepath: str, dry_run: bool, encoding: str, delimiter: str):
    required_cols = ["room_no", "building", "owner_name", "fee_period", "base_amount", "due_date"]
    optional_cols = ["unit", "floor", "phone", "area", "property_type", "fee_type", "remark"]

    with open(filepath, "r", encoding=encoding, newline="") as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        headers = reader.fieldnames or []

        missing = [c for c in required_cols if c not in headers]
        if missing:
            console.print(f"[red]错误：CSV缺少必需列: {', '.join(missing)}[/red]")
            console.print(f"必需列: {', '.join(required_cols)}")
            console.print(f"可选列: {', '.join(optional_cols)}")
            return

        rows = list(reader)
        if not rows:
            console.print("[yellow]警告：CSV文件为空[/yellow]")
            return

    valid_rows: List[Dict[str, Any]] = []
    errors: List[str] = []

    for i, row in enumerate(rows, start=1):
        try:
            cleaned = {}
            for col in required_cols:
                val = (row.get(col) or "").strip()
                if not val:
                    raise ValueError(f"列 {col} 不能为空")
                cleaned[col] = val

            for col in optional_cols:
                cleaned[col] = (row.get(col) or "").strip()

            base_amount = float(cleaned["base_amount"].replace(",", ""))
            if base_amount <= 0:
                raise ValueError(f"base_amount 必须大于0")
            cleaned["base_amount"] = base_amount

            parsed_due = parse_date(cleaned["due_date"])
            cleaned["due_date_parsed"] = parsed_due.strftime("%Y-%m-%d")

            area = cleaned.get("area", "")
            cleaned["area"] = float(area.replace(",", "")) if area else 0.0

            floor = cleaned.get("floor", "")
            cleaned["floor"] = int(floor) if floor and floor.isdigit() else None

            valid_rows.append(cleaned)
        except Exception as e:
            errors.append(f"第{i}行: {str(e)}")

    if errors:
        console.print(f"[red]发现 {len(errors)} 条数据错误：[/red]")
        for err in errors[:20]:
            console.print(f"  - {err}")
        if len(errors) > 20:
            console.print(f"  ... 还有 {len(errors) - 20} 条错误")
        if not click.confirm("是否跳过错误行，继续导入有效数据？"):
            return

    table = Table(title=f"待导入数据预览（共 {len(valid_rows)} 条）", show_lines=False)
    table.add_column("房号", style="cyan")
    table.add_column("楼栋", style="green")
    table.add_column("业主", style="magenta")
    table.add_column("账期", style="yellow")
    table.add_column("本金(元)", justify="right", style="blue")
    table.add_column("应缴日", style="white")

    for r in valid_rows[:10]:
        table.add_row(
            r["room_no"],
            r["building"],
            r["owner_name"],
            r["fee_period"],
            f"{r['base_amount']:,.2f}",
            r["due_date_parsed"],
        )
    if len(valid_rows) > 10:
        table.add_row("...", "...", "...", "...", "...", "...")
    console.print(table)

    if dry_run:
        console.print("[yellow]已执行dry-run，未写入数据库[/yellow]")
        return

    if not click.confirm(f"确认导入 {len(valid_rows)} 条数据？"):
        return

    conn = get_connection()
    inserted_households = 0
    updated_households = 0
    inserted_arrears = 0
    updated_arrears = 0

    try:
        cursor = conn.cursor()
        for r in valid_rows:
            cursor.execute("SELECT id FROM households WHERE room_no = ?", (r["room_no"],))
            hh_row = cursor.fetchone()
            if hh_row:
                household_id = hh_row["id"]
                cursor.execute("""
                    UPDATE households SET
                        building = ?,
                        unit = COALESCE(NULLIF(?, ''), unit),
                        floor = COALESCE(?, floor),
                        owner_name = ?,
                        phone = COALESCE(NULLIF(?, ''), phone),
                        area = COALESCE(NULLIF(?, 0), area),
                        property_type = COALESCE(NULLIF(?, ''), property_type),
                        updated_at = datetime('now','localtime')
                    WHERE id = ?
                """, (
                    r["building"], r.get("unit") or None, r.get("floor"),
                    r["owner_name"], r.get("phone") or None,
                    r.get("area") or None, r.get("property_type") or None,
                    household_id,
                ))
                updated_households += 1
            else:
                cursor.execute("""
                    INSERT INTO households
                    (room_no, building, unit, floor, owner_name, phone, area, property_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    r["room_no"], r["building"], r.get("unit") or None, r.get("floor"),
                    r["owner_name"], r.get("phone") or None,
                    r.get("area") or 0, r.get("property_type") or "住宅",
                ))
                household_id = cursor.lastrowid
                inserted_households += 1

            fee_type = r.get("fee_type") or "物业费"
            cursor.execute("""
                SELECT id, base_amount, late_fee, total_amount, status, paid_amount, discount_amount
                FROM arrears WHERE household_id = ? AND fee_period = ? AND fee_type = ?
            """, (household_id, r["fee_period"], fee_type))
            ar_row = cursor.fetchone()

            base_amt = r["base_amount"]
            due = r["due_date_parsed"]

            if ar_row:
                new_total = base_amt + (ar_row["late_fee"] or 0) - (ar_row["discount_amount"] or 0)
                cursor.execute("""
                    UPDATE arrears SET
                        base_amount = ?,
                        total_amount = ?,
                        due_date = ?,
                        remark = COALESCE(NULLIF(?, ''), remark),
                        updated_at = datetime('now','localtime')
                    WHERE id = ?
                """, (base_amt, new_total, due, r.get("remark") or None, ar_row["id"]))
                updated_arrears += 1
            else:
                cursor.execute("""
                    INSERT INTO arrears
                    (household_id, fee_period, fee_type, base_amount, late_fee, total_amount,
                     due_date, paid_amount, discount_amount, status, remark)
                    VALUES (?, ?, ?, ?, 0, ?, ?, 0, 0, '未缴', ?)
                """, (
                    household_id, r["fee_period"], fee_type, base_amt, base_amt,
                    due, r.get("remark") or None,
                ))
                inserted_arrears += 1

        conn.commit()
        console.print("[green]导入成功！[/green]")
        console.print(f"  住户：新增 {inserted_households} 条，更新 {updated_households} 条")
        console.print(f"  欠费：新增 {inserted_arrears} 条，更新 {updated_arrears} 条")
    except Exception as e:
        conn.rollback()
        console.print(f"[red]导入失败: {str(e)}[/red]")
    finally:
        conn.close()


@import_cmd.command("template", help="生成CSV导入模板文件")
@click.argument("output", type=click.Path(writable=True), default="arrears_template.csv")
def generate_template(output: str):
    headers = [
        "room_no", "building", "unit", "floor", "owner_name", "phone",
        "area", "property_type", "fee_period", "fee_type", "base_amount", "due_date", "remark"
    ]
    sample = [
        ["1-1-101", "1栋", "1单元", "1", "张三", "13800138000", "120.5", "住宅", "2026-01", "物业费", "301.25", "2026-01-15", "无"],
        ["2-2-302", "2栋", "2单元", "3", "李四", "13900139000", "95.0", "住宅", "2026-Q1", "物业费", "712.50", "2026-04-01", "无"],
    ]
    with open(output, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(sample)
    console.print(f"[green]模板已生成: {output}[/green]")
    console.print("说明：room_no, building, owner_name, fee_period, base_amount, due_date 为必填列")


@import_cmd.command("holidays", help="导入节假日清单")
@click.argument("filepath", type=click.Path(exists=True, readable=True))
@click.option("--clear", is_flag=True, help="导入前清空现有节假日")
def import_holidays(filepath: str, clear: bool):
    with open(filepath, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        if "date" not in headers:
            console.print("[red]错误：CSV缺少必填列 'date'[/red]")
            return
        rows = list(reader)

    conn = get_connection()
    try:
        if clear:
            conn.execute("DELETE FROM holidays")
            console.print("[yellow]已清空现有节假日[/yellow]")

        inserted = 0
        skipped = 0
        for r in rows:
            try:
                date_str = parse_date(r["date"]).strftime("%Y-%m-%d")
                name = (r.get("name") or "").strip() or None
                conn.execute(
                    "INSERT OR IGNORE INTO holidays (holiday_date, holiday_name) VALUES (?, ?)",
                    (date_str, name),
                )
                changes = conn.total_changes
                if changes > 0:
                    inserted += 1
                else:
                    skipped += 1
            except Exception as e:
                skipped += 1
                console.print(f"[yellow]跳过无效日期 {r.get('date')}: {e}[/yellow]")

        conn.commit()
        console.print(f"[green]导入完成：新增 {inserted} 个节假日，跳过 {skipped} 个[/green]")
    finally:
        conn.close()
