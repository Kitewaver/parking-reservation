#!/usr/bin/env python3
"""
駐車場予約システム - 実運用版
シャルマン鶴見市場 No.1

改善履歴:
  v2 - 改善1: キャンセルをメールのワンタイムリンク方式に変更（payment_id入力廃止）
       改善2: 予約フォームに月単位の空き状況カレンダーを追加（予約済み・休業日を事前表示）
"""

from flask import Flask, request, jsonify, render_template_string, send_file
from functools import wraps
import stripe
import json
from datetime import datetime, timedelta
import sqlite3
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import base64
import secrets  # 改善1: ワンタイムトークン生成用
from email.header import Header
import os
from zoneinfo import ZoneInfo

app = Flask(__name__)

JST = ZoneInfo("Asia/Tokyo")

DATABASE_URL = os.environ.get('DATABASE_URL')

if DATABASE_URL and DATABASE_URL.startswith('postgres'):
    import psycopg2
    import psycopg2.extras
    USE_POSTGRES = True
else:
    USE_POSTGRES = False

stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', 'sk_test_YOUR_SECRET_KEY')
BASE_URL = os.environ.get('BASE_URL', 'https://parking-reservation-rzck.onrender.com')
STRIPE_PUBLIC_KEY = os.environ.get('STRIPE_PUBLIC_KEY', 'pk_test_51T0HAtR79rW14GmdmCOvmZaWGgfFXUzEctTgJ4UT555NcH8RnWk5V0MXKcxrFprMhPbTdJEwnVpOGp6ekqO65pTY00Kb69zulE')
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', 'whsec_YOUR_WEBHOOK_SECRET')
EMAIL_SENDER = os.environ.get('EMAIL_SENDER', 'your-email@gmail.com')

EMAILJS_SERVICE_ID = os.environ.get('EMAILJS_SERVICE_ID', 'service_sy9y2dl')
EMAILJS_TEMPLATE_ID = os.environ.get('EMAILJS_TEMPLATE_ID', 'template_oh0iha7')
EMAILJS_CANCEL_TEMPLATE_ID = os.environ.get('EMAILJS_CANCEL_TEMPLATE_ID', 'template_g0jcbbq')
EMAILJS_PUBLIC_KEY = os.environ.get('EMAILJS_PUBLIC_KEY')
EMAILJS_PRIVATE_KEY = os.environ.get('EMAILJS_PRIVATE_KEY')

if EMAILJS_PUBLIC_KEY or EMAILJS_PRIVATE_KEY:
    USE_EMAILJS = True
else:
    USE_EMAILJS = False
    EMAIL_PASSWORD = os.environ.get('EMAIL_PASSWORD', 'your-app-password')
    SMTP_SERVER = "smtp.gmail.com"
    SMTP_PORT = 587

ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'changeme')
DB_PATH = os.environ.get('DB_PATH', '/tmp/parking_system.db' if os.path.exists('/tmp') else 'parking_system.db')

# ─────────────────────────────────────────
# 改善1: キャンセルトークン生成ヘルパー
# ─────────────────────────────────────────

def generate_cancel_token():
    """URL-safe な 32バイト（64文字）ワンタイムトークンを生成"""
    return secrets.token_urlsafe(32)


def get_cancel_token_expiry(reservation_date: str, time_slot: str) -> str:
    """
    トークンの有効期限を返す。
    入庫2時間前（キャンセル締切と同じタイミング）を期限とする。
    """
    dt = datetime.fromisoformat(reservation_date)
    if time_slot == 'afternoon':
        dt = dt.replace(hour=12, minute=0, second=0, tzinfo=JST)
    else:
        dt = dt.replace(hour=0, minute=0, second=0, tzinfo=JST)
    return (dt - timedelta(hours=2)).isoformat()


# ─────────────────────────────────────────
# 認証デコレーター
# ─────────────────────────────────────────

def require_admin_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not (auth.username == ADMIN_USERNAME and auth.password == ADMIN_PASSWORD):
            return ('認証が必要です', 401, {'WWW-Authenticate': 'Basic realm="Admin Area"'})
        return f(*args, **kwargs)
    return decorated


# ─────────────────────────────────────────
# DB接続
# ─────────────────────────────────────────

def get_db_connection():
    if USE_POSTGRES:
        try:
            conn = psycopg2.connect(DATABASE_URL, sslmode='require')
            return conn
        except Exception as e:
            print(f"❌ PostgreSQL接続エラー: {e}")
            raise
    else:
        conn = sqlite3.connect(DB_PATH, timeout=10.0)
        return conn


def cleanup_old_pending_reservations():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cutoff_time = (datetime.now() - timedelta(hours=24)).isoformat()
        if USE_POSTGRES:
            cursor.execute("DELETE FROM reservations WHERE status = %s AND created_at < %s", ('pending', cutoff_time))
        else:
            cursor.execute("DELETE FROM reservations WHERE status = ? AND created_at < ?", ('pending', cutoff_time))
        deleted_count = cursor.rowcount
        conn.commit()
        conn.close()
        if deleted_count > 0:
            print(f"🧹 古いpending予約を{deleted_count}件削除しました")
        return deleted_count
    except Exception as e:
        print(f"⚠️ クリーンアップエラー: {e}")
        return 0


# ─────────────────────────────────────────
# メール送信
# ─────────────────────────────────────────

def send_email_via_gmail_api(to_email, subject, html_content):
    try:
        token_json = os.environ.get('GMAIL_TOKEN_JSON')
        if token_json:
            creds = Credentials.from_authorized_user_info(json.loads(token_json))
        else:
            creds = Credentials.from_authorized_user_file('token.json')
        service = build('gmail', 'v1', credentials=creds)
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = f'{Header("シャルマン鶴見市場駐車場", "utf-8")} <{EMAIL_SENDER}>'
        msg['To'] = to_email
        msg.attach(MIMEText(html_content, 'html'))
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        result = service.users().messages().send(userId='me', body={'raw': raw}).execute()
        print(f"📧 Gmail API送信成功: {result['id']}")
        return True
    except Exception as e:
        print(f"❌ Gmail APIエラー: {e}")
        return False


def send_reservation_email(to_email, customer_name, reservation_data):
    """
    改善1: 予約完了メールにキャンセルワンタイムリンクを含める
    """
    time_label = '0-12時' if reservation_data.get('time_slot') == 'morning' else '12-24時'
    cancel_url = reservation_data.get('cancel_url', f"{BASE_URL}/cancel")

    html = f"""
    <html>
    <body style="font-family: sans-serif; max-width: 600px; margin: 0 auto;">
        <h2 style="color: #667eea;">駐車場予約が完了しました</h2>
        <p>{customer_name} 様</p>
        <p>ご予約ありがとうございます。以下の内容で予約を承りました。</p>
        <div style="background: #f5f5f5; padding: 20px; border-radius: 8px; margin: 20px 0;">
            <h3>予約内容</h3>
            <table style="width: 100%; border-collapse: collapse;">
                <tr><td style="padding:6px;"><strong>ご利用日:</strong></td><td>{reservation_data.get('date', '')}</td></tr>
                <tr><td style="padding:6px;"><strong>時間帯:</strong></td><td>{time_label}</td></tr>
                <tr><td style="padding:6px;"><strong>車両番号:</strong></td><td>{reservation_data.get('car_number', '')}</td></tr>
                <tr><td style="padding:6px;"><strong>料金:</strong></td><td>¥{reservation_data.get('amount', 0):,}</td></tr>
            </table>
        </div>
        <div style="background: #fff3cd; padding: 15px; border-radius: 8px; margin: 20px 0;">
            <h3>⚠️ キャンセルポリシー</h3>
            <ul>
                <li>入庫2時間前まで: キャンセル可能（手数料¥100）</li>
                <li>2時間を切った場合: キャンセル不可・全額収納</li>
            </ul>
        </div>
        <div style="background: #e8f4fd; padding: 20px; border-radius: 8px; margin: 20px 0; text-align: center;">
            <p style="margin-bottom: 12px;">キャンセルはこちらのボタンから手続きできます。</p>
            <a href="{cancel_url}"
               style="display: inline-block; background: #e74c3c; color: white;
                      padding: 14px 32px; border-radius: 6px; text-decoration: none;
                      font-weight: bold; font-size: 16px;">
                予約をキャンセルする
            </a>
            <p style="margin-top: 12px; font-size: 12px; color: #666;">
                ※このリンクは入庫2時間前まで有効です。<br>
                リンクが表示されない場合: {cancel_url}
            </p>
        </div>
        <p>ご不明な点がございましたら、お気軽にお問い合わせください。</p>
        <hr>
        <p style="color: #666; font-size: 12px;">
            シャルマン鶴見市場 No.1<br>
            〒230-0025 神奈川県横浜市鶴見区市場大和町4-9<br>
            電話: 090-6137-9489
        </p>
    </body>
    </html>
    """
    return send_email_via_gmail_api(to_email, '【予約完了】シャルマン鶴見市場 No.1 駐車場', html)


def send_cancellation_email(to_email, customer_name, reservation_data, refund_amount, fee):
    time_label = '0-12時' if reservation_data.get('time_slot') == 'morning' else '12-24時'
    original_amount = refund_amount + fee
    html = f"""
    <html>
    <body style="font-family: sans-serif; max-width: 600px; margin: 0 auto;">
        <h2 style="color: #e74c3c;">予約キャンセルが完了しました</h2>
        <p>{customer_name} 様</p>
        <p>以下の予約をキャンセルしました。</p>
        <div style="background: #f5f5f5; padding: 20px; border-radius: 8px; margin: 20px 0;">
            <h3>キャンセル内容</h3>
            <table style="width: 100%; border-collapse: collapse;">
                <tr><td style="padding:6px;"><strong>ご利用日:</strong></td><td>{reservation_data.get('date', '')}</td></tr>
                <tr><td style="padding:6px;"><strong>時間帯:</strong></td><td>{time_label}</td></tr>
                <tr><td style="padding:6px;"><strong>車両番号:</strong></td><td>{reservation_data.get('car_number', '')}</td></tr>
                <tr><td style="padding:6px;"><strong>お支払い金額:</strong></td><td>¥{original_amount:,}</td></tr>
                <tr><td style="padding:6px;"><strong>キャンセル手数料:</strong></td><td>¥{fee:,}</td></tr>
                <tr><td style="padding:6px;"><strong>返金額:</strong></td><td style="color:#27ae60;font-weight:bold;">¥{refund_amount:,}</td></tr>
            </table>
        </div>
        <div style="background: #d4edda; padding: 15px; border-radius: 8px; margin: 20px 0;">
            <p>返金は数日以内にクレジットカードに反映されます。</p>
        </div>
        <hr>
        <p style="color: #666; font-size: 12px;">
            シャルマン鶴見市場 No.1<br>
            〒230-0025 神奈川県横浜市鶴見区市場大和町4-9<br>
            電話: 090-6137-9489
        </p>
    </body>
    </html>
    """
    return send_email_via_gmail_api(to_email, '【キャンセル完了】シャルマン鶴見市場 No.1 駐車場', html)


# ─────────────────────────────────────────
# Google Calendar
# ─────────────────────────────────────────

def add_to_google_calendar(reservation_data, customer_name):
    try:
        token_json = os.environ.get('GMAIL_TOKEN_JSON')
        if token_json:
            creds = Credentials.from_authorized_user_info(json.loads(token_json))
        else:
            creds = Credentials.from_authorized_user_file('token.json')
        service = build('calendar', 'v3', credentials=creds)
        date = reservation_data.get('date', '')
        time_slot = reservation_data.get('time_slot', '')
        if time_slot == 'morning':
            start_time, end_time, slot_label = f"{date}T00:00:00", f"{date}T12:00:00", '0-12時'
        else:
            start_time, end_time, slot_label = f"{date}T12:00:00", f"{date}T23:59:00", '12-24時'
        event = {
            'summary': f"🚗 駐車場予約: {customer_name}",
            'description': (
                f"車両番号: {reservation_data.get('car_number', '')}\n"
                f"時間帯: {slot_label}\n"
                f"料金: ¥{reservation_data.get('amount', 0):,}\n"
                f"決済ID: {reservation_data.get('payment_id', '')}\n"
                f"電話: {reservation_data.get('phone', '')}\n"
                f"メール: {reservation_data.get('email', '')}"
            ),
            'start': {'dateTime': start_time, 'timeZone': 'Asia/Tokyo'},
            'end': {'dateTime': end_time, 'timeZone': 'Asia/Tokyo'},
        }
        result = service.events().insert(calendarId='primary', body=event).execute()
        print(f"📅 カレンダー登録成功: {result.get('id')}")
        return result.get('id')
    except Exception as e:
        print(f"❌ カレンダー登録エラー: {e}")
        return None


def delete_from_google_calendar(calendar_event_id):
    try:
        token_json = os.environ.get('GMAIL_TOKEN_JSON')
        if token_json:
            creds = Credentials.from_authorized_user_info(json.loads(token_json))
        else:
            creds = Credentials.from_authorized_user_file('token.json')
        service = build('calendar', 'v3', credentials=creds)
        service.events().delete(calendarId='primary', eventId=calendar_event_id).execute()
        print(f"📅 カレンダー削除成功: {calendar_event_id}")
        return True
    except Exception as e:
        print(f"❌ カレンダー削除エラー: {e}")
        return False


# ─────────────────────────────────────────
# DB初期化
# ─────────────────────────────────────────

def init_database():
    conn = get_db_connection()
    cursor = conn.cursor()

    if USE_POSTGRES:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS reservations (
                id SERIAL PRIMARY KEY,
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
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS webhook_events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                payment_id TEXT,
                processed_at TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS closed_dates (
                id SERIAL PRIMARY KEY,
                date TEXT NOT NULL,
                time_slot TEXT NOT NULL CHECK (time_slot IN ('morning', 'afternoon')),
                reason TEXT,
                created_at TEXT,
                calendar_event_id TEXT,
                UNIQUE(date, time_slot)
            )
        ''')
        # 既存テーブルへのカラム追加（既存DB対応）
        for col, typedef in [
            ('calendar_event_id', 'TEXT'),
            ('cancel_token', 'TEXT'),
            ('cancel_token_expires_at', 'TEXT'),
        ]:
            try:
                cursor.execute(f'ALTER TABLE reservations ADD COLUMN IF NOT EXISTS {col} {typedef}')
            except Exception:
                pass
        try:
            cursor.execute('ALTER TABLE closed_dates ADD COLUMN IF NOT EXISTS calendar_event_id TEXT')
        except Exception:
            pass
    else:
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
                cancelled_at TEXT,
                calendar_event_id TEXT,
                cancel_token TEXT UNIQUE,
                cancel_token_expires_at TEXT,
                UNIQUE(date, time_slot, status) ON CONFLICT IGNORE
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS webhook_events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                payment_id TEXT,
                processed_at TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS closed_dates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                time_slot TEXT NOT NULL CHECK (time_slot IN ('morning', 'afternoon')),
                reason TEXT,
                created_at TEXT,
                calendar_event_id TEXT,
                UNIQUE(date, time_slot)
            )
        ''')

    conn.commit()
    conn.close()
    print("✅ データベース初期化完了")


init_database()


# ─────────────────────────────────────────
# ビジネスロジック
# ─────────────────────────────────────────

def calculate_price(time_slot):
    if time_slot == "morning":
        return 500
    elif time_slot == "afternoon":
        return 1100
    return 0


def is_cancellable(reservation_date, time_slot):
    now = datetime.now(JST)
    res_dt = datetime.fromisoformat(reservation_date)
    if time_slot == "afternoon":
        res_dt = res_dt.replace(hour=12, minute=0, second=0)
    else:
        res_dt = res_dt.replace(hour=0, minute=0, second=0)
    res_dt = res_dt.replace(tzinfo=JST)
    return now < res_dt - timedelta(hours=2)


# ─────────────────────────────────────────
# API: 空き状況（改善2: 月単位バルク取得）
# ─────────────────────────────────────────

@app.route('/api/check-availability', methods=['POST'])
def check_availability():
    """単一日時の空き確認（既存互換）"""
    try:
        data = request.json
        date = data.get('date')
        time_slot = data.get('time_slot')
        now = datetime.now(JST)
        selected_date = datetime.fromisoformat(date)

        if selected_date.date() < now.date():
            return jsonify({'available': False, 'reason': 'past_date'})
        if selected_date.date() == now.date():
            if time_slot == "morning" and now.hour >= 12:
                return jsonify({'available': False, 'reason': 'time_passed'})

        conn = get_db_connection()
        cursor = conn.cursor()

        if USE_POSTGRES:
            cursor.execute(
                'SELECT * FROM closed_dates WHERE date = %s AND time_slot = %s',
                (date, time_slot)
            )
        else:
            cursor.execute(
                'SELECT * FROM closed_dates WHERE date = ? AND time_slot = ?',
                (date, time_slot)
            )
        if cursor.fetchone():
            conn.close()
            return jsonify({'available': False, 'reason': 'closed'})

        if USE_POSTGRES:
            cursor.execute(
                "SELECT * FROM reservations WHERE date = %s AND time_slot = %s AND status = 'confirmed'",
                (date, time_slot)
            )
        else:
            cursor.execute(
                "SELECT * FROM reservations WHERE date = ? AND time_slot = ? AND status = 'confirmed'",
                (date, time_slot)
            )
        if cursor.fetchone():
            conn.close()
            return jsonify({'available': False, 'reason': 'reserved'})

        conn.close()
        return jsonify({'available': True, 'price': calculate_price(time_slot)})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/month-availability', methods=['GET'])
def month_availability():
    """
    改善2: 月単位の空き状況を一括返却
    クエリパラメータ: year=2026&month=5
    レスポンス例:
    {
      "2026-05-01": {"morning": "available", "afternoon": "reserved"},
      "2026-05-03": {"morning": "closed",    "afternoon": "closed"},
      ...
    }
    status: "available" | "reserved" | "closed" | "past"
    """
    try:
        year = int(request.args.get('year', datetime.now(JST).year))
        month = int(request.args.get('month', datetime.now(JST).month))

        # 月の初日と末日
        from calendar import monthrange
        _, last_day = monthrange(year, month)
        month_start = datetime(year, month, 1).date()
        month_end = datetime(year, month, last_day).date()
        today = datetime.now(JST).date()
        now_hour = datetime.now(JST).hour

        conn = get_db_connection()
        cursor = conn.cursor()

        # 月内の確定予約を取得
        start_str = month_start.isoformat()
        end_str = month_end.isoformat()
        if USE_POSTGRES:
            cursor.execute(
                "SELECT date, time_slot FROM reservations WHERE date >= %s AND date <= %s AND status = 'confirmed'",
                (start_str, end_str)
            )
        else:
            cursor.execute(
                "SELECT date, time_slot FROM reservations WHERE date >= ? AND date <= ? AND status = 'confirmed'",
                (start_str, end_str)
            )
        reserved = {(row[0], row[1]) for row in cursor.fetchall()}

        # 月内の休業日を取得（date, time_slot のペアで管理）
        if USE_POSTGRES:
            cursor.execute(
                "SELECT date, time_slot FROM closed_dates WHERE date >= %s AND date <= %s",
                (start_str, end_str)
            )
        else:
            cursor.execute(
                "SELECT date, time_slot FROM closed_dates WHERE date >= ? AND date <= ?",
                (start_str, end_str)
            )
        closed = {(row[0], row[1]) for row in cursor.fetchall()}
        conn.close()

        result = {}
        current = month_start
        while current <= month_end:
            date_str = current.isoformat()
            day_status = {}

            if current < today:
                day_status = {"morning": "past", "afternoon": "past"}
            else:
                for slot in ["morning", "afternoon"]:
                    if (date_str, slot) in closed:
                        day_status[slot] = "closed"
                    elif (date_str, slot) in reserved:
                        day_status[slot] = "reserved"
                    elif current == today:
                        if slot == "morning" and now_hour >= 12:
                            day_status[slot] = "past"
                        else:
                            day_status[slot] = "available"
                    else:
                        day_status[slot] = "available"

            result[date_str] = day_status
            current += timedelta(days=1)

        return jsonify(result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 400


# ─────────────────────────────────────────
# API: 決済
# ─────────────────────────────────────────

@app.route('/api/create-payment-intent', methods=['POST'])
def create_payment_intent():
    try:
        data = request.json
        time_slot = data.get('time_slot')
        date = data.get('date')

        conn = get_db_connection()
        cursor = conn.cursor()

        if USE_POSTGRES:
            cursor.execute(
                "SELECT * FROM reservations WHERE date = %s AND time_slot = %s AND status IN ('confirmed','pending')",
                (date, time_slot)
            )
        else:
            cursor.execute(
                "SELECT * FROM reservations WHERE date = ? AND time_slot = ? AND status IN ('confirmed','pending')",
                (date, time_slot)
            )
        if cursor.fetchone():
            conn.close()
            return jsonify({'error': 'この時間帯は既に予約されています'}), 400

        temp_id = f"temp_{int(datetime.now().timestamp() * 1000)}"
        try:
            if USE_POSTGRES:
                cursor.execute('''
                    INSERT INTO reservations
                    (payment_id, car_number, customer_name, phone, email, date, time_slot, amount, status, created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s)
                ''', (temp_id, data.get('car_number'), data.get('customer_name'),
                      data.get('phone'), data.get('email'), date, time_slot,
                      calculate_price(time_slot), datetime.now().isoformat()))
            else:
                cursor.execute('''
                    INSERT INTO reservations
                    (payment_id, car_number, customer_name, phone, email, date, time_slot, amount, status, created_at)
                    VALUES (?,?,?,?,?,?,?,?,'pending',?)
                ''', (temp_id, data.get('car_number'), data.get('customer_name'),
                      data.get('phone'), data.get('email'), date, time_slot,
                      calculate_price(time_slot), datetime.now().isoformat()))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            return jsonify({'error': 'この時間帯は既に予約されています'}), 400

        conn.close()

        amount = calculate_price(time_slot)
        payment_intent = stripe.PaymentIntent.create(
            amount=amount,
            currency='jpy',
            automatic_payment_methods={'enabled': True},
            metadata={
                'car_number': data.get('car_number'),
                'customer_name': data.get('customer_name'),
                'phone': data.get('phone'),
                'email': data.get('email'),
                'date': date,
                'time_slot': time_slot,
                'temp_reservation_id': temp_id,
            }
        )
        return jsonify({'clientSecret': payment_intent.client_secret, 'amount': amount})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


# ─────────────────────────────────────────
# API: Stripe Webhook
# ─────────────────────────────────────────

@app.route('/api/webhook', methods=['POST'])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')

    if WEBHOOK_SECRET and WEBHOOK_SECRET != "whsec_YOUR_WEBHOOK_SECRET":
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
            event_type = event['type']
            event_data = event['data']['object']
        except (ValueError, stripe.error.SignatureVerificationError) as e:
            return jsonify({'error': str(e)}), 400
    else:
        event = request.json
        event_type = event.get('type')
        event_data = event.get('data', {}).get('object', {})

    try:
        print(f"\n📨 Webhook: {event_type}")
        cleanup_old_pending_reservations()

        event_id = event.get('id') if isinstance(event, dict) else event.get('id')

        if event_id:
            conn = get_db_connection()
            cursor = conn.cursor()
            if USE_POSTGRES:
                cursor.execute('SELECT event_id FROM webhook_events WHERE event_id = %s', (event_id,))
            else:
                cursor.execute('SELECT event_id FROM webhook_events WHERE event_id = ?', (event_id,))
            if cursor.fetchone():
                conn.close()
                return jsonify({'status': 'success'}), 200
            try:
                if USE_POSTGRES:
                    cursor.execute(
                        'INSERT INTO webhook_events (event_id,event_type,payment_id,processed_at) VALUES(%s,%s,%s,%s)',
                        (event_id, event_type, 'processing', datetime.now().isoformat())
                    )
                else:
                    cursor.execute(
                        'INSERT INTO webhook_events (event_id,event_type,payment_id,processed_at) VALUES(?,?,?,?)',
                        (event_id, event_type, 'processing', datetime.now().isoformat())
                    )
                conn.commit()
            except Exception:
                conn.close()
                return jsonify({'status': 'success'}), 200
            conn.close()

        if event_type == 'payment_intent.succeeded':
            payment_id = event_data['id']
            amount = event_data['amount']
            raw_meta = event_data.get('metadata', {})
            if isinstance(raw_meta, dict):
                metadata = raw_meta
            elif hasattr(raw_meta, 'to_dict_recursive'):
                metadata = raw_meta.to_dict_recursive()
            else:
                metadata = dict(raw_meta)

            if metadata and metadata.get('car_number'):
                conn = get_db_connection()
                cursor = conn.cursor()

                # 改善1: cancel_token を生成して保存
                cancel_token = generate_cancel_token()
                expires_at = get_cancel_token_expiry(
                    metadata.get('date', ''), metadata.get('time_slot', '')
                )

                temp_id = metadata.get('temp_reservation_id')
                if temp_id:
                    if USE_POSTGRES:
                        cursor.execute('''
                            UPDATE reservations
                            SET payment_id=%s, status='confirmed',
                                cancel_token=%s, cancel_token_expires_at=%s
                            WHERE payment_id=%s AND status='pending'
                        ''', (payment_id, cancel_token, expires_at, temp_id))
                    else:
                        cursor.execute('''
                            UPDATE reservations
                            SET payment_id=?, status='confirmed',
                                cancel_token=?, cancel_token_expires_at=?
                            WHERE payment_id=? AND status='pending'
                        ''', (payment_id, cancel_token, expires_at, temp_id))
                    if cursor.rowcount == 0:
                        # フォールバック: 新規作成
                        _insert_confirmed(cursor, payment_id, amount, metadata, cancel_token, expires_at)
                else:
                    _insert_confirmed(cursor, payment_id, amount, metadata, cancel_token, expires_at)

                conn.commit()

                # キャンセルURLを生成してメール送信
                cancel_url = f"{BASE_URL}/cancel?token={cancel_token}"
                if metadata.get('email'):
                    try:
                        send_reservation_email(
                            to_email=metadata['email'],
                            customer_name=metadata.get('customer_name', 'お客様'),
                            reservation_data={
                                'date': metadata.get('date'),
                                'time_slot': metadata.get('time_slot'),
                                'car_number': metadata.get('car_number'),
                                'amount': amount,
                                'payment_id': payment_id,
                                'cancel_url': cancel_url,   # 改善1: トークンリンクを渡す
                            }
                        )
                    except Exception as e:
                        print(f"⚠️ メール送信エラー: {e}")

                # Google Calendar 登録
                try:
                    cal_id = add_to_google_calendar(
                        reservation_data={
                            'date': metadata.get('date'),
                            'time_slot': metadata.get('time_slot'),
                            'car_number': metadata.get('car_number'),
                            'amount': amount,
                            'payment_id': payment_id,
                            'phone': metadata.get('phone', ''),
                            'email': metadata.get('email', ''),
                        },
                        customer_name=metadata.get('customer_name', 'お客様')
                    )
                    if cal_id:
                        if USE_POSTGRES:
                            cursor.execute('UPDATE reservations SET calendar_event_id=%s WHERE payment_id=%s', (cal_id, payment_id))
                        else:
                            cursor.execute('UPDATE reservations SET calendar_event_id=? WHERE payment_id=?', (cal_id, payment_id))
                        conn.commit()
                except Exception as e:
                    print(f"⚠️ カレンダー登録エラー: {e}")

                conn.close()

        elif event_type == 'charge.refunded':
            try:
                payment_id = event_data['payment_intent']
                conn = get_db_connection()
                cursor = conn.cursor()
                if USE_POSTGRES:
                    cursor.execute("UPDATE reservations SET status='refunded' WHERE payment_id=%s", (payment_id,))
                else:
                    cursor.execute("UPDATE reservations SET status='refunded' WHERE payment_id=?", (payment_id,))
                conn.commit()
                conn.close()
            except Exception as e:
                print(f"❌ charge.refunded処理エラー: {e}")

        elif event_type == 'charge.succeeded':
            pass  # metadataなしのため処理不要

        return jsonify({'status': 'success'}), 200

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 400


def _insert_confirmed(cursor, payment_id, amount, metadata, cancel_token, expires_at):
    """確定予約を新規挿入するヘルパー"""
    if USE_POSTGRES:
        cursor.execute('''
            INSERT INTO reservations
            (payment_id, car_number, customer_name, phone, email, date, time_slot,
             amount, status, created_at, cancel_token, cancel_token_expires_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'confirmed',%s,%s,%s)
        ''', (
            payment_id,
            metadata.get('car_number', ''),
            metadata.get('customer_name', ''),
            metadata.get('phone', ''),
            metadata.get('email', ''),
            metadata.get('date', ''),
            metadata.get('time_slot', ''),
            amount,
            datetime.now().isoformat(),
            cancel_token,
            expires_at,
        ))
    else:
        cursor.execute('''
            INSERT INTO reservations
            (payment_id, car_number, customer_name, phone, email, date, time_slot,
             amount, status, created_at, cancel_token, cancel_token_expires_at)
            VALUES (?,?,?,?,?,?,?,?,'confirmed',?,?,?)
        ''', (
            payment_id,
            metadata.get('car_number', ''),
            metadata.get('customer_name', ''),
            metadata.get('phone', ''),
            metadata.get('email', ''),
            metadata.get('date', ''),
            metadata.get('time_slot', ''),
            amount,
            datetime.now().isoformat(),
            cancel_token,
            expires_at,
        ))


# ─────────────────────────────────────────
# API: 予約一覧・休業日（管理画面用）
# ─────────────────────────────────────────

@app.route('/api/reservations', methods=['GET'])
@require_admin_auth
def get_reservations():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM reservations WHERE status='confirmed' ORDER BY date, time_slot")
    rows = cursor.fetchall()
    reservations = []
    for row in rows:
        reservations.append({
            'id': row[0], 'payment_id': row[1], 'car_number': row[2],
            'customer_name': row[3], 'phone': row[4], 'email': row[5],
            'date': row[6], 'time_slot': row[7], 'amount': row[8],
            'status': row[9], 'created_at': row[10]
        })
    conn.close()
    return jsonify({'total': len(reservations), 'reservations': reservations})


@app.route('/api/closed-dates', methods=['GET'])
@require_admin_auth
def get_closed_dates():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM closed_dates ORDER BY date')
    rows = cursor.fetchall()
    closed_dates = [{'id': r[0], 'date': r[1], 'time_slot': r[2], 'reason': r[3]} for r in rows]
    conn.close()
    return jsonify(closed_dates)


@app.route('/api/closed-dates', methods=['POST'])
@require_admin_auth
def add_closed_date():
    try:
        data = request.json
        date = data.get('date')
        time_slot = data.get('time_slot')  # 'morning' or 'afternoon'
        reason = data.get('reason', '休業日')

        if time_slot not in ('morning', 'afternoon'):
            return jsonify({'error': 'time_slot は morning または afternoon を指定してください'}), 400

        target_date = datetime.fromisoformat(date).date()
        max_date = (datetime.now() + timedelta(days=60)).date()
        if target_date > max_date:
            return jsonify({'error': '2ヶ月先までしか設定できません'}), 400

        conn = get_db_connection()
        cursor = conn.cursor()

        # 同一スロットに予約が入っていないかチェック
        if USE_POSTGRES:
            cursor.execute(
                "SELECT COUNT(*) FROM reservations WHERE date=%s AND time_slot=%s AND status='confirmed'",
                (date, time_slot)
            )
        else:
            cursor.execute(
                "SELECT COUNT(*) FROM reservations WHERE date=? AND time_slot=? AND status='confirmed'",
                (date, time_slot)
            )
        if cursor.fetchone()[0] > 0:
            slot_label = '午前' if time_slot == 'morning' else '午後'
            conn.close()
            return jsonify({'error': f'この日の{slot_label}枠には既に予約が入っています'}), 400

        if USE_POSTGRES:
            cursor.execute(
                'INSERT INTO closed_dates (date,time_slot,reason,created_at) VALUES(%s,%s,%s,%s)',
                (date, time_slot, reason, datetime.now().isoformat())
            )
        else:
            cursor.execute(
                'INSERT INTO closed_dates (date,time_slot,reason,created_at) VALUES(?,?,?,?)',
                (date, time_slot, reason, datetime.now().isoformat())
            )
        conn.commit()

        # Google Calendar 登録
        slot_label = '午前（0-12時）' if time_slot == 'morning' else '午後（12-24時）'
        try:
            token_json = os.environ.get('GMAIL_TOKEN_JSON')
            if token_json:
                creds = Credentials.from_authorized_user_info(json.loads(token_json))
            else:
                creds = Credentials.from_authorized_user_file('token.json')
            cal = build('calendar', 'v3', credentials=creds)
            event = {
                'summary': f'🚫 駐車場休業: {slot_label} {reason}',
                'description': reason,
                'start': {'date': date},
                'end': {'date': date}
            }
            cal_result = cal.events().insert(calendarId='primary', body=event).execute()
            cal_id = cal_result.get('id')
            if USE_POSTGRES:
                cursor.execute(
                    'UPDATE closed_dates SET calendar_event_id=%s WHERE date=%s AND time_slot=%s',
                    (cal_id, date, time_slot)
                )
            else:
                cursor.execute(
                    'UPDATE closed_dates SET calendar_event_id=? WHERE date=? AND time_slot=?',
                    (cal_id, date, time_slot)
                )
            conn.commit()
        except Exception as e:
            print(f"⚠️ 休業日カレンダー登録エラー: {e}")

        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/closed-dates/<int:id>', methods=['DELETE'])
@require_admin_auth
def delete_closed_date(id):
    conn = get_db_connection()
    cursor = conn.cursor()
    if USE_POSTGRES:
        cursor.execute('SELECT calendar_event_id FROM closed_dates WHERE id=%s', (id,))
    else:
        cursor.execute('SELECT calendar_event_id FROM closed_dates WHERE id=?', (id,))
    row = cursor.fetchone()
    cal_id = row[0] if row else None
    if USE_POSTGRES:
        cursor.execute('DELETE FROM closed_dates WHERE id=%s', (id,))
    else:
        cursor.execute('DELETE FROM closed_dates WHERE id=?', (id,))
    conn.commit()
    conn.close()
    if cal_id:
        try:
            token_json = os.environ.get('GMAIL_TOKEN_JSON')
            creds = Credentials.from_authorized_user_info(json.loads(token_json)) if token_json \
                else Credentials.from_authorized_user_file('token.json')
            cal = build('calendar', 'v3', credentials=creds)
            cal.events().delete(calendarId='primary', eventId=cal_id).execute()
        except Exception as e:
            print(f"⚠️ 休業日カレンダー削除エラー: {e}")
    return jsonify({'success': True})


# ─────────────────────────────────────────
# 改善1: キャンセルAPI（トークン方式）
# ─────────────────────────────────────────

@app.route('/api/cancel-reservation', methods=['POST'])
def cancel_reservation():
    """
    改善1: cancel_token でキャンセル処理。payment_id 入力不要。
    リクエスト: {"token": "xxxx"}
    """
    try:
        data = request.json
        token = data.get('token')

        if not token:
            return jsonify({'error': 'トークンが指定されていません'}), 400

        conn = get_db_connection()
        cursor = conn.cursor()

        # トークンで予約を検索
        if USE_POSTGRES:
            cursor.execute(
                "SELECT * FROM reservations WHERE cancel_token=%s AND status='confirmed'",
                (token,)
            )
        else:
            cursor.execute(
                "SELECT * FROM reservations WHERE cancel_token=? AND status='confirmed'",
                (token,)
            )
        row = cursor.fetchone()

        if not row:
            conn.close()
            return jsonify({'error': '無効なリンクか、既にキャンセル済みの予約です'}), 404

        # カラムインデックスマッピング (id,payment_id,car_number,customer_name,phone,email,date,time_slot,amount,status,...)
        payment_id = row[1]
        customer_name = row[3]
        customer_email = row[5]
        reservation_date = row[6]
        time_slot = row[7]
        amount = row[8]
        cancel_token_expires_at = row[14] if len(row) > 14 else None

        # トークン有効期限チェック（DB値を使用）
        now = datetime.now(JST)
        if cancel_token_expires_at:
            expires_dt = datetime.fromisoformat(cancel_token_expires_at)
            if expires_dt.tzinfo is None:
                expires_dt = expires_dt.replace(tzinfo=JST)
            if now >= expires_dt:
                conn.close()
                return jsonify({'error': 'キャンセル期限（入庫2時間前）を過ぎているためキャンセルできません'}), 400
        else:
            # expires_at が未設定の場合は is_cancellable で確認
            if not is_cancellable(reservation_date, time_slot):
                conn.close()
                return jsonify({'error': '入庫2時間前を過ぎているためキャンセルできません'}), 400

        cancellation_fee = 100
        refund_amount = max(0, amount - cancellation_fee)

        # Stripe 払い戻し
        try:
            refund = stripe.Refund.create(payment_intent=payment_id, amount=refund_amount)
        except stripe.error.InvalidRequestError as e:
            conn.close()
            return jsonify({'error': f'Stripe払い戻しエラー: {str(e)}'}), 400

        # ステータス更新 + トークン無効化
        if USE_POSTGRES:
            cursor.execute(
                "UPDATE reservations SET status='cancelled', cancelled_at=%s, cancel_token=NULL WHERE payment_id=%s",
                (datetime.now().isoformat(), payment_id)
            )
        else:
            cursor.execute(
                "UPDATE reservations SET status='cancelled', cancelled_at=?, cancel_token=NULL WHERE payment_id=?",
                (datetime.now().isoformat(), payment_id)
            )
        conn.commit()

        # キャンセルメール送信
        if customer_email:
            send_cancellation_email(
                to_email=customer_email,
                customer_name=customer_name or 'お客様',
                reservation_data={'date': reservation_date, 'time_slot': time_slot, 'car_number': row[2]},
                refund_amount=refund_amount,
                fee=cancellation_fee
            )

        # Google Calendar 削除
        try:
            cal_id = row[12] if len(row) > 12 else None
            if cal_id:
                delete_from_google_calendar(cal_id)
        except Exception as e:
            print(f"⚠️ カレンダー削除エラー: {e}")

        conn.close()
        return jsonify({
            'success': True,
            'refund_id': refund.id,
            'refund_amount': refund_amount,
            'cancellation_fee': cancellation_fee,
            'message': f'キャンセル完了。¥{refund_amount}を払い戻します（手数料¥{cancellation_fee}）'
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 400


# ─────────────────────────────────────────
# ページ: ランディングページ
# ─────────────────────────────────────────

@app.route('/')
def landing():
    return render_template_string('''
<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>シャルマン鶴見市場 No.1 駐車場 | 鶴見市場駅徒歩3分・当日予約OK</title>
    <meta name="description" content="横浜市鶴見区の時間貸し駐車場。京急鶴見市場駅徒歩3分。午前¥500・午後¥1,100。当日予約OK・クレジットカード払い・スマホから簡単予約。">
    <meta name="keywords" content="鶴見市場 駐車場, 横浜 鶴見 駐車場, シャルマン鶴見市場, 京急 鶴見市場駅 駐車場, 鶴見区 時間貸し駐車場, 市場大和町 駐車場">
    <meta name="robots" content="index, follow">
    <meta property="og:title" content="シャルマン鶴見市場 No.1 駐車場">
    <meta property="og:description" content="京急鶴見市場駅徒歩3分。午前¥500・午後¥1,100。当日予約OK・スマホで簡単予約。">
    <meta property="og:type" content="website">
    <meta property="og:url" content="https://parking-reservation-rzck.onrender.com/">
    <link rel="canonical" href="https://parking-reservation-rzck.onrender.com/">
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "ParkingFacility",
      "name": "シャルマン鶴見市場 No.1 駐車場",
      "description": "京急鶴見市場駅徒歩3分の時間貸し駐車場。午前¥500・午後¥1,100。当日予約OK。",
      "url": "https://parking-reservation-rzck.onrender.com/",
      "telephone": "090-6137-9489",
      "address": {
        "@type": "PostalAddress",
        "streetAddress": "市場大和町4-9",
        "addressLocality": "鶴見区",
        "addressRegion": "神奈川県",
        "postalCode": "230-0025",
        "addressCountry": "JP"
      },
      "geo": {
        "@type": "GeoCoordinates",
        "latitude": 35.5172,
        "longitude": 139.6844
      },
      "openingHours": "Mo-Su 00:00-24:00",
      "priceRange": "¥500〜¥1,100",
      "amenityFeature": [
        {"@type": "LocationFeatureSpecification", "name": "オンライン予約", "value": true},
        {"@type": "LocationFeatureSpecification", "name": "クレジットカード払い", "value": true},
        {"@type": "LocationFeatureSpecification", "name": "当日予約", "value": true}
      ]
    }
    </script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; line-height: 1.6; color: #333; }
        header { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 20px 0; }
        .header-content { max-width: 1200px; margin: 0 auto; padding: 0 20px; display: flex; justify-content: space-between; align-items: center; }
        h1 { font-size: 24px; }
        .reserve-btn { background: white; color: #667eea; padding: 12px 30px; border-radius: 25px; text-decoration: none; font-weight: bold; }
        .hero { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; text-align: center; padding: 100px 20px; }
        .hero h2 { font-size: 42px; margin-bottom: 20px; }
        .hero p { font-size: 20px; margin-bottom: 30px; }
        .cta { display: inline-block; background: #ffc107; color: #333; padding: 18px 50px; border-radius: 30px; text-decoration: none; font-size: 20px; font-weight: bold; }
        section { max-width: 1200px; margin: 0 auto; padding: 60px 20px; }
        h3 { font-size: 32px; margin-bottom: 40px; text-align: center; color: #667eea; }
        .features { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 30px; }
        .feature { text-align: center; padding: 30px; background: white; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        .feature-icon { font-size: 48px; margin-bottom: 15px; }
        .pricing { background: #f8f9fa; padding: 40px; border-radius: 15px; max-width: 600px; margin: 0 auto; }
        .price-item { display: flex; justify-content: space-between; padding: 20px; border-bottom: 1px solid #ddd; }
        .price-item:last-child { border-bottom: none; }
        .price { font-size: 28px; font-weight: bold; color: #667eea; }
        .access-info { display: grid; grid-template-columns: 1fr 1fr; gap: 40px; align-items: start; }
        .map-container { width: 100%; height: 400px; border-radius: 10px; overflow: hidden; }
        .specs { background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        .spec-table { width: 100%; border-collapse: collapse; }
        .spec-table td { padding: 12px; border-bottom: 1px solid #eee; }
        .spec-table td:first-child { font-weight: bold; width: 150px; }
        footer { background: #2c3e50; color: white; padding: 40px 20px 20px; text-align: center; }
        .footer-links a { color: white; text-decoration: none; margin: 0 15px; }
        @media (max-width: 768px) { .hero h2 { font-size: 28px; } .access-info { grid-template-columns: 1fr; } .header-content { flex-direction: column; gap: 15px; } }
    </style>
</head>
<body>
    <header>
        <div class="header-content">
            <h1>🅿️ シャルマン鶴見市場 No.1</h1>
            <a href="/reserve" class="reserve-btn">今すぐ予約</a>
        </div>
    </header>
    <div class="hero">
        <h2>鶴見市場駅徒歩3分<br>屋外駐車場</h2>
        <p>当日予約OK | 24時間利用可能 | クレジットカード決済</p>
        <a href="/reserve" class="cta">予約する</a>
    </div>
    <section>
        <h3>選ばれる理由</h3>
        <div class="features">
            <div class="feature"><div class="feature-icon">🅿️</div><h4>平置き駐車場</h4><p>停めやすい平置きタイプ。</p></div>
            <div class="feature"><div class="feature-icon">🚃</div><h4>駅近</h4><p>京急鶴見市場駅から徒歩3分。</p></div>
            <div class="feature"><div class="feature-icon">💳</div><h4>簡単決済</h4><p>クレジットカードでオンライン予約。</p></div>
            <div class="feature"><div class="feature-icon">📱</div><h4>当日予約OK</h4><p>スマホから簡単予約。</p></div>
        </div>
    </section>
    <section style="background: #f8f9fa;">
        <h3>料金</h3>
        <div class="pricing">
            <div class="price-item"><div><strong>午前（0時〜12時）</strong><br><small>12時間</small></div><div class="price">¥500</div></div>
            <div class="price-item"><div><strong>午後（12時〜24時）</strong><br><small>12時間</small></div><div class="price">¥1,100</div></div>
        </div>
        <p style="text-align:center;margin-top:20px;color:#666;">※キャンセルは入庫2時間前まで可能（手数料¥100）</p>
    </section>
    <section>
        <h3>駐車場の様子</h3>
        <img src="/static/parking_photo.png" alt="駐車場" style="width:100%;max-width:800px;display:block;margin:40px auto;border-radius:15px;" onerror="this.style.display='none'">
    </section>
    <section>
        <h3>駐車場スペック</h3>
        <div class="specs">
            <table class="spec-table">
                <tr><td>長さ</td><td>500cm</td></tr>
                <tr><td>幅</td><td>190cm</td></tr>
                <tr><td>高さ</td><td>220cm</td></tr>
                <tr><td>重量制限</td><td>2,000kg</td></tr>
                <tr><td>車室タイプ</td><td>屋外平置き</td></tr>
            </table>
        </div>
    </section>
    <section style="background: #f8f9fa;">
        <h3>アクセス</h3>
        <div class="access-info">
            <div>
                <h4 style="margin-bottom:15px;">所在地</h4>
                <p style="font-size:18px;margin-bottom:20px;">〒230-0025<br>神奈川県横浜市鶴見区<br>市場大和町4-9</p>
                <h4 style="margin-bottom:15px;">交通</h4>
                <p>🚃 <strong>京急鶴見市場駅</strong> 徒歩3分</p>
            </div>
            <div class="map-container">
                <iframe src="https://www.google.com/maps/embed?pb=!1m18!1m12!1m3!1d3247.4781255889693!2d139.6843947735998!3d35.51718053897196!2m3!1f0!2f0!3f0!3m2!1i1024!2i768!4f13.1!3m3!1m2!1s0x60185fcae10377b3%3A0x28c708976222e79d!2z44K344Yd44Or44Oe44Oz6ba06KaL5biC5aC0!5e0!3m2!1sja!2sjp!4v1775621450603!5m2!1sja!2sjp" width="100%" height="100%" style="border:0;" allowfullscreen loading="lazy"></iframe>
            </div>
        </div>
    </section>
    <section style="text-align:center;padding:80px 20px;">
        <h3>今すぐ予約</h3>
        <p style="font-size:18px;margin-bottom:30px;">オンラインで簡単予約。当日利用もOK！</p>
        <a href="/reserve" class="cta">予約ページへ</a>
    </section>
    <footer>
        <div class="footer-links">
            <a href="/terms">利用規約</a><a href="/privacy">プライバシーポリシー</a><a href="/legal">特定商取引法</a><a href="/refund">返金ポリシー</a>
        </div>
        <p style="margin-top:20px;">運営: 有限会社滝沢商店<br>〒230-0025 神奈川県横浜市鶴見区市場大和町4-9<br>Email: noboru.takizawa@blueflag-sys.com</p>
        <p style="margin-top:20px;color:#95a5a6;">&copy; 2026 有限会社滝沢商店 All rights reserved.</p>
    </footer>
</body>
</html>
    ''')


# ─────────────────────────────────────────
# ページ: 予約フォーム（改善2: 月カレンダー）
# ─────────────────────────────────────────

@app.route('/reserve')
def index():
    return render_template_string('''
<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>駐車場予約 - シャルマン鶴見市場 No.1</title>
    <script src="https://js.stripe.com/v3/"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f5f5f5; padding: 20px; }
        .container { max-width: 600px; margin: 0 auto; }
        .card { background: white; border-radius: 12px; padding: 30px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
        h1 { color: #333; margin-bottom: 10px; }
        .info { color: #666; font-size: 14px; margin-bottom: 8px; }
        .policy { background: #fff3cd; border-left: 4px solid #ffc107; padding: 15px; margin: 20px 0; border-radius: 4px; }
        .policy h3 { color: #856404; margin-bottom: 10px; font-size: 16px; }
        .policy ul { margin-left: 20px; color: #856404; }
        .form-group { margin-bottom: 20px; }
        label { display: block; margin-bottom: 8px; color: #555; font-weight: 500; }
        input, select { width: 100%; padding: 12px; border: 1px solid #ddd; border-radius: 6px; font-size: 14px; }
        button[type="submit"] {
            width: 100%; padding: 16px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white; border: none; border-radius: 8px;
            font-size: 16px; font-weight: bold; cursor: pointer;
        }
        button[type="submit"]:disabled { opacity: 0.5; cursor: not-allowed; }
        #card-element { padding: 12px; border: 1px solid #ddd; border-radius: 6px; }
        .error { color: #e74c3c; margin-top: 10px; font-size: 14px; }
        .success { color: #27ae60; margin-top: 10px; font-size: 14px; }

        /* ── 改善2: 月カレンダー ── */
        .calendar-section { margin-bottom: 20px; }
        .cal-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
        .cal-nav { background: none; border: 1px solid #ddd; border-radius: 6px; padding: 6px 14px; cursor: pointer; font-size: 16px; width: auto; }
        .cal-nav:hover { background: #f0f0f0; }
        .cal-month-label { font-weight: bold; font-size: 16px; color: #333; }
        .cal-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 2px; width: 100%; box-sizing: border-box; }
        .cal-dayname { text-align: center; font-size: 10px; font-weight: 600; color: #888; padding: 3px 0; }
        .cal-dayname:first-child { color: #e74c3c; }
        .cal-dayname:last-child  { color: #667eea; }
        .cal-cell {
            border-radius: 4px; padding: 3px 1px; text-align: center;
            font-size: 10px; cursor: default; min-height: 48px;
            display: flex; flex-direction: column; align-items: center; justify-content: flex-start;
            gap: 1px; border: 1.5px solid transparent;
            box-sizing: border-box; overflow: hidden; min-width: 0;
        }
        .cal-cell .cal-date { font-size: 12px; font-weight: 500; padding: 1px 0; }
        .cal-cell.empty { background: transparent; }
        .cal-cell.past  { background: #fafafa; opacity: 0.45; }
        .cal-cell.closed {
            background: #fee; border-color: #fcc;
        }
        .cal-cell.full  { background: #fff0f0; }
        .cal-cell.partial { background: #fffbe6; }
        .cal-cell.available { background: #f0fff4; cursor: pointer; }
        .cal-cell.available:hover { border-color: #667eea; background: #eef0ff; }
        .cal-cell.selected { border-color: #667eea !important; background: #eef0ff !important; }
        .slot-badge {
            font-size: 9px; padding: 1px 2px; border-radius: 2px;
            font-weight: 500; white-space: normal; word-break: break-all;
            display: block; width: 100%; text-align: center; line-height: 1.2;
        }
        @media (max-width: 400px) {
            .cal-cell { min-height: 44px; padding: 2px 1px; }
            .cal-cell .cal-date { font-size: 11px; }
            .slot-badge { font-size: 8px; }
        }
        .badge-available { background: #d4edda; color: #155724; }
        .badge-reserved  { background: #f8d7da; color: #721c24; }
        .badge-closed    { background: #f8d7da; color: #721c24; }
        .badge-past      { background: #e9ecef; color: #6c757d; }
        .cal-loading { text-align: center; padding: 30px; color: #999; font-size: 14px; }
        .cal-legend { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 10px; font-size: 11px; color: #555; }
        .legend-item { display: flex; align-items: center; gap: 4px; }
        .legend-dot { width: 10px; height: 10px; border-radius: 2px; }

        /* 時間帯セレクター */
        .time-slots { display: grid; grid-template-columns: 1fr 1fr; gap: 15px; }
        .time-slot {
            border: 2px solid #ddd; border-radius: 8px; padding: 20px;
            cursor: pointer; transition: all 0.2s;
        }
        .time-slot:hover { border-color: #667eea; }
        .time-slot.selected { border-color: #667eea; background: #f0f4ff; }
        .time-slot.unavailable { opacity: 0.4; cursor: not-allowed; background: #f5f5f5; }
        .slot-time { font-weight: bold; color: #333; margin-bottom: 5px; }
        .slot-price { color: #667eea; font-size: 18px; font-weight: bold; }
        .slot-status { font-size: 11px; margin-top: 4px; }
    </style>
</head>
<body>
<div class="container">
    <div class="card">
        <h1>🅿️ シャルマン鶴見市場 No.1</h1>
        <p class="info">神奈川県横浜市鶴見区市場大和町4-9</p>
        <p class="info">💰 0-12時: ¥500 / 12-24時: ¥1,100</p>
    </div>

    <div class="card">
        <h3 style="color:#333;margin-bottom:15px;">📐 車室サイズ</h3>
        <table style="width:100%;border-collapse:collapse;">
            <tr><td style="padding:8px;border-bottom:1px solid #eee;width:40%;"><strong>長さ</strong></td><td style="padding:8px;border-bottom:1px solid #eee;">500cm</td></tr>
            <tr><td style="padding:8px;border-bottom:1px solid #eee;"><strong>幅</strong></td><td style="padding:8px;border-bottom:1px solid #eee;">190cm</td></tr>
            <tr><td style="padding:8px;border-bottom:1px solid #eee;"><strong>高さ</strong></td><td style="padding:8px;border-bottom:1px solid #eee;">220cm</td></tr>
            <tr><td style="padding:8px;border-bottom:1px solid #eee;"><strong>重量制限</strong></td><td style="padding:8px;border-bottom:1px solid #eee;">2,000kg</td></tr>
        </table>
    </div>

    <div class="card">
        <div class="policy">
            <h3>⚠️ キャンセルポリシー</h3>
            <ul>
                <li>入庫2時間前まで: キャンセル可能（手数料¥100）</li>
                <li>2時間を切った場合: キャンセル不可・全額収納</li>
            </ul>
        </div>
    </div>

    <div class="card">
        <form id="reservation-form">

            <!-- 改善2: 月カレンダー -->
            <div class="form-group calendar-section">
                <label>ご利用日</label>
                <div class="cal-header">
                    <button type="button" class="cal-nav" id="cal-prev">‹</button>
                    <span class="cal-month-label" id="cal-month-label"></span>
                    <button type="button" class="cal-nav" id="cal-next">›</button>
                </div>
                <div class="cal-grid" id="cal-grid">
                    <div class="cal-loading" style="grid-column:1/-1;">読み込み中...</div>
                </div>
                <div class="cal-legend">
                    <div class="legend-item"><div class="legend-dot" style="background:#d4edda;border:1px solid #c3e6cb;"></div>空き</div>
                    <div class="legend-item"><div class="legend-dot" style="background:#fff0f0;border:1px solid #fcc;"></div>満室</div>
                    <div class="legend-item"><div class="legend-dot" style="background:#fee;border:1px solid #fcc;"></div>休業日</div>
                    <div class="legend-item"><div class="legend-dot" style="background:#fffbe6;border:1px solid #ffe;"></div>一部空き</div>
                </div>
                <input type="hidden" id="date" required>
            </div>

            <!-- 時間帯 -->
            <div class="form-group">
                <label>時間帯</label>
                <div class="time-slots" id="time-slots">
                    <div class="time-slot" id="slot-morning" data-slot="morning">
                        <div class="slot-time">0:00 〜 12:00</div>
                        <div class="slot-price">¥500</div>
                        <div class="slot-status" id="status-morning"></div>
                    </div>
                    <div class="time-slot" id="slot-afternoon" data-slot="afternoon">
                        <div class="slot-time">12:00 〜 24:00</div>
                        <div class="slot-price">¥1,100</div>
                        <div class="slot-status" id="status-afternoon"></div>
                    </div>
                </div>
                <input type="hidden" id="time_slot" required>
            </div>

            <div class="form-group">
                <label>車両番号</label>
                <input type="text" id="car_number" placeholder="横浜 303 あ 19-27" required>
            </div>
            <div class="form-group">
                <label>お名前</label>
                <input type="text" id="customer_name" placeholder="山田 太郎" required>
            </div>
            <div class="form-group">
                <label>電話番号</label>
                <input type="tel" id="phone" placeholder="090-1234-5678" required>
            </div>
            <div class="form-group">
                <label>メールアドレス</label>
                <input type="email" id="email" placeholder="example@example.com" required>
            </div>
            <div class="form-group">
                <label>カード情報</label>
                <div id="card-element"></div>
                <div id="card-errors" class="error"></div>
            </div>

            <button type="submit" id="submit-button">予約を確定する</button>
            <div id="message"></div>
        </form>
    </div>
</div>

<footer style="max-width:600px;margin:40px auto 20px;text-align:center;color:#666;font-size:14px;padding:20px;border-top:1px solid #ddd;">
    <p style="margin-bottom:10px;">
        <a href="/terms" style="color:#667eea;text-decoration:none;margin:0 10px;">利用規約</a> |
        <a href="/privacy" style="color:#667eea;text-decoration:none;margin:0 10px;">プライバシーポリシー</a> |
        <a href="/legal" style="color:#667eea;text-decoration:none;margin:0 10px;">特定商取引法</a> |
        <a href="/refund" style="color:#667eea;text-decoration:none;margin:0 10px;">返金ポリシー</a>
    </p>
    <p>&copy; 2026 有限会社滝沢商店 All rights reserved.</p>
</footer>

<script>
// ── Stripe セットアップ ──
const stripe = Stripe('{{ stripe_public_key }}');
const elements = stripe.elements();
const cardElement = elements.create('card', { hidePostalCode: true });
cardElement.mount('#card-element');

// ── 改善2: 月カレンダー ──
const today = new Date();
let viewYear = today.getFullYear();
let viewMonth = today.getMonth() + 1; // 1始まり
let availabilityCache = {}; // { "YYYY-MM": {date: {morning:..., afternoon:...}} }
let selectedDate = null;

const DAY_NAMES = ['日','月','火','水','木','金','土'];
const MONTH_LABELS = ['1月','2月','3月','4月','5月','6月','7月','8月','9月','10月','11月','12月'];

function monthKey(y, m) { return `${y}-${String(m).padStart(2,'0')}`; }

async function fetchMonthAvailability(year, month) {
    const key = monthKey(year, month);
    if (availabilityCache[key]) return availabilityCache[key];
    try {
        const res = await fetch(`/api/month-availability?year=${year}&month=${month}`);
        const data = await res.json();
        availabilityCache[key] = data;
        return data;
    } catch(e) {
        console.error('月データ取得エラー:', e);
        return {};
    }
}

function getDayCellClass(dayData) {
    if (!dayData) return 'past';
    const { morning, afternoon } = dayData;
    if (morning === 'closed' && afternoon === 'closed') return 'closed';
    if (morning === 'past' && afternoon === 'past') return 'past';
    const bothUnavailable = (s) => s === 'reserved' || s === 'past' || s === 'closed';
    if (bothUnavailable(morning) && bothUnavailable(afternoon)) return 'full';
    if (morning === 'available' && afternoon === 'available') return 'available';
    return 'partial'; // どちらか片方が空き
}

function slotBadgeHTML(status, label) {
    const cls = {
        available: 'badge-available',
        reserved:  'badge-reserved',
        closed:    'badge-closed',
        past:      'badge-past',
    }[status] || 'badge-past';
    const text = {
        available: '空',
        reserved:  '済',
        closed:    '休',
        past:      '-',
    }[status] || '-';
    // ラベルも短縮（午前→前、午後→後）
    const shortLabel = label === '午前' ? '前' : '後';
    return `<span class="slot-badge ${cls}">${shortLabel}${text}</span>`;
}

async function renderCalendar() {
    const grid = document.getElementById('cal-grid');
    const label = document.getElementById('cal-month-label');
    label.textContent = `${viewYear}年 ${MONTH_LABELS[viewMonth-1]}`;

    // ローディング表示
    grid.innerHTML = '<div class="cal-loading" style="grid-column:1/-1;">読み込み中...</div>';

    const data = await fetchMonthAvailability(viewYear, viewMonth);

    // 月の情報
    const firstDay = new Date(viewYear, viewMonth - 1, 1).getDay(); // 0=日
    const lastDate = new Date(viewYear, viewMonth, 0).getDate();

    // 曜日ヘッダー
    let html = DAY_NAMES.map(d => `<div class="cal-dayname">${d}</div>`).join('');

    // 空白セル（月初前）
    for (let i = 0; i < firstDay; i++) {
        html += '<div class="cal-cell empty"></div>';
    }

    // 日付セル
    for (let d = 1; d <= lastDate; d++) {
        const dateStr = `${viewYear}-${String(viewMonth).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
        const dayData = data[dateStr];
        const cellClass = getDayCellClass(dayData);
        const isSelected = dateStr === selectedDate;
        const isClickable = cellClass === 'available' || cellClass === 'partial';

        const mHTML = dayData ? slotBadgeHTML(dayData.morning, '午前') : '';
        const aHTML = dayData ? slotBadgeHTML(dayData.afternoon, '午後') : '';

        const selectedAttr = isSelected ? ' selected' : '';
        const clickAttr = isClickable ? `onclick="selectDate('${dateStr}')"` : '';

        html += `<div class="cal-cell ${cellClass}${selectedAttr}" ${clickAttr}>
            <div class="cal-date">${d}</div>
            ${mHTML}
            ${aHTML}
        </div>`;
    }

    grid.innerHTML = html;
}

function selectDate(dateStr) {
    selectedDate = dateStr;
    document.getElementById('date').value = dateStr;

    // 時間帯スロットを更新
    const key = monthKey(viewYear, viewMonth);
    const data = availabilityCache[key] || {};
    const dayData = data[dateStr] || {};
    updateTimeSlots(dayData);

    // カレンダー再描画（選択状態反映）
    renderCalendar();
}

function updateTimeSlots(dayData) {
    // 選択リセット
    document.getElementById('time_slot').value = '';
    document.querySelectorAll('.time-slot').forEach(s => s.classList.remove('selected'));

    const slots = ['morning', 'afternoon'];
    slots.forEach(slot => {
        const el = document.getElementById(`slot-${slot}`);
        const statusEl = document.getElementById(`status-${slot}`);
        const st = dayData[slot] || 'past';

        el.classList.remove('unavailable', 'selected');
        if (st === 'available') {
            el.classList.remove('unavailable');
            el.style.pointerEvents = 'auto';
            statusEl.innerHTML = '<span style="color:#27ae60;font-size:11px;">● 予約可能</span>';
        } else {
            el.classList.add('unavailable');
            el.style.pointerEvents = 'none';
            const msg = st === 'reserved' ? '予約済み' : st === 'closed' ? '休業日' : '----';
            statusEl.innerHTML = `<span style="color:#999;font-size:11px;">${msg}</span>`;
        }
    });
}

document.getElementById('cal-prev').addEventListener('click', () => {
    const minYear = today.getFullYear(), minMonth = today.getMonth() + 1;
    if (viewYear === minYear && viewMonth === minMonth) return; // 当月より前には戻らない
    viewMonth--;
    if (viewMonth < 1) { viewMonth = 12; viewYear--; }
    renderCalendar();
});
document.getElementById('cal-next').addEventListener('click', () => {
    // 最大2ヶ月先
    const maxDate = new Date(today.getFullYear(), today.getMonth() + 2, 1);
    const nextDate = new Date(viewYear, viewMonth, 1);
    if (nextDate >= maxDate) return;
    viewMonth++;
    if (viewMonth > 12) { viewMonth = 1; viewYear++; }
    renderCalendar();
});

// 時間帯クリック
document.querySelectorAll('.time-slot').forEach(slot => {
    slot.addEventListener('click', () => {
        if (slot.classList.contains('unavailable')) return;
        document.querySelectorAll('.time-slot').forEach(s => s.classList.remove('selected'));
        slot.classList.add('selected');
        document.getElementById('time_slot').value = slot.dataset.slot;
    });
});

// 初期描画
renderCalendar();

// ── 予約フォーム送信 ──
let isSubmitting = false;
document.getElementById('reservation-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    if (isSubmitting) return;

    const submitBtn = document.getElementById('submit-button');
    const messageDiv = document.getElementById('message');
    isSubmitting = true;
    submitBtn.disabled = true;
    submitBtn.textContent = '処理中...';
    messageDiv.innerHTML = '';

    const formData = {
        date: document.getElementById('date').value,
        time_slot: document.getElementById('time_slot').value,
        car_number: document.getElementById('car_number').value,
        customer_name: document.getElementById('customer_name').value,
        phone: document.getElementById('phone').value,
        email: document.getElementById('email').value
    };

    try {
        const res = await fetch('/api/create-payment-intent', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(formData)
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.error || `サーバーエラー(${res.status})`);
        }
        const { clientSecret } = await res.json();

        submitBtn.textContent = '決済処理中...';
        const { error, paymentIntent } = await stripe.confirmCardPayment(clientSecret, {
            payment_method: { card: cardElement }
        });

        if (error) {
            messageDiv.innerHTML = `<p class="error">${error.message}</p>`;
        } else {
            messageDiv.innerHTML = `<p class="success">予約が完了しました！<br>確認メールをお送りします。キャンセルはメール内のリンクからお手続きください。</p>`;
            // キャッシュをクリアして再描画（空き状況を更新）
            availabilityCache = {};
            setTimeout(() => renderCalendar(), 1500);
        }
    } catch(err) {
        messageDiv.innerHTML = `<p class="error">${err.message || 'エラーが発生しました'}</p>`;
    } finally {
        isSubmitting = false;
        submitBtn.disabled = false;
        submitBtn.textContent = '予約を確定する';
    }
});
</script>
</body>
</html>
    ''', stripe_public_key=STRIPE_PUBLIC_KEY)


# ─────────────────────────────────────────
# ページ: キャンセル（改善1: トークン方式）
# ─────────────────────────────────────────

@app.route('/cancel')
def cancel_page():
    """
    改善1: URLにtokenパラメータがあれば自動入力・即時キャンセル確認
    /cancel?token=xxxx でアクセスされる想定
    """
    return render_template_string('''
<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>予約キャンセル - シャルマン鶴見市場 No.1</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f5f5f5; padding: 20px; }
        .container { max-width: 560px; margin: 0 auto; }
        .card { background: white; padding: 30px; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); margin-bottom: 20px; }
        h1 { margin-bottom: 6px; color: #333; }
        .subtitle { color: #666; font-size: 14px; margin-bottom: 24px; }
        .notice { background: #fff3cd; border-left: 4px solid #ffc107; padding: 15px; margin-bottom: 24px; border-radius: 4px; }
        .notice strong { color: #856404; }
        .confirm-box { background: #f8f9fa; border-radius: 8px; padding: 20px; margin-bottom: 24px; }
        .confirm-box h3 { font-size: 15px; margin-bottom: 12px; color: #555; }
        .confirm-row { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #eee; font-size: 14px; }
        .confirm-row:last-child { border-bottom: none; }
        .confirm-row .val { color: #333; font-weight: 500; }
        .btn-cancel {
            width: 100%; padding: 16px; background: #e74c3c; color: white;
            border: none; border-radius: 8px; font-size: 16px; font-weight: bold;
            cursor: pointer;
        }
        .btn-cancel:hover { background: #c0392b; }
        .btn-cancel:disabled { opacity: 0.5; cursor: not-allowed; }
        .btn-back { display: inline-block; margin-top: 16px; color: #667eea; text-decoration: none; font-size: 14px; }
        .error { color: #e74c3c; margin-top: 12px; font-size: 14px; padding: 10px; background: #fff5f5; border-radius: 6px; }
        .success { color: #27ae60; margin-top: 12px; font-size: 14px; padding: 10px; background: #f0fff4; border-radius: 6px; }
        .loading { color: #999; font-size: 14px; margin: 20px 0; text-align: center; }
        .invalid { text-align: center; padding: 40px 20px; }
        .invalid p { color: #666; margin-top: 12px; }
    </style>
</head>
<body>
<div class="container">
    <div class="card">
        <h1>予約キャンセル</h1>
        <p class="subtitle">シャルマン鶴見市場 No.1</p>

        <div id="content">
            <div class="loading">確認中...</div>
        </div>
    </div>
</div>

<script>
const params = new URLSearchParams(location.search);
const token = params.get('token');
const contentEl = document.getElementById('content');

// トークンなし: 案内のみ表示
if (!token) {
    contentEl.innerHTML = `
        <div class="invalid">
            <p style="font-size:48px;">📧</p>
            <p>キャンセルは予約完了メールに記載のリンクからお手続きください。</p>
            <a href="/" style="display:inline-block;margin-top:20px;color:#667eea;">トップページへ戻る</a>
        </div>`;
} else {
    // トークンあり: 確認UI表示
    contentEl.innerHTML = `
        <div class="notice">
            <strong>キャンセル手数料: ¥100</strong><br>
            入庫2時間前までキャンセル可能です。
        </div>
        <div class="confirm-box" id="confirm-box">
            <h3>キャンセル内容の確認</h3>
            <div id="reservation-detail"><div class="loading">予約情報を確認中...</div></div>
        </div>
        <button class="btn-cancel" id="cancel-btn" onclick="doCancel()" disabled>
            キャンセルを確定する
        </button>
        <div id="message"></div>
        <a href="/" class="btn-back">← トップページへ戻る</a>
    `;

    // トークンから予約情報を取得して表示
    fetchReservationInfo();
}

async function fetchReservationInfo() {
    try {
        const res = await fetch('/api/reservation-by-token?token=' + encodeURIComponent(token));
        const data = await res.json();
        const detailEl = document.getElementById('reservation-detail');
        const cancelBtn = document.getElementById('cancel-btn');

        if (!res.ok || data.error) {
            detailEl.innerHTML = `<p class="error">${data.error || '予約情報の取得に失敗しました'}</p>`;
            return;
        }

        const slotLabel = data.time_slot === 'morning' ? '0時〜12時' : '12時〜24時';
        const fee = 100;
        const refund = data.amount - fee;

        detailEl.innerHTML = `
            <div class="confirm-row"><span>ご利用日</span><span class="val">${data.date}</span></div>
            <div class="confirm-row"><span>時間帯</span><span class="val">${slotLabel}</span></div>
            <div class="confirm-row"><span>車両番号</span><span class="val">${data.car_number}</span></div>
            <div class="confirm-row"><span>お支払い金額</span><span class="val">¥${data.amount.toLocaleString()}</span></div>
            <div class="confirm-row"><span>キャンセル手数料</span><span class="val">¥${fee}</span></div>
            <div class="confirm-row"><span style="font-weight:bold;">返金額</span><span class="val" style="color:#27ae60;font-weight:bold;">¥${refund.toLocaleString()}</span></div>
        `;
        cancelBtn.disabled = false;
    } catch(e) {
        document.getElementById('reservation-detail').innerHTML =
            '<p class="error">予約情報の取得に失敗しました。ページを再読み込みしてください。</p>';
    }
}

let isCancelling = false;
async function doCancel() {
    if (isCancelling) return;
    if (!confirm('本当にキャンセルしますか？')) return;

    const cancelBtn = document.getElementById('cancel-btn');
    const messageDiv = document.getElementById('message');
    isCancelling = true;
    cancelBtn.disabled = true;
    cancelBtn.textContent = '処理中...';
    messageDiv.innerHTML = '';

    try {
        const res = await fetch('/api/cancel-reservation', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ token })
        });
        const data = await res.json();

        if (res.ok) {
            messageDiv.innerHTML = `<div class="success">✅ ${data.message}</div>`;
            cancelBtn.style.display = 'none';
        } else {
            messageDiv.innerHTML = `<div class="error">❌ ${data.error}</div>`;
            isCancelling = false;
            cancelBtn.disabled = false;
            cancelBtn.textContent = 'キャンセルを確定する';
        }
    } catch(e) {
        messageDiv.innerHTML = '<div class="error">通信エラーが発生しました</div>';
        isCancelling = false;
        cancelBtn.disabled = false;
        cancelBtn.textContent = 'キャンセルを確定する';
    }
}
</script>
</body>
</html>
    ''')


# ─────────────────────────────────────────
# API: トークンから予約情報取得（キャンセルページ用）
# ─────────────────────────────────────────

@app.route('/api/reservation-by-token', methods=['GET'])
def reservation_by_token():
    """キャンセルページで予約内容を表示するための読み取り専用API"""
    token = request.args.get('token')
    if not token:
        return jsonify({'error': 'トークンが指定されていません'}), 400

    conn = get_db_connection()
    cursor = conn.cursor()

    if USE_POSTGRES:
        cursor.execute(
            "SELECT date, time_slot, car_number, amount, cancel_token_expires_at FROM reservations WHERE cancel_token=%s AND status='confirmed'",
            (token,)
        )
    else:
        cursor.execute(
            "SELECT date, time_slot, car_number, amount, cancel_token_expires_at FROM reservations WHERE cancel_token=? AND status='confirmed'",
            (token,)
        )
    row = cursor.fetchone()
    conn.close()

    if not row:
        return jsonify({'error': '無効なリンクか、既にキャンセル済みの予約です'}), 404

    # 期限チェック
    expires_at = row[4]
    if expires_at:
        expires_dt = datetime.fromisoformat(expires_at)
        if expires_dt.tzinfo is None:
            expires_dt = expires_dt.replace(tzinfo=JST)
        if datetime.now(JST) >= expires_dt:
            return jsonify({'error': 'キャンセル期限（入庫2時間前）を過ぎています'}), 400

    return jsonify({
        'date': row[0],
        'time_slot': row[1],
        'car_number': row[2],
        'amount': row[3],
    })


# ─────────────────────────────────────────
# 既存ページ（利用規約・プライバシー等）
# ─────────────────────────────────────────

@app.route('/terms')
def terms():
    return render_template_string('''<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8"><title>利用規約</title><meta name="viewport" content="width=device-width,initial-scale=1.0"><style>body{font-family:sans-serif;line-height:1.8;max-width:800px;margin:0 auto;padding:20px;}h1{border-bottom:3px solid #667eea;padding-bottom:10px;}h2{color:#667eea;margin-top:30px;}.back{display:inline-block;margin-bottom:20px;color:#667eea;text-decoration:none;}</style></head><body>
    <a href="/" class="back">← トップに戻る</a>
    <h1>利用規約</h1><p>最終更新日: 2026年4月6日</p>
    <h2>第1条（適用）</h2><p>本規約は、有限会社滝沢商店が運営する駐車場予約サービスの利用条件を定めるものです。</p>
    <h2>第2条（予約）</h2><p>予約の成立は、決済完了時点とします。予約確定後、登録メールアドレスに確認メールを送信します。</p>
    <h2>第3条（料金）</h2><p>午前（0時-12時）: 500円 / 午後（12時-24時）: 1,100円。料金は予約時にクレジットカードで事前決済されます。</p>
    <h2>第4条（キャンセル）</h2><p>入庫予定時刻の2時間前までキャンセル可能（手数料100円）。2時間前を過ぎた場合はキャンセル不可・全額収納。返金は3-5営業日以内に処理されます。</p>
    <h2>第5条（免責）</h2><p>駐車中の車両の盗難・損傷について責任を負いません。</p>
    <h2>第6条（連絡先）</h2><p>有限会社滝沢商店 〒230-0025 神奈川県横浜市鶴見区市場大和町4-9 Email: noboru.takizawa@blueflag-sys.com</p>
    </body></html>''')


@app.route('/privacy')
def privacy():
    return render_template_string('''<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8"><title>プライバシーポリシー</title><meta name="viewport" content="width=device-width,initial-scale=1.0"><style>body{font-family:sans-serif;line-height:1.8;max-width:800px;margin:0 auto;padding:20px;}h1{border-bottom:3px solid #667eea;padding-bottom:10px;}h2{color:#667eea;margin-top:30px;}.back{display:inline-block;margin-bottom:20px;color:#667eea;text-decoration:none;}</style></head><body>
    <a href="/" class="back">← トップに戻る</a>
    <h1>プライバシーポリシー</h1><p>最終更新日: 2026年4月6日</p>
    <h2>1. 収集する情報</h2><p>氏名、メールアドレス、電話番号、車両番号、決済情報（クレジットカード情報はStripe社が管理）。</p>
    <h2>2. 利用目的</h2><p>予約管理・確認、予約確認メール・キャンセル通知の送信、決済処理、お問い合わせ対応。</p>
    <h2>3. 第三者提供</h2><p>ご本人の同意がある場合、法令に基づく場合、決済処理のためStripe社に提供する場合を除き、個人情報を第三者に提供しません。</p>
    <h2>4. お問い合わせ</h2><p>有限会社滝沢商店 Email: noboru.takizawa@blueflag-sys.com</p>
    </body></html>''')


@app.route('/legal')
def legal():
    return render_template_string('''<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8"><title>特定商取引法に基づく表記</title><meta name="viewport" content="width=device-width,initial-scale=1.0"><style>body{font-family:sans-serif;line-height:1.8;max-width:800px;margin:0 auto;padding:20px;}h1{border-bottom:3px solid #667eea;padding-bottom:10px;}table{width:100%;border-collapse:collapse;margin:20px 0;}th,td{border:1px solid #ddd;padding:12px;text-align:left;}th{background:#f5f5f5;width:200px;}.back{display:inline-block;margin-bottom:20px;color:#667eea;text-decoration:none;}</style></head><body>
    <a href="/" class="back">← トップに戻る</a>
    <h1>特定商取引法に基づく表記</h1>
    <table>
        <tr><th>事業者名</th><td>有限会社滝沢商店</td></tr>
        <tr><th>代表者</th><td>滝沢 登</td></tr>
        <tr><th>所在地</th><td>〒230-0025 神奈川県横浜市鶴見区市場大和町4-9</td></tr>
        <tr><th>電話番号</th><td>090-6137-1111</td></tr>
        <tr><th>メールアドレス</th><td>noboru.takizawa@blueflag-sys.com</td></tr>
        <tr><th>販売価格</th><td>午前（0-12時）: 500円 / 午後（12-24時）: 1,100円</td></tr>
        <tr><th>支払方法</th><td>クレジットカード（Stripe決済）</td></tr>
        <tr><th>支払時期</th><td>予約確定時に即時決済</td></tr>
        <tr><th>キャンセル・返金</th><td>入庫2時間前までキャンセル可能（手数料100円）</td></tr>
    </table>
    </body></html>''')


@app.route('/refund')
def refund_policy():
    return render_template_string('''<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8"><title>返金ポリシー</title><meta name="viewport" content="width=device-width,initial-scale=1.0"><style>body{font-family:sans-serif;line-height:1.8;max-width:800px;margin:0 auto;padding:20px;}h1{border-bottom:3px solid #667eea;padding-bottom:10px;}h2{color:#667eea;margin-top:30px;}.back{display:inline-block;margin-bottom:20px;color:#667eea;text-decoration:none;}.notice{background:#fff3cd;border-left:4px solid #ffc107;padding:15px;margin:20px 0;}</style></head><body>
    <a href="/" class="back">← トップに戻る</a>
    <h1>返金ポリシー</h1>
    <div class="notice"><strong>入庫2時間前までのキャンセル</strong><ul><li>キャンセル手数料: 100円</li><li>返金額: 支払額 - 100円</li></ul></div>
    <div class="notice"><strong>入庫2時間前を過ぎた場合</strong><ul><li>キャンセル不可・返金なし</li></ul></div>
    <h2>返金処理</h2><p>返金は3-5営業日以内に処理されます。カード会社の処理により1-2週間かかる場合があります。</p>
    <h2>キャンセル方法</h2><p>予約完了メールに記載のリンクからお手続きください。</p>
    <h2>お問い合わせ</h2><p>Email: noboru.takizawa@blueflag-sys.com / 電話: 090-6137-9489</p>
    </body></html>''')


@app.route('/admin')
@require_admin_auth
def admin():
    return render_template_string('''
<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <title>管理画面</title>
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family:sans-serif; padding:20px; background:#f5f5f5; }
        .container { max-width:1200px; margin:0 auto; }
        h1 { margin-bottom:20px; }
        .section { background:white; padding:20px; border-radius:8px; margin-bottom:20px; }
        table { width:100%; border-collapse:collapse; }
        th,td { border:1px solid #ddd; padding:12px; text-align:left; }
        th { background:#667eea; color:white; }
        button { padding:8px 16px; background:#667eea; color:white; border:none; border-radius:4px; cursor:pointer; }
        button.danger { background:#e74c3c; }
        input[type="date"] { padding:8px; border:1px solid #ddd; border-radius:4px; margin-right:10px; }
        .tabs { display:flex; gap:10px; margin-bottom:20px; }
        .tab { padding:10px 20px; background:#ddd; border-radius:4px; cursor:pointer; }
        .tab.active { background:#667eea; color:white; }
        .tab-content { display:none; }
        .tab-content.active { display:block; }
    </style>
</head>
<body>
<div class="container">
    <h1>🅿️ 管理画面</h1>
    <div class="tabs">
        <div class="tab active" onclick="switchTab('reservations',this)">予約一覧</div>
        <div class="tab" onclick="switchTab('closed',this)">休業日設定</div>
    </div>
    <div id="reservations" class="tab-content active">
        <div class="section">
            <h2>予約一覧</h2>
            <div id="reservations-list"></div>
        </div>
    </div>
    <div id="closed" class="tab-content">
        <div class="section">
            <h2>休業日追加</h2>
            <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
                <input type="date" id="new-closed-date">
                <select id="new-closed-slot" style="padding:8px;border:1px solid #ddd;border-radius:4px;">
                    <option value="morning">午前（0-12時）</option>
                    <option value="afternoon">午後（12-24時）</option>
                </select>
                <button onclick="addClosedDate()">追加</button>
            </div>
            <p style="margin-top:8px;font-size:12px;color:#888;">※終日休業にする場合は午前・午後を個別に2回追加してください</p>
        </div>
        <div class="section">
            <h2>休業日一覧</h2>
            <div id="closed-dates-list"></div>
        </div>
    </div>
</div>
<script>
function switchTab(tab, el) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    el.classList.add('active');
    document.getElementById(tab).classList.add('active');
    if (tab === 'reservations') loadReservations();
    if (tab === 'closed') loadClosedDates();
}
async function loadReservations() {
    const res = await fetch('/api/reservations');
    const data = await res.json();
    let html = '<table><tr><th>日付</th><th>時間帯</th><th>車両番号</th><th>氏名</th><th>金額</th></tr>';
    data.reservations.forEach(r => {
        html += `<tr><td>${r.date}</td><td>${r.time_slot === 'morning' ? '0-12時' : '12-24時'}</td><td>${r.car_number}</td><td>${r.customer_name}</td><td>¥${r.amount}</td></tr>`;
    });
    html += '</table>';
    document.getElementById('reservations-list').innerHTML = html;
}
async function loadClosedDates() {
    const res = await fetch('/api/closed-dates');
    const data = await res.json();
    let html = '<table><tr><th>日付</th><th>時間帯</th><th>理由</th><th>操作</th></tr>';
    data.forEach(d => {
        const slotLabel = d.time_slot === 'morning' ? '午前（0-12時）' : '午後（12-24時）';
        html += `<tr>
            <td>${d.date}</td>
            <td>${slotLabel}</td>
            <td>${d.reason}</td>
            <td><button class="danger" onclick="deleteClosedDate(${d.id})">削除</button></td>
        </tr>`;
    });
    html += '</table>';
    document.getElementById('closed-dates-list').innerHTML = html;
}
async function addClosedDate() {
    const date = document.getElementById('new-closed-date').value;
    const time_slot = document.getElementById('new-closed-slot').value;
    if (!date) return alert('日付を選択してください');
    const res = await fetch('/api/closed-dates', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({date, time_slot, reason: '休業日'})
    });
    if (res.ok) {
        const slotLabel = time_slot === 'morning' ? '午前' : '午後';
        alert(`${date} ${slotLabel}を休業日に設定しました`);
        loadClosedDates();
    } else {
        const e = await res.json();
        alert(e.error);
    }
}
async function deleteClosedDate(id) {
    if (!confirm('削除しますか？')) return;
    await fetch(`/api/closed-dates/${id}`, { method:'DELETE' });
    alert('削除しました');
    loadClosedDates();
}
loadReservations();
</script>
</body>
</html>
    ''')


@app.route('/static/parking_photo.png')
def parking_photo():
    photo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'parking_photo.png')
    if os.path.exists(photo_path):
        return send_file(photo_path, mimetype='image/png')
    return 'Photo not found', 404


if __name__ == '__main__':
    print("=" * 60)
    print("🚀 駐車場予約システム v2（改善1・2適用版）")
    print("=" * 60)
    print("📍 予約フォーム: http://localhost:5000/reserve")
    print("📊 管理画面:     http://localhost:5000/admin")
    print("=" * 60)
    init_database()
    app.run(host='0.0.0.0', port=5000, debug=True)
else:
    print(f"🔗 DB: {'PostgreSQL' if USE_POSTGRES else 'SQLite'}")
    try:
        init_database()
        cleanup_old_pending_reservations()
        print("✅ 起動準備完了")
    except Exception as e:
        print(f"⚠️ 初期化エラー: {e}")
