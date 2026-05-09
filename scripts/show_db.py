import json
import sqlite3
from pathlib import Path

from scripts.support.tou_price import TimeOfUsePriceResolver


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "homeassistant.db"
CACHE_PATH = DATA_DIR / "ha_95598_cache.json"


def show_sqlite_db() -> None:
    if not DB_PATH.exists():
        print(f"SQLite database not found: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    tables = []
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cur.fetchall()]

    print(f"SQLite database: {DB_PATH}")
    print(f"Tables: {', '.join(tables) if tables else '(none)'}")

    for table in ("daily_usage", "monthly_usage", "yearly_usage"):
        if table not in tables:
            continue
        print(f"\n[{table}]")
        order_column = "date" if table == "daily_usage" else "month" if table == "monthly_usage" else "year"
        cur.execute(f"SELECT * FROM {table} ORDER BY user_id, {order_column}")
        rows = cur.fetchall()
        for row in rows:
            print(" | ".join(str(value) for value in row))
        if not rows:
            print("(empty)")

    conn.close()


def show_cache() -> None:
    if not CACHE_PATH.exists():
        print(f"\nCache file not found: {CACHE_PATH}")
        return

    print(f"\nCache file: {CACHE_PATH}")
    with open(CACHE_PATH, "r", encoding="utf-8") as file:
        data = json.load(file)
    print(json.dumps(data, ensure_ascii=False, indent=2))


def show_tou_config() -> None:
    resolver = TimeOfUsePriceResolver()
    print(f"\nTOU config: {resolver.config_path}")
    if not resolver.config_path.exists():
        print("TOU config not found")
        return
    with open(resolver.config_path, "r", encoding="utf-8") as file:
        data = json.load(file)
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    show_sqlite_db()
    show_cache()
    show_tou_config()
