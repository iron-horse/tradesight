import sqlite3
import os
import glob

data_dir = "/Users/bhargavpatel/Projects/Trade/tradesight/data"
dbs = glob.glob(os.path.join(data_dir, "*.db"))

print(f"Found {len(dbs)} databases in {data_dir}:")
for db in dbs:
    print(f"\n========================================\nDatabase: {os.path.basename(db)}\n========================================")
    try:
        conn = sqlite3.connect(db)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()
        print("Tables:", [t[0] for t in tables])
        
        for table in tables:
            tname = table[0]
            cursor.execute(f"PRAGMA table_info({tname});")
            schema = cursor.fetchall()
            print(f"\n  Table: {tname}")
            print("    Columns:", [s[1] for s in schema])
            cursor.execute(f"SELECT COUNT(*) FROM {tname};")
            count = cursor.fetchone()[0]
            print(f"    Row count: {count}")
            if count > 0:
                cursor.execute(f"SELECT * FROM {tname} LIMIT 3;")
                rows = cursor.fetchall()
                print("    Sample rows:")
                for r in rows:
                    print("      ", r)
        conn.close()
    except Exception as e:
        print(f"  Error reading database: {e}")
