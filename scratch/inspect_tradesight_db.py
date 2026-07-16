import sqlite3
import os

db_path = "/Users/bhargavpatel/Projects/Trade/tradesight/data/tradesight.db"
if not os.path.exists(db_path):
    print("Database not found!")
    exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()
cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
tables = cursor.fetchall()
print("Tables in tradesight.db:", [t[0] for t in tables])

for table in tables:
    tname = table[0]
    cursor.execute(f"PRAGMA table_info({tname});")
    schema = cursor.fetchall()
    print(f"\nTable: {tname}")
    print("  Columns:", [s[1] for s in schema])
    cursor.execute(f"SELECT COUNT(*) FROM {tname};")
    count = cursor.fetchone()[0]
    print(f"  Row count: {count}")
    if count > 0:
        cursor.execute(f"SELECT * FROM {tname} LIMIT 5;")
        rows = cursor.fetchall()
        print("  Sample rows:")
        for r in rows:
            print("    ", r)

conn.close()
