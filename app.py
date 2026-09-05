"""
UPI Auto-Payment Verifier – Vercel Serverless Edition
Complete error handling, safe Supabase/Gmail fallback, premium UI with success screen.
"""

import os
import re
import time
import json
import logging
import sys
from io import BytesIO
from datetime import datetime, timedelta, timezone

from flask import Flask, request, jsonify, send_file, render_template_string
from flask_cors import CORS

# Optional imports with safe fallback
try:
    import imaplib
    import email
    from email.header import decode_header
except ImportError:
    imaplib = None
    email = None
    decode_header = None

try:
    import secrets
except ImportError:
    secrets = None

try:
    import qrcode
except ImportError:
    qrcode = None

# ============================================
# CONFIG (read from environment, fallback defaults)
# ============================================
CONFIG = {
    'UPI_ID': os.getenv('UPI_ID', '9304619487@fam'),
    'PAYEE_NAME': os.getenv('PAYEE_NAME', 'Md Nooralam'),
    'GMAIL_APP_PASSWORD': os.getenv('GMAIL_APP_PASSWORD', 'owjwtlotkfjnsftm'),
    'GMAIL_EMAIL': os.getenv('GMAIL_EMAIL', 'nkg166465@gmail.com'),
    'TIME_WINDOW_MINUTES': int(os.getenv('TIME_WINDOW_MINUTES', 5)),
    'ADMIN_API_KEY': os.getenv('ADMIN_API_KEY', 'admin_1234567890'),
    'MAX_EMAILS_CHECK': int(os.getenv('MAX_EMAILS_CHECK', 50)),
    'SUPABASE_URL': os.getenv('SUPABASE_URL'),
    'SUPABASE_KEY': os.getenv('SUPABASE_KEY'),
}

# ============================================
# LOGGING (write to stderr for Vercel logs)
# ============================================
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

# ============================================
# FLASK APP
# ============================================
app = Flask(__name__)
CORS(app)

# ============================================
# GLOBAL EXCEPTION HANDLER (returns JSON for API, HTML for pages)
# ============================================
@app.errorhandler(Exception)
def handle_exception(e):
    logger.error(f"Unhandled exception: {e}", exc_info=True)
    if request.path.startswith('/api/') or request.path.startswith('/verify-') or request.path.startswith('/admin_'):
        return jsonify({
            'status': 'error',
            'message': 'Internal server error',
            'detail': str(e) if app.debug else None
        }), 500
    return render_template_string('''
        <!DOCTYPE html>
        <html>
        <head><title>Error</title>
        <style>body{font-family:sans-serif;text-align:center;padding:2rem;background:#e8f4fd;color:#1a202c;}</style>
        </head>
        <body>
        <h1>⚠️ Something went wrong</h1>
        <p>Please try again later.</p>
        <p><a href="/" style="color:#4a6cf7;">Go Home</a></p>
        </body>
        </html>
    ''', 500)

# ============================================
# SUPABASE CLIENT (optional, with safe fallback)
# ============================================
supabase_client = None
try:
    from supabase import create_client
    if CONFIG['SUPABASE_URL'] and CONFIG['SUPABASE_KEY']:
        supabase_client = create_client(CONFIG['SUPABASE_URL'], CONFIG['SUPABASE_KEY'])
        logger.info("Supabase client initialized.")
    else:
        logger.warning("Supabase credentials missing; using in‑memory fallback.")
except ImportError:
    logger.warning("supabase package not installed; using in‑memory fallback.")
except Exception as e:
    logger.error(f"Supabase init error: {e}; using in‑memory fallback.")

# ============================================
# FALLBACK STORAGE (in‑memory)
# ============================================
app.fallback_orders = {}
app.fallback_api_keys = {}
app.fallback_utrs = {}

# ============================================
# HELPER: timezone‑aware IST
# ============================================
IST = timezone(timedelta(hours=5, minutes=30))
def now_ist():
    return datetime.now(IST)

def format_ist(dt):
    return dt.strftime('%d-%m-%Y %H:%M:%S')

# ============================================
# DATABASE HELPERS (Supabase + fallback)
# ============================================
def db_create_order(api_key, amount):
    try:
        order_id = f"Khan_{secrets.token_hex(4).upper()}" if secrets else f"Khan_{int(time.time())}"
    except:
        order_id = f"Khan_{int(time.time())}"
    now_utc = datetime.now(timezone.utc)
    now_ist_dt = now_utc.astimezone(IST)
    expires_utc = now_utc + timedelta(minutes=CONFIG['TIME_WINDOW_MINUTES'])
    expires_ist_dt = expires_utc.astimezone(IST)
    created_at_ist = format_ist(now_ist_dt)
    expires_at_ist = format_ist(expires_ist_dt)
    data = {
        'order_id': order_id,
        'api_key': api_key,
        'amount': amount,
        'payable_amount': amount,
        'status': 'pending',
        'created_at': created_at_ist,
        'expires_at': expires_at_ist,
        'utr': None,
        'transaction_id': None,
        'sender_name': None,
        'payment_time': None,
        'verified_at': None
    }
    if supabase_client:
        try:
            supabase_client.table('orders').insert(data).execute()
            logger.info(f"Order {order_id} created in Supabase.")
        except Exception as e:
            logger.error(f"Supabase insert error: {e}")
            app.fallback_orders[order_id] = data
    else:
        app.fallback_orders[order_id] = data
    return order_id

def db_get_order(order_id):
    if supabase_client:
        try:
            result = supabase_client.table('orders').select('*').eq('order_id', order_id).execute()
            if result.data:
                return result.data[0]
        except Exception as e:
            logger.error(f"Supabase get error: {e}")
    return app.fallback_orders.get(order_id)

def db_update_order(order_id, **kwargs):
    if supabase_client:
        try:
            supabase_client.table('orders').update(kwargs).eq('order_id', order_id).execute()
        except Exception as e:
            logger.error(f"Supabase update error: {e}")
            if order_id in app.fallback_orders:
                app.fallback_orders[order_id].update(kwargs)
    else:
        if order_id in app.fallback_orders:
            app.fallback_orders[order_id].update(kwargs)

def db_create_api_key(name, expiry_hours=24):
    try:
        api_key = f"fam_{secrets.token_hex(20)}" if secrets else f"fam_{int(time.time())}"
    except:
        api_key = f"fam_{int(time.time())}"
    now_utc = datetime.now(timezone.utc)
    expires_utc = now_utc + timedelta(hours=expiry_hours)
    data = {
        'api_key': api_key,
        'name': name,
        'created_at': now_utc.isoformat(),
        'expires_at': expires_utc.isoformat(),
        'is_active': 1
    }
    if supabase_client:
        try:
            supabase_client.table('api_keys').insert(data).execute()
        except Exception as e:
            logger.error(f"Supabase insert api_key error: {e}")
            app.fallback_api_keys[api_key] = data
    else:
        app.fallback_api_keys[api_key] = data
    return api_key

def db_validate_api_key(api_key):
    if supabase_client:
        try:
            result = supabase_client.table('api_keys').select('*').eq('api_key', api_key).eq('is_active', 1).execute()
            if result.data:
                key = result.data[0]
                if datetime.now(timezone.utc).isoformat() < key['expires_at']:
                    return key
            return None
        except Exception as e:
            logger.error(f"Supabase validate error: {e}")
    key = app.fallback_api_keys.get(api_key)
    if key and key['is_active'] == 1 and datetime.now(timezone.utc).isoformat() < key['expires_at']:
        return key
    return None

def db_is_utr_verified(utr):
    if supabase_client:
        try:
            result = supabase_client.table('verified_utrs').select('*').eq('utr', utr).execute()
            return len(result.data) > 0
        except Exception as e:
            logger.error(f"Supabase verify utr error: {e}")
    return utr in app.fallback_utrs

def db_mark_utr_verified(utr, order_id):
    data = {'utr': utr, 'order_id': order_id, 'verified_at': datetime.now(timezone.utc).isoformat()}
    if supabase_client:
        try:
            supabase_client.table('verified_utrs').insert(data).execute()
        except Exception as e:
            logger.error(f"Supabase mark utr error: {e}")
            app.fallback_utrs[utr] = data
    else:
        app.fallback_utrs[utr] = data

def db_get_pending_orders():
    if supabase_client:
        try:
            result = supabase_client.table('orders').select('*').eq('status', 'pending').execute()
            return result.data
        except Exception as e:
            logger.error(f"Supabase get pending error: {e}")
    return [o for o in app.fallback_orders.values() if o['status'] == 'pending']

def db_update_api_key(api_key, **kwargs):
    if supabase_client:
        try:
            supabase_client.table('api_keys').update(kwargs).eq('api_key', api_key).execute()
        except Exception as e:
            logger.error(f"Supabase update api_key error: {e}")
            if api_key in app.fallback_api_keys:
                app.fallback_api_keys[api_key].update(kwargs)
    else:
        if api_key in app.fallback_api_keys:
            app.fallback_api_keys[api_key].update(kwargs)

# ============================================
# GMAIL VERIFICATION (on-demand, with safe fallback)
# ============================================
def connect_imap():
    if not CONFIG['GMAIL_EMAIL'] or not CONFIG['GMAIL_APP_PASSWORD']:
        raise Exception("Gmail credentials not configured")
    if imaplib is None:
        raise Exception("IMAP library not available")
    mail = imaplib.IMAP4_SSL('imap.gmail.com')
    mail.login(CONFIG['GMAIL_EMAIL'], CONFIG['GMAIL_APP_PASSWORD'])
    mail.select('INBOX')
    return mail

def get_email_body(mail, msg_id):
    try:
        result, data = mail.fetch(msg_id, '(RFC822)')
        if result != 'OK':
            return ''
        raw = data[0][1]
        msg = email.message_from_bytes(raw)
        body = ''
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == 'text/plain' and 'attachment' not in str(part.get('Content-Disposition')):
                    try:
                        body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                        break
                    except:
                        continue
        else:
            try:
                body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
            except:
                pass
        return body
    except:
        return ''

def parse_payment_email(body):
    details = {'amount': None, 'utr': None, 'transaction_id': None, 'sender': None,
               'date': None, 'type': None, 'payment_datetime': None, 'time_diff_minutes': None}
    if 'successfully received' in body.lower():
        details['type'] = 'received'
    elif 'successfully paid' in body.lower():
        details['type'] = 'paid'
        return details
    patterns = [r'₹([0-9]+(\.[0-9]+)?)', r'Amount\s*[:]\s*₹([0-9]+(\.[0-9]+)?)']
    for p in patterns:
        m = re.search(p, body, re.IGNORECASE)
        if m:
            details['amount'] = float(m.group(1))
            break
    utr_patterns = [r'UTR\s*[:]\s*([0-9]+)', r'UTR\s*([0-9]+)']
    for p in utr_patterns:
        m = re.search(p, body, re.IGNORECASE)
        if m:
            details['utr'] = m.group(1)
            break
    tx_patterns = [r'Transaction ID\s*[:]\s*([A-Z0-9]+)', r'Txn\s*[:]\s*([A-Z0-9]+)']
    for p in tx_patterns:
        m = re.search(p, body, re.IGNORECASE)
        if m:
            details['transaction_id'] = m.group(1)
            break
    sender_match = re.search(r'from\s*([A-Za-z\s.]+)', body, re.IGNORECASE)
    if sender_match:
        details['sender'] = sender_match.group(1).strip()
    date_match = re.search(r'([0-9]{2}:[0-9]{2}\s*(AM|PM)\s*IST,\s*[0-9]{2}\s*[A-Za-z]+\s*[0-9]{4})', body, re.IGNORECASE)
    if date_match:
        details['date'] = date_match.group(1)
        try:
            time_str = date_match.group(1)
            time_part = re.search(r'([0-9]{2}:[0-9]{2})\s*(AM|PM)', time_str)
            if time_part:
                hour, minute = map(int, time_part.group(1).split(':'))
                ampm = time_part.group(2)
                if ampm == 'PM' and hour != 12:
                    hour += 12
                elif ampm == 'AM' and hour == 12:
                    hour = 0
                now_utc = datetime.now(timezone.utc)
                now_ist = now_utc.astimezone(IST)
                dt = datetime(now_ist.year, now_ist.month, now_ist.day, hour, minute, tzinfo=IST)
                if dt > now_ist:
                    dt -= timedelta(days=1)
                details['payment_datetime'] = dt.isoformat()
                details['time_diff_minutes'] = round((now_ist - dt).total_seconds() / 60, 1)
        except:
            pass
    return details

def search_gmail_payment(amount=None, utr=None, time_window=None):
    if time_window is None:
        time_window = CONFIG['TIME_WINDOW_MINUTES']
    mail = None
    try:
        mail = connect_imap()
        result, data = mail.search(None, 'ALL')
        if result != 'OK' or not data[0]:
            return None
        ids = data[0].split()
        recent_ids = ids[-CONFIG['MAX_EMAILS_CHECK']:]
        now_ist = datetime.now(IST)
        for msg_id in recent_ids:
            msg_id_str = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
            body = get_email_body(mail, msg_id_str)
            if not body:
                continue
            details = parse_payment_email(body)
            if details.get('type') != 'received':
                continue
            if details.get('payment_datetime'):
                dt = datetime.fromisoformat(details['payment_datetime'])
                if (now_ist - dt).total_seconds() / 60 > time_window:
                    continue
            elif details.get('time_diff_minutes') is not None and details['time_diff_minutes'] > time_window:
                continue
            else:
                try:
                    result2, data2 = mail.fetch(msg_id, '(BODY.PEEK[HEADER.FIELDS (DATE)])')
                    if result2 == 'OK':
                        header = data2[0][1].decode('utf-8', errors='ignore')
                        date_match = re.search(r'Date:\s*(.+)', header, re.IGNORECASE)
                        if date_match:
                            email_date = email.utils.parsedate_to_datetime(date_match.group(1))
                            if email_date.tzinfo is None:
                                email_date = email_date.replace(tzinfo=timezone.utc)
                            diff = (datetime.now(timezone.utc) - email_date).total_seconds() / 60
                            if diff > time_window:
                                continue
                except:
                    pass
            if amount is not None and details.get('amount') and abs(details['amount'] - amount) < 0.01:
                if utr is not None:
                    if details.get('utr') == utr:
                        return details
                else:
                    return details
            elif utr is not None and details.get('utr') == utr:
                return details
        return None
    except Exception as e:
        logger.error(f"Gmail search error: {e}")
        return None
    finally:
        if mail:
            try:
                mail.close()
                mail.logout()
            except:
                pass

# ============================================
# ADMIN ROUTES
# ============================================
ADMIN_KEY = CONFIG['ADMIN_API_KEY']

def admin_required():
    provided = request.args.get('admin_key')
    auth_header = request.headers.get('Authorization')
    if auth_header and auth_header.startswith('Bearer '):
        provided = auth_header.split(' ')[1]
    return provided == ADMIN_KEY

@app.route('/apikey_generate', methods=['GET'])
def apikey_generate():
    try:
        if not admin_required():
            return jsonify({'status': 'error', 'message': 'Invalid or missing admin_key'}), 401
        name = request.args.get('name')
        if not name:
            return jsonify({'status': 'error', 'message': 'name parameter required'}), 400
        hours = request.args.get('hours')
        days = request.args.get('days')
        expiry_hours = 24
        if hours:
            try: expiry_hours = int(hours)
            except: pass
        elif days:
            try: expiry_hours = int(days) * 24
            except: pass
        api_key = db_create_api_key(name, expiry_hours)
        return jsonify({
            'status': 'success',
            'api_key': api_key,
            'name': name,
            'expires_at': (datetime.now(timezone.utc) + timedelta(hours=expiry_hours)).isoformat()
        })
    except Exception as e:
        logger.error(f"apikey_generate error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/admin_orders', methods=['GET'])
def admin_orders():
    try:
        if not admin_required():
            return jsonify({'status': 'error', 'message': 'Invalid or missing admin_key'}), 401
        orders = db_get_pending_orders()
        return jsonify({'status': 'success', 'orders': orders})
    except Exception as e:
        logger.error(f"admin_orders error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/admin_keys', methods=['GET'])
def admin_keys():
    try:
        if not admin_required():
            return jsonify({'status': 'error', 'message': 'Invalid or missing admin_key'}), 401
        if supabase_client:
            try:
                result = supabase_client.table('api_keys').select('*').execute()
                return jsonify({'status': 'success', 'api_keys': result.data})
            except:
                pass
        return jsonify({'status': 'success', 'api_keys': list(app.fallback_api_keys.values())})
    except Exception as e:
        logger.error(f"admin_keys error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/admin_revoke', methods=['GET'])
def admin_revoke():
    try:
        if not admin_required():
            return jsonify({'status': 'error', 'message': 'Invalid or missing admin_key'}), 401
        api_key = request.args.get('api_key')
        if not api_key:
            return jsonify({'status': 'error', 'message': 'api_key parameter required'}), 400
        db_update_api_key(api_key, is_active=0)
        return jsonify({'status': 'success', 'message': f'API key {api_key} revoked'})
    except Exception as e:
        logger.error(f"admin_revoke error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/admin_verify', methods=['GET'])
def admin_verify():
    try:
        if not admin_required():
            return jsonify({'status': 'error', 'message': 'Invalid or missing admin_key'}), 401
        order_id = request.args.get('order_id')
        utr = request.args.get('utr')
        if not order_id or not utr:
            return jsonify({'status': 'error', 'message': 'order_id and utr parameters required'}), 400
        order = db_get_order(order_id)
        if not order:
            return jsonify({'status': 'error', 'message': 'Order not found'}), 404
        if order['status'] == 'verified':
            return jsonify({'status': 'error', 'message': 'Order already verified'}), 400
        if db_is_utr_verified(utr):
            return jsonify({'status': 'error', 'message': 'UTR already used'}), 400
        db_update_order(order_id, status='verified', utr=utr)
        db_mark_utr_verified(utr, order_id)
        return jsonify({'status': 'success', 'message': 'Order verified manually'})
    except Exception as e:
        logger.error(f"admin_verify error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ============================================
# PUBLIC API
# ============================================
@app.route('/api/qr.php', methods=['GET'])
def api_qr():
    try:
        api_key = request.args.get('api_key')
        amount = request.args.get('amount')
        if not api_key or not amount:
            return jsonify({'status': 'error', 'message': 'api_key and amount required'}), 400
        key_info = db_validate_api_key(api_key)
        if not key_info:
            return jsonify({'status': 'error', 'message': 'Invalid or expired API key'}), 401
        try:
            amount = float(amount)
            if amount <= 0:
                raise ValueError
        except:
            return jsonify({'status': 'error', 'message': 'Invalid amount'}), 400
        order_id = db_create_order(api_key, amount)
        order = db_get_order(order_id)
        if not order:
            return jsonify({'status': 'error', 'message': 'Failed to create order'}), 500
        upi_intent = f"upi://pay?pa={CONFIG['UPI_ID']}&pn=FamPay&tr={order_id}&tn=Payment+for+Order+{order_id}&am={amount}&cu=INR"
        base_url = request.url_root.rstrip('/')
        qr_url = f"{base_url}/api/qr-image.php?order_id={order_id}"
        checkout_url = f"{base_url}/pay.php?order_id={order_id}"
        return jsonify({
            'status': 'success',
            'data': {
                'order_id': order_id,
                'qr_url': qr_url,
                'checkout_url': checkout_url,
                'upi_id': CONFIG['UPI_ID'],
                'amount': str(amount),
                'payable_amount': str(amount),
                'upi_intent': upi_intent,
                'created_at_ist': order['created_at'],
                'expires_at_ist': order['expires_at']
            }
        })
    except Exception as e:
        logger.error(f"api_qr error: {e}")
        return jsonify({'status': 'error', 'message': 'Failed to create order'}), 500

@app.route('/api/verify-order.php', methods=['GET'])
def api_verify_order():
    try:
        api_key = request.args.get('api_key')
        order_id = request.args.get('order_id')
        if not api_key or not order_id:
            return jsonify({'status': 'error', 'message': 'api_key and order_id required'}), 400
        if not db_validate_api_key(api_key):
            return jsonify({'status': 'error', 'message': 'Invalid API key'}), 401
        order = db_get_order(order_id)
        if not order:
            return jsonify({'status': 'error', 'message': 'Order not found'}), 404

        now_ist = datetime.now(IST)
        expires_ist = datetime.strptime(order['expires_at'], '%d-%m-%Y %H:%M:%S').replace(tzinfo=IST)
        if now_ist > expires_ist and order['status'] == 'pending':
            db_update_order(order_id, status='expired')
            order = db_get_order(order_id)

        if order['status'] == 'pending':
            payment = search_gmail_payment(amount=order['amount'])
            if payment:
                utr = payment.get('utr')
                if utr and not db_is_utr_verified(utr):
                    db_update_order(order_id, status='verified', utr=utr,
                                 transaction_id=payment.get('transaction_id'),
                                 sender_name=payment.get('sender'),
                                 payment_time=payment.get('date'))
                    db_mark_utr_verified(utr, order_id)
                    order = db_get_order(order_id)

        return jsonify({
            'status': 'success',
            'data': {
                'order_id': order['order_id'],
                'status': order['status'],
                'amount': order['amount'],
                'payable_amount': order['payable_amount'],
                'utr': order.get('utr'),
                'transaction_id': order.get('transaction_id'),
                'sender_name': order.get('sender_name'),
                'payment_time_ist': order.get('payment_time')
            }
        })
    except Exception as e:
        logger.error(f"api_verify_order error: {e}")
        return jsonify({'status': 'error', 'message': 'Failed to verify order'}), 500

@app.route('/api/qr-image.php', methods=['GET'])
def qr_image():
    try:
        order_id = request.args.get('order_id')
        if not order_id:
            return jsonify({'status': 'error', 'message': 'order_id required'}), 400
        order = db_get_order(order_id)
        if not order:
            return jsonify({'status': 'error', 'message': 'Order not found'}), 404
        upi_intent = f"upi://pay?pa={CONFIG['UPI_ID']}&pn=FamPay&tr={order_id}&tn=Payment+for+Order+{order_id}&am={order['amount']}&cu=INR"
        if qrcode is None:
            return jsonify({'status': 'error', 'message': 'QR library not available'}), 500
        qr = qrcode.QRCode(box_size=10, border=4)
        qr.add_data(upi_intent)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#4a6cf7", back_color="#FFFFFF")
        img_io = BytesIO()
        img.save(img_io, 'PNG')
        img_io.seek(0)
        return send_file(img_io, mimetype='image/png')
    except Exception as e:
        logger.error(f"qr_image error: {e}")
        return jsonify({'status': 'error', 'message': 'Failed to generate QR'}), 500

# ============================================
# PAYMENT PAGE TEMPLATE – Proper Jinja2 with Success UI
# ============================================
PAYMENT_PAGE_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pay ₹{{ amount }} – FamGateway</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #e8f4fd;
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 20px;
            margin: 0;
            color: #1a202c;
            transition: background 0.4s;
        }
        .card {
            background: white;
            border-radius: 28px;
            padding: 32px 28px;
            max-width: 400px;
            width: 100%;
            box-shadow: 0 20px 60px rgba(0,0,0,0.08);
            position: relative;
            transition: box-shadow 0.3s;
            min-height: 480px;
        }
        .header { text-align: center; margin-bottom: 20px; }
        .brand {
            font-size: 22px;
            font-weight: 700;
            letter-spacing: -0.5px;
            color: #1a202c;
        }
        .brand span { color: #4a6cf7; }

        .order-total {
            background: #f7fafc;
            border-radius: 18px;
            padding: 16px 12px;
            text-align: center;
            margin-bottom: 24px;
        }
        .order-total .label {
            font-size: 13px;
            color: #718096;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            font-weight: 500;
        }
        .order-total .amount {
            font-size: 36px;
            font-weight: 700;
            color: #1a202c;
            margin-top: 2px;
        }
        .order-total .currency {
            font-size: 20px;
            color: #718096;
        }

        .qr-section { text-align: center; margin: 16px 0 12px; }
        .qr-wrapper {
            display: inline-block;
            padding: 8px;
            background: white;
            border-radius: 16px;
            box-shadow: 0 0 30px rgba(0,0,0,0.05);
        }
        .qr-wrapper img {
            display: block;
            width: 180px;
            height: 180px;
            border-radius: 10px;
            background: white;
        }
        .qr-section .sub {
            font-size: 14px;
            color: #718096;
            margin-top: 10px;
            font-weight: 500;
        }
        .qr-section .save-btn {
            display: inline-block;
            margin-top: 10px;
            background: #f7fafc;
            color: #2d3748;
            padding: 6px 18px;
            border-radius: 30px;
            font-size: 13px;
            font-weight: 500;
            text-decoration: none;
            border: 1px solid #e2e8f0;
            transition: background 0.2s;
        }
        .qr-section .save-btn:hover { background: #edf2f7; }

        .details {
            margin: 18px 0;
            border-top: 1px solid #edf2f7;
            padding-top: 16px;
        }
        .detail-row {
            display: flex;
            justify-content: space-between;
            padding: 6px 0;
            font-size: 15px;
        }
        .detail-row .label { color: #718096; }
        .detail-row .value {
            font-weight: 500;
            color: #2d3748;
        }
        .timer-warning { color: #e53e3e; font-weight: 600; }
        .glow-text {
            animation: textGlow 3s ease-in-out infinite alternate;
        }
        @keyframes textGlow {
            0% { text-shadow: 0 0 10px rgba(74,108,247,0.15); }
            100% { text-shadow: 0 0 20px rgba(74,108,247,0.3); }
        }

        .status {
            background: #f7fafc;
            border-radius: 16px;
            padding: 14px;
            text-align: center;
            margin: 14px 0;
            font-size: 15px;
            font-weight: 500;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            border: 1px solid #e2e8f0;
            transition: background 0.4s, border-color 0.4s, color 0.4s;
        }
        .status .spinner {
            display: inline-block;
            width: 18px;
            height: 18px;
            border: 3px solid #e2e8f0;
            border-top-color: #4a6cf7;
            border-radius: 50%;
            animation: spin 1s linear infinite;
        }
        @keyframes spin { to { transform: rotate(360deg); } }

        .check-link {
            text-align: center;
            margin-top: 10px;
            font-size: 14px;
        }
        .check-link a {
            color: #4a6cf7;
            text-decoration: none;
            font-weight: 500;
        }
        .check-link a:hover { text-decoration: underline; }

        .footer {
            text-align: center;
            margin-top: 18px;
            font-size: 13px;
            color: #a0aec0;
        }

        /* ===== SUCCESS OVERLAY ===== */
        .success-overlay {
            display: none;
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: white;
            border-radius: 28px;
            padding: 32px 28px;
            z-index: 10;
            text-align: center;
            animation: fadeInScale 0.5s ease forwards;
        }
        .success-overlay.active {
            display: block;
        }
        @keyframes fadeInScale {
            0% { opacity: 0; transform: scale(0.96); }
            100% { opacity: 1; transform: scale(1); }
        }

        .success-icon {
            position: relative;
            width: 90px;
            height: 90px;
            margin: 0 auto 16px;
        }
        .success-icon .circle {
            width: 100%;
            height: 100%;
            background: #48bb78;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            box-shadow: 0 0 0 0 rgba(72,187,120,0.4);
            animation: pulse-green 2s ease-in-out infinite;
        }
        .success-icon .circle svg {
            width: 50px;
            height: 50px;
            fill: none;
            stroke: white;
            stroke-width: 4;
            stroke-linecap: round;
            stroke-linejoin: round;
            stroke-dasharray: 50;
            stroke-dashoffset: 50;
            animation: drawCheck 0.5s ease forwards 0.3s;
        }
        @keyframes drawCheck {
            100% { stroke-dashoffset: 0; }
        }
        @keyframes pulse-green {
            0% { box-shadow: 0 0 0 0 rgba(72,187,120,0.4); }
            70% { box-shadow: 0 0 0 20px rgba(72,187,120,0); }
            100% { box-shadow: 0 0 0 0 rgba(72,187,120,0); }
        }
        /* Ripple ring */
        .ripple {
            position: absolute;
            top: -10px;
            left: -10px;
            width: calc(100% + 20px);
            height: calc(100% + 20px);
            border: 2px solid rgba(72,187,120,0.3);
            border-radius: 50%;
            animation: rippleExpand 2s ease-out infinite;
        }
        @keyframes rippleExpand {
            0% { transform: scale(0.8); opacity: 1; }
            100% { transform: scale(1.4); opacity: 0; }
        }

        .success-title {
            font-size: 24px;
            font-weight: 700;
            color: #1a202c;
            margin: 12px 0 4px;
        }
        .success-sub {
            color: #718096;
            font-size: 16px;
            margin-bottom: 16px;
        }
        .verified-badge {
            display: inline-block;
            background: #c6f6d5;
            color: #276749;
            padding: 6px 18px;
            border-radius: 30px;
            font-size: 14px;
            font-weight: 600;
            margin-bottom: 16px;
        }

        /* Confetti container (full page) */
        .confetti-container {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            pointer-events: none;
            z-index: 999;
            overflow: hidden;
        }
        .confetti {
            position: absolute;
            width: 10px;
            height: 10px;
            opacity: 0.9;
            animation: confettiFall linear forwards;
        }
        @keyframes confettiFall {
            0% { transform: translateY(-10px) rotate(0deg) scale(1); opacity: 1; }
            100% { transform: translateY(110vh) rotate(720deg) scale(0.5); opacity: 0; }
        }

        .btn {
            display: inline-block;
            background: #4a6cf7;
            color: white;
            padding: 10px 32px;
            border-radius: 40px;
            text-decoration: none;
            font-weight: 600;
            margin-top: 12px;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        .btn:hover {
            transform: scale(1.02);
            box-shadow: 0 4px 12px rgba(74,108,247,0.3);
        }

        @media (max-width: 480px) {
            .card { padding: 22px 16px; }
            .qr-wrapper img { width: 150px; height: 150px; }
            .order-total .amount { font-size: 30px; }
            .success-icon { width: 70px; height: 70px; }
            .success-icon .circle svg { width: 40px; height: 40px; }
        }
        @media (prefers-reduced-motion: reduce) {
            .success-icon .circle, .ripple, .confetti { animation: none !important; }
        }
    </style>
</head>
<body>
    <div class="card" id="paymentCard">
        <!-- PENDING UI -->
        <div id="pendingUI">
            <div class="header"><div class="brand">Fam<span>Gateway</span>™</div></div>
            <div class="order-total">
                <div class="label">ORDER TOTAL</div>
                <div class="amount">₹ {{ amount }} <span class="currency">INR</span></div>
            </div>
            <div class="qr-section">
                <div class="qr-wrapper"><img id="qr-img" src="{{ qr_url }}" alt="QR Code"></div>
                <div class="sub">SCAN WITH ANY UPI APP</div>
                <a href="{{ qr_url }}" download="qr_{{ order_id }}.png" class="save-btn">🔗 Save QR</a>
            </div>
            <div class="details">
                <div class="detail-row"><span class="label">Merchant</span><span class="value">{{ merchant }}</span></div>
                <div class="detail-row"><span class="label">Order ID</span><span class="value">{{ order_id }}</span></div>
                <div class="detail-row"><span class="label">Expires In</span><span class="value glow-text" id="timer">--:--</span></div>
            </div>
            <div class="status" id="status">
                <span class="spinner"></span>
                <span id="status-text">Waiting for payment…</span>
            </div>
            <div class="check-link"><a href="{{ verify_url }}" target="_blank">Check Status</a></div>
            <div class="footer">⚡ Auto‑verified instantly after UPI payment</div>
        </div>

        <!-- SUCCESS OVERLAY -->
        <div class="success-overlay" id="successOverlay">
            <div class="success-icon">
                <div class="ripple"></div>
                <div class="circle">
                    <svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>
                </div>
            </div>
            <div class="success-title">Payment Successful!</div>
            <div class="success-sub">Thank you for your payment.</div>
            <div class="verified-badge">✓ Payment received</div>
            <div class="details" style="text-align:left;">
                <div class="detail-row"><span class="label">Order ID</span><span class="value">{{ order_id }}</span></div>
                <div class="detail-row"><span class="label">Amount</span><span class="value">₹ {{ amount }}</span></div>
            </div>
            <a href="/" class="btn">Done</a>
        </div>
    </div>

    <!-- Confetti container -->
    <div class="confetti-container" id="confettiContainer"></div>

    <script>
        // Timer
        var expiresStr = "{{ expires_at }}";
        var datePart, timePart, dd, mm, yyyy, hh, min, sec;
        if (expiresStr) {
            var parts = expiresStr.split(' ');
            datePart = parts[0];
            timePart = parts[1];
            var dateParts = datePart.split('-');
            dd = parseInt(dateParts[0], 10);
            mm = parseInt(dateParts[1], 10) - 1;
            yyyy = parseInt(dateParts[2], 10);
            var timeParts = timePart.split(':');
            hh = parseInt(timeParts[0], 10);
            min = parseInt(timeParts[1], 10);
            sec = parseInt(timeParts[2], 10);
        } else {
            var now = new Date();
            now.setMinutes(now.getMinutes() + 5);
            yyyy = now.getFullYear();
            mm = now.getMonth();
            dd = now.getDate();
            hh = now.getHours();
            min = now.getMinutes();
            sec = now.getSeconds();
        }
        var expires = new Date(yyyy, mm, dd, hh, min, sec).getTime();
        var timerEl = document.getElementById('timer');
        function updateTimer() {
            var now = Date.now();
            var diff = expires - now;
            if (diff < 0) {
                timerEl.textContent = 'Expired';
                timerEl.className = 'timer-warning';
                document.getElementById('status').className = 'status expired';
                document.getElementById('status-text').textContent = '⏰ Order expired';
                // stop polling if expired
                if (window.checkInterval) clearInterval(window.checkInterval);
                return;
            }
            var mins = Math.floor(diff / 60000);
            var secs = Math.floor((diff % 60000) / 1000);
            timerEl.textContent = String(mins).padStart(2,'0') + ':' + String(secs).padStart(2,'0');
        }
        updateTimer();
        setInterval(updateTimer, 1000);

        // Polling
        var verifyUrl = "{{ verify_url }}";
        var statusEl = document.getElementById('status');
        var statusText = document.getElementById('status-text');
        var successOverlay = document.getElementById('successOverlay');
        var pendingUI = document.getElementById('pendingUI');
        var confettiContainer = document.getElementById('confettiContainer');
        var isSuccess = false;
        var checkInterval;

        function showSuccess() {
            if (isSuccess) return;
            isSuccess = true;
            if (checkInterval) clearInterval(checkInterval);
            // Hide pending, show success overlay
            pendingUI.style.display = 'none';
            successOverlay.classList.add('active');
            // Launch confetti
            launchConfetti();
        }

        function launchConfetti() {
            var colors = ['#48bb78', '#4a6cf7', '#ed64a6', '#f6ad55', '#63b3ed', '#9f7aea', '#fc8181', '#68d391'];
            for (var i = 0; i < 80; i++) {
                var el = document.createElement('div');
                el.className = 'confetti';
                el.style.left = Math.random() * 100 + '%';
                el.style.width = (Math.random() * 10 + 5) + 'px';
                el.style.height = (Math.random() * 10 + 5) + 'px';
                el.style.background = colors[Math.floor(Math.random() * colors.length)];
                el.style.borderRadius = Math.random() > 0.5 ? '50%' : '2px';
                el.style.animationDuration = (Math.random() * 2 + 1.5) + 's';
                el.style.animationDelay = (Math.random() * 1.5) + 's';
                confettiContainer.appendChild(el);
            }
        }

        function checkStatus() {
            if (isSuccess) return;
            fetch(verifyUrl)
                .then(function(res) { return res.json(); })
                .then(function(data) {
                    if (data.data && data.data.status === 'verified') {
                        showSuccess();
                    } else if (data.data && data.data.status === 'expired') {
                        if (checkInterval) clearInterval(checkInterval);
                        statusEl.className = 'status expired';
                        statusText.textContent = '⏰ Order expired';
                    }
                })
                .catch(function() { /* ignore network errors, keep polling */ });
        }

        // Start polling every 1 second
        checkStatus(); // immediate first check
        checkInterval = setInterval(checkStatus, 1000);
    </script>
</body>
</html>
'''

# ============================================
# SUCCESS PAGE (if order already verified)
# ============================================
SUCCESS_PAGE = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Payment Successful 🎉</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #e8f4fd;
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 20px;
            margin: 0;
            color: #1a202c;
            overflow: hidden;
        }
        .card {
            background: white;
            border-radius: 28px;
            padding: 40px 32px;
            max-width: 400px;
            width: 100%;
            box-shadow: 0 20px 60px rgba(0,0,0,0.08);
            text-align: center;
            animation: popIn 0.5s ease forwards;
        }
        @keyframes popIn {
            0% { transform: scale(0.9); opacity: 0; }
            100% { transform: scale(1); opacity: 1; }
        }
        .success-icon {
            position: relative;
            width: 90px;
            height: 90px;
            margin: 0 auto 16px;
        }
        .success-icon .circle {
            width: 100%;
            height: 100%;
            background: #48bb78;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            box-shadow: 0 0 0 0 rgba(72,187,120,0.4);
            animation: pulse-green 2s ease-in-out infinite;
        }
        .success-icon .circle svg {
            width: 50px;
            height: 50px;
            fill: none;
            stroke: white;
            stroke-width: 4;
            stroke-linecap: round;
            stroke-linejoin: round;
            stroke-dasharray: 50;
            stroke-dashoffset: 50;
            animation: drawCheck 0.5s ease forwards 0.3s;
        }
        @keyframes drawCheck {
            100% { stroke-dashoffset: 0; }
        }
        @keyframes pulse-green {
            0% { box-shadow: 0 0 0 0 rgba(72,187,120,0.4); }
            70% { box-shadow: 0 0 0 20px rgba(72,187,120,0); }
            100% { box-shadow: 0 0 0 0 rgba(72,187,120,0); }
        }
        .ripple {
            position: absolute;
            top: -10px;
            left: -10px;
            width: calc(100% + 20px);
            height: calc(100% + 20px);
            border: 2px solid rgba(72,187,120,0.3);
            border-radius: 50%;
            animation: rippleExpand 2s ease-out infinite;
        }
        @keyframes rippleExpand {
            0% { transform: scale(0.8); opacity: 1; }
            100% { transform: scale(1.4); opacity: 0; }
        }
        h1 {
            font-size: 24px;
            font-weight: 700;
            color: #1a202c;
            margin: 12px 0 4px;
        }
        .sub {
            color: #718096;
            font-size: 16px;
            margin-bottom: 16px;
        }
        .verified-badge {
            display: inline-block;
            background: #c6f6d5;
            color: #276749;
            padding: 6px 18px;
            border-radius: 30px;
            font-size: 14px;
            font-weight: 600;
            margin-bottom: 16px;
        }
        .detail-box {
            background: #f7fafc;
            border-radius: 16px;
            padding: 16px;
            text-align: left;
            margin: 16px 0;
        }
        .detail-row {
            display: flex;
            justify-content: space-between;
            padding: 6px 0;
            font-size: 15px;
        }
        .detail-row .label { color: #718096; }
        .detail-row .value { font-weight: 500; color: #2d3748; }
        .btn {
            display: inline-block;
            background: #4a6cf7;
            color: white;
            padding: 10px 32px;
            border-radius: 40px;
            text-decoration: none;
            font-weight: 600;
            margin-top: 12px;
            transition: transform 0.2s;
        }
        .btn:hover { transform: scale(1.02); }
        .confetti-container {
            position: fixed;
            top: 0; left: 0;
            width: 100%; height: 100%;
            pointer-events: none;
            z-index: 999;
            overflow: hidden;
        }
        .confetti {
            position: absolute;
            width: 10px; height: 10px;
            opacity: 0.9;
            animation: confettiFall linear forwards;
        }
        @keyframes confettiFall {
            0% { transform: translateY(-10px) rotate(0deg) scale(1); opacity: 1; }
            100% { transform: translateY(110vh) rotate(720deg) scale(0.5); opacity: 0; }
        }
        @media (max-width: 480px) {
            .card { padding: 28px 18px; }
            .success-icon { width: 70px; height: 70px; }
            .success-icon .circle svg { width: 40px; height: 40px; }
        }
    </style>
</head>
<body>
    <div class="confetti-container" id="confetti"></div>
    <div class="card">
        <div class="success-icon">
            <div class="ripple"></div>
            <div class="circle">
                <svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>
            </div>
        </div>
        <h1>Payment Successful!</h1>
        <p class="sub">Thank you for your payment.</p>
        <div class="verified-badge">✓ Payment received</div>
        <div class="detail-box">
            <div class="detail-row"><span class="label">Order ID</span><span class="value">{{ order_id }}</span></div>
            <div class="detail-row"><span class="label">Amount</span><span class="value">₹ {{ amount }}</span></div>
            <div class="detail-row"><span class="label">UTR</span><span class="value">{{ utr }}</span></div>
            <div class="detail-row"><span class="label">Payment Time</span><span class="value">{{ payment_time }}</span></div>
            <div class="detail-row"><span class="label">Sender</span><span class="value">{{ sender }}</span></div>
        </div>
        <a href="/" class="btn">Done</a>
    </div>
    <script>
        (function() {
            var container = document.getElementById('confetti');
            var colors = ['#48bb78', '#4a6cf7', '#ed64a6', '#f6ad55', '#63b3ed', '#9f7aea', '#fc8181', '#68d391'];
            for (var i = 0; i < 80; i++) {
                var el = document.createElement('div');
                el.className = 'confetti';
                el.style.left = Math.random() * 100 + '%';
                el.style.width = (Math.random() * 10 + 5) + 'px';
                el.style.height = (Math.random() * 10 + 5) + 'px';
                el.style.background = colors[Math.floor(Math.random() * colors.length)];
                el.style.borderRadius = Math.random() > 0.5 ? '50%' : '2px';
                el.style.animationDuration = (Math.random() * 2 + 1.5) + 's';
                el.style.animationDelay = (Math.random() * 1.5) + 's';
                container.appendChild(el);
            }
        })();
    </script>
</body>
</html>
'''

# ============================================
# EXPIRED PAGE
# ============================================
EXPIRED_PAGE = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Order Expired ⏰</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #e8f4fd;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            padding: 20px;
        }
        .card {
            background: white;
            border-radius: 28px;
            padding: 40px 32px;
            max-width: 420px;
            width: 100%;
            text-align: center;
            box-shadow: 0 20px 60px rgba(0,0,0,0.08);
        }
        .icon { font-size: 64px; margin-bottom: 16px; }
        h1 { color: #e53e3e; font-size: 26px; margin-bottom: 8px; }
        .sub { color: #718096; font-size: 16px; margin-bottom: 20px; }
        .detail { background: #f7fafc; border-radius: 12px; padding: 16px; margin: 16px 0; text-align:left; }
        .detail .row { display: flex; justify-content: space-between; padding: 6px 0; font-size: 15px; }
        .row .label { color: #718096; }
        .row .value { font-weight: 500; }
        .btn {
            display: inline-block;
            background: #4a6cf7;
            color: white;
            padding: 10px 32px;
            border-radius: 40px;
            text-decoration: none;
            font-weight: 600;
            transition: transform 0.2s;
        }
        .btn:hover { transform: scale(1.02); }
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">⏰</div>
        <h1>Order Expired</h1>
        <p class="sub">This payment session has expired. Please create a new order.</p>
        <div class="detail">
            <div class="row"><span class="label">Order ID</span><span class="value">{{ order_id }}</span></div>
            <div class="row"><span class="label">Amount</span><span class="value">₹ {{ amount }}</span></div>
            <div class="row"><span class="label">Expired at</span><span class="value">{{ expires_at }}</span></div>
        </div>
        <a href="/" class="btn">Go Home</a>
    </div>
</body>
</html>
'''

# ============================================
# PAYMENT PAGE ROUTE
# ============================================
@app.route('/pay.php', methods=['GET'])
def pay_page():
    try:
        order_id = request.args.get('order_id')
        if not order_id:
            return "Order ID missing", 400
        order = db_get_order(order_id)
        if not order:
            return "Order not found", 404

        base_url = request.url_root.rstrip('/')
        qr_url = f"{base_url}/api/qr-image.php?order_id={order_id}"
        verify_url = f"{base_url}/api/verify-order.php?api_key={order['api_key']}&order_id={order_id}"
        amount = order['amount']
        expires_at = order['expires_at']
        merchant = CONFIG['PAYEE_NAME']
        status = order['status']

        # If order already verified, show success page directly
        if status == 'verified':
            return render_template_string(SUCCESS_PAGE,
                order_id=order_id,
                utr=order.get('utr', 'N/A'),
                amount=amount,
                merchant=merchant,
                payment_time=order.get('payment_time', ''),
                sender=order.get('sender_name', '')
            )

        # Check expiry
        now_ist = datetime.now(IST)
        expires_ist = datetime.strptime(expires_at, '%d-%m-%Y %H:%M:%S').replace(tzinfo=IST)
        if now_ist > expires_ist:
            if order['status'] == 'pending':
                db_update_order(order_id, status='expired')
            return render_template_string(EXPIRED_PAGE,
                order_id=order_id,
                amount=amount,
                merchant=merchant,
                expires_at=expires_at
            )

        # Pending – show the payment page with polling
        return render_template_string(PAYMENT_PAGE_TEMPLATE,
            amount=amount,
            qr_url=qr_url,
            order_id=order_id,
            merchant=merchant,
            expires_at=expires_at,
            verify_url=verify_url
        )
    except Exception as e:
        logger.error(f"pay_page error: {e}")
        return render_template_string('''
            <!DOCTYPE html>
            <html>
            <head><title>Error</title>
            <style>body{font-family:sans-serif;text-align:center;padding:2rem;background:#e8f4fd;color:#1a202c;}</style>
            </head>
            <body>
            <h1>⚠️ Unable to load payment page</h1>
            <p>Please check the order ID and try again.</p>
            <p><a href="/" style="color:#4a6cf7;">Go Home</a></p>
            </body>
            </html>
        ''', 500)

# ============================================
# OTHER ENDPOINTS
# ============================================
@app.route('/verify-fast', methods=['GET'])
def verify_fast():
    try:
        amount = request.args.get('amount')
        utr = request.args.get('utr')
        time_window = request.args.get('time_window', CONFIG['TIME_WINDOW_MINUTES'])
        if not amount and not utr:
            return jsonify({'status': 'error', 'message': 'Provide amount or utr'}), 400
        try:
            if amount:
                amount = float(amount)
            time_window = int(time_window)
        except:
            return jsonify({'status': 'error', 'message': 'Invalid input'}), 400
        payment = search_gmail_payment(amount=amount, utr=utr, time_window=time_window)
        if payment:
            return jsonify({'status': 'success', 'message': '✅ Payment found!', 'data': payment})
        else:
            return jsonify({'status': 'not_found', 'message': f'❌ No matching payment found in last {time_window} minutes.'})
    except Exception as e:
        logger.error(f"verify_fast error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/verify-by-utr', methods=['GET', 'POST'])
def verify_by_utr():
    try:
        if request.method == 'GET':
            utr = request.args.get('utr')
            time_window = request.args.get('time_window', CONFIG['TIME_WINDOW_MINUTES'])
        else:
            data = request.get_json()
            utr = data.get('utr') if data else None
            time_window = data.get('time_window', CONFIG['TIME_WINDOW_MINUTES'])
        if not utr:
            return jsonify({'status': 'error', 'message': 'UTR required'}), 400
        try:
            time_window = int(time_window)
        except:
            return jsonify({'status': 'error', 'message': 'Invalid time_window'}), 400
        payment = search_gmail_payment(utr=utr, time_window=time_window)
        if payment:
            return jsonify({'status': 'success', 'message': '✅ Payment found by UTR', 'data': payment})
        else:
            return jsonify({'status': 'not_found', 'message': f'No payment with UTR {utr} in last {time_window} min'})
    except Exception as e:
        logger.error(f"verify_by_utr error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/verify-last-payment', methods=['GET'])
def verify_last_payment():
    try:
        amount = request.args.get('amount')
        time_window = request.args.get('time_window', CONFIG['TIME_WINDOW_MINUTES'])
        if not amount:
            return jsonify({'status': 'error', 'message': 'Amount required'}), 400
        try:
            amount = float(amount)
            time_window = int(time_window)
        except:
            return jsonify({'status': 'error', 'message': 'Invalid input'}), 400
        payment = search_gmail_payment(amount=amount, time_window=time_window)
        if payment:
            return jsonify({'status': 'success', 'message': '✅ Payment found', 'data': payment})
        else:
            return jsonify({'status': 'not_found', 'message': f'No payment of ₹{amount} in last {time_window} min'})
    except Exception as e:
        logger.error(f"verify_last_payment error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/verify-payment', methods=['GET', 'POST'])
def verify_payment_legacy():
    try:
        if request.method == 'GET':
            amount = request.args.get('amount')
            time_window = request.args.get('time_window', CONFIG['TIME_WINDOW_MINUTES'])
        else:
            data = request.get_json()
            amount = data.get('amount') if data else None
            time_window = data.get('time_window', CONFIG['TIME_WINDOW_MINUTES'])
        if not amount:
            return jsonify({'status': 'error', 'message': 'Amount required'}), 400
        try:
            amount = float(amount)
            time_window = int(time_window)
        except:
            return jsonify({'status': 'error', 'message': 'Invalid input'}), 400
        payment = search_gmail_payment(amount=amount, time_window=time_window)
        if payment:
            return jsonify({'status': 'success', 'message': '✅ Payment verified', 'data': payment})
        else:
            return jsonify({'status': 'pending', 'message': f'⏳ No payment found in last {time_window} min'})
    except Exception as e:
        logger.error(f"verify_payment error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/generate-qr', methods=['GET'])
def generate_qr_legacy():
    try:
        amount = request.args.get('amount')
        if not amount:
            return jsonify({'status': 'error', 'message': 'Amount required'}), 400
        try:
            amount = float(amount)
        except:
            return jsonify({'status': 'error', 'message': 'Invalid amount'}), 400
        upi_intent = f"upi://pay?pa={CONFIG['UPI_ID']}&pn=FamPay&am={amount}&cu=INR"
        if qrcode is None:
            return jsonify({'status': 'error', 'message': 'QR library not available'}), 500
        qr = qrcode.QRCode(box_size=10, border=4)
        qr.add_data(upi_intent)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#4a6cf7", back_color="#FFFFFF")
        img_io = BytesIO()
        img.save(img_io, 'PNG')
        img_io.seek(0)
        return send_file(img_io, mimetype='image/png')
    except Exception as e:
        logger.error(f"generate_qr error: {e}")
        return jsonify({'status': 'error', 'message': 'Failed to generate QR'}), 500

@app.route('/debug-emails', methods=['GET'])
def debug_emails():
    try:
        payment = search_gmail_payment()
        return jsonify({'status': 'debug', 'last_payment': payment})
    except Exception as e:
        logger.error(f"debug_emails error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now(IST).isoformat(),
        'supabase_configured': bool(supabase_client),
        'gmail_configured': bool(CONFIG['GMAIL_EMAIL'] and CONFIG['GMAIL_APP_PASSWORD'])
    })

@app.route('/', methods=['GET'])
def index():
    base_url = request.url_root.rstrip('/')
    return jsonify({
        'name': 'UPI Auto-Payment Verifier',
        'version': '5.0.0',
        'description': 'Premium UI with real payment verification.',
        'endpoints': {
            'public': {
                '/': 'GET - Documentation',
                '/health': 'GET - Health check',
                '/generate-qr': 'GET - Generate colored QR (e.g., ?amount=499)',
                '/verify-fast': 'GET - Instant verify by amount or UTR',
                '/verify-by-utr': 'GET/POST - Verify by UTR only',
                '/verify-last-payment': 'GET - One-shot check by amount',
                '/verify-payment': 'GET/POST - Legacy polling',
                '/api/qr.php': 'GET - Create order and get QR (api_key, amount)',
                '/api/verify-order.php': 'GET - Check order status (api_key, order_id)',
                '/api/qr-image.php': 'GET - Get colored QR image (order_id)',
                '/pay.php': 'GET - Payment page with success UI (order_id)',
                '/debug-emails': 'GET - Debug'
            }
        },
        'examples': {
            'create_order': f'curl "{base_url}/api/qr.php?api_key=fam_YOUR_KEY&amount=499"',
            'verify_by_amount': f'curl "{base_url}/verify-fast?amount=1"',
            'payment_page': f'Open in browser: {base_url}/pay.php?order_id=Khan_YOUR_ID'
        }
    })

# ============================================
# VERCEL ENTRY POINT
# ============================================
if __name__ == '__main__':
    # This block is NOT executed on Vercel – only for local development
    app.run(host='0.0.0.0', port=5000, debug=False)