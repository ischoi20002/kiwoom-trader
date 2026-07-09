# -*- coding: utf-8 -*-
import sqlite3

c = sqlite3.connect(r"c:\vibe\trading_data.db")
cur = c.cursor()
print(
    "1m count 2026-05-12:",
    cur.execute(
        "select count(*) from bars_1m where code=? and datetime >= ? and datetime < ?",
        ("004170", "2026-05-12 00:00:00", "2026-05-13 00:00:00"),
    ).fetchone(),
)
print(
    "1m minmax:",
    cur.execute(
        "select min(datetime),max(datetime) from bars_1m where code=?",
        ("004170",),
    ).fetchone(),
)
print(
    "15m count 2026-05-12:",
    cur.execute(
        "select count(*) from bars_15m where code=? and datetime >= ? and datetime < ?",
        ("004170", "2026-05-12 00:00:00", "2026-05-13 00:00:00"),
    ).fetchone(),
)
print(
    "sync_meta:",
    cur.execute(
        "select last_sync_dt,earliest_sync_dt,status from sync_meta where code=?",
        ("004170",),
    ).fetchone(),
)
c.close()
