#!/usr/bin/env python3
"""
駐車場予約システム - 実運用版
シャルマン鶴見市場 No.1

機能:
- 時間帯別料金（0-12時: ¥500、12-24時: ¥1,100）
- キャンセル機能（入庫2時間前まで・手数料¥100）
- 休業日設定（2ヶ月先まで）
- 予約カレンダー（空き状況表示）
"""

from flask import Flask, request, jsonify, render_template_string, send_file
import stripe
import json
from datetime import datetime, timedelta
import sqlite3
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os

app = Flask(__name__)

# データベース設定
DATABASE_URL = os.environ.get('DATABASE_URL')

# PostgreSQL用のインポート（利用可能な場合）
if DATABASE_URL and DATABASE_URL.startswith('postgres'):
    import psycopg2
    import psycopg2.extras
    USE_POSTGRES = True
else:
    USE_POSTGRES = False

# Stripe設定
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', 'sk_test_YOUR_SECRET_KEY')
STRIPE_PUBLIC_KEY = os.environ.get('STRIPE_PUBLIC_KEY', 'pk_test_51T0HAtR79rW14GmdmCOvmZaWGgfFXUzEctTgJ4UT555NcH8RnWk5V0MXKcxrFprMhPbTdJEwnVpOGp6ekqO65pTY00Kb69zulE')

# Webhookシークレット（Stripeダッシュボードから取得）
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', 'whsec_YOUR_WEBHOOK_SECRET')

# メール設定（Gmail SMTP）
EMAIL_SENDER = os.environ.get('EMAIL_SENDER', 'your-email@gmail.com')
EMAIL_PASSWORD = os.environ.get('EMAIL_PASSWORD', 'your-app-password')
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587


# データベースパス
# 本番では外部DBサービス（PostgreSQL等）を推奨
# Render環境では /tmp を使用（永続化しない一時データベース）
DB_PATH = os.environ.get('DB_PATH', '/tmp/parking_system.db' if os.path.exists('/tmp') else 'parking_system.db')


def get_db_connection():
    """データベース接続を取得（PostgreSQL または SQLite）"""
    if USE_POSTGRES:
        # PostgreSQL接続
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    else:
        # SQLite接続（ローカル開発用）
        conn = sqlite3.connect(DB_PATH, timeout=10.0)
        return conn


def send_reservation_email(to_email, customer_name, reservation_data):
    """予約完了メール送信"""
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = '【予約完了】シャルマン鶴見市場 No.1 駐車場'
        msg['From'] = EMAIL_SENDER
        msg['To'] = to_email
        
        # メール本文（HTML）
        html = f"""
        <html>
        <body style="font-family: sans-serif;">
            <h2>駐車場予約が完了しました</h2>
            <p>{customer_name} 様</p>
            <p>ご予約ありがとうございます。以下の内容で予約を承りました。</p>
            
            <div style="background: #f5f5f5; padding: 20px; border-radius: 8px; margin: 20px 0;">
                <h3>予約内容</h3>
                <table style="width: 100%;">
                    <tr><td><strong>ご利用日:</strong></td><td>{reservation_data['date']}</td></tr>
                    <tr><td><strong>時間帯:</strong></td><td>{'0-12時' if reservation_data['time_slot'] == 'morning' else '12-24時'}</td></tr>
                    <tr><td><strong>車両番号:</strong></td><td>{reservation_data['car_number']}</td></tr>
                    <tr><td><strong>料金:</strong></td><td>¥{reservation_data['amount']:,}</td></tr>
                    <tr><td><strong>決済ID:</strong></td><td>{reservation_data['payment_id']}</td></tr>
                </table>
            </div>
            
            <div style="background: #fff3cd; padding: 15px; border-radius: 8px; margin: 20px 0;">
                <h3>⚠️ キャンセルポリシー</h3>
                <ul>
                    <li>入庫2時間前まで: キャンセル可能（手数料¥100）</li>
                    <li>2時間を切った場合: キャンセル不可・全額収納</li>
                </ul>
                <p>キャンセルはこちら: <a href="http://localhost:5000/cancel">キャンセルページ</a></p>
            </div>
            
            <p>ご不明な点がございましたら、お気軽にお問い合わせください。</p>
            <p>当日のご利用をお待ちしております。</p>
            
            <hr>
            <p style="color: #666; font-size: 12px;">
                シャルマン鶴見市場 No.1<br>
                神奈川県横浜市鶴見区市場大和町4-9
            </p>
        </body>
        </html>
        """
        
        part = MIMEText(html, 'html')
        msg.attach(part)
        
        # SMTP送信
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.send_message(msg)
        
        print(f"📧 予約完了メール送信成功: {to_email}")
        return True
        
    except Exception as e:
        print(f"❌ メール送信エラー: {e}")
        return False


def send_cancellation_email(to_email, customer_name, reservation_data, refund_amount, fee):
    """キャンセル確認メール送信"""
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = '【キャンセル完了】シャルマン鶴見市場 No.1 駐車場'
        msg['From'] = EMAIL_SENDER
        msg['To'] = to_email
        
        html = f"""
        <html>
        <body style="font-family: sans-serif;">
            <h2>予約キャンセルが完了しました</h2>
            <p>{customer_name} 様</p>
            <p>以下の予約をキャンセルいたしました。</p>
            
            <div style="background: #f5f5f5; padding: 20px; border-radius: 8px; margin: 20px 0;">
                <h3>キャンセル内容</h3>
                <table style="width: 100%;">
                    <tr><td><strong>ご利用日:</strong></td><td>{reservation_data['date']}</td></tr>
                    <tr><td><strong>時間帯:</strong></td><td>{'0-12時' if reservation_data['time_slot'] == 'morning' else '12-24時'}</td></tr>
                    <tr><td><strong>返金額:</strong></td><td>¥{refund_amount:,}</td></tr>
                    <tr><td><strong>キャンセル手数料:</strong></td><td>¥{fee}</td></tr>
                </table>
            </div>
            
            <p>返金は3-5営業日以内にお客様のカードに処理されます。</p>
            <p>またのご利用をお待ちしております。</p>
            
            <hr>
            <p style="color: #666; font-size: 12px;">
                シャルマン鶴見市場 No.1<br>
                神奈川県横浜市鶴見区市場大和町4-9
            </p>
        </body>
        </html>
        """
        
        part = MIMEText(html, 'html')
        msg.attach(part)
        
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.send_message(msg)
        
        print(f"📧 キャンセルメール送信成功: {to_email}")
        return True
        
    except Exception as e:
        print(f"❌ メール送信エラー: {e}")
        return False


def init_database():
    """データベース初期化（PostgreSQL / SQLite対応）"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if USE_POSTGRES:
        # PostgreSQL用SQL
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
                UNIQUE(date, time_slot, status)
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
                date TEXT UNIQUE,
                reason TEXT,
                created_at TEXT
            )
        ''')
    else:
        # SQLite用SQL
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
                date TEXT UNIQUE,
                reason TEXT,
                created_at TEXT
            )
        ''')
    
    conn.commit()
    conn.close()
    print("✅ データベース初期化完了")


init_database()


def calculate_price(time_slot):
    """時間帯から料金を計算"""
    if time_slot == "morning":
        return 500
    elif time_slot == "afternoon":
        return 1100
    return 0


def is_cancellable(reservation_date, time_slot):
    """キャンセル可能かチェック（入庫2時間前まで）"""
    now = datetime.now()
    
    # 入庫時刻を計算
    res_datetime = datetime.fromisoformat(reservation_date)
    if time_slot == "afternoon":
        res_datetime = res_datetime.replace(hour=12, minute=0, second=0)
    else:
        res_datetime = res_datetime.replace(hour=0, minute=0, second=0)
    
    # 2時間前まで
    cancellation_deadline = res_datetime - timedelta(hours=2)
    
    return now < cancellation_deadline


@app.route('/api/check-availability', methods=['POST'])
def check_availability():
    """空き状況確認"""
    try:
        data = request.json
        date = data.get('date')
        time_slot = data.get('time_slot')
        
        # 当日の場合、時間帯が過ぎていないかチェック
        now = datetime.now()
        selected_date = datetime.fromisoformat(date)
        
        if selected_date.date() == now.date():
            # 当日の場合
            current_hour = now.hour
            
            if time_slot == "morning" and current_hour >= 12:
                # 午前枠は12時を過ぎたら予約不可
                return jsonify({'available': False, 'reason': 'time_passed'})
            elif time_slot == "afternoon" and current_hour >= 24:
                # 午後枠は24時を過ぎたら予約不可（実質翌日）
                return jsonify({'available': False, 'reason': 'time_passed'})
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 休業日チェック
        cursor.execute('SELECT * FROM closed_dates WHERE date = ?', (date,))
        if cursor.fetchone():
            conn.close()
            return jsonify({'available': False, 'reason': 'closed'})
        
        # 予約済みチェック
        cursor.execute('''
            SELECT * FROM reservations 
            WHERE date = ? AND time_slot = ? AND status = 'confirmed'
        ''', (date, time_slot))
        
        if cursor.fetchone():
            conn.close()
            return jsonify({'available': False, 'reason': 'reserved'})
        
        conn.close()
        
        return jsonify({
            'available': True,
            'price': calculate_price(time_slot)
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/create-payment-intent', methods=['POST'])
def create_payment_intent():
    """決済Intent作成"""
    try:
        data = request.json
        time_slot = data.get('time_slot')
        date = data.get('date')
        
        # 二重予約チェック（トランザクション開始）
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 既に予約が存在するかチェック
        cursor.execute('''
            SELECT * FROM reservations 
            WHERE date = ? AND time_slot = ? AND status IN ('confirmed', 'pending')
        ''', (date, time_slot))
        
        if cursor.fetchone():
            conn.close()
            return jsonify({'error': 'この時間帯は既に予約されています'}), 400
        
        # 一時予約を作成（pending状態）
        temp_reservation_id = f"temp_{int(datetime.now().timestamp() * 1000)}"
        
        try:
            cursor.execute('''
                INSERT INTO reservations 
                (payment_id, car_number, customer_name, phone, email, date, time_slot, amount, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            ''', (
                temp_reservation_id,
                data.get('car_number'),
                data.get('customer_name'),
                data.get('phone'),
                data.get('email'),
                date,
                time_slot,
                calculate_price(time_slot),
                datetime.now().isoformat()
            ))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            return jsonify({'error': 'この時間帯は既に予約されています'}), 400
        
        conn.close()
        
        # Stripe PaymentIntent作成
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
                'temp_reservation_id': temp_reservation_id  # 追加
            }
        )
        
        return jsonify({
            'clientSecret': payment_intent.client_secret,
            'amount': amount
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/webhook', methods=['POST'])
def stripe_webhook():
    """Stripe Webhookエンドポイント"""
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')
    
    # 本番環境では署名検証を有効化
    if WEBHOOK_SECRET and WEBHOOK_SECRET != "whsec_YOUR_WEBHOOK_SECRET":
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, WEBHOOK_SECRET
            )
            # Stripeから返されるeventはオブジェクトなので辞書アクセスに変換
            event_type = event['type']
            event_data = event['data']['object']
        except ValueError as e:
            print(f"❌ 不正なペイロード: {e}")
            return jsonify({'error': 'Invalid payload'}), 400
        except stripe.error.SignatureVerificationError as e:
            print(f"❌ 署名検証失敗: {e}")
            return jsonify({'error': 'Invalid signature'}), 400
        except Exception as e:
            print(f"❌ イベント解析エラー: {e}")
            return jsonify({'error': 'Event parse error'}), 400
    else:
        # テスト環境では署名検証をスキップ
        try:
            event = request.json
            event_type = event.get('type')
            event_data = event.get('data', {}).get('object', {})
        except Exception as e:
            print(f"❌ JSONパースエラー: {e}")
            return jsonify({'error': 'Invalid JSON'}), 400
    
    try:
        print(f"\n📨 Webhook: {event_type}")
        
        # イベントIDを取得（二重処理防止）
        try:
            event_id = event.get('id') if isinstance(event, dict) else event['id']
        except (KeyError, AttributeError, TypeError) as e:
            print(f"⚠️  イベントID取得エラー: {e}")
            # イベントIDがない場合はスキップ（古い形式の可能性）
            event_id = None
        
        # イベントIDがある場合のみ重複チェック
        if event_id:
            # 既に処理済みかチェック
            conn = get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute('SELECT event_id FROM webhook_events WHERE event_id = ?', (event_id,))
            if cursor.fetchone():
                conn.close()
                print(f"⚠️  既に処理済みのイベント: {event_id}")
                return jsonify({'status': 'success', 'message': 'Already processed'}), 200
            
            # イベントを記録（処理前）
            try:
                cursor.execute('''
                    INSERT INTO webhook_events (event_id, event_type, payment_id, processed_at)
                    VALUES (?, ?, ?, ?)
                ''', (event_id, event_type, 'processing', datetime.now().isoformat()))
                conn.commit()
            except sqlite3.IntegrityError:
                # 別のリクエストが同時に処理中
                conn.close()
                print(f"⚠️  同時処理検出: {event_id}")
                return jsonify({'status': 'success', 'message': 'Concurrent processing detected'}), 200
            
            conn.close()
        
        if event_type == 'payment_intent.succeeded' or event_type == 'charge.succeeded':
            # payment_intent.succeeded と charge.succeeded の両方に対応
            # Stripeオブジェクトから直接データを取得
            try:
                if event_type == 'charge.succeeded':
                    # charge.succeededの場合
                    payment_id = event_data['payment_intent']
                    # chargeオブジェクトにはmetadataがないのでスキップ
                    print(f"⚠️  charge.succeededイベントはスキップ（metadataなし）")
                    return jsonify({'status': 'success'}), 200
                else:
                    # payment_intent.succeededの場合
                    payment_id = event_data['id']
                    amount = event_data['amount']
                    
                    # デバッグ: event_dataの内容を確認
                    print(f"🔍 payment_id: {payment_id}")
                    print(f"🔍 amount: {amount}")
                    print(f"🔍 'metadata' in event_data: {'metadata' in event_data}")
                    
                    # metadataの取得（Stripeオブジェクトから辞書に変換）
                    if 'metadata' in event_data:
                        raw_metadata = event_data['metadata']
                        print(f"🔍 raw_metadata type: {type(raw_metadata)}")
                        print(f"🔍 raw_metadata: {raw_metadata}")
                        
                        # StripeObjectを辞書に変換（to_dict_recursiveメソッドを使用）
                        if raw_metadata:
                            try:
                                # StripeObjectにはto_dict_recursiveメソッドがある
                                if hasattr(raw_metadata, 'to_dict_recursive'):
                                    metadata = raw_metadata.to_dict_recursive()
                                else:
                                    # または_dataプロパティを直接使う
                                    metadata = raw_metadata._data if hasattr(raw_metadata, '_data') else {}
                                print(f"🔍 converted metadata: {metadata}")
                            except Exception as e:
                                print(f"❌ metadata変換エラー: {e}")
                                metadata = {}
                        else:
                            metadata = {}
                            print(f"⚠️  metadataは空です")
                    else:
                        metadata = {}
                        print(f"⚠️  metadataキーが存在しません")
                    
            except (KeyError, TypeError, IndexError) as e:
                print(f"❌ イベントデータ取得エラー: {e}")
                import traceback
                traceback.print_exc()
                return jsonify({'status': 'success'}), 200
            
            # metadataが存在し、車両番号がある場合のみ予約を確定
            if metadata and 'car_number' in metadata and metadata['car_number']:
                conn = get_db_connection()
                cursor = conn.cursor()
                
                try:
                    # 一時予約IDがある場合は更新、ない場合は新規作成
                    temp_reservation_id = metadata.get('temp_reservation_id')
                    
                    if temp_reservation_id:
                        # 一時予約を本予約に更新
                        cursor.execute('''
                            UPDATE reservations 
                            SET payment_id = ?, status = 'confirmed'
                            WHERE payment_id = ? AND status = 'pending'
                        ''', (payment_id, temp_reservation_id))
                        
                        if cursor.rowcount > 0:
                            print(f"✅ 一時予約を本予約に更新: {payment_id}")
                        else:
                            print(f"⚠️  一時予約が見つかりません、新規作成します")
                            # フォールバック: 新規作成
                            cursor.execute('''
                                INSERT INTO reservations 
                                (payment_id, car_number, customer_name, phone, email, date, time_slot, amount, status, created_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ''', (
                                payment_id,
                                metadata.get('car_number', ''),
                                metadata.get('customer_name', ''),
                                metadata.get('phone', ''),
                                metadata.get('email', ''),
                                metadata.get('date', ''),
                                metadata.get('time_slot', ''),
                                amount,
                                'confirmed',
                                datetime.now().isoformat()
                            ))
                    else:
                        # 旧バージョン対応: temp_reservation_idがない場合は新規作成
                        cursor.execute('''
                            INSERT INTO reservations 
                            (payment_id, car_number, customer_name, phone, email, date, time_slot, amount, status, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            payment_id,
                            metadata.get('car_number', ''),
                            metadata.get('customer_name', ''),
                            metadata.get('phone', ''),
                            metadata.get('email', ''),
                            metadata.get('date', ''),
                            metadata.get('time_slot', ''),
                            amount,
                            'confirmed',
                            datetime.now().isoformat()
                        ))
                    
                    conn.commit()
                    print(f"✅ 予約確定: {metadata.get('date')} {metadata.get('time_slot')}")
                    
                    # Webhookイベントにpayment_idを記録（event_idがある場合のみ）
                    if event_id:
                        cursor.execute('''
                            UPDATE webhook_events 
                            SET payment_id = ?
                            WHERE event_id = ?
                        ''', (payment_id, event_id))
                        conn.commit()
                    
                    # 予約完了メール送信
                    if metadata.get('email'):
                        send_reservation_email(
                            to_email=metadata.get('email'),
                            customer_name=metadata.get('customer_name', 'お客様'),
                            reservation_data={
                                'date': metadata.get('date'),
                                'time_slot': metadata.get('time_slot'),
                                'car_number': metadata.get('car_number'),
                                'amount': amount,
                                'payment_id': payment_id
                            }
                        )
                    
                except sqlite3.IntegrityError as e:
                    print(f"⚠️  既に処理済みまたは二重予約: {payment_id}, {e}")
                except Exception as e:

                    print(f"❌ DB保存エラー: {e}")
                    import traceback
                    traceback.print_exc()
                finally:
                    conn.close()
            else:
                print(f"⚠️  metadata未設定または車両番号なし。metadata: {metadata}")
        
        elif event_type == 'charge.refunded':
            # 払い戻し完了イベント
            try:
                payment_id = event_data['payment_intent']
                refund_amount = event_data['amount_refunded']
                
                print(f"💰 払い戻し完了: {payment_id}, 金額: ¥{refund_amount}")
                
                conn = get_db_connection()
                cursor = conn.cursor()
                
                # 予約ステータスを refunded に更新
                cursor.execute('''
                    UPDATE reservations 
                    SET status = 'refunded'
                    WHERE payment_id = ?
                ''', (payment_id,))
                
                affected = cursor.rowcount
                conn.commit()
                conn.close()
                
                if affected > 0:
                    print(f"✅ 予約ステータスを refunded に更新: {payment_id}")
                else:
                    print(f"⚠️  該当する予約が見つかりません: {payment_id}")
                
            except (KeyError, TypeError) as e:
                print(f"❌ charge.refunded処理エラー: {e}")
                import traceback
                traceback.print_exc()
        
        elif event_type == 'payment_intent.payment_failed':
            # 決済失敗イベント
            try:
                payment_id = event_data['id']
                
                # last_payment_errorの取得
                if 'last_payment_error' in event_data and event_data['last_payment_error']:
                    error_obj = event_data['last_payment_error']
                    # Stripeオブジェクトを辞書に変換
                    if hasattr(error_obj, 'to_dict_recursive'):
                        error_message = error_obj.to_dict_recursive()
                    elif hasattr(error_obj, '_data'):
                        error_message = error_obj._data
                    else:
                        error_message = str(error_obj)
                else:
                    error_message = "Unknown error"
                
                print(f"❌ 決済失敗: {payment_id}")
                print(f"   エラー詳細: {error_message}")
                
                # エラーログをファイルに記録
                with open('payment_errors.log', 'a') as f:
                    f.write(f"{datetime.now().isoformat()} - {payment_id} - {error_message}\n")
                
            except Exception as e:
                print(f"❌ payment_failed処理エラー: {e}")
                import traceback
                traceback.print_exc()
        
        return jsonify({'status': 'success'}), 200
        
    except Exception as e:
        print(f"❌ Webhookエラー: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 400


@app.route('/api/reservations', methods=['GET'])
def get_reservations():
    """予約一覧取得"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT * FROM reservations 
        WHERE status = 'confirmed'
        ORDER BY date, time_slot
    ''')
    rows = cursor.fetchall()
    
    reservations = []
    for row in rows:
        reservations.append({
            'id': row[0],
            'payment_id': row[1],
            'car_number': row[2],
            'customer_name': row[3],
            'phone': row[4],
            'email': row[5],
            'date': row[6],
            'time_slot': row[7],
            'amount': row[8],
            'status': row[9],
            'created_at': row[10]
        })
    
    conn.close()
    
    return jsonify({
        'total': len(reservations),
        'reservations': reservations
    })


@app.route('/api/cancel-reservation', methods=['POST'])
def cancel_reservation():
    """予約キャンセル（手数料¥100）"""
    try:
        data = request.json
        payment_id = data.get('payment_id')
        
        print(f"\n💰 キャンセル要求: {payment_id}")
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 予約情報取得
        cursor.execute('SELECT * FROM reservations WHERE payment_id = ? AND status = "confirmed"', (payment_id,))
        row = cursor.fetchone()
        
        if not row:
            conn.close()
            print(f"❌ 予約が見つかりません: {payment_id}")
            return jsonify({'error': '予約が見つかりません'}), 404
        
        reservation_date = row[6]
        time_slot = row[7]
        amount = row[8]
        
        print(f"   予約情報: {reservation_date} {time_slot} ¥{amount}")
        
        # キャンセル可能かチェック
        if not is_cancellable(reservation_date, time_slot):
            conn.close()
            print(f"❌ キャンセル期限切れ")
            return jsonify({
                'error': '入庫2時間前を過ぎているためキャンセルできません'
            }), 400
        
        # キャンセル手数料を差し引いて払い戻し
        cancellation_fee = 100  # キャンセル手数料
        refund_amount = amount - cancellation_fee
        
        if refund_amount < 0:
            refund_amount = 0
        
        print(f"   払戻額: ¥{refund_amount} (手数料¥{cancellation_fee})")
        
        # Stripeで部分払い戻し
        try:
            refund = stripe.Refund.create(
                payment_intent=payment_id,
                amount=refund_amount
            )
        except stripe.error.InvalidRequestError as e:
            conn.close()
            print(f"❌ Stripeエラー: {e}")
            return jsonify({'error': f'Stripe払い戻しエラー: {str(e)}'}), 400
        
        # 予約をキャンセル状態に更新
        cursor.execute('''
            UPDATE reservations 
            SET status = 'cancelled', cancelled_at = ?
            WHERE payment_id = ?
        ''', (datetime.now().isoformat(), payment_id))
        
        conn.commit()
        
        # キャンセル確認メール送信
        customer_email = row[5]  # emailカラム
        customer_name = row[3]   # customer_nameカラム
        
        if customer_email:
            send_cancellation_email(
                to_email=customer_email,
                customer_name=customer_name or 'お客様',
                reservation_data={
                    'date': reservation_date,
                    'time_slot': time_slot
                },
                refund_amount=refund_amount,
                fee=cancellation_fee
            )
        
        conn.close()
        
        print(f"✅ キャンセル完了: 払戻¥{refund_amount}")
        
        return jsonify({
            'success': True,
            'refund_id': refund.id,
            'refund_amount': refund_amount,
            'cancellation_fee': cancellation_fee,
            'message': f'キャンセル完了。¥{refund_amount}を払い戻します（手数料¥{cancellation_fee}）'
        })
        
    except Exception as e:
        print(f"❌ キャンセル処理エラー: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 400


@app.route('/api/closed-dates', methods=['GET'])
def get_closed_dates():
    """休業日一覧取得"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM closed_dates ORDER BY date')
    rows = cursor.fetchall()
    
    closed_dates = []
    for row in rows:
        closed_dates.append({
            'id': row[0],
            'date': row[1],
            'reason': row[2]
        })
    
    conn.close()
    
    return jsonify(closed_dates)


@app.route('/api/closed-dates', methods=['POST'])
def add_closed_date():
    """休業日追加"""
    try:
        data = request.json
        date = data.get('date')
        reason = data.get('reason', '休業日')
        
        # 2ヶ月先までチェック
        target_date = datetime.fromisoformat(date)
        max_date = datetime.now() + timedelta(days=60)
        
        if target_date > max_date:
            return jsonify({'error': '2ヶ月先までしか設定できません'}), 400
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO closed_dates (date, reason, created_at)
            VALUES (?, ?, ?)
        ''', (date, reason, datetime.now().isoformat()))
        
        conn.commit()
        conn.close()
        
        return jsonify({'success': True})
        
    except sqlite3.IntegrityError:
        return jsonify({'error': '既に登録されています'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/closed-dates/<int:id>', methods=['DELETE'])
def delete_closed_date(id):
    """休業日削除"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('DELETE FROM closed_dates WHERE id = ?', (id,))
    conn.commit()
    conn.close()
    
    return jsonify({'success': True})


@app.route('/')
def landing():
    """ランディングページ"""
    return render_template_string('''
<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>シャルマン鶴見市場 No.1 駐車場 | 鶴見市場駅徒歩3分</title>
    <meta name="description" content="横浜市鶴見区の月極・時間貸し駐車場。鶴見市場駅徒歩3分。屋内駐車場で雨の日も安心。">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            line-height: 1.6;
            color: #333;
        }
        
        /* ヘッダー */
        header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 20px 0;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        .header-content {
            max-width: 1200px;
            margin: 0 auto;
            padding: 0 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        h1 { font-size: 24px; }
        .reserve-btn {
            background: white;
            color: #667eea;
            padding: 12px 30px;
            border-radius: 25px;
            text-decoration: none;
            font-weight: bold;
            transition: transform 0.2s;
        }
        .reserve-btn:hover { transform: scale(1.05); }
        
        /* ヒーローセクション */
        .hero {
            background: linear-gradient(rgba(0,0,0,0.3), rgba(0,0,0,0.3)), 
                        url('data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 600"><rect fill="%23667eea" width="1200" height="600"/></svg>');
            background-size: cover;
            background-position: center;
            color: white;
            text-align: center;
            padding: 100px 20px;
        }
        .hero h2 { font-size: 42px; margin-bottom: 20px; }
        .hero p { font-size: 20px; margin-bottom: 30px; }
        .hero .cta {
            display: inline-block;
            background: #ffc107;
            color: #333;
            padding: 18px 50px;
            border-radius: 30px;
            text-decoration: none;
            font-size: 20px;
            font-weight: bold;
            box-shadow: 0 4px 15px rgba(0,0,0,0.2);
        }
        
        /* セクション共通 */
        section {
            max-width: 1200px;
            margin: 0 auto;
            padding: 60px 20px;
        }
        h3 {
            font-size: 32px;
            margin-bottom: 40px;
            text-align: center;
            color: #667eea;
        }
        
        /* 特徴 */
        .features {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 30px;
        }
        .feature {
            text-align: center;
            padding: 30px;
            background: white;
            border-radius: 10px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        .feature-icon {
            font-size: 48px;
            margin-bottom: 15px;
        }
        
        /* 料金表 */
        .pricing {
            background: #f8f9fa;
            padding: 40px;
            border-radius: 15px;
            max-width: 600px;
            margin: 0 auto;
        }
        .price-item {
            display: flex;
            justify-content: space-between;
            padding: 20px;
            border-bottom: 1px solid #ddd;
        }
        .price-item:last-child { border-bottom: none; }
        .price { font-size: 28px; font-weight: bold; color: #667eea; }
        
        /* アクセス */
        .access-info {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 40px;
            align-items: start;
        }
        .map-container {
    <script>
            width: 100%;
            height: 400px;
            border-radius: 10px;
            overflow: hidden;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        
        /* 駐車場写真 */
        .parking-image {
            width: 100%;
            max-width: 800px;
            margin: 40px auto;
            display: block;
            border-radius: 15px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.2);
        }
        
        /* スペック */
        .specs {
            background: white;
            padding: 30px;
            border-radius: 10px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        .spec-table {
            width: 100%;
            border-collapse: collapse;
        }
        .spec-table td {
            padding: 12px;
            border-bottom: 1px solid #eee;
        }
        .spec-table td:first-child {
            font-weight: bold;
            width: 150px;
        }
        
        /* フッター */
        footer {
            background: #2c3e50;
            color: white;
            padding: 40px 20px 20px;
            text-align: center;
        }
        .footer-links {
            margin-bottom: 20px;
        }
        .footer-links a {
            color: white;
            text-decoration: none;
            margin: 0 15px;
        }
        
        @media (max-width: 768px) {
            .hero h2 { font-size: 28px; }
            .access-info { grid-template-columns: 1fr; }
            .header-content { flex-direction: column; gap: 15px; }
        }
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
            <div class="feature">
                <div class="feature-icon">🅿️</div>
                <h4>平置き駐車場</h4>
                <p>停めやすい平置きタイプ。出し入れ自由。</p>
            </div>
            <div class="feature">
                <div class="feature-icon">🚃</div>
                <h4>駅近</h4>
                <p>京急鶴見市場駅から徒歩3分。アクセス抜群。</p>
            </div>
            <div class="feature">
                <div class="feature-icon">💳</div>
                <h4>簡単決済</h4>
                <p>クレジットカードでオンライン予約。現金不要。</p>
            </div>
            <div class="feature">
                <div class="feature-icon">📱</div>
                <h4>当日予約OK</h4>
                <p>スマホから簡単予約。急な用事にも対応。</p>
            </div>
        </div>
    </section>
    
    <section style="background: #f8f9fa;">
        <h3>料金</h3>
        <div class="pricing">
            <div class="price-item">
                <div>
                    <strong>午前（0時〜12時）</strong><br>
                    <small>12時間</small>
                </div>
                <div class="price">¥500</div>
            </div>
            <div class="price-item">
                <div>
                    <strong>午後（12時〜24時）</strong><br>
                    <small>12時間</small>
                </div>
                <div class="price">¥1,100</div>
            </div>
        </div>
        <p style="text-align: center; margin-top: 20px; color: #666;">
            ※キャンセルは入庫2時間前まで可能（手数料¥100）
        </p>
    </section>
    
    <section>
        <h3>駐車場の様子</h3>
        <img src="/static/parking_photo.png" alt="シャルマン鶴見市場 No.1 駐車場" class="parking-image" 
             onerror="this.style.display='none'">
        <p style="text-align: center; color: #666; margin-top: 20px;">
            平置きタイプで出し入れ簡単。建物向かって左側のスペースです。
        </p>
    </section>
    
    <section>
        <h3>駐車場スペック</h3>
        <div class="specs">
            <table class="spec-table">
                <tr>
                    <td>長さ</td>
                    <td>500cm</td>
                </tr>
                <tr>
                    <td>幅</td>
                    <td>190cm</td>
                </tr>
                <tr>
                    <td>高さ</td>
                    <td>220cm（制限なし）</td>
                </tr>
                <tr>
                    <td>重量制限</td>
                    <td>2,000kg</td>
                </tr>
                <tr>
                    <td>車室タイプ</td>
                    <td>屋外平置き</td>
                </tr>
            </table>
        </div>
    </section>
    
    <section style="background: #f8f9fa;">
        <h3>アクセス</h3>
        <div class="access-info">
            <div>
                <h4 style="margin-bottom: 15px;">所在地</h4>
                <p style="font-size: 18px; margin-bottom: 20px;">
                    〒230-0002<br>
                    神奈川県横浜市鶴見区<br>
                    市場大和町4-9
                </p>
                
                <h4 style="margin-bottom: 15px;">交通</h4>
                <p style="margin-bottom: 10px;">
                    🚃 <strong>京急鶴見市場駅</strong> 徒歩3分
                </p>
                <p>
                    🚗 首都高速横羽線「汐入IC」より車で5分
                </p>
            </div>
            <div class="map-container">
                <iframe 
                    src="https://www.google.com/maps/embed?pb=!1m18!1m12!1m3!1d3247.4781255889693!2d139.6843947735998!3d35.51718053897196!2m3!1f0!2f0!3f0!3m2!1i1024!2i768!4f13.1!3m3!1m2!1s0x60185fcae10377b3%3A0x28c708976222e79d!2z44K344Oj44Or44Oe44Oz6ba06KaL5biC5aC0!5e0!3m2!1sja!2sjp!4v1775621450603!5m2!1sja!2sjp" width="600" height="450" style="border:0;" allowfullscreen="" loading="lazy" referrerpolicy="no-referrer-when-downgrade"
                    width="100%" 
                    height="100%" 
                    style="border:0;" 
                    allowfullscreen="" 
                    loading="lazy"
                    referrerpolicy="no-referrer-when-downgrade">
                </iframe>
            </div>
        </div>
    </section>
    
    <section style="text-align: center; padding: 80px 20px;">
        <h3>今すぐ予約</h3>
        <p style="font-size: 18px; margin-bottom: 30px;">
            オンラインで簡単予約。当日利用もOK！
        </p>
        <a href="/reserve" class="cta">予約ページへ</a>
    </section>
    
    <footer>
        <div class="footer-links">
            <a href="/terms">利用規約</a>
            <a href="/privacy">プライバシーポリシー</a>
            <a href="/legal">特定商取引法</a>
            <a href="/refund">返金ポリシー</a>
        </div>
        <p style="margin-top: 20px;">
            運営: 有限会社滝沢商店<br>
            〒230-0002 神奈川県横浜市鶴見区市場大和町4-9<br>
            Email: noboru.takizawa@blueflag-sys.com
        </p>
        <p style="margin-top: 20px; color: #95a5a6;">
            &copy; 2026 有限会社滝沢商店 All rights reserved.
        </p>
    </footer>
</body>
</html>
    ''')


@app.route('/reserve')
def index():
    """予約フォーム"""
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
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #f5f5f5;
            padding: 20px;
        }
        .container { max-width: 600px; margin: 0 auto; }
        .card {
            background: white;
            border-radius: 12px;
            padding: 30px;
            margin-bottom: 20px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }
        h1 { color: #333; margin-bottom: 10px; }
        .info { color: #666; font-size: 14px; margin-bottom: 20px; }
        .policy {
            background: #fff3cd;
            border-left: 4px solid #ffc107;
            padding: 15px;
            margin: 20px 0;
            border-radius: 4px;
        }
        .policy h3 { color: #856404; margin-bottom: 10px; font-size: 16px; }
        .policy ul { margin-left: 20px; color: #856404; }
        .policy ul li { margin: 5px 0; }
        .form-group { margin-bottom: 20px; }
        label {
            display: block;
            margin-bottom: 8px;
            color: #555;
            font-weight: 500;
        }
        input, select {
            width: 100%;
            padding: 12px;
            border: 1px solid #ddd;
            border-radius: 6px;
            font-size: 14px;
        }
        .time-slots {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 15px;
        }
        .time-slot {
            border: 2px solid #ddd;
            border-radius: 8px;
            padding: 20px;
            cursor: pointer;
            transition: all 0.3s;
        }
        .time-slot:hover { border-color: #667eea; }
        .time-slot.selected {
            border-color: #667eea;
            background: #f0f4ff;
        }
        .time-slot.unavailable {
            opacity: 0.5;
            cursor: not-allowed;
            background: #f5f5f5;
        }
        .slot-time { font-weight: bold; color: #333; margin-bottom: 5px; }
        .slot-price { color: #667eea; font-size: 18px; font-weight: bold; }
        button {
            width: 100%;
            padding: 16px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            border-radius: 8px;
            font-size: 16px;
            font-weight: bold;
            cursor: pointer;
        }
        button:hover { opacity: 0.9; }
        button:disabled { opacity: 0.5; cursor: not-allowed; }
        #card-element {
            padding: 12px;
            border: 1px solid #ddd;
            border-radius: 6px;
        }
        .error { color: #e74c3c; margin-top: 10px; font-size: 14px; }
        .success { color: #27ae60; margin-top: 10px; font-size: 14px; }
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
            <h3 style="color: #333; margin-bottom: 15px;">📐 車室サイズ</h3>
            <table style="width: 100%; border-collapse: collapse;">
                <tr>
                    <td style="padding: 8px; border-bottom: 1px solid #eee; width: 40%;"><strong>長さ</strong></td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">500cm</td>
                </tr>
                <tr>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;"><strong>幅</strong></td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">190cm</td>
                </tr>
                <tr>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;"><strong>高さ</strong></td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">220cm</td>
                </tr>
                <tr>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;"><strong>重量制限</strong></td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">2,000kg</td>
                </tr>
            </table>
            <p style="color: #999; font-size: 12px; margin-top: 10px;">※サイズを超える車両は駐車できません</p>
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
                <div class="form-group">
                    <label>ご利用日</label>
                    <input type="date" id="date" required>
                </div>

                <div class="form-group">
                    <label>時間帯</label>
                    <div class="time-slots" id="time-slots">
                        <div class="time-slot" data-slot="morning">
                            <div class="slot-time">0:00 〜 12:00</div>
                            <div class="slot-price">¥500</div>
                        </div>
                        <div class="time-slot" data-slot="afternoon">
                            <div class="slot-time">12:00 〜 24:00</div>
                            <div class="slot-price">¥1,100</div>
                        </div>
                    </div>
                    <input type="hidden" id="time_slot" required>
                </div>

                <div class="form-group">
                    <label>車両番号</label>
                    <input type="text" id="car_number" placeholder="横浜 303 あ　1100" required>
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
    
    <footer style="max-width: 600px; margin: 40px auto 20px; text-align: center; color: #666; font-size: 14px; padding: 20px; border-top: 1px solid #ddd;">
        <p style="margin-bottom: 10px;">
            <a href="/terms" style="color: #667eea; text-decoration: none; margin: 0 10px;">利用規約</a> | 
            <a href="/privacy" style="color: #667eea; text-decoration: none; margin: 0 10px;">プライバシーポリシー</a> | 
            <a href="/legal" style="color: #667eea; text-decoration: none; margin: 0 10px;">特定商取引法</a> | 
            <a href="/refund" style="color: #667eea; text-decoration: none; margin: 0 10px;">返金ポリシー</a>
        </p>
        <p>&copy; 2026 有限会社滝沢商店 All rights reserved.</p>
    </footer>

    <script>
        const stripe = Stripe('{{ stripe_public_key }}');
        const elements = stripe.elements();
        const cardElement = elements.create('card', {
            hidePostalCode: true  // 郵便番号を非表示
        });
        cardElement.mount('#card-element');

        const dateInput = document.getElementById('date');
        const timeSlots = document.querySelectorAll('.time-slot');
        const timeSlotInput = document.getElementById('time_slot');

        // 当日から予約可能に変更
        const today = new Date();
        dateInput.min = today.toISOString().split('T')[0];

        // 2ヶ月後まで
        const maxDate = new Date();
        maxDate.setDate(maxDate.getDate() + 60);
        dateInput.max = maxDate.toISOString().split('T')[0];

        dateInput.addEventListener('change', checkAvailability);

        async function checkAvailability() {
            const date = dateInput.value;
            if (!date) return;

            for (const slot of timeSlots) {
                const timeSlot = slot.dataset.slot;
                
                try {
                    const controller = new AbortController();
                    const timeout = setTimeout(() => controller.abort(), 10000); // 10秒タイムアウト
                    
                    const response = await fetch('/api/check-availability', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({date, time_slot: timeSlot}),
                        signal: controller.signal
                    });
                    
                    clearTimeout(timeout);

                    if (!response.ok) {
                        throw new Error(`HTTP ${response.status}`);
                    }

                    const data = await response.json();
                    
                    if (!data.available) {
                        slot.classList.add('unavailable');
                        slot.style.pointerEvents = 'none';
                    } else {
                        slot.classList.remove('unavailable');
                        slot.style.pointerEvents = 'auto';
                    }
                } catch (error) {
                    console.error('空き確認エラー:', error);
                    // エラー時は念のため予約不可にする
                    slot.classList.add('unavailable');
                    slot.style.pointerEvents = 'none';
                    
                    if (error.name === 'AbortError') {
                        alert('通信がタイムアウトしました。ページを再読み込みしてください。');
                    }
                }
            }
        }

        timeSlots.forEach(slot => {
            slot.addEventListener('click', () => {
                if (slot.classList.contains('unavailable')) return;
                
                timeSlots.forEach(s => s.classList.remove('selected'));
                slot.classList.add('selected');
                timeSlotInput.value = slot.dataset.slot;
            });
        });

        let isSubmitting = false; // 二重送信防止フラグ

        document.getElementById('reservation-form').addEventListener('submit', async (e) => {
            e.preventDefault();
            
            // 二重送信防止
            if (isSubmitting) {
                console.log('処理中のため送信をスキップ');
                return;
            }
            
            const submitButton = document.getElementById('submit-button');
            const messageDiv = document.getElementById('message');
            
            isSubmitting = true;
            submitButton.disabled = true;
            submitButton.textContent = '処理中...';
            messageDiv.innerHTML = '';

            const formData = {
                date: dateInput.value,
                time_slot: timeSlotInput.value,
                car_number: document.getElementById('car_number').value,
                customer_name: document.getElementById('customer_name').value,
                phone: document.getElementById('phone').value,
                email: document.getElementById('email').value
            };

            try {
                // PaymentIntent作成（タイムアウト付き）
                const controller = new AbortController();
                const timeout = setTimeout(() => controller.abort(), 30000); // 30秒タイムアウト
                
                const response = await fetch('/api/create-payment-intent', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(formData),
                    signal: controller.signal
                });
                
                clearTimeout(timeout);

                if (!response.ok) {
                    const errorData = await response.json().catch(() => ({}));
                    throw new Error(errorData.error || `サーバーエラー (${response.status})`);
                }

                const {clientSecret} = await response.json();

                // Stripe決済実行
                submitButton.textContent = '決済処理中...';
                
                const {error, paymentIntent} = await stripe.confirmCardPayment(clientSecret, {
                    payment_method: {card: cardElement}
                });

                if (error) {
                    // Stripeエラー
                    let errorMessage = error.message;
                    if (error.type === 'card_error') {
                        errorMessage = 'カード情報に問題があります。確認してください。';
                    } else if (error.type === 'validation_error') {
                        errorMessage = '入力内容に不備があります。';
                    }
                    messageDiv.innerHTML = `<p class="error">${errorMessage}</p>`;
                } else {
                    // 成功
                    messageDiv.innerHTML = `<p class="success">予約が完了しました！<br>決済ID: ${paymentIntent.id}</p>`;
                    
                    // フォームをリセット
                    document.getElementById('reservation-form').reset();
                    timeSlots.forEach(s => s.classList.remove('selected'));
                    
                    // 3秒後にリロード
                    setTimeout(() => location.reload(), 3000);
                }
            } catch (err) {
                console.error('予約エラー:', err);
                
                let errorMessage = 'エラーが発生しました。';
                
                if (err.name === 'AbortError') {
                    errorMessage = '通信がタイムアウトしました。もう一度お試しください。';
                } else if (err.message) {
                    errorMessage = err.message;
                }
                
                messageDiv.innerHTML = `<p class="error">${errorMessage}</p>`;
            } finally {
                isSubmitting = false;
                submitButton.disabled = false;
                submitButton.textContent = '予約を確定する';
            }
        });
    </script>
</body>
</html>
    ''', stripe_public_key=STRIPE_PUBLIC_KEY)


@app.route('/cancel')
def cancel_page():
    """キャンセルページ"""
    return render_template_string('''
<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <title>予約キャンセル</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: sans-serif;
            background: #f5f5f5;
            padding: 20px;
        }
        .container { max-width: 600px; margin: 0 auto; }
        .card {
            background: white;
            padding: 30px;
            border-radius: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }
        h1 { margin-bottom: 20px; }
        .form-group { margin-bottom: 20px; }
        label { display: block; margin-bottom: 8px; font-weight: 500; }
        input {
            width: 100%;
            padding: 12px;
            border: 1px solid #ddd;
            border-radius: 6px;
        }
        button {
            width: 100%;
            padding: 16px;
            background: #e74c3c;
            color: white;
            border: none;
            border-radius: 8px;
            font-size: 16px;
            font-weight: bold;
            cursor: pointer;
        }
        button:hover { opacity: 0.9; }
        .notice {
            background: #fff3cd;
            border-left: 4px solid #ffc107;
            padding: 15px;
            margin-bottom: 20px;
            border-radius: 4px;
        }
        .error { color: #e74c3c; margin-top: 10px; }
        .success { color: #27ae60; margin-top: 10px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="card">
            <h1>予約キャンセル</h1>
            
            <div class="notice">
                <strong>キャンセル手数料: ¥100</strong><br>
                入庫2時間前までキャンセル可能です。
            </div>
            
            <div class="form-group">
                <label>決済ID</label>
                <input type="text" id="payment_id" placeholder="pi_xxxxxxxxxxxxx" required>
                <small>予約完了時に表示された決済IDを入力してください</small>
            </div>
            
            <button onclick="cancelReservation()">キャンセルする</button>
            <div id="message"></div>
        </div>
    </div>
    
    <script>
        let isCancelling = false; // 二重送信防止
        
        async function cancelReservation() {
            // 二重送信防止
            if (isCancelling) {
                console.log('処理中のため送信をスキップ');
                return;
            }
            
            const paymentId = document.getElementById('payment_id').value;
            const messageDiv = document.getElementById('message');
            const cancelButton = document.querySelector('button');
            
            if (!paymentId) {
                messageDiv.innerHTML = '<p class="error">決済IDを入力してください</p>';
                return;
            }
            
            isCancelling = true;
            cancelButton.disabled = true;
            cancelButton.textContent = '処理中...';
            messageDiv.innerHTML = '';
            
            try {
                const response = await fetch('/api/cancel-reservation', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({payment_id: paymentId})
                });
                
                const data = await response.json();
                
                if (response.ok) {
                    messageDiv.innerHTML = `<p class="success">${data.message}</p>`;
                    // 成功したらボタンを無効化したまま
                } else {
                    messageDiv.innerHTML = `<p class="error">${data.error}</p>`;
                    // エラー時はボタンを再有効化
                    isCancelling = false;
                    cancelButton.disabled = false;
                    cancelButton.textContent = 'キャンセルする';
                }
            } catch (err) {
                messageDiv.innerHTML = '<p class="error">エラーが発生しました</p>';
                isCancelling = false;
                cancelButton.disabled = false;
                cancelButton.textContent = 'キャンセルする';
            }
        }
    </script>
</body>
</html>
    ''')


@app.route('/terms')
def terms():
    """利用規約"""
    return render_template_string('''
<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <title>利用規約 - シャルマン鶴見市場 No.1</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body { font-family: sans-serif; line-height: 1.8; max-width: 800px; margin: 0 auto; padding: 20px; }
        h1 { border-bottom: 3px solid #667eea; padding-bottom: 10px; }
        h2 { color: #667eea; margin-top: 30px; }
        .back { display: inline-block; margin-bottom: 20px; color: #667eea; text-decoration: none; }
    </style>
</head>
<body>
    <a href="/" class="back">← トップに戻る</a>
    
    <h1>利用規約</h1>
    <p>最終更新日: 2026年4月6日</p>
    
    <h2>第1条（適用）</h2>
    <p>本規約は、有限会社滝沢商店（以下「当社」）が運営する駐車場予約サービス（以下「本サービス」）の利用条件を定めるものです。</p>
    
    <h2>第2条（予約）</h2>
    <p>1. 利用者は、本サービスを通じて駐車場の予約を行うことができます。</p>
    <p>2. 予約の成立は、決済完了時点とします。</p>
    <p>3. 予約確定後、登録メールアドレスに確認メールを送信します。</p>
    
    <h2>第3条（料金）</h2>
    <p>1. 駐車料金は以下の通りです：</p>
    <ul>
        <li>午前（0時-12時）: 500円</li>
        <li>午後（12時-24時）: 1,100円</li>
    </ul>
    <p>2. 料金は予約時にクレジットカードで事前決済されます。</p>
    
    <h2>第4条（キャンセル）</h2>
    <p>1. 入庫予定時刻の2時間前までキャンセル可能です。</p>
    <p>2. キャンセル手数料として100円を申し受けます。</p>
    <p>3. 2時間前を過ぎた場合、キャンセル不可・全額収納となります。</p>
    <p>4. 返金は3-5営業日以内にクレジットカードに処理されます。</p>
    
    <h2>第5条（禁止事項）</h2>
    <p>利用者は以下の行為を行ってはなりません：</p>
    <ul>
        <li>虚偽の情報による予約</li>
        <li>他人のクレジットカードの不正使用</li>
        <li>予約枠の転売</li>
        <li>駐車場設備の破損・汚損</li>
    </ul>
    
    <h2>第6条（免責）</h2>
    <p>1. 当社は、駐車中の車両の盗難・損傷について責任を負いません。</p>
    <p>2. システム障害等により予約が正常に処理されない場合、速やかに返金対応いたします。</p>
    
    <h2>第7条（連絡先）</h2>
    <p>
        有限会社滝沢商店<br>
        〒230-0002 神奈川県横浜市鶴見区市場大和町4-9<br>
        Email: noboru.takizawa@blueflag-sys.com
    </p>
</body>
</html>
    ''')


@app.route('/privacy')
def privacy():
    """プライバシーポリシー"""
    return render_template_string('''
<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <title>プライバシーポリシー - シャルマン鶴見市場 No.1</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body { font-family: sans-serif; line-height: 1.8; max-width: 800px; margin: 0 auto; padding: 20px; }
        h1 { border-bottom: 3px solid #667eea; padding-bottom: 10px; }
        h2 { color: #667eea; margin-top: 30px; }
        .back { display: inline-block; margin-bottom: 20px; color: #667eea; text-decoration: none; }
    </style>
</head>
<body>
    <a href="/" class="back">← トップに戻る</a>
    
    <h1>プライバシーポリシー</h1>
    <p>最終更新日: 2026年4月6日</p>
    
    <h2>1. 収集する情報</h2>
    <p>当社は、本サービスの提供にあたり以下の情報を収集します：</p>
    <ul>
        <li>氏名</li>
        <li>メールアドレス</li>
        <li>電話番号</li>
        <li>車両番号</li>
        <li>決済情報（クレジットカード情報はStripe社が管理）</li>
    </ul>
    
    <h2>2. 利用目的</h2>
    <p>収集した個人情報は以下の目的で利用します：</p>
    <ul>
        <li>予約管理・確認</li>
        <li>予約確認メール・キャンセル通知の送信</li>
        <li>決済処理</li>
        <li>お問い合わせ対応</li>
    </ul>
    
    <h2>3. 第三者提供</h2>
    <p>当社は、以下の場合を除き、個人情報を第三者に提供しません：</p>
    <ul>
        <li>ご本人の同意がある場合</li>
        <li>法令に基づく場合</li>
        <li>決済処理のためStripe社に提供する場合</li>
    </ul>
    
    <h2>4. 安全管理措置</h2>
    <p>当社は、個人情報の漏洩・滅失・毀損を防止するため、適切な安全管理措置を講じます。</p>
    
    <h2>5. 開示・訂正・削除</h2>
    <p>ご本人からの個人情報の開示・訂正・削除の請求には、速やかに対応いたします。</p>
    
    <h2>6. お問い合わせ</h2>
    <p>
        個人情報に関するお問い合わせ：<br>
        有限会社滝沢商店<br>
        Email: noboru.takizawa@blueflag-sys.com
    </p>
</body>
</html>
    ''')


@app.route('/legal')
def legal():
    """特定商取引法に基づく表記"""
    return render_template_string('''
<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <title>特定商取引法に基づく表記 - シャルマン鶴見市場 No.1</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body { font-family: sans-serif; line-height: 1.8; max-width: 800px; margin: 0 auto; padding: 20px; }
        h1 { border-bottom: 3px solid #667eea; padding-bottom: 10px; }
        table { width: 100%; border-collapse: collapse; margin: 20px 0; }
        th, td { border: 1px solid #ddd; padding: 12px; text-align: left; }
        th { background: #f5f5f5; width: 200px; }
        .back { display: inline-block; margin-bottom: 20px; color: #667eea; text-decoration: none; }
    </style>
</head>
<body>
    <a href="/" class="back">← トップに戻る</a>
    
    <h1>特定商取引法に基づく表記</h1>
    
    <table>
        <tr>
            <th>事業者名</th>
            <td>有限会社滝沢商店</td>
        </tr>
        <tr>
            <th>代表者</th>
            <td>滝沢 登</td>
        </tr>
        <tr>
            <th>所在地</th>
            <td>〒230-0002 神奈川県横浜市鶴見区市場大和町4-9</td>
        </tr>
        <tr>
            <th>電話番号</th>
            <td>090-6137-9489</td>
        </tr>
        <tr>
            <th>メールアドレス</th>
            <td>noboru.takizawa@blueflag-sys.com</td>
        </tr>
        <tr>
            <th>販売価格</th>
            <td>午前（0-12時）: 500円<br>午後（12-24時）: 1,100円</td>
        </tr>
        <tr>
            <th>支払方法</th>
            <td>クレジットカード（Stripe決済）</td>
        </tr>
        <tr>
            <th>支払時期</th>
            <td>予約確定時に即時決済</td>
        </tr>
        <tr>
            <th>サービス提供時期</th>
            <td>予約日当日</td>
        </tr>
        <tr>
            <th>キャンセル・返金</th>
            <td>入庫2時間前までキャンセル可能（手数料100円）<br>2時間を切った場合はキャンセル不可</td>
        </tr>
    </table>
</body>
</html>
    ''')


@app.route('/refund')
def refund_policy():
    """返金ポリシー"""
    return render_template_string('''
<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <title>返金ポリシー - シャルマン鶴見市場 No.1</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body { font-family: sans-serif; line-height: 1.8; max-width: 800px; margin: 0 auto; padding: 20px; }
        h1 { border-bottom: 3px solid #667eea; padding-bottom: 10px; }
        h2 { color: #667eea; margin-top: 30px; }
        .back { display: inline-block; margin-bottom: 20px; color: #667eea; text-decoration: none; }
        .notice { background: #fff3cd; border-left: 4px solid #ffc107; padding: 15px; margin: 20px 0; }
    </style>
</head>
<body>
    <a href="/" class="back">← トップに戻る</a>
    
    <h1>返金ポリシー</h1>
    
    <h2>キャンセル・返金について</h2>
    
    <div class="notice">
        <strong>入庫2時間前までのキャンセル</strong>
        <ul>
            <li>キャンセル可能</li>
            <li>キャンセル手数料: 100円</li>
            <li>返金額: 支払額 - 100円</li>
        </ul>
    </div>
    
    <div class="notice">
        <strong>入庫2時間前を過ぎた場合</strong>
        <ul>
            <li>キャンセル不可</li>
            <li>返金なし（全額収納）</li>
        </ul>
    </div>
    
    <h2>返金処理について</h2>
    <p>1. 返金はクレジットカードへの返金となります。</p>
    <p>2. 返金処理は3-5営業日以内に完了します。</p>
    <p>3. カード会社の処理により、実際の返金まで1-2週間かかる場合があります。</p>
    
    <h2>キャンセル方法</h2>
    <p>予約確認メールに記載されているキャンセルページから手続きできます。</p>
    <p>キャンセルページ: <a href="/cancel">こちら</a></p>
    
    <h2>システム障害による返金</h2>
    <p>システム障害等により予約が正常に処理されない場合、全額返金いたします。</p>
    
    <h2>お問い合わせ</h2>
    <p>
        返金に関するお問い合わせ：<br>
        Email: noboru.takizawa@blueflag-sys.com<br>
        電話: 090-6137-1111
    </p>
</body>
</html>
    ''')


@app.route('/admin')
def admin():
    """管理画面"""
    return render_template_string('''
<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <title>管理画面</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: sans-serif; padding: 20px; background: #f5f5f5; }
        .container { max-width: 1200px; margin: 0 auto; }
        h1 { margin-bottom: 20px; }
        .section {
            background: white;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 20px;
        }
        table { width: 100%; border-collapse: collapse; }
        th, td { border: 1px solid #ddd; padding: 12px; text-align: left; }
        th { background: #667eea; color: white; }
        button {
            padding: 8px 16px;
            background: #667eea;
            color: white;
            border: none;
            border-radius: 4px;
            cursor: pointer;
        }
        button:hover { opacity: 0.9; }
        button.danger { background: #e74c3c; }
        input[type="date"] {
            padding: 8px;
            border: 1px solid #ddd;
            border-radius: 4px;
            margin-right: 10px;
        }
        .tabs {
            display: flex;
            gap: 10px;
            margin-bottom: 20px;
        }
        .tab {
            padding: 10px 20px;
            background: #ddd;
            border-radius: 4px;
            cursor: pointer;
        }
        .tab.active { background: #667eea; color: white; }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🅿️ 管理画面</h1>

        <div class="tabs">
            <div class="tab active" onclick="switchTab('reservations')">予約一覧</div>
            <div class="tab" onclick="switchTab('closed')">休業日設定</div>
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
                <input type="date" id="new-closed-date">
                <button onclick="addClosedDate()">追加</button>
            </div>

            <div class="section">
                <h2>休業日一覧</h2>
                <div id="closed-dates-list"></div>
            </div>
        </div>
    </div>

    <script>
        function switchTab(tab) {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
            event.target.classList.add('active');
            document.getElementById(tab).classList.add('active');

            if (tab === 'reservations') loadReservations();
            if (tab === 'closed') loadClosedDates();
        }

        async function loadReservations() {
            const response = await fetch('/api/reservations');
            const data = await response.json();

            let html = '<table><tr><th>日付</th><th>時間帯</th><th>車両番号</th><th>氏名</th><th>金額</th></tr>';
            data.reservations.forEach(r => {
                const slot = r.time_slot === 'morning' ? '0-12時' : '12-24時';
                html += `<tr>
                    <td>${r.date}</td>
                    <td>${slot}</td>
                    <td>${r.car_number}</td>
                    <td>${r.customer_name}</td>
                    <td>¥${r.amount}</td>
                </tr>`;
            });
            html += '</table>';
            document.getElementById('reservations-list').innerHTML = html;
        }

        async function loadClosedDates() {
            const response = await fetch('/api/closed-dates');
            const data = await response.json();

            let html = '<table><tr><th>日付</th><th>理由</th><th>操作</th></tr>';
            data.forEach(d => {
                html += `<tr>
                    <td>${d.date}</td>
                    <td>${d.reason}</td>
                    <td><button class="danger" onclick="deleteClosedDate(${d.id})">削除</button></td>
                </tr>`;
            });
            html += '</table>';
            document.getElementById('closed-dates-list').innerHTML = html;
        }

        async function addClosedDate() {
            const date = document.getElementById('new-closed-date').value;
            if (!date) return alert('日付を選択してください');

            const response = await fetch('/api/closed-dates', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({date, reason: '休業日'})
            });

            if (response.ok) {
                alert('追加しました');
                loadClosedDates();
            } else {
                const error = await response.json();
                alert(error.error);
            }
        }

        async function deleteClosedDate(id) {
            if (!confirm('削除しますか？')) return;

            await fetch(`/api/closed-dates/${id}`, {method: 'DELETE'});
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
    """駐車場写真を配信"""
    # カレントディレクトリのstaticフォルダから配信
    photo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'parking_photo.png')
    if os.path.exists(photo_path):
        return send_file(photo_path, mimetype='image/png')
    # ファイルがない場合は404
    return 'Photo not found', 404


if __name__ == '__main__':
    print("=" * 60)
    print("🚀 駐車場予約システム（実運用版）")
    print("=" * 60)
    print("📍 予約フォーム: http://localhost:5000/")
    print("📊 管理画面: http://localhost:5000/admin")
    print("\n料金:")
    print("  0:00-12:00  → ¥500")
    print("  12:00-24:00 → ¥1,100")
    print("\nキャンセル:")
    print("  入庫2時間前まで無料")
    print("=" * 60)
    
    app.run(host='0.0.0.0', port=5000, debug=True)
