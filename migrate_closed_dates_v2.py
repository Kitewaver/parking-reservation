#!/usr/bin/env python3
"""
closed_dates テーブル マイグレーション v2
- date UNIQUE → (date, time_slot) の複合ユニークに変更
- 既存データは morning / afternoon 両方に複製して移行

実行方法:
  DATABASE_URL=... python migrate_closed_dates_v2.py
"""
import os, sys
from datetime import datetime

DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL and DATABASE_URL.startswith('postgres'):
    import psycopg2
    USE_POSTGRES = True
else:
    import sqlite3
    USE_POSTGRES = False

def get_conn():
    if USE_POSTGRES:
        return psycopg2.connect(DATABASE_URL, sslmode='require')
    db_path = os.environ.get('DB_PATH', 'parking_system.db')
    return sqlite3.connect(db_path)

def run():
    conn = get_conn()
    cur = conn.cursor()
    print(f"🗄  DB: {'PostgreSQL' if USE_POSTGRES else 'SQLite'}")

    if USE_POSTGRES:
        # 既存データを取得
        cur.execute("SELECT date, reason, created_at, calendar_event_id FROM closed_dates")
        existing = cur.fetchall()
        print(f"   既存データ: {len(existing)} 件")

        # テーブル作り直し
        cur.execute("DROP TABLE IF EXISTS closed_dates")
        cur.execute('''
            CREATE TABLE closed_dates (
                id SERIAL PRIMARY KEY,
                date TEXT NOT NULL,
                time_slot TEXT NOT NULL CHECK (time_slot IN ('morning', 'afternoon')),
                reason TEXT,
                created_at TEXT,
                calendar_event_id TEXT,
                UNIQUE(date, time_slot)
            )
        ''')

        # 既存データを morning / afternoon に複製
        for row in existing:
            date, reason, created_at, cal_id = row
            for slot in ['morning', 'afternoon']:
                cur.execute('''
                    INSERT INTO closed_dates (date, time_slot, reason, created_at, calendar_event_id)
                    VALUES (%s, %s, %s, %s, %s)
                ''', (date, slot, reason, created_at, cal_id))
        print(f"   移行完了: {len(existing)} 件 → {len(existing)*2} 件（AM/PM展開）")

    else:
        # SQLite: 既存データ取得
        try:
            cur.execute("SELECT date, reason, created_at FROM closed_dates")
            existing = cur.fetchall()
        except Exception:
            existing = []
        print(f"   既存データ: {len(existing)} 件")

        cur.execute("DROP TABLE IF EXISTS closed_dates")
        cur.execute('''
            CREATE TABLE closed_dates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                time_slot TEXT NOT NULL CHECK (time_slot IN ('morning', 'afternoon')),
                reason TEXT,
                created_at TEXT,
                calendar_event_id TEXT,
                UNIQUE(date, time_slot)
            )
        ''')
        for row in existing:
            date, reason, created_at = row[0], row[1], row[2]
            for slot in ['morning', 'afternoon']:
                cur.execute('''
                    INSERT INTO closed_dates (date, time_slot, reason, created_at)
                    VALUES (?, ?, ?, ?)
                ''', (date, slot, reason, created_at))
        print(f"   移行完了: {len(existing)} 件 → {len(existing)*2} 件（AM/PM展開）")

    conn.commit()
    conn.close()
    print("\n✅ マイグレーション完了")

if __name__ == '__main__':
    run()
