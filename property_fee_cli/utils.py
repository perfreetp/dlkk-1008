from datetime import datetime, date, timedelta
from typing import Optional
from .database import get_connection


def mask_phone(phone: str) -> str:
    if not phone or len(phone) < 7:
        return phone or ""
    return phone[:3] + "****" + phone[-4:]


def mask_name(name: str) -> str:
    if not name or len(name) <= 1:
        return name or ""
    if len(name) == 2:
        return name[0] + "*"
    return name[0] + "*" * (len(name) - 2) + name[-1]


def should_hide_sensitive() -> bool:
    conn = get_connection()
    row = conn.execute("SELECT value FROM config WHERE key = 'hide_sensitive'").fetchone()
    conn.close()
    return row["value"] == "1" if row else True


def get_config(key: str, default: Optional[str] = None) -> Optional[str]:
    conn = get_connection()
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_config(key: str, value: str) -> None:
    conn = get_connection()
    conn.execute("""
        INSERT INTO config (key, value, updated_at) VALUES (?, ?, datetime('now','localtime'))
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = datetime('now','localtime')
    """, (key, value))
    conn.commit()
    conn.close()


def is_holiday(check_date: date) -> bool:
    if check_date.weekday() >= 5:
        return True
    date_str = check_date.strftime("%Y-%m-%d")
    conn = get_connection()
    row = conn.execute("SELECT id FROM holidays WHERE holiday_date = ?", (date_str,)).fetchone()
    conn.close()
    return row is not None


def next_workday(from_date: Optional[date] = None) -> date:
    if from_date is None:
        from_date = date.today()
    current = from_date + timedelta(days=1)
    while is_holiday(current):
        current += timedelta(days=1)
    return current


def count_workdays(start_date: date, end_date: date) -> int:
    if start_date > end_date:
        return 0
    count = 0
    current = start_date
    while current <= end_date:
        if not is_holiday(current):
            count += 1
        current += timedelta(days=1)
    return count


def add_workdays(start_date: date, days: int) -> date:
    current = start_date
    added = 0
    while added < days:
        current += timedelta(days=1)
        if not is_holiday(current):
            added += 1
    return current


def parse_date(date_str: str) -> date:
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except (ValueError, TypeError):
            continue
    raise ValueError(f"无法解析日期: {date_str}")


def format_money(amount: float) -> str:
    return f"{amount:,.2f}"
