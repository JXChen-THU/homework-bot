"""SQLite storage for completion state and manually added tasks."""
import os
import sqlite3


def get_conn(db_path):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS done (uid TEXT PRIMARY KEY, done_at TEXT)")
    conn.execute("""CREATE TABLE IF NOT EXISTS manual_tasks (
        uid TEXT PRIMARY KEY, title TEXT, course TEXT, due TEXT, url TEXT, created_at TEXT
    )""")
    conn.commit()
    return conn
