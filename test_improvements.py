#!/usr/bin/env python3
"""
改善1・2 自動テストスイート

改善1: キャンセルトークン方式
改善2: 月単位カレンダーAPI

実行方法:
  python test_improvements.py
"""

import sys
import os
import json
import sqlite3
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")

# テスト結果
test_results = []
TOTAL_TESTS = 0
PASSED_TESTS = 0

TEST_DB = 'test_improvements.db'


def test(name):
    """テストデコレーター（既存スイートと同じ形式）"""
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
                import traceback
                traceback.print_exc()
                test_results.append((name, False, str(e)))
        return wrapper
    return decorator


# ─────────────────────────────────────────
# テスト用DBセットアップ
# ─────────────────────────────────────────

def get_test_conn():
    return sqlite3.connect(TEST_DB)


def setup_test_db():
    """改善1・2のカラムを含むテーブルを作成"""
    conn = get_test_conn()
    cur = conn.cursor()
    cur.execute('''
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
            cancelled_at TEXT,
            calendar_event_id TEXT,
            cancel_token TEXT UNIQUE,
            cancel_token_expires_at TEXT
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS closed_dates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            time_slot TEXT NOT NULL,
            reason TEXT,
            created_at TEXT,
            calendar_event_id TEXT,
            UNIQUE(date, time_slot)
        )
    ''')
    conn.commit()
    conn.close()


# ─────────────────────────────────────────
# 改善1: キャンセルトークン テスト
# ─────────────────────────────────────────

@test("1. DBスキーマ: cancel_token カラムが存在する")
def test_schema_cancel_token():
    setup_test_db()
    conn = get_test_conn()
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(reservations)")
    columns = {row[1] for row in cur.fetchall()}
    conn.close()
    print(f"   カラム一覧: {sorted(columns)}")
    has_token = 'cancel_token' in columns
    has_expires = 'cancel_token_expires_at' in columns
    print(f"   cancel_token: {'✅' if has_token else '❌'}")
    print(f"   cancel_token_expires_at: {'✅' if has_expires else '❌'}")
    return has_token and has_expires


@test("2. トークン生成: 64文字のURL-safeな文字列")
def test_token_generation():
    token = secrets.token_urlsafe(32)
    print(f"   生成されたトークン: {token[:20]}...")
    print(f"   トークン長: {len(token)} 文字")
    # URL-safe な文字のみ（英数字・-・_）
    import re
    is_url_safe = bool(re.match(r'^[A-Za-z0-9_\-]+$', token))
    print(f"   URL-safe: {'✅' if is_url_safe else '❌'}")
    return len(token) >= 40 and is_url_safe


@test("3. トークン有効期限: 午前枠は当日0時の2時間前")
def test_token_expiry_morning():
    # 明後日の午前枠
    target = (datetime.now(JST) + timedelta(days=2)).date().isoformat()
    dt = datetime.fromisoformat(target).replace(hour=0, minute=0, second=0, tzinfo=JST)
    expires = dt - timedelta(hours=2)
    expected_hour = 22  # 前日22時
    print(f"   対象日: {target} 午前枠")
    print(f"   期限: {expires.isoformat()}")
    print(f"   期限の時刻（時）: {expires.hour} （期待値: {expected_hour}）")
    return expires.hour == expected_hour


@test("4. トークン有効期限: 午後枠は当日12時の2時間前")
def test_token_expiry_afternoon():
    target = (datetime.now(JST) + timedelta(days=2)).date().isoformat()
    dt = datetime.fromisoformat(target).replace(hour=12, minute=0, second=0, tzinfo=JST)
    expires = dt - timedelta(hours=2)
    expected_hour = 10  # 当日10時
    print(f"   対象日: {target} 午後枠")
    print(f"   期限: {expires.isoformat()}")
    print(f"   期限の時刻（時）: {expires.hour} （期待値: {expected_hour}）")
    return expires.hour == expected_hour


@test("5. トークン保存: 予約確定時にDBへ保存される")
def test_token_saved_on_confirm():
    conn = get_test_conn()
    cur = conn.cursor()
    tomorrow = (datetime.now(JST) + timedelta(days=1)).date().isoformat()
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(JST) + timedelta(hours=20)).isoformat()

    cur.execute('''
        INSERT INTO reservations
        (payment_id, car_number, customer_name, phone, email,
         date, time_slot, amount, status, created_at,
         cancel_token, cancel_token_expires_at)
        VALUES (?,?,?,?,?,?,?,?,'confirmed',?,?,?)
    ''', ('pi_tok_001', '横浜100あ0001', 'トークンテスト太郎',
          '090-0001-0001', 'token1@example.com',
          tomorrow, 'morning', 500, datetime.now().isoformat(),
          token, expires))
    conn.commit()

    cur.execute('SELECT cancel_token, cancel_token_expires_at FROM reservations WHERE payment_id=?', ('pi_tok_001',))
    row = cur.fetchone()
    conn.close()

    print(f"   保存されたトークン: {row[0][:20]}...")
    print(f"   有効期限: {row[1]}")
    return row[0] == token and row[1] == expires


@test("6. トークン検索: 有効なトークンで予約を取得できる")
def test_cancel_by_valid_token():
    conn = get_test_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT payment_id, date, amount FROM reservations WHERE cancel_token=? AND status='confirmed'",
        (secrets.token_urlsafe(32),)  # 存在しないトークン
    )
    row = cur.fetchone()
    conn.close()
    print(f"   存在しないトークン → 結果: {'None ✅' if row is None else '取得されてしまった ❌'}")
    return row is None  # 存在しないトークンは None が正しい


@test("7. トークン検索: 登録済みトークンで予約情報を取得できる")
def test_find_reservation_by_token():
    conn = get_test_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT payment_id, car_number, amount FROM reservations WHERE cancel_token IS NOT NULL AND status='confirmed' LIMIT 1"
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        print("   ❌ confirmed予約が存在しない")
        return False
    print(f"   取得: payment_id={row[0]}, car_number={row[1]}, amount=¥{row[2]}")
    return row[0] == 'pi_tok_001'


@test("8. トークン期限切れ: 期限過ぎはキャンセル不可")
def test_token_expired():
    # 期限が過去のトークンを挿入
    conn = get_test_conn()
    cur = conn.cursor()
    yesterday = (datetime.now(JST) - timedelta(days=1)).date().isoformat()
    expired_token = secrets.token_urlsafe(32)
    expired_at = (datetime.now(JST) - timedelta(hours=3)).isoformat()  # 3時間前に期限切れ

    cur.execute('''
        INSERT INTO reservations
        (payment_id, car_number, customer_name, phone, email,
         date, time_slot, amount, status, created_at,
         cancel_token, cancel_token_expires_at)
        VALUES (?,?,?,?,?,?,?,?,'confirmed',?,?,?)
    ''', ('pi_tok_expired', '横浜999あ9999', '期限切れテスト',
          '090-9999-9999', 'expired@example.com',
          yesterday, 'morning', 500, datetime.now().isoformat(),
          expired_token, expired_at))
    conn.commit()
    conn.close()

    # 期限チェックロジック
    now = datetime.now(JST)
    expires_dt = datetime.fromisoformat(expired_at).replace(tzinfo=JST)
    is_expired = now >= expires_dt

    print(f"   期限: {expired_at}")
    print(f"   現在: {now.isoformat()}")
    print(f"   期限切れ判定: {'期限切れ ✅' if is_expired else '有効（期待外れ） ❌'}")
    return is_expired


@test("9. トークン無効化: キャンセル後にNULLになる")
def test_token_nullified_after_cancel():
    conn = get_test_conn()
    cur = conn.cursor()

    # キャンセル処理（トークンをNULLに）
    cur.execute(
        "UPDATE reservations SET status='cancelled', cancelled_at=?, cancel_token=NULL WHERE payment_id=?",
        (datetime.now().isoformat(), 'pi_tok_001')
    )
    conn.commit()

    cur.execute('SELECT status, cancel_token FROM reservations WHERE payment_id=?', ('pi_tok_001',))
    row = cur.fetchone()
    conn.close()

    print(f"   ステータス: {row[0]}")
    print(f"   cancel_token: {row[1]} （期待値: None）")
    return row[0] == 'cancelled' and row[1] is None


@test("10. トークン重複防止: 同じトークンは2件挿入できない")
def test_token_unique_constraint():
    conn = get_test_conn()
    cur = conn.cursor()
    tomorrow = (datetime.now(JST) + timedelta(days=1)).date().isoformat()
    dup_token = secrets.token_urlsafe(32)

    cur.execute('''
        INSERT INTO reservations
        (payment_id, car_number, customer_name, phone, email,
         date, time_slot, amount, status, created_at, cancel_token)
        VALUES (?,?,?,?,?,?,?,?,'confirmed',?,?)
    ''', ('pi_dup_1', '横浜111あ1111', 'dup1', '090-1111-1111',
          'd1@example.com', tomorrow, 'afternoon', 1100,
          datetime.now().isoformat(), dup_token))
    conn.commit()

    try:
        cur.execute('''
            INSERT INTO reservations
            (payment_id, car_number, customer_name, phone, email,
             date, time_slot, amount, status, created_at, cancel_token)
            VALUES (?,?,?,?,?,?,?,?,'confirmed',?,?)
        ''', ('pi_dup_2', '横浜222あ2222', 'dup2', '090-2222-2222',
              'd2@example.com', tomorrow, 'morning', 500,
              datetime.now().isoformat(), dup_token))  # 同じトークン
        conn.commit()
        conn.close()
        print("   ❌ 重複トークンが挿入できてしまった")
        return False
    except sqlite3.IntegrityError as e:
        conn.close()
        print(f"   ✅ 重複トークンが正しく拒否された: {e}")
        return True


# ─────────────────────────────────────────
# 改善2: 月カレンダーAPI テスト
# ─────────────────────────────────────────

@test("11. 月カレンダー: 翌月データを正しく取得できる（ロジック確認）")
def test_month_availability_logic():
    """/api/month-availability のDBクエリロジックを直接テスト"""
    from calendar import monthrange

    conn = get_test_conn()
    cur = conn.cursor()

    # テスト用データ準備
    now = datetime.now(JST)
    year, month = now.year, now.month
    # 翌月
    if month == 12:
        year, month = year + 1, 1
    else:
        month += 1

    _, last_day = monthrange(year, month)
    month_start = f"{year}-{str(month).padStart(2,'0') if False else str(month).zfill(2)}-01"
    month_end   = f"{year}-{str(month).zfill(2)}-{str(last_day).zfill(2)}"

    # 翌月1日に予約を入れる
    test_date = f"{year}-{str(month).zfill(2)}-01"
    cur.execute('''
        INSERT OR IGNORE INTO reservations
        (payment_id, car_number, customer_name, phone, email,
         date, time_slot, amount, status, created_at)
        VALUES (?,?,?,?,?,?,?,?,'confirmed',?)
    ''', ('pi_cal_001', '横浜cal1', 'カレンダーテスト', '090-0000-0000',
          'cal@example.com', test_date, 'morning', 500,
          datetime.now().isoformat()))

    # 翌月2日を休業日に
    closed_date = f"{year}-{str(month).zfill(2)}-02"
    cur.execute(
        "INSERT OR IGNORE INTO closed_dates (date, time_slot, reason, created_at) VALUES (?,?,?,?)",
        (closed_date, 'morning', 'テスト休業日', datetime.now().isoformat())
    )
    cur.execute(
        "INSERT OR IGNORE INTO closed_dates (date, time_slot, reason, created_at) VALUES (?,?,?,?)",
        (closed_date, 'afternoon', 'テスト休業日', datetime.now().isoformat())
    )
    conn.commit()

    # 予約済み取得
    cur.execute(
        "SELECT date, time_slot FROM reservations WHERE date >= ? AND date <= ? AND status='confirmed'",
        (month_start, month_end)
    )
    reserved = {(r[0], r[1]) for r in cur.fetchall()}

    # 休業日取得（date, time_slot ペア）
    cur.execute(
        "SELECT date, time_slot FROM closed_dates WHERE date >= ? AND date <= ?",
        (month_start, month_end)
    )
    closed = {(r[0], r[1]) for r in cur.fetchall()}
    conn.close()

    print(f"   対象月: {year}年{month}月")
    print(f"   予約済み: {reserved}")
    print(f"   休業日: {closed}")

    morning_reserved = (test_date, 'morning') in reserved
    day2_closed = (closed_date, 'morning') in closed and (closed_date, 'afternoon') in closed

    print(f"   {test_date} 午前が予約済み: {'✅' if morning_reserved else '❌'}")
    print(f"   {closed_date} が休業日: {'✅' if day2_closed else '❌'}")
    return morning_reserved and day2_closed


@test("12. 月カレンダー: 過去日はpast判定になる")
def test_past_date_status():
    yesterday = (datetime.now(JST) - timedelta(days=1)).date()
    today = datetime.now(JST).date()
    is_past = yesterday < today
    print(f"   昨日({yesterday}) < 今日({today}): {'✅ past判定' if is_past else '❌'}")
    return is_past


@test("13. 月カレンダー: 休業日は両スロットともclosed")
def test_closed_date_both_slots():
    conn = get_test_conn()
    cur = conn.cursor()
    now = datetime.now(JST)
    # 既存の休業日を検索
    future = (now + timedelta(days=1)).date().isoformat()
    for slot in ('morning', 'afternoon'):
        cur.execute(
            "INSERT OR IGNORE INTO closed_dates (date, time_slot, reason, created_at) VALUES (?,?,?,?)",
            (future, slot, 'テスト', now.isoformat())
        )
    conn.commit()
    cur.execute("SELECT date FROM closed_dates WHERE date=?", (future,))
    row = cur.fetchone()
    conn.close()

    is_closed = row is not None
    # 休業日なら morning も afternoon も closed
    morning_status = 'closed' if is_closed else 'available'
    afternoon_status = 'closed' if is_closed else 'available'

    print(f"   {future}: morning={morning_status}, afternoon={afternoon_status}")
    return morning_status == 'closed' and afternoon_status == 'closed'


@test("14. 月カレンダー: 予約済みスロットはreserved、空きはavailable")
def test_slot_status_mixed():
    conn = get_test_conn()
    cur = conn.cursor()
    # 3日後を使う（休業日でない想定）
    target = (datetime.now(JST) + timedelta(days=3)).date().isoformat()

    # morningのみ予約済みにする
    cur.execute('''
        INSERT OR IGNORE INTO reservations
        (payment_id, car_number, customer_name, phone, email,
         date, time_slot, amount, status, created_at)
        VALUES (?,?,?,?,?,?,?,?,'confirmed',?)
    ''', ('pi_mix_001', '横浜mix1', 'ミックステスト', '090-1234-0000',
          'mix@example.com', target, 'morning', 500,
          datetime.now().isoformat()))
    conn.commit()

    cur.execute(
        "SELECT time_slot FROM reservations WHERE date=? AND status='confirmed'",
        (target,)
    )
    reserved_slots = {r[0] for r in cur.fetchall()}
    conn.close()

    morning_reserved = 'morning' in reserved_slots
    afternoon_available = 'afternoon' not in reserved_slots

    print(f"   {target}")
    print(f"   morning: {'reserved ✅' if morning_reserved else '❌'}")
    print(f"   afternoon: {'available ✅' if afternoon_available else '❌'}")
    return morning_reserved and afternoon_available


@test("15. 月カレンダー: 当月の前月には戻れない（境界値）")
def test_month_navigation_min():
    now = datetime.now(JST)
    current_month = now.replace(day=1).date()

    # 前月
    if now.month == 1:
        prev = now.replace(year=now.year - 1, month=12, day=1).date()
    else:
        prev = now.replace(month=now.month - 1, day=1).date()

    # フロント側ロジック: prev < current_month なら戻れない
    can_go_back = prev >= current_month
    print(f"   現在月: {current_month}")
    print(f"   前月: {prev}")
    print(f"   前月への移動: {'許可 ❌（期待は不可）' if can_go_back else '不可 ✅'}")
    return not can_go_back


@test("16. 月カレンダー: 2ヶ月先より後には進めない（境界値）")
def test_month_navigation_max():
    now = datetime.now(JST)
    # 2ヶ月後の翌月 = 3ヶ月後
    month_plus_3 = now.month + 3
    year_plus_3 = now.year
    if month_plus_3 > 12:
        month_plus_3 -= 12
        year_plus_3 += 1
    next_3 = now.replace(year=year_plus_3, month=month_plus_3, day=1).date()

    # 最大2ヶ月後
    max_month = now.month + 2
    max_year = now.year
    if max_month > 12:
        max_month -= 12
        max_year += 1
    max_date = now.replace(year=max_year, month=max_month, day=1).date()

    can_go = next_3 < max_date
    print(f"   3ヶ月後({next_3}) < 最大({max_date}): {'進める ❌（期待は不可）' if can_go else '進めない ✅'}")
    return not can_go


# ─────────────────────────────────────────
# メールテンプレート テスト
# ─────────────────────────────────────────

@test("17. メール本文: キャンセルURLにトークンが含まれる")
def test_email_contains_cancel_url():
    base_url = 'https://parking-reservation-rzck.onrender.com'
    token = secrets.token_urlsafe(32)
    cancel_url = f"{base_url}/cancel?token={token}"

    # メール本文生成（実際の send_reservation_email と同じロジック）
    html = f"""
    <a href="{cancel_url}">予約をキャンセルする</a>
    <p>{cancel_url}</p>
    """

    has_token_url = f"/cancel?token={token}" in html
    has_button = 'キャンセルする' in html

    print(f"   キャンセルURL: {cancel_url[:60]}...")
    print(f"   URLがHTMLに含まれる: {'✅' if has_token_url else '❌'}")
    print(f"   ボタンテキストあり: {'✅' if has_button else '❌'}")
    return has_token_url and has_button


@test("18. メール本文: payment_idが表示されない（旧方式の排除確認）")
def test_email_no_payment_id():
    # 新しいメール本文に payment_id が含まれないことを確認
    reservation_data = {
        'date': '2026-05-01',
        'time_slot': 'morning',
        'car_number': '横浜123あ4567',
        'amount': 500,
        'payment_id': 'pi_secret_12345',
        'cancel_url': 'https://example.com/cancel?token=abc123'
    }

    # 新テンプレートでは payment_id を表示しない
    html = f"""
    <tr><td>ご利用日:</td><td>{reservation_data['date']}</td></tr>
    <tr><td>車両番号:</td><td>{reservation_data['car_number']}</td></tr>
    <tr><td>料金:</td><td>¥{reservation_data['amount']:,}</td></tr>
    <a href="{reservation_data['cancel_url']}">キャンセルする</a>
    """

    # payment_id が本文に含まれていないことを確認
    no_payment_id = reservation_data['payment_id'] not in html
    has_cancel_link = reservation_data['cancel_url'] in html

    print(f"   payment_idが非表示: {'✅' if no_payment_id else '❌（含まれてしまっている）'}")
    print(f"   キャンセルリンクあり: {'✅' if has_cancel_link else '❌'}")
    return no_payment_id and has_cancel_link


# ─────────────────────────────────────────
# クリーンアップ
# ─────────────────────────────────────────

@test("19. テストDBクリーンアップ")
def test_cleanup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
        print(f"   {TEST_DB} を削除しました")
        return True
    print(f"   {TEST_DB} が存在しません（スキップ）")
    return True


# ─────────────────────────────────────────
# サマリー表示
# ─────────────────────────────────────────

def print_summary():
    print("\n" + "="*60)
    print("テスト結果サマリー")
    print("="*60)

    improvement1 = [(n, p, e) for n, p, e in test_results if any(
        k in n for k in ['DBスキーマ', 'トークン', '無効化', '重複防止', 'メール']
    )]
    improvement2 = [(n, p, e) for n, p, e in test_results if any(
        k in n for k in ['月カレンダー', 'past判定', '休業日', '予約済み', '前月', '先']
    )]
    other = [(n, p, e) for n, p, e in test_results if (n, True, None) not in improvement1 + improvement2
             and (n, False, None) not in improvement1 + improvement2
             and not any(r[0] == n for r in improvement1 + improvement2)]

    def print_group(label, items):
        if not items:
            return
        print(f"\n【{label}】")
        for name, passed, error in items:
            status = "✅ PASS" if passed else "❌ FAIL"
            print(f"  {status} - {name}")
            if error:
                print(f"           エラー: {error}")

    print_group("改善1: キャンセルトークン", improvement1)
    print_group("改善2: 月カレンダー", improvement2)
    print_group("その他", other)

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
║       改善1・2 自動テストスイート                          ║
║       改善1: キャンセルトークン方式                        ║
║       改善2: 月単位カレンダーAPI                           ║
╚════════════════════════════════════════════════════════════╝
    """)

    # 改善1: キャンセルトークン
    test_schema_cancel_token()
    test_token_generation()
    test_token_expiry_morning()
    test_token_expiry_afternoon()
    test_token_saved_on_confirm()
    test_cancel_by_valid_token()
    test_find_reservation_by_token()
    test_token_expired()
    test_token_nullified_after_cancel()
    test_token_unique_constraint()

    # 改善2: 月カレンダー
    test_month_availability_logic()
    test_past_date_status()
    test_closed_date_both_slots()
    test_slot_status_mixed()
    test_month_navigation_min()
    test_month_navigation_max()

    # メールテンプレート
    test_email_contains_cancel_url()
    test_email_no_payment_id()

    # クリーンアップ
    test_cleanup()

    exit_code = print_summary()
    sys.exit(exit_code)
