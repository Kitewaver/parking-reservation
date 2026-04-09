#!/usr/bin/env python3
"""
駐車場予約システム 自動テストスイート
"""

import sys
import time
import json
import sqlite3
from datetime import datetime, timedelta

# テスト結果
test_results = []
TOTAL_TESTS = 0
PASSED_TESTS = 0

def test(name):
    """テストデコレーター"""
    def decorator(func):
        def wrapper():
            global TOTAL_TESTS, PASSED_TESTS
            TOTAL_TESTS += 1
            try:
                print(f"\n{'='*60}")
                print(f"テスト {TOTAL_TESTS}: {name}")
                print(f"{'='*60}")
                result = func()
                if result:
                    PASSED_TESTS += 1
                    print(f"✅ 成功")
                    test_results.append((name, True, None))
                else:
                    print(f"❌ 失敗")
                    test_results.append((name, False, "テストが False を返しました"))
            except Exception as e:
                print(f"❌ エラー: {e}")
                test_results.append((name, False, str(e)))
        return wrapper
    return decorator


@test("1. データベース初期化")
def test_database_init():
    """データベース初期化テスト"""
    import os
    
    # テスト用DBがあれば削除
    if os.path.exists('test_parking.db'):
        os.remove('test_parking.db')
    
    # 新しいDB作成
    conn = sqlite3.connect('test_parking.db')
    cursor = conn.cursor()
    
    # テーブル作成
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id TEXT UNIQUE,
            car_number TEXT,
            customer_name TEXT,
            phone TEXT,
            email TEXT,
            date TEXT,
            time_slot TEXT,
            amount INTEGER,
            status TEXT,
            created_at TEXT,
            cancelled_at TEXT
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS webhook_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            payment_id TEXT,
            processed_at TEXT NOT NULL
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS closed_dates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT UNIQUE,
            reason TEXT,
            created_at TEXT
        )
    ''')
    
    conn.commit()
    
    # テーブル存在確認
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cursor.fetchall()]
    
    conn.close()
    
    print(f"   作成されたテーブル: {tables}")
    return 'reservations' in tables and 'webhook_events' in tables


@test("2. 予約データ挿入")
def test_insert_reservation():
    """予約データ挿入テスト"""
    conn = sqlite3.connect('test_parking.db')
    cursor = conn.cursor()
    
    tomorrow = (datetime.now() + timedelta(days=1)).date().isoformat()
    
    cursor.execute('''
        INSERT INTO reservations 
        (payment_id, car_number, customer_name, phone, email, date, time_slot, amount, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        'pi_test_001',
        '横浜123あ4567',
        'テスト太郎',
        '090-1234-5678',
        'test@example.com',
        tomorrow,
        'morning',
        500,
        'confirmed',
        datetime.now().isoformat()
    ))
    
    conn.commit()
    
    # 確認
    cursor.execute('SELECT * FROM reservations WHERE payment_id = ?', ('pi_test_001',))
    row = cursor.fetchone()
    
    conn.close()
    
    print(f"   挿入されたデータ: payment_id={row[1]}, car_number={row[2]}")
    return row is not None and row[1] == 'pi_test_001'


@test("3. payment_id 重複防止")
def test_duplicate_payment_id():
    """payment_id UNIQUE制約テスト"""
    conn = sqlite3.connect('test_parking.db')
    cursor = conn.cursor()
    
    tomorrow = (datetime.now() + timedelta(days=1)).date().isoformat()
    
    try:
        # 同じpayment_idで再挿入を試みる
        cursor.execute('''
            INSERT INTO reservations 
            (payment_id, car_number, customer_name, phone, email, date, time_slot, amount, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            'pi_test_001',  # 既存のpayment_id
            '横浜999あ9999',
            'テスト次郎',
            '090-9999-9999',
            'test2@example.com',
            tomorrow,
            'afternoon',
            1100,
            'confirmed',
            datetime.now().isoformat()
        ))
        conn.commit()
        conn.close()
        print("   ❌ UNIQUE制約が機能していません")
        return False
    except sqlite3.IntegrityError as e:
        conn.close()
        print(f"   ✅ 正しくエラーが発生: {e}")
        return True


@test("4. Webhookイベント記録")
def test_webhook_event_tracking():
    """Webhookイベント記録テスト"""
    conn = sqlite3.connect('test_parking.db')
    cursor = conn.cursor()
    
    event_id = 'evt_test_001'
    
    # イベント記録
    cursor.execute('''
        INSERT INTO webhook_events (event_id, event_type, payment_id, processed_at)
        VALUES (?, ?, ?, ?)
    ''', (event_id, 'payment_intent.succeeded', 'pi_test_002', datetime.now().isoformat()))
    
    conn.commit()
    
    # 確認
    cursor.execute('SELECT * FROM webhook_events WHERE event_id = ?', (event_id,))
    row = cursor.fetchone()
    
    conn.close()
    
    print(f"   イベント記録: event_id={row[0]}, event_type={row[1]}")
    return row is not None and row[0] == event_id


@test("5. Webhookイベント重複検出")
def test_webhook_duplicate_detection():
    """Webhookイベント重複検出テスト"""
    conn = sqlite3.connect('test_parking.db')
    cursor = conn.cursor()
    
    # 既存のイベントIDで確認
    cursor.execute('SELECT event_id FROM webhook_events WHERE event_id = ?', ('evt_test_001',))
    existing = cursor.fetchone()
    
    if not existing:
        conn.close()
        print("   ❌ イベントが存在しません")
        return False
    
    print(f"   ✅ 既存イベント検出: {existing[0]}")
    
    # 同じイベントIDで再挿入を試みる
    try:
        cursor.execute('''
            INSERT INTO webhook_events (event_id, event_type, payment_id, processed_at)
            VALUES (?, ?, ?, ?)
        ''', ('evt_test_001', 'payment_intent.succeeded', 'pi_test_003', datetime.now().isoformat()))
        conn.commit()
        conn.close()
        print("   ❌ 重複が許可されました")
        return False
    except sqlite3.IntegrityError:
        conn.close()
        print("   ✅ 重複が正しく拒否されました")
        return True


@test("6. キャンセル処理")
def test_cancellation():
    """キャンセル処理テスト"""
    conn = sqlite3.connect('test_parking.db')
    cursor = conn.cursor()
    
    # キャンセル実行
    cursor.execute('''
        UPDATE reservations 
        SET status = 'cancelled', cancelled_at = ?
        WHERE payment_id = ?
    ''', (datetime.now().isoformat(), 'pi_test_001'))
    
    affected = cursor.rowcount
    conn.commit()
    
    # 確認
    cursor.execute('SELECT status FROM reservations WHERE payment_id = ?', ('pi_test_001',))
    row = cursor.fetchone()
    
    conn.close()
    
    print(f"   更新された行数: {affected}, ステータス: {row[0]}")
    return affected > 0 and row[0] == 'cancelled'


@test("7. 払い戻し記録")
def test_refund_status():
    """払い戻しステータス更新テスト"""
    conn = sqlite3.connect('test_parking.db')
    cursor = conn.cursor()
    
    # 新しい予約を追加
    tomorrow = (datetime.now() + timedelta(days=1)).date().isoformat()
    cursor.execute('''
        INSERT INTO reservations 
        (payment_id, car_number, customer_name, phone, email, date, time_slot, amount, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        'pi_test_refund',
        '横浜555あ5555',
        'テスト三郎',
        '090-5555-5555',
        'test3@example.com',
        tomorrow,
        'morning',
        500,
        'cancelled',
        datetime.now().isoformat()
    ))
    conn.commit()
    
    # refundedに更新
    cursor.execute('''
        UPDATE reservations 
        SET status = 'refunded'
        WHERE payment_id = ?
    ''', ('pi_test_refund',))
    
    conn.commit()
    
    # 確認
    cursor.execute('SELECT status FROM reservations WHERE payment_id = ?', ('pi_test_refund',))
    row = cursor.fetchone()
    
    conn.close()
    
    print(f"   ステータス: {row[0]}")
    return row[0] == 'refunded'


@test("8. 休業日設定")
def test_closed_dates():
    """休業日設定テスト"""
    conn = sqlite3.connect('test_parking.db')
    cursor = conn.cursor()
    
    closed_date = (datetime.now() + timedelta(days=7)).date().isoformat()
    
    cursor.execute('''
        INSERT INTO closed_dates (date, reason, created_at)
        VALUES (?, ?, ?)
    ''', (closed_date, 'メンテナンス', datetime.now().isoformat()))
    
    conn.commit()
    
    # 確認
    cursor.execute('SELECT * FROM closed_dates WHERE date = ?', (closed_date,))
    row = cursor.fetchone()
    
    conn.close()
    
    print(f"   休業日: {row[1]}, 理由: {row[2]}")
    return row is not None and row[1] == closed_date


@test("9. 予約一覧取得")
def test_list_reservations():
    """予約一覧取得テスト"""
    conn = sqlite3.connect('test_parking.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM reservations ORDER BY created_at DESC')
    rows = cursor.fetchall()
    
    conn.close()
    
    print(f"   取得された予約数: {len(rows)}")
    for row in rows:
        print(f"   - payment_id: {row[1]}, status: {row[9]}")
    
    return len(rows) > 0


@test("10. データベースクリーンアップ")
def test_cleanup():
    """テストデータクリーンアップ"""
    import os
    
    if os.path.exists('test_parking.db'):
        os.remove('test_parking.db')
        print("   テストDBを削除しました")
        return True
    return False


def print_summary():
    """テスト結果サマリー表示"""
    print("\n" + "="*60)
    print("テスト結果サマリー")
    print("="*60)
    
    for name, passed, error in test_results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status} - {name}")
        if error:
            print(f"         エラー: {error}")
    
    print("\n" + "="*60)
    print(f"合計: {TOTAL_TESTS} テスト")
    print(f"成功: {PASSED_TESTS} ({PASSED_TESTS/TOTAL_TESTS*100:.1f}%)")
    print(f"失敗: {TOTAL_TESTS - PASSED_TESTS}")
    print("="*60)
    
    if PASSED_TESTS == TOTAL_TESTS:
        print("\n🎉 すべてのテストに成功しました！")
        return 0
    else:
        print(f"\n⚠️  {TOTAL_TESTS - PASSED_TESTS} 件のテストが失敗しました")
        return 1


if __name__ == '__main__':
    print("""
╔════════════════════════════════════════════════════════════╗
║        駐車場予約システム 自動テストスイート               ║
╚════════════════════════════════════════════════════════════╝
    """)
    
    # テスト実行
    test_database_init()
    test_insert_reservation()
    test_duplicate_payment_id()
    test_webhook_event_tracking()
    test_webhook_duplicate_detection()
    test_cancellation()
    test_refund_status()
    test_closed_dates()
    test_list_reservations()
    test_cleanup()
    
    # 結果表示
    exit_code = print_summary()
    sys.exit(exit_code)
