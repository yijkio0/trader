# BTC/USD Live Trader — Django Dashboard

A real-time monitoring frontend for `live_trader_complete.py`.

## Project Structure

```
trader_dashboard/           ← Django project root
├── manage.py
├── requirements.txt
├── live_trader_complete.py ← your trader (place here)
├── logs/                   ← trader writes logs here
│   ├── trades.csv
│   ├── daily_btc_performance.csv
│   ├── predictions.log
│   ├── heartbeat.log
│   └── crash.log
├── trader_dashboard/       ← Django settings/urls/wsgi
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
└── dashboard/              ← Django app
    ├── views.py            ← all API endpoints
    ├── urls.py
    └── templates/dashboard/index.html
```

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Place live_trader_complete.py in the project root
#    (same folder as manage.py)

# 3. Run the dashboard
python manage.py runserver 8000

# 4. Open http://127.0.0.1:8000 in your browser
```

## Features

| Tab | What you see |
|-----|-------------|
| **Overview** | KPIs, open position, equity curve, win/loss donut, last 10 trades |
| **Trade Log** | Full paginated trade history with P&L |
| **Daily BTC** | Daily BTC return bar chart + performance table |
| **Signals** | Live signal/pattern log (colour-coded) |
| **Heartbeat** | Loop heartbeat log |
| **Config** | Trader config parsed from the script |
| **Crashes** | Crash log with red alert indicator |

## API Endpoints

| Endpoint | Returns |
|----------|---------|
| `GET /api/status/` | Running state, KPI stats, open position, last heartbeat |
| `GET /api/trades/` | Paired entry/exit trade list |
| `GET /api/daily/` | Daily BTC performance rows |
| `GET /api/signals/` | Prediction log lines |
| `GET /api/heartbeat/` | Heartbeat log lines |
| `GET /api/config/` | Trader config key-value pairs |
| `GET /api/crashes/` | Crash log lines |
| `POST /api/control/` | `{"action":"start"}` or `{"action":"stop"}` |

## Notes

- Dashboard polls every **15 seconds** automatically.
- START/STOP buttons launch/terminate `live_trader_complete.py` as a subprocess.
- All log files are read directly from the `logs/` directory.
- No database required — all data comes from the trader's CSV/log files.
