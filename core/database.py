"""SQLite storage for completion state and manually added tasks."""
import os
import sqlite3


def get_conn(db_path):
    """打开一个连接，并确保两张表存在。

    这里的 `CREATE TABLE IF NOT EXISTS` 每次调用都会跑两条：它是幂等的，
    开销在微秒级，换来的是"数据库文件在运行期被删掉/换掉后仍能自愈"。
    刻意**不做**"建表只跑一次"的缓存——缓存后若 data/ 被删，程序会以
    `no such table` 报错，比多跑两条 DDL 严重得多。

    也刻意不开 WAL：本应用的写入是"勾选/新增任务"这种低频小事务，
    WAL 会额外产生 -wal/-shm 文件，收益不明显。
    """
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    # timeout=5.0 是 sqlite3 的默认值；显式写出来是为了让并发行为可读（双开时靠它兜住）
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS done (uid TEXT PRIMARY KEY, done_at TEXT)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS manual_tasks ("
            "uid TEXT PRIMARY KEY, title TEXT, course TEXT, due TEXT, url TEXT, created_at TEXT)"
        )
        conn.commit()
    except Exception:
        # 建表失败时不能把连接漏在外面：调用方拿不到 conn，也就没人会 close 它
        try:
            conn.close()
        except Exception:
            pass
        raise
    return conn
