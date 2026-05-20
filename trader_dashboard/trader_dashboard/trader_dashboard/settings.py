import os
from pathlib import Path
 
BASE_DIR = Path(__file__).resolve().parent.parent
 
SECRET_KEY = 'django-insecure-btc-trader-dashboard-key-change-in-production'
DEBUG = True
ALLOWED_HOSTS = ['*']
 
INSTALLED_APPS = [
    'django.contrib.staticfiles',
    'dashboard',
]
 
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.middleware.common.CommonMiddleware',
]
 
ROOT_URLCONF = 'trader_dashboard.urls'
 
TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
            ],
        },
    },
]
 
WSGI_APPLICATION = 'trader_dashboard.wsgi.application'
STATIC_URL = '/static/'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
 
# ── Trader script lives ONE level above the Django project ────────
# Folder layout:
#   C:\Samay\build_model\
#     live_trader.py            <- trader script  (config read from here)
#     live_trader.lock
#     logs\
#     trader_dashboard\         <- Django project root  (BASE_DIR)
#       manage.py
#       trader_dashboard\
#       dashboard\
TRADER_ROOT      = BASE_DIR.parent                         # C:\Samay\build_model
TRADER_SCRIPT    = str(TRADER_ROOT / 'live_trader.py')
TRADER_LOCK_FILE = str(TRADER_ROOT / 'live_trader.lock')
TRADER_LOG_DIR   = str(TRADER_ROOT / 'logs')
 
TRADE_LOG      = os.path.join(TRADER_LOG_DIR, 'trades.csv')
DAILY_LOG      = os.path.join(TRADER_LOG_DIR, 'daily_btc_performance.csv')
PREDICTION_LOG = os.path.join(TRADER_LOG_DIR, 'predictions.log')
HEARTBEAT_LOG  = os.path.join(TRADER_LOG_DIR, 'heartbeat.log')
CRASH_LOG      = os.path.join(TRADER_LOG_DIR, 'crash.log')
 
# ── Notification settings ─────────────────────────────────────────
# Twilio SMS  (leave blank to disable)
TWILIO_ACCOUNT_SID = ''        # 'ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
TWILIO_AUTH_TOKEN  = ''        # 'your_auth_token'
TWILIO_FROM_NUMBER = ''        # '+15005550006'
NOTIFY_SMS_TO      = ''        # '+919876543210'
 
# Email via SMTP  (Gmail example — use an App Password, not your real password)
NOTIFY_EMAIL_FROM     = ''     # 'yourbot@gmail.com'
NOTIFY_EMAIL_PASSWORD = ''     # Gmail App Password
NOTIFY_EMAIL_TO       = ''     # 'you@gmail.com'
NOTIFY_EMAIL_HOST     = 'smtp.gmail.com'
NOTIFY_EMAIL_PORT     = 587