import os
import csv
import json
import signal
import smtplib
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
 
 
# ═══════════════════════════════════════════════════════════════
# FILE HELPERS
# ═══════════════════════════════════════════════════════════════
 
def _read_csv(path, max_rows=500):
    if not os.path.exists(path):
        return []
    rows = []
    try:
        with open(path, newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                rows.append(dict(row))
    except Exception:
        pass
    return rows[-max_rows:]
 
 
def _read_log_tail(path, n=200):
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        return [l.rstrip('\n') for l in lines[-n:]]
    except Exception:
        return []
 
 
# ═══════════════════════════════════════════════════════════════
# TRADER STATE
# ═══════════════════════════════════════════════════════════════
 
def _trader_pid():
    lock = settings.TRADER_LOCK_FILE
    if not os.path.exists(lock):
        return None
    try:
        with open(lock) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, OSError):
        return None
 
 
def _is_running():
    return _trader_pid() is not None
 
 
def _compute_stats(trades):
    exits  = [t for t in trades if t.get('type') == 'EXIT']
    total  = len(exits)
    wins   = sum(1 for t in exits if float(t.get('pnl') or 0) > 0)
    losses = total - wins
    wr     = (wins / total * 100) if total else 0
    total_pnl = sum(float(t.get('pnl') or 0) for t in exits)
    avg_pnl   = (total_pnl / total) if total else 0
 
    balance = 10000.0
    if trades:
        try:
            balance = float(trades[-1].get('balance') or 10000)
        except (ValueError, TypeError):
            pass
 
    streak = 0
    for t in reversed(exits):
        pnl = float(t.get('pnl') or 0)
        if streak == 0:
            streak = 1 if pnl > 0 else -1
        elif streak > 0 and pnl > 0:
            streak += 1
        elif streak < 0 and pnl < 0:
            streak -= 1
        else:
            break
 
    peak = max_dd = 0.0
    running = 10000.0
    for t in exits:
        try:
            running = float(t.get('balance') or running)
            if running > peak:
                peak = running
            if peak:
                dd = (peak - running) / peak * 100
                if dd > max_dd:
                    max_dd = dd
        except (ValueError, TypeError):
            pass
 
    return {
        'total_trades': total, 'wins': wins, 'losses': losses,
        'win_rate': round(wr, 1), 'total_pnl': round(total_pnl, 2),
        'avg_pnl': round(avg_pnl, 2), 'balance': round(balance, 2),
        'streak': streak, 'max_drawdown': round(max_dd, 2),
    }
 
 
def _open_position(trades):
    entries = [t for t in trades if t.get('type') == 'ENTRY']
    exits   = [t for t in trades if t.get('type') == 'EXIT']
    if len(entries) > len(exits):
        return entries[-1]
    return None
 
 
# ═══════════════════════════════════════════════════════════════
# NOTIFICATIONS
# ═══════════════════════════════════════════════════════════════
 
def _send_sms(body: str):
    """Send SMS via Twilio. No-op if credentials not configured."""
    sid   = getattr(settings, 'TWILIO_ACCOUNT_SID', '')
    token = getattr(settings, 'TWILIO_AUTH_TOKEN', '')
    from_ = getattr(settings, 'TWILIO_FROM_NUMBER', '')
    to_   = getattr(settings, 'NOTIFY_SMS_TO', '')
    if not all([sid, token, from_, to_]):
        return False, 'Twilio credentials not configured in settings.py'
    try:
        import urllib.request, urllib.parse, base64
        url  = f'https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json'
        data = urllib.parse.urlencode({'From': from_, 'To': to_, 'Body': body}).encode()
        req  = urllib.request.Request(url, data=data)
        creds = base64.b64encode(f'{sid}:{token}'.encode()).decode()
        req.add_header('Authorization', f'Basic {creds}')
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        return True, result.get('sid', 'sent')
    except Exception as e:
        return False, str(e)
 
 
def _send_email(subject: str, body: str):
    """Send email via SMTP. No-op if credentials not configured."""
    from_  = getattr(settings, 'NOTIFY_EMAIL_FROM', '')
    pwd    = getattr(settings, 'NOTIFY_EMAIL_PASSWORD', '')
    to_    = getattr(settings, 'NOTIFY_EMAIL_TO', '')
    host   = getattr(settings, 'NOTIFY_EMAIL_HOST', 'smtp.gmail.com')
    port   = getattr(settings, 'NOTIFY_EMAIL_PORT', 587)
    if not all([from_, pwd, to_]):
        return False, 'Email credentials not configured in settings.py'
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From']    = from_
        msg['To']      = to_
        msg.attach(MIMEText(body, 'plain'))
        with smtplib.SMTP(host, port, timeout=10) as srv:
            srv.ehlo()
            srv.starttls()
            srv.login(from_, pwd)
            srv.sendmail(from_, to_, msg.as_string())
        return True, 'sent'
    except Exception as e:
        return False, str(e)
 
 
def _notify_trade(trade_type: str, details: dict):
    """
    Fire-and-forget trade notification.
    Called after every ENTRY and EXIT event.
    """
    direction = details.get('direction', '').upper()
    price     = details.get('price', '—')
    pnl       = details.get('pnl', '')
    balance   = details.get('balance', '—')
 
    if trade_type == 'ENTRY':
        subject = f"[TRADER] {direction} position opened @ ${price}"
        body = (
            f"BTC/USD Trade Alert\n"
            f"{'─'*30}\n"
            f"Action    : ENTRY\n"
            f"Direction : {direction}\n"
            f"Entry     : ${price}\n"
            f"Stop Loss : ${details.get('sl','—')}\n"
            f"Take Profit: ${details.get('tp','—')}\n"
            f"Quantity  : {details.get('qty','—')}\n"
            f"Investment: ${details.get('investment','—')}\n"
            f"Balance   : ${balance}\n"
            f"Time      : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
    else:  # EXIT
        pnl_val = float(pnl) if pnl else 0
        emoji   = '✅' if pnl_val >= 0 else '❌'
        subject = f"[TRADER] {emoji} {direction} closed {'+' if pnl_val>=0 else ''}{pnl_val:.2f} USD"
        body = (
            f"BTC/USD Trade Alert\n"
            f"{'─'*30}\n"
            f"Action    : EXIT\n"
            f"Direction : {direction}\n"
            f"Exit      : ${price}\n"
            f"P&L       : {'+' if pnl_val>=0 else ''}{pnl_val:.2f} USD\n"
            f"Reason    : {details.get('reason','—')}\n"
            f"Balance   : ${balance}\n"
            f"Time      : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
 
    # Run both channels in background threads so they don't block the response
    threading.Thread(target=_send_email, args=(subject, body), daemon=True).start()
    threading.Thread(target=_send_sms,   args=(body,),          daemon=True).start()
 
 
# ═══════════════════════════════════════════════════════════════
# PAYMENT / TRADING SETUP  (stored in a simple JSON file)
# ═══════════════════════════════════════════════════════════════
 
_PAYMENT_FILE = os.path.join(os.path.dirname(__file__), 'payment_profile.json')
 
def _load_payment():
    if not os.path.exists(_PAYMENT_FILE):
        return {}
    try:
        with open(_PAYMENT_FILE) as f:
            return json.load(f)
    except Exception:
        return {}
 
 
def _save_payment(data: dict):
    with open(_PAYMENT_FILE, 'w') as f:
        json.dump(data, f, indent=2)
 
 
# ═══════════════════════════════════════════════════════════════
# VIEWS
# ═══════════════════════════════════════════════════════════════
 
def index(request):
    return render(request, 'dashboard/index.html')
 
 
def api_status(request):
    trades   = _read_csv(settings.TRADE_LOG)
    stats    = _compute_stats(trades)
    open_pos = _open_position(trades)
 
    hb_lines = _read_log_tail(settings.HEARTBEAT_LOG, 1)
    last_hb  = hb_lines[0] if hb_lines else None
    hb_age   = None
    if last_hb:
        try:
            ts   = datetime.fromisoformat(last_hb.split(' | ')[0])
            hb_age = int((datetime.now() - ts).total_seconds())
        except Exception:
            pass
 
    return JsonResponse({
        'running'       : _is_running(),
        'pid'           : _trader_pid(),
        'stats'         : stats,
        'open_position' : open_pos,
        'last_heartbeat': last_hb,
        'heartbeat_age' : hb_age,
        'timestamp'     : datetime.now().isoformat(),
    })
 
 
def api_trades(request):
    n      = int(request.GET.get('n', 100))
    trades = _read_csv(settings.TRADE_LOG, max_rows=n * 2)
    paired, entry_stack = [], []
 
    for t in trades:
        if t.get('type') == 'ENTRY':
            entry_stack.append(t)
        elif t.get('type') == 'EXIT' and entry_stack:
            entry = entry_stack.pop(0)
            pnl   = float(t.get('pnl') or 0)
            paired.append({
                'open_time'  : entry.get('timestamp', ''),
                'close_time' : t.get('timestamp', ''),
                'direction'  : entry.get('direction', ''),
                'entry_price': entry.get('entry_price', ''),
                'exit_price' : t.get('exit_price', ''),
                'quantity'   : entry.get('quantity', ''),
                'investment' : entry.get('investment', ''),
                'pnl'        : round(pnl, 2),
                'pnl_pct'   : round(pnl / float(entry.get('investment') or 1) * 100, 2),
                'balance'   : t.get('balance', ''),
                'result'    : 'WIN' if pnl > 0 else 'LOSS',
            })
 
    for entry in entry_stack:
        paired.append({
            'open_time'  : entry.get('timestamp', ''),
            'close_time' : None, 'direction': entry.get('direction', ''),
            'entry_price': entry.get('entry_price', ''), 'exit_price': None,
            'quantity'   : entry.get('quantity', ''), 'investment': entry.get('investment', ''),
            'pnl': None, 'pnl_pct': None, 'balance': entry.get('balance', ''), 'result': 'OPEN',
        })
 
    return JsonResponse({'trades': paired[-n:], 'total': len(paired)})
 
 
def api_daily(request):
    return JsonResponse({'daily': _read_csv(settings.DAILY_LOG, max_rows=90)})
 
 
def api_signals(request):
    n = int(request.GET.get('n', 100))
    lines = _read_log_tail(settings.PREDICTION_LOG, n)
    parsed = []
    for line in lines:
        parts = line.split(' | ', 1)
        parsed.append({'timestamp': parts[0] if parts else '',
                        'message':  parts[1] if len(parts) > 1 else line})
    return JsonResponse({'signals': list(reversed(parsed))})
 
 
def api_heartbeat(request):
    n = int(request.GET.get('n', 50))
    lines = _read_log_tail(settings.HEARTBEAT_LOG, n)
    parsed = []
    for line in lines:
        parts = line.split(' | ', 1)
        parsed.append({'timestamp': parts[0] if parts else '',
                        'message':  parts[1] if len(parts) > 1 else line})
    return JsonResponse({'heartbeat': list(reversed(parsed))})
 
 
def api_crashes(request):
    lines = _read_log_tail(settings.CRASH_LOG, 200)
    return JsonResponse({'crashes': lines, 'has_crashes': bool(lines)})
 
 
@csrf_exempt
@require_POST
def api_control(request):
    try:
        body   = json.loads(request.body)
        action = body.get('action')
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
 
    if action == 'start':
        if _is_running():
            return JsonResponse({'ok': False, 'msg': 'Trader already running'})
        script = settings.TRADER_SCRIPT
        if not os.path.exists(script):
            return JsonResponse({'ok': False, 'msg': f'Script not found: {script}'})
        try:
            proc = subprocess.Popen(
                [sys.executable, script],
                cwd=os.path.dirname(script),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return JsonResponse({'ok': True, 'msg': f'Trader started (PID {proc.pid})'})
        except Exception as e:
            return JsonResponse({'ok': False, 'msg': str(e)})
 
    elif action == 'stop':
        pid = _trader_pid()
        if pid is None:
            return JsonResponse({'ok': False, 'msg': 'Trader not running'})
        try:
            os.kill(pid, signal.SIGTERM)
            return JsonResponse({'ok': True, 'msg': f'SIGTERM sent to PID {pid}'})
        except OSError as e:
            return JsonResponse({'ok': False, 'msg': str(e)})
 
    return JsonResponse({'error': 'Unknown action'}, status=400)
 
 
def api_config(request):
    """Read config values from live_trader.py (one level above manage.py)."""
    script = settings.TRADER_SCRIPT
    config = {'_path': script, '_exists': os.path.exists(script)}
    if os.path.exists(script):
        try:
            with open(script, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    for key in ['API_KEY', 'TICKER_TD', 'INTERVAL_30MIN',
                                'LOOKBACK_HOURS', 'RR_RATIO', 'RISK_PER_TRADE',
                                'BALANCE', 'MAX_DAILY_DD', 'DRY_RUN']:
                        if line.startswith(key + ' ') or line.startswith(key + '='):
                            val = line.split('=', 1)[1].strip().split('#')[0].strip()
                            config[key] = val
        except Exception as e:
            config['_error'] = str(e)
    return JsonResponse({'config': config})
 
 
# ── Payment / trading setup ───────────────────────────────────────
 
def api_payment_get(request):
    """Return the current payment / trading profile."""
    return JsonResponse({'profile': _load_payment()})
 
 
@csrf_exempt
@require_POST
def api_payment_save(request):
    """Save trading profile (amount, mode, notifications)."""
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
 
    required = ['amount', 'trade_mode']
    for k in required:
        if k not in data:
            return JsonResponse({'error': f'Missing field: {k}'}, status=400)
 
    try:
        amount = float(data['amount'])
        if amount <= 0:
            raise ValueError
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Amount must be a positive number'}, status=400)
 
    profile = {
        'amount'        : amount,
        'trade_mode'    : data.get('trade_mode', 'paper'),      # paper | auto | manual
        'risk_pct'      : float(data.get('risk_pct', 1.0)),
        'rr_ratio'      : float(data.get('rr_ratio', 2.0)),
        'notify_sms'    : bool(data.get('notify_sms', False)),
        'notify_email'  : bool(data.get('notify_email', False)),
        'sms_to'        : data.get('sms_to', ''),
        'email_to'      : data.get('email_to', ''),
        'updated_at'    : datetime.now().isoformat(),
    }
    _save_payment(profile)
 
    # Persist email/sms targets back to Django settings at runtime
    if profile['email_to']:
        settings.NOTIFY_EMAIL_TO = profile['email_to']
    if profile['sms_to']:
        settings.NOTIFY_SMS_TO = profile['sms_to']
 
    return JsonResponse({'ok': True, 'profile': profile})
 
 
# ── Manual notification test ──────────────────────────────────────
 
@csrf_exempt
@require_POST
def api_notify_test(request):
    """Send a test notification so the user can verify their setup."""
    try:
        data    = json.loads(request.body)
        channel = data.get('channel', 'email')   # 'email' | 'sms'
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
 
    subject = '[TRADER] Test notification'
    body    = (
        f"This is a test notification from BTC/USD Trader Dashboard.\n"
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"If you received this, notifications are working correctly."
    )
 
    if channel == 'sms':
        ok, msg = _send_sms(body)
    else:
        ok, msg = _send_email(subject, body)
 
    return JsonResponse({'ok': ok, 'msg': msg})
 
 
# ── Webhook called by live_trader.py on every trade ──────────────
 
@csrf_exempt
@require_POST
def api_trade_webhook(request):
    """
    live_trader.py can POST here after every order to trigger notifications.
    Payload: { "type":"ENTRY"|"EXIT", "direction":"long"|"short",
               "price":..., "sl":..., "tp":..., "qty":...,
               "investment":..., "pnl":..., "balance":..., "reason":... }
    """
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
 
    profile = _load_payment()
    trade_type = data.get('type', 'ENTRY')
 
    # Only send if at least one notification channel is enabled
    if profile.get('notify_email') or profile.get('notify_sms'):
        # Temporarily update targets from profile
        if profile.get('email_to'):
            settings.NOTIFY_EMAIL_TO = profile['email_to']
        if profile.get('sms_to'):
            settings.NOTIFY_SMS_TO = profile['sms_to']
        _notify_trade(trade_type, data)
 
    return JsonResponse({'ok': True})