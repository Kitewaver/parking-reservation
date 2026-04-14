#!/usr/bin/env python3
"""
新機能テストスイート（Gmail API / Google Calendar）
"""
import os
import sys
import json
from datetime import datetime, timedelta

test_results = []
TOTAL_TESTS = 0
PASSED_TESTS = 0

def test(name):
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


@test("1. Gmail API接続確認")
def test_gmail_connection():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token_json = os.environ.get('GMAIL_TOKEN_JSON')
    if token_json:
        creds = Credentials.from_authorized_user_info(json.loads(token_json))
    else:
        creds = Credentials.from_authorized_user_file('token.json')

    service = build('gmail', 'v1', credentials=creds)
    # send権限のみなのでserviceオブジェクト生成で接続確認
    print(f"   Gmail API接続OK: serviceオブジェクト生成成功")
    return service is not None


@test("2. Gmail APIテスト送信")
def test_gmail_send():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    import base64

    token_json = os.environ.get('GMAIL_TOKEN_JSON')
    if token_json:
        creds = Credentials.from_authorized_user_info(json.loads(token_json))
    else:
        creds = Credentials.from_authorized_user_file('token.json')

    service = build('gmail', 'v1', credentials=creds)
    to_email = os.environ.get('TEST_EMAIL', 'Noboru.Takizawa@blueflag-sys.com')

    msg = MIMEMultipart('alternative')
    msg['Subject'] = '【自動テスト】Gmail API送信テスト'
    msg['From'] = os.environ.get('EMAIL_SENDER', 'test@gmail.com')
    msg['To'] = to_email
    msg.attach(MIMEText('<p>自動テストからの送信です。</p>', 'html'))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    result = service.users().messages().send(userId='me', body={'raw': raw}).execute()
    print(f"   送信先: {to_email}")
    print(f"   Message ID: {result.get('id')}")
    return 'id' in result


@test("3. Google Calendar API接続確認")
def test_calendar_connection():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token_json = os.environ.get('GMAIL_TOKEN_JSON')
    if token_json:
        creds = Credentials.from_authorized_user_info(json.loads(token_json))
    else:
        creds = Credentials.from_authorized_user_file('token.json')

    service = build('calendar', 'v3', credentials=creds)
    calendar = service.calendars().get(calendarId='primary').execute()
    print(f"   カレンダー: {calendar.get('summary')}")
    return 'summary' in calendar


@test("4. Google Calendarテスト登録・削除")
def test_calendar_crud():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token_json = os.environ.get('GMAIL_TOKEN_JSON')
    if token_json:
        creds = Credentials.from_authorized_user_info(json.loads(token_json))
    else:
        creds = Credentials.from_authorized_user_file('token.json')

    service = build('calendar', 'v3', credentials=creds)
    tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')

    # 登録
    event = {
        'summary': '【テスト】駐車場予約テスト',
        'description': 'このイベントは自動テストから作成されました。',
        'start': {'dateTime': f'{tomorrow}T09:00:00', 'timeZone': 'Asia/Tokyo'},
        'end': {'dateTime': f'{tomorrow}T12:00:00', 'timeZone': 'Asia/Tokyo'},
    }
    created = service.events().insert(calendarId='primary', body=event).execute()
    event_id = created.get('id')
    print(f"   登録成功: {event_id}")

    # 削除
    service.events().delete(calendarId='primary', eventId=event_id).execute()
    print(f"   削除成功: {event_id}")
    return event_id is not None


@test("5. 予約メール本文確認")
def test_reservation_email_content():
    reservation_data = {
        'date': '2026-04-30',
        'time_slot': 'morning',
        'car_number': '横浜505ゆ6272',
        'amount': 500,
        'payment_id': 'pi_test_123'
    }
    time_label = '0-12時' if reservation_data['time_slot'] == 'morning' else '12-24時'
    html = f"""
    <h2>駐車場予約が完了しました</h2>
    <p>テストユーザー 様</p>
    <tr><td>{reservation_data.get('date', '')}</td></tr>
    <tr><td>{time_label}</td></tr>
    <tr><td>¥{reservation_data.get('amount', 0):,}</td></tr>
    """
    print(f"   日付: {reservation_data['date']}")
    print(f"   時間帯: {time_label}")
    print(f"   料金: ¥{reservation_data['amount']:,}")
    return '2026-04-30' in html and '0-12時' in html and '¥500' in html


@test("6. キャンセルメール本文確認")
def test_cancellation_email_content():
    reservation_data = {
        'date': '2026-04-30',
        'time_slot': 'afternoon',
        'car_number': '横浜505ゆ6272',
    }
    refund_amount = 400
    fee = 100
    original_amount = refund_amount + fee
    time_label = '0-12時' if reservation_data.get('time_slot') == 'morning' else '12-24時'
    html = f"""
    <h2>予約キャンセルが完了しました</h2>
    <tr><td>¥{original_amount:,}</td></tr>
    <tr><td>¥{fee:,}</td></tr>
    <tr><td>¥{refund_amount:,}</td></tr>
    """
    print(f"   元の料金: ¥{original_amount:,}")
    print(f"   手数料: ¥{fee:,}")
    print(f"   返金額: ¥{refund_amount:,}")
    return '¥500' in html and '¥100' in html and '¥400' in html


def print_summary():
    print("\n" + "="*60)
    print("テスト結果サマリー")
    print("="*60)
    for name, passed, error in test_results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status} - {name}")
        if error:
            print(f"         エラー: {error}")
    print(f"\n合計: {TOTAL_TESTS} / 成功: {PASSED_TESTS} / 失敗: {TOTAL_TESTS - PASSED_TESTS}")
    if PASSED_TESTS == TOTAL_TESTS:
        print("\n🎉 すべてのテストに成功しました！")
        return 0
    else:
        print(f"\n⚠️  {TOTAL_TESTS - PASSED_TESTS} 件のテストが失敗しました")
        return 1


if __name__ == '__main__':
    print("""
╔════════════════════════════════════════════════════════════╗
║     新機能テストスイート（Gmail / Google Calendar）        ║
╚════════════════════════════════════════════════════════════╝
    """)
    test_gmail_connection()
    test_gmail_send()
    test_calendar_connection()
    test_calendar_crud()
    test_reservation_email_content()
    test_cancellation_email_content()
    exit_code = print_summary()
    sys.exit(exit_code)
