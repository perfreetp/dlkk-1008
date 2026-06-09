import sqlite3
import os
import json
from datetime import datetime, date
from typing import Optional, List, Dict, Any, Tuple


DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "property_fee.db")
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def get_connection() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """执行数据库迁移，确保现有数据库也有新字段"""
    cursor = conn.cursor()

    def col_exists(table: str, col: str) -> bool:
        cursor.execute(f"PRAGMA table_info({table})")
        return any(r["name"] == col for r in cursor.fetchall())

    def table_exists(table: str) -> bool:
        cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        return cursor.fetchone() is not None

    if not col_exists("notice_records", "is_mock"):
        cursor.execute("ALTER TABLE notice_records ADD COLUMN is_mock INTEGER DEFAULT 1")

    if not col_exists("notice_records", "provider"):
        cursor.execute("ALTER TABLE notice_records ADD COLUMN provider TEXT DEFAULT 'mock'")

    if not col_exists("notice_records", "message_id"):
        cursor.execute("ALTER TABLE notice_records ADD COLUMN message_id TEXT")

    if not col_exists("arrears", "original_total"):
        cursor.execute("ALTER TABLE arrears ADD COLUMN original_total REAL DEFAULT 0")

    if not col_exists("arrears", "paid_amount"):
        cursor.execute("ALTER TABLE arrears ADD COLUMN paid_amount REAL NOT NULL DEFAULT 0")

    if not col_exists("arrears", "discount_amount"):
        cursor.execute("ALTER TABLE arrears ADD COLUMN discount_amount REAL NOT NULL DEFAULT 0")

    if not table_exists("payment_records"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS payment_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                arrear_id INTEGER NOT NULL,
                household_id INTEGER NOT NULL,
                amount REAL NOT NULL DEFAULT 0,
                pay_method TEXT,
                pay_date TEXT,
                operator TEXT,
                remark TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (arrear_id) REFERENCES arrears(id) ON DELETE CASCADE,
                FOREIGN KEY (household_id) REFERENCES households(id) ON DELETE CASCADE
            )
        """)

    if not col_exists("discount_records", "remark"):
        cursor.execute("ALTER TABLE discount_records ADD COLUMN remark TEXT")

    if not col_exists("notice_records", "batch_id"):
        cursor.execute("ALTER TABLE notice_records ADD COLUMN batch_id INTEGER")

    if not table_exists("pending_payments"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pending_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trans_no TEXT,
                trans_date TEXT,
                payer_name TEXT,
                payer_phone TEXT,
                payer_account TEXT,
                amount REAL NOT NULL DEFAULT 0,
                remark TEXT,
                match_status TEXT DEFAULT '待匹配',
                match_score INTEGER DEFAULT 0,
                matched_household_id INTEGER,
                matched_arrear_id INTEGER,
                matched_room TEXT,
                operator TEXT,
                raw_data TEXT,
                source_file TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                updated_at TEXT DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (matched_household_id) REFERENCES households(id) ON DELETE SET NULL,
                FOREIGN KEY (matched_arrear_id) REFERENCES arrears(id) ON DELETE SET NULL
            )
        """)

    if not table_exists("batches"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_no TEXT NOT NULL UNIQUE,
                batch_name TEXT NOT NULL,
                batch_type TEXT DEFAULT '短信',
                description TEXT,
                status TEXT DEFAULT '草稿',
                target_count INTEGER DEFAULT 0,
                sent_count INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                fail_count INTEGER DEFAULT 0,
                call_count INTEGER DEFAULT 0,
                promised_count INTEGER DEFAULT 0,
                repaid_count INTEGER DEFAULT 0,
                repaid_amount REAL DEFAULT 0,
                created_by TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                closed_at TEXT
            )
        """)

    if not table_exists("batch_members"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS batch_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                household_id INTEGER NOT NULL,
                arrear_id INTEGER,
                room_no TEXT,
                owner_name TEXT,
                unpaid_amount REAL DEFAULT 0,
                notice_status TEXT DEFAULT '未发送',
                call_status TEXT DEFAULT '未联系',
                commitment_status TEXT DEFAULT '未承诺',
                repayment_status TEXT DEFAULT '未回款',
                repayment_amount REAL DEFAULT 0,
                remark TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE,
                FOREIGN KEY (household_id) REFERENCES households(id) ON DELETE CASCADE,
                FOREIGN KEY (arrear_id) REFERENCES arrears(id) ON DELETE SET NULL,
                UNIQUE(batch_id, arrear_id, household_id)
            )
        """)

    if not table_exists("audit_exceptions"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS audit_exceptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                check_time TEXT DEFAULT (datetime('now','localtime')),
                exception_type TEXT NOT NULL,
                arrear_id INTEGER,
                room_no TEXT,
                fee_period TEXT,
                description TEXT,
                base_amount REAL DEFAULT 0,
                late_fee REAL DEFAULT 0,
                paid_amount REAL DEFAULT 0,
                discount_amount REAL DEFAULT 0,
                unpaid_amount REAL DEFAULT 0,
                diff_amount REAL DEFAULT 0,
                handle_status TEXT DEFAULT '待处理',
                handle_remark TEXT,
                handled_by TEXT,
                handled_at TEXT,
                FOREIGN KEY (arrear_id) REFERENCES arrears(id) ON DELETE SET NULL
            )
        """)

    sms_configs = [
        ("sms_provider", ""),
        ("sms_signature", ""),
        ("sms_api_url", ""),
        ("sms_app_key", ""),
        ("sms_app_secret", ""),
        ("sms_template_code", ""),
    ]
    for k, v in sms_configs:
        cursor.execute("SELECT value FROM config WHERE key = ?", (k,))
        if not cursor.fetchone():
            cursor.execute("INSERT INTO config (key, value) VALUES (?, ?)", (k, v))

    conn.commit()


def init_db() -> None:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.executescript("""
    CREATE TABLE IF NOT EXISTS households (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_no TEXT NOT NULL UNIQUE,
        building TEXT NOT NULL,
        unit TEXT,
        floor INTEGER,
        owner_name TEXT NOT NULL,
        phone TEXT,
        area REAL DEFAULT 0,
        property_type TEXT DEFAULT '住宅',
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    );

    CREATE TABLE IF NOT EXISTS arrears (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        household_id INTEGER NOT NULL,
        fee_period TEXT NOT NULL,
        fee_type TEXT DEFAULT '物业费',
        base_amount REAL NOT NULL DEFAULT 0,
        late_fee REAL NOT NULL DEFAULT 0,
        total_amount REAL NOT NULL DEFAULT 0,
        original_total REAL DEFAULT 0,
        due_date TEXT NOT NULL,
        paid_amount REAL NOT NULL DEFAULT 0,
        discount_amount REAL NOT NULL DEFAULT 0,
        status TEXT DEFAULT '未缴',
        remark TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (household_id) REFERENCES households(id) ON DELETE CASCADE,
        UNIQUE(household_id, fee_period, fee_type)
    );

    CREATE TABLE IF NOT EXISTS notice_templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        content TEXT NOT NULL,
        is_default INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );

    CREATE TABLE IF NOT EXISTS notice_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        arrear_id INTEGER NOT NULL,
        household_id INTEGER NOT NULL,
        channel TEXT DEFAULT '短信',
        template_name TEXT,
        content TEXT NOT NULL,
        phone TEXT,
        status TEXT DEFAULT '待发送',
        is_mock INTEGER DEFAULT 1,
        provider TEXT DEFAULT 'mock',
        message_id TEXT,
        retry_count INTEGER DEFAULT 0,
        max_retry INTEGER DEFAULT 3,
        sent_at TEXT,
        error_msg TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (arrear_id) REFERENCES arrears(id) ON DELETE CASCADE,
        FOREIGN KEY (household_id) REFERENCES households(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS call_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        household_id INTEGER NOT NULL,
        arrear_id INTEGER,
        contact_person TEXT,
        call_time TEXT NOT NULL,
        call_result TEXT,
        commitment_date TEXT,
        commitment_amount REAL DEFAULT 0,
        remark TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (household_id) REFERENCES households(id) ON DELETE CASCADE,
        FOREIGN KEY (arrear_id) REFERENCES arrears(id) ON DELETE SET NULL
    );

    CREATE TABLE IF NOT EXISTS discount_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        arrear_id INTEGER NOT NULL,
        household_id INTEGER NOT NULL,
        discount_amount REAL NOT NULL DEFAULT 0,
        discount_reason TEXT,
        approved_by TEXT,
        approved_at TEXT,
        remark TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (arrear_id) REFERENCES arrears(id) ON DELETE CASCADE,
        FOREIGN KEY (household_id) REFERENCES households(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS payment_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        arrear_id INTEGER NOT NULL,
        household_id INTEGER NOT NULL,
        amount REAL NOT NULL DEFAULT 0,
        pay_method TEXT,
        pay_date TEXT,
        operator TEXT,
        remark TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (arrear_id) REFERENCES arrears(id) ON DELETE CASCADE,
        FOREIGN KEY (household_id) REFERENCES households(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS holidays (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        holiday_date TEXT NOT NULL UNIQUE,
        holiday_name TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );

    CREATE TABLE IF NOT EXISTS config (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    );
    """)

    cursor.execute("SELECT COUNT(*) FROM notice_templates")
    if cursor.fetchone()[0] == 0:
        cursor.executemany("""
        INSERT INTO notice_templates (name, content, is_default) VALUES (?, ?, ?)
        """, [
            ("通用催缴短信", "【{signature}】尊敬的{owner_name}业主，您{room_no}室的{fee_period}{fee_type}共计{total_amount}元已于{due_date}到期，请您尽快缴纳。如有疑问请致电{service_phone}。退订回T", 1),
            ("温馨提醒短信", "【{signature}】温馨提醒：尊敬的{owner_name}业主，您{room_no}室尚有{fee_period}的物业费用未缴，合计{unpaid_amount}元。请您在方便时前往物业中心或线上缴纳，感谢您的配合！退订回T", 0),
            ("滞纳金提醒", "【{signature}】尊敬的{owner_name}业主，您{room_no}室的{fee_period}{fee_type}已逾期，本金{base_amount}元，产生滞纳金{late_fee}元，合计{unpaid_amount}元。请尽快缴纳以免产生更多滞纳金。退订回T", 0),
        ])

    cursor.execute("SELECT COUNT(*) FROM config")
    if cursor.fetchone()[0] == 0:
        default_configs = [
            ("late_fee_rate", "0.0005"),
            ("grace_days", "30"),
            ("sms_send_hour_start", "9"),
            ("sms_send_hour_end", "20"),
            ("company_name", "XX物业服务有限公司"),
            ("service_phone", "400-123-4567"),
            ("hide_sensitive", "1"),
            ("sms_provider", ""),
            ("sms_signature", "XX物业"),
            ("sms_api_url", ""),
            ("sms_app_key", ""),
            ("sms_app_secret", ""),
            ("sms_template_code", ""),
        ]
        cursor.executemany("INSERT INTO config (key, value) VALUES (?, ?)", default_configs)

    cursor.execute("SELECT COUNT(*) FROM holidays")
    if cursor.fetchone()[0] == 0:
        default_holidays = [
            ("2026-01-01", "元旦"),
            ("2026-02-16", "春节"),
            ("2026-02-17", "春节"),
            ("2026-02-18", "春节"),
            ("2026-04-04", "清明节"),
            ("2026-05-01", "劳动节"),
            ("2026-05-02", "劳动节"),
            ("2026-05-03", "劳动节"),
            ("2026-06-19", "端午节"),
            ("2026-09-25", "中秋节"),
            ("2026-10-01", "国庆节"),
            ("2026-10-02", "国庆节"),
            ("2026-10-03", "国庆节"),
            ("2026-10-04", "国庆节"),
            ("2026-10-05", "国庆节"),
            ("2026-10-06", "国庆节"),
            ("2026-10-07", "国庆节"),
        ]
        cursor.executemany("INSERT INTO holidays (holiday_date, holiday_name) VALUES (?, ?)", default_holidays)

    conn.commit()
    _migrate(conn)
    conn.close()


init_db()
