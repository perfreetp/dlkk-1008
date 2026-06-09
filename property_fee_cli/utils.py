from datetime import datetime, date, timedelta
from typing import Optional, Tuple
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


def calc_unpaid(base_amount: float, late_fee: float, paid_amount: float, discount_amount: float) -> float:
    """统一口径：尚欠 = 本金 + 滞纳金 - 已缴 - 减免。浮点保留两位，负值修正为0。"""
    raw = round(float(base_amount or 0) + float(late_fee or 0)
                - float(paid_amount or 0) - float(discount_amount or 0), 2)
    return raw if raw > 0 else 0.0


def calc_total_owed(base_amount: float, late_fee: float, discount_amount: float) -> float:
    """统一口径：应缴总额 = 本金 + 滞纳金 - 减免"""
    return round(float(base_amount or 0) + float(late_fee or 0) - float(discount_amount or 0), 2)


UNPAID_SQL_EXPR = "(COALESCE(a.base_amount,0) + COALESCE(a.late_fee,0) - COALESCE(a.paid_amount,0) - COALESCE(a.discount_amount,0))"
TOTAL_OWED_SQL_EXPR = "(COALESCE(a.base_amount,0) + COALESCE(a.late_fee,0) - COALESCE(a.discount_amount,0))"


def is_sms_real_configured() -> Tuple[bool, dict]:
    """检查是否配置了真实短信服务。返回(已配置, 配置字典)"""
    keys = ["sms_provider", "sms_signature", "sms_api_url", "sms_app_key", "sms_app_secret", "sms_template_code"]
    cfg = {}
    conn = get_connection()
    for k in keys:
        row = conn.execute("SELECT value FROM config WHERE key = ?", (k,)).fetchone()
        cfg[k] = row["value"] if row else ""
    conn.close()

    required = ["sms_provider", "sms_api_url", "sms_app_key", "sms_app_secret"]
    ok = all(cfg[k] and cfg[k].strip() for k in required)
    return ok, cfg


def get_sms_signature() -> str:
    return get_config("sms_signature") or "物业提醒"


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


def determine_status(unpaid: float, has_commitment: bool = False) -> str:
    """根据尚欠金额统一判断状态"""
    if unpaid <= 0.01:
        return "已缴"
    if has_commitment:
        return "承诺付款"
    return "未缴"
