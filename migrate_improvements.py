#!/usr/bin/env python3
"""
改善1・2 マイグレーションスクリプト
- reservations テーブルに cancel_token, cancel_token_expires_at を追加
- closed_dates テーブルが存在しない場合は作成

実行方法:
  DATABASE_URL=... python migrate_improvements.py
"""

import os
import sys
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
    else:
        db_path = os.environ.get('DB_PATH', 'parking_system.db')
        return sqlite3.connect(db_path)


def run():
    conn = get_conn()
    cur = conn.cursor()
    print(f"🗄  DB: {'PostgreSQL' if USE_POSTGRES else 'SQLite'}")

    # --- reservations: cancel_token カラム追加 ---
    try:
        if USE_POSTGRES:
            cur.execute("""
                ALTER TABLE reservations
                ADD COLUMN IF NOT EXISTS cancel_token TEXT;
            """)
            cur.execute("""
                ALTER TABLE reservations
                ADD COLUMN IF NOT EXISTS cancel_token_expires_at TEXT;
            """)
        else:
            # SQLite は ADD COLUMN IF NOT EXISTS 非対応 → 存在チェックしてから追加
            cur.execute("PRAGMA table_info(reservations)")
            existing = {row[1] for row in cur.fetchall()}
            if 'cancel_token' not in existing:
                cur.execute("ALTER TABLE reservations ADD COLUMN cancel_token TEXT")
                print("  ✅ cancel_token カラム追加")
            else:
                print("  ⏭  cancel_token カラムは既存")
            if 'cancel_token_expires_at' not in existing:
                cur.execute("ALTER TABLE reservations ADD COLUMN cancel_token_expires_at TEXT")
                print("  ✅ cancel_token_expires_at カラム追加")
            else:
                print("  ⏭  cancel_token_expires_at カラムは既存")

        if USE_POSTGRES:
            print("  ✅ cancel_token / cancel_token_expires_at カラム追加（PostgreSQL）")

        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"  ❌ reservations カラム追加エラー: {e}")
        sys.exit(1)

    # --- cancel_token にユニークインデックス ---
    try:
        if USE_POSTGRES:
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_reservations_cancel_token
                ON reservations(cancel_token)
                WHERE cancel_token IS NOT NULL;
            """)
        else:
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_reservations_cancel_token
                ON reservations(cancel_token);
            """)
        conn.commit()
        print("  ✅ cancel_token ユニークインデックス作成")
    except Exception as e:
        conn.rollback()
        print(f"  ⚠  インデックス作成スキップ（既存の可能性）: {e}")

    # --- closed_dates: calendar_event_id カラム追加（既存テーブルに未追加の場合）---
    try:
        if USE_POSTGRES:
            cur.execute("""
                ALTER TABLE closed_dates
                ADD COLUMN IF NOT EXISTS calendar_event_id TEXT;
            """)
        else:
            cur.execute("PRAGMA table_info(closed_dates)")
            existing = {row[1] for row in cur.fetchall()}
            if 'calendar_event_id' not in existing:
                cur.execute("ALTER TABLE closed_dates ADD COLUMN calendar_event_id TEXT")
                print("  ✅ closed_dates.calendar_event_id カラム追加")
            else:
                print("  ⏭  closed_dates.calendar_event_id カラムは既存")
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"  ⚠  closed_dates カラム追加スキップ: {e}")

    conn.close()
    print("\n✅ マイグレーション完了")


if __name__ == '__main__':
    run()
