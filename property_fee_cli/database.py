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
            ("通用催缴短信", "【XX物业】尊敬的{owner_name}业主，您{room_no}室的{fee_period}{fee_type}共计{total_amount}元已于{due_date}到期，请您尽快缴纳。如有疑问请致电客服热线。退订回T", 1),
            ("温馨提醒短信", "【XX物业】温馨提醒：尊敬的{owner_name}业主，您{room_no}室尚有{fee_period}的物业费用未缴，合计{total_amount}元。请您在方便时前往物业中心或线上缴纳，感谢您的配合！退订回T", 0),
            ("滞纳金提醒", "【XX物业】尊敬的{owner_name}业主，您{room_no}室的{fee_period}{fee_type}已逾期，本金{base_amount}元，产生滞纳金{late_fee}元，合计{total_amount}元。请尽快缴纳以免产生更多滞纳金。退订回T", 0),
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
    conn.close()


init_db()
