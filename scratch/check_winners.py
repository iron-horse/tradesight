import sqlite3
db_path = "/Users/bhargavpatel/Projects/Trade/tradesight/data/tournament_history.db"
conn = sqlite3.connect(db_path)
cursor = conn.cursor()
cursor.execute("SELECT winner, winner_avg_score, start_time FROM tournament_sessions ORDER BY winner_avg_score DESC LIMIT 10;")
for r in cursor.fetchall():
    print(r)
conn.close()
