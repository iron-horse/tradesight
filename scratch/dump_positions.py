import sqlite3
db_path = "/Users/bhargavpatel/Projects/Trade/tradesight/data/positions.db"
conn = sqlite3.connect(db_path)
cursor = conn.cursor()
cursor.execute("SELECT id, symbol, strategy, side, quantity, entry_price, current_price, status, entry_time FROM positions;")
for r in cursor.fetchall():
    print(r)
conn.close()
