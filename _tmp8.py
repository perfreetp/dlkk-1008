import sys
sys.path.insert(0, '.')
from property_fee_cli.database import get_connection

conn = get_connection()
try:
    print("=== 所有表 ===")
    tables = [x[0] for x in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
    print(tables)
    for t in tables:
        if 'template' in t.lower() or 'notice' in t.lower() or 'sms' in t.lower():
            print(f"\n=== {t} 列 ===")
            cols = conn.execute(f"PRAGMA table_info({t})").fetchall()
            for c in cols:
                print(f"  {c[1]} {c[2]}")
finally:
    conn.close()
