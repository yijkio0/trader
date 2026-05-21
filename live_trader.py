"""
Architecture 
------------
  - Data : Twelvedata (OHLCV) + Binance (volume/taker/trades)
  - Signals : Candlestick patterns (reversal/continuation) at two confirmation tiers (standard + high-conviction)
  - FVG : Fair-Value-Gap helpers (unmitigated, consolidating, inversion, reaction, support/resistance)
  - Risk : Fixed-fractional sizing, 2:1 RR, per-trade stop/target
  - Logging : CSV trade log + daily BTC performance + heartbeat + crash log (all under logs/)
  - Loop : 30-min candle cadence; sleeps until next candle boundary; single-instance lock file prevents duplicate processes
"""
# ============================
# STDLIB
# ============================
import os 
import csv 
import time 
import atexit 
import traceback 
import sys
from datetime import datetime,timedelta 
# ============================
# THIRD-PARTY 
# ============================
import pandas as pd 
import requests 
from requests.adapters import HTTPAdapter 
from urllib3.util.retry import Retry  
import numpy as np 
import joblib
try:
    import pickle
    import tensorflow as tf
    from sklearn.preprocessing import RobustScaler as _RobustScaler
    _TF_AVAILABLE=True
except ImportError:
    _TF_AVAILABLE=False
    print(f"[WARN] Tensorflow/sklearn not found - RNN filter disabled.")
try:
    from performance_index import signal_fired,signal_traded,signal_skipped,signal_outcome,rnn_fired,rnn_traded,rnn_skipped,rnn_outcome,print_performance_report
    _PERF_AVAILABLE=True 
except ImportError:
    _PERF_AVAILABLE=False 
    def signal_fired(s):pass
    def signal_traded(s):pass
    def signal_skipped(s):pass
    def signal_outcome(s,w,p):pass
    def rnn_fired(b,p):pass
    def rnn_traded(b):pass
    def rnn_skipped(b):pass
    def rnn_outcome(b,w,p):pass
    def print_performance_report():print("[PERF] performance_index.py not found.")
    print("[WARN] performance_index.py not found - performance tracking disabled.")
# ============================
# CONFIG
# ============================
API_KEY='b0b16ad6834c4ebea1b181e3afa92e21'
# API_KEY='e62c395ec9844095ada4b688d012a975'
TICKER_TD='BTC/USD'
TICKER_BN='BTCUSDT'
TIMEZONE='Asia/Calcutta'
INTERVAL_30MIN='30min'
INTERVAL_BN='30m'
INTERVAL_4H='4h'
LOOKBACK_HOURS=120
RISK_PER_TRADE=0.01
RR_RATIO=2
BALANCE=100000.0
MAX_DAILY_DD=0.05
DRY_RUN=True
BASE_DIR=os.path.dirname(os.path.abspath(__file__))
RNN_MODEL_PATH=os.path.join(BASE_DIR,"models/rnn_btc_overlap.keras")
RNN_SCALER_PATH=os.path.join(BASE_DIR,"scalers/scaler.pkl")
RNN_SEQ_LEN=72
RNN_5MIN_BARS=400
RNN_ENABLED=True 
RNN_CONFIDENCE_TIERS=[
    (0.00,0.10,"short",1.50,"VERY_HIGH_SHORT"),
    (0.10,0.20,"short",1.25,"HIGH_SHORT"),
    (0.20,0.30,"short",1.00,"MODERATE_SHORT"),
    (0.30,0.40,"short",0.75,"WEAK_SHORT"),
    (0.40,0.45,"short",0.50,"BORDERLINE_SHORT"),
    (0.45,0.60,"skip",0.00,"DEAD_ZONE"),
    (0.60,0.70,"long",0.75,"WEAK_LONG"),
    (0.70,0.80,"long",1.00,"MODERATE_LONG"),
    (0.80,0.90,"long",1.25,"HIGH_LONG"),
    (0.90,1.01,"long",1.50,"VERY_HIGH_LONG"),
]
LOG_DIR=os.path.join(BASE_DIR,"logs")
LOCK_FILE=os.path.join(BASE_DIR,"live_trader.lock")
os.makedirs(LOG_DIR,exist_ok=True)
# ============================
# SINGLE INSTANCE LOCK
# ============================
def _cleanup():
    if os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)
atexit.register(_cleanup)
if os.path.exists(LOCK_FILE):
    print(f'[WARN] Lock file exists ({LOCK_FILE}). Another instance may be running. Exiting')
    sys.exit(0)
with open(LOCK_FILE,"w") as _f:
    _f.write(str(os.getpid()))
# ============================
# LOG FILE HEADERS 
# ============================
TRADE_LOG=os.path.join(LOG_DIR,"trades.csv")
DAILY_LOG=os.path.join(LOG_DIR,"daily_btc_performance.csv")
PREDICTION_LOG=os.path.join(LOG_DIR,"predictions.log")
HEARTBEAT_LOG=os.path.join(LOG_DIR,"heartbeat.log")
CRASH_LOG=os.path.join(LOG_DIR,"crash.log")
def _ensure_csv(path,header):
    if not os.path.exists(path):
        with open(path,"w",newline="") as f:
            csv.writer(f).writerow(header)
_ensure_csv(TRADE_LOG,["timestamp","type","direction","entry_price","exit_price","quantity","investment","pnl","balance"])
_ensure_csv(DAILY_LOG,["date","btc_open","btc_close","btc_return_pct","btc_return","account_balance","daily_pnl"])
# ============================
# GLOBAL STATE 
# ============================
position=None 
balance=BALANCE 
start_balance=BALANCE 
current_day=datetime.now().date()
last_logged=None 
# ============================
# UTILITIES
# ============================
def heartbeat(msg:str):
    ts=datetime.now().isoformat()
    with open(HEARTBEAT_LOG,"a") as f:
        f.write(f"{ts} | {msg}\n")
def log_signal(msg:str):
    ts=datetime.now().isoformat()
    with open(PREDICTION_LOG,"a") as f:
        f.write(f"{ts} | {msg}\n")
    print(f"[SIGNAL] {ts} | {msg}")
def _http_session()->requests.Session:
    session=requests.Session()
    retries=Retry(total=5,backoff_factor=1,status_forcelist=[429,500,502,503,504])
    session.mount("https://",HTTPAdapter(max_retries=retries))
    return session  
# ============================
# DATA FETCHER
# ============================
def _fetch_twelvedata(start:datetime,end:datetime)->pd.DataFrame:
    s,e=start,end
    session=_http_session()
    for attempt in range(10):
        url=(f"https://api.twelvedata.com/time_series?symbol={TICKER_TD}&order=asc&timezone={TIMEZONE}&start_date={s.strftime('%Y-%m-%d %H:%M:%S')}"
             f"&end_date={e.strftime('%Y-%m-%d %H:%M:%S')}&interval={INTERVAL_30MIN}&outputsize=5000&apikey={API_KEY}")
        resp=session.get(url,timeout=15)
        data=resp.json()
        if "values" in data:
            df=pd.DataFrame(data["values"])
            if len(df)>=10:
                heartbeat(f"TwelveData OK - {len(df)} rows")
                return df
            print(f"[WARN] TwelveData returned only {len(df)} rows. Shifting window.")
            s-=timedelta(minutes=30)
            e-=timedelta(minutes=30)
        elif data.get("code")==429:
            print(f"[WARN] TwelveData rate-limit. Waiting 15 s …")
            time.sleep(15)
        else:
            raise RuntimeError(f"TwelveData error: {data}")
    raise RuntimeError("TwelveData fetch failed after 10 attempts")
def _fetch_binance_volume(start_ms:int,end_ms:int)->pd.DataFrame:
    session=_http_session()
    url=(f"https://api1.binance.com/api/v3/klines?symbol={TICKER_BN}&interval={INTERVAL_BN}&startTime={start_ms}&endTime={end_ms}&limit=1000&timeZone=5:30")
    resp=session.get(url,timeout=15)
    klines=resp.json()
    if not klines:
        raise RuntimeError(f"Binance returned empty response: {klines}")
    return pd.DataFrame({
        "open_time":pd.to_datetime([k[0] for k in klines],unit='ms'),
        "volume":[float(k[7]) for k in klines],
        "taker_volume":[float(k[10]) for k in klines],
        "no_of_trades":[float(k[8]) for k in klines]
    })
def _add_candle_fields(df:pd.DataFrame)->pd.DataFrame:
    df=df.copy()
    for col in ["open","high","low","close"]:
        df[col]=pd.to_numeric(df[col])
    df["color"]=np.where(df["close"]>df["open"],1,0)
    df["body"]=df["close"]-df["open"]
    df["abs_body"]=df["body"].abs()
    df["upper_wick"]=np.where(df["color"]==1,df["high"]-df["close"],df["high"]-df["open"])
    df["lower_wick"]=np.where(df["color"]==1,df["open"]-df["low"],df["close"]-df["low"])
    return df
def get_latest_data()->pd.DataFrame:
    now=datetime.now()
    start=now-timedelta(hours=LOOKBACK_HOURS)
    df_td=_fetch_twelvedata(start,now)
    df_td["datetime"]=(pd.to_datetime(df_td["datetime"]).dt.tz_localize("Asia/Kolkata"))
    heartbeat("TwelveData fetch completed")
    start_ms=int(start.timestamp()*1000)
    end_ms=int(now.timestamp()*1000)
    df_bn=_fetch_binance_volume(start_ms,end_ms)
    df_bn["open_time"]=(df_bn["open_time"].dt.tz_localize("UTC").dt.tz_convert("Asia/Kolkata"))
    heartbeat("Binance fetch complete")
    df=pd.merge_asof(df_td.sort_values("datetime"),df_bn.sort_values("open_time"),left_on="datetime",right_on="open_time",direction="nearest",
                     tolerance=pd.Timedelta("30min"))
    df["volume"]=pd.to_numeric(df["volume"])
    df["taker_volume"]=pd.to_numeric(df["taker_volume"])
    df["no_of_trades"]=pd.to_numeric(df["no_of_trades"])
    df["taker_buy_ratio"]=df["taker_volume"]/df["volume"].replace(0,np.nan)
    df["avg_trade_size"]=df["volume"]/df["no_of_trades"].replace(0,np.nan)
    df["vol_z"]=((df["volume"]-df["volume"].rolling(48).mean().shift(1))/df["volume"].rolling(48).std().shift(1))
    df=_add_candle_fields(df)
    df=df.reset_index(drop=True)
    heartbeat(f"Data ready - {len(df)} rows, last={df['datetime'].iloc[-1]}")
    return df 
# =======================================================
# RNN - 5-MIN DATA FETCHER + FEATURE BUILDER + PREDICTOR
# =======================================================
_rnn_model=None
_rnn_scaler=None
RNN_FEATURE_COLS=['price_change','price_change_2','price_change_3','price_change_4','price_change_5','price_change_6','sma_5','avg_trade_size','taker_buy_ratio','vol_z','realised_vol']
def _load_rnn_model():
    global _rnn_model,_rnn_scaler
    if _rnn_model is not None:
        return _rnn_model,_rnn_scaler
    if not _TF_AVAILABLE:
        return None,None
    if not os.path.exists(RNN_MODEL_PATH):
        print(f"[RNN] Model not found at {RNN_MODEL_PATH} - RNN disabled.")
        return None,None 
    if not os.path.exists(RNN_SCALER_PATH):
        print(f"[RNN] Scaler not found at {RNN_SCALER_PATH} - RNN disabled.")
        return None,None 
    try:
        _rnn_model=tf.keras.models.load_model(RNN_MODEL_PATH,compile=False)
        with open(RNN_SCALER_PATH,"rb") as f:
            _rnn_scaler=pickle.load(f)
        print(f'[RNN] Model loaded from {RNN_MODEL_PATH}')
        heartbeat('RNN model loaded')
        return _rnn_model,_rnn_scaler
    except Exception as exc:
        print(f"[RNN] Load error: {exc}")
        return None,None 
def _fetch_5min_ohlc_twelvedata(n_bars:int=RNN_5MIN_BARS)->pd.DataFrame:
    session=_http_session()
    now=datetime.now()
    start=now-timedelta(minutes=n_bars*5)
    url=(f"https://api.twelvedata.com/time_series?symbol={TICKER_TD}&interval=5min&order=asc&timezone={TIMEZONE}&start_date={start.strftime('%Y-%m-%d %H:%M:%S')}"
         f"&end_date={now.strftime('%Y-%m-%d %H:%M:%S')}&outputsize=5000&apikey={API_KEY}")
    for attempt in range(5):
        resp=session.get(url,timeout=15)
        data=resp.json()
        if "values" in data:
            df=pd.DataFrame(data["values"])
            df["datetime"]=pd.to_datetime(df["datetime"]).dt.tz_localize("Asia/Kolkata")
            for col in ["open","high","low","close"]:
                df[col]=pd.to_numeric(df[col])
            heartbeat(f"Twelvedata 5m OK - {len(df)} rows")
            return df.sort_values("datetime").reset_index(drop=True)
        elif data.get("code")==429:
            print(f"[WARN] Twelvedata 5m rate-limit. Waiting 15 s … (attempt {attempt+1})")
            time.sleep(15)
        else:
            raise RuntimeError(f"Twelvedata 5m error: {data}")
    raise RuntimeError("twelvedata 5m fetch failed after 5 attempts")
def _fetch_5min_volume_balance(n_bars:int=RNN_5MIN_BARS)->pd.DataFrame:
    session=_http_session()
    end_ms=int(datetime.now().timestamp()*1000)
    start_ms=end_ms-n_bars*5*60*1000
    url=f"https://api1.binance.com/api/v3/klines?symbol={TICKER_BN}&interval=5m&startTime={start_ms}&endTime={end_ms}&limit={min(n_bars,1000)}&timeZone=5:30"
    resp=session.get(url,timeout=15)
    klines=resp.json()
    if not klines or isinstance(klines,dict):
        raise RuntimeError(f"Binance 5m fetch failed: {klines}")
    df=pd.DataFrame({
        'open_time':pd.to_datetime([k[0] for k in klines],unit='ms'),
        'volume':[float(k[7] for k in klines)],
        'taker_volume':[float(k[10] for k in klines)],
        'no_of_trades':[float(k[8] for k in klines)]
    })
    df['open_time']=df['open_time'].dt.tz_localize('UTC').dt.tz_convert('Asia/Kolkata')
    heartbeat(f"Binance 5m volume OK - {len(df)} rows")
    return df.sort_values('open_time').reset_index(drop=True)
def _fetch_5min_binance(n_bars:int=RNN_5MIN_BARS)->pd.DataFrame:
    df_td=_fetch_5min_ohlc_twelvedata(n_bars)
    df_bn=_fetch_5min_volume_balance(n_bars)
    df=pd.merge_asof(df_td.sort_values("datetime"),df_bn.sort_values("open_time"),left_on="datetime",right_on="open_time",direction="nearest",
                     tolerance=pd.Timedelta("5min"))
    for col in ["volume","taker_volume","no_of_trades"]:
        df[col]=pd.to_numeric(df[col],errors="coerce").fillna(0.0)
    heartbeat(f"5m data merged - {len(df)} rows (TD={len(df_td)}, BN={len(df_bn)})")
    return df[["datetime","open","high","low","close","volume","taker_volume","no_of_trades"]].reset_index(drop=True)
def _build_rnn_features(df:pd.DataFrame)->pd.DataFrame:
    df=df.copy()
    df['price_change']=df['close'].pct_change(5)
    df['price_change_2']=df['close'].pct_change(10)
    df['price_change_3']=df['close'].pct_change(15)
    df['price_change_4']=df['close'].pct_change(20)
    df['price_change_5']=df['close'].pct_change(25)
    df['price_change_6']=df['close'].pct_change(30)
    df['sma_5']=df['close'].rolling(25).mean()
    df['volume']=pd.to_numeric(df['volume'])
    df['taker_volume']=pd.to_numeric(df['taker_volume'])
    df['no_of_trades']=pd.to_numeric(df['no_of_trades'])
    df['avg_trade_size']=df['volume']/(df['no_of_trades'].replace(0,np.nan))
    df['taker_buy_ratio']=df['taker_volume']/(df['volume'].replace(0,np.nan))
    roll_mean=df['volume'].rolling(240,min_periods=1).mean()
    roll_std=df['volume'].rolling(240,min_periods=1).std().replace(0,np.nan)
    df['vol_z']=(df['volume']-roll_mean)/roll_std
    df['realised_vol']=df['close'].pct_change().rolling(12).std()
    return df 
def _classify_prob(prob:float)->tuple:
    for low,high,direction,size_mult,label in RNN_CONFIDENCE_TIERS:
        if low<=prob<=high:
            return direction,size_mult,label
    return "skip",0.0,"UNCOVERED"
def rnn_predict()->tuple:
    if not RNN_ENABLED:
        return 'uncertain',0.5,0.0,"DISABLED"
    model,scaler=_load_rnn_model()
    if model is None:
        return 'uncertain',0.5,0.0,"MODEL_MISSING"
    try:
        df=_fetch_5min_binance(RNN_5MIN_BARS)
        df=_build_rnn_features(df)
        df=df.dropna(subset=RNN_FEATURE_COLS).reset_index(drop=True)
        if len(df)<RNN_SEQ_LEN:
            print(f"[RNN] Not enough rows ({len(df)} < {RNN_SEQ_LEN}) - uncertain")
            return 'uncertain',0.5
        df[RNN_FEATURE_COLS]=scaler.transform(df[RNN_FEATURE_COLS])
        seq=df[RNN_FEATURE_COLS].values[-RNN_SEQ_LEN:]
        X=seq[np.newaxis,:,:]
        prob=float(model.predict(X,verbose=0)[0][0])
        direction,size_mult,tier_label=_classify_prob(prob)
        bias=direction if direction!="skip" else "uncertain"
        print(f"[RNN] P(UP)={prob:.4f} tier={tier_label} bias={bias} size_mult={size_mult:.2f}x")
        log_signal(f"RNN P(UP)={prob:.4f} tier={tier_label} bias={bias} size_mult={size_mult:.2f}x")
        heartbeat(f"RNN prob={prob:.4f} tier={tier_label} bias={bias}")
        return bias,prob,size_mult,tier_label  
    except Exception as exc:
        tb=traceback.format_exc()
        print(f"[RNN] Prediction error: {exc}")
        with open(CRASH_LOG,'a') as f:
            f.write(f"{datetime.now().isoformat()} | RNN error: {exc}\n{tb}\n{'='*40}\n")
        return "uncertain",0.5,0.0,"ERROR"
# ================================
# CANDLESTICK PATTERNS - REVERSAL
# ================================
def bearish_reversal(data:pd.DataFrame)->int:
    try:
        if data["close"].iloc[-2]<data["close"].iloc[-1]:  # Shooting Star
            if data["color"].iloc[-1]==0: 
                if data["upper_wick"].iloc[-1]>2*data["abs_body"].iloc[-1]:
                    if data["lower_wick"].iloc[-1]<0.2*data["upper_wick"].iloc[-1]:
                        return 1
        if data["color"].iloc[-2]==1:  # Bearish Engulfing 
            if data["color"].iloc[-1]==0:
                if data["abs_body"].iloc[-1]>data["abs_body"].iloc[-2]:
                    return 2
        if all(data["color"].iloc[-3:]==1):  # Advance Block
            if data["body"].iloc[-3]>data["body"].iloc[-2]:
                if data["body"].iloc[-2]>data["body"].iloc[-1]:
                    if data["upper_wick"].iloc[-2]>data["upper_wick"].iloc[-3]:
                        if data["upper_wick"].iloc[-1]>data["upper_wick"].iloc[-3]:
                            if data["upper_wick"].iloc[-1]>data["upper_wick"].iloc[-2]:
                                return 3
        if data["color"].iloc[-1]==0:  # Hanging Man
            if data["lower_wick"].iloc[-1]>2*data["abs_body"].iloc[-1]:
                if data["upper_wick"].iloc[-1]<0.1*data["lower_wick"].iloc[-1]:
                    return 4
        if data["close"].iloc[-3]<data["close"].iloc[-2]:  # Tweezer Top
            if data["color"].iloc[-1]==0:
                if data["color"].iloc[-2]==1:
                    if 0.99*data["high"].iloc[-1]<data["high"].iloc[-2]<1.01*data["high"].iloc[-1]:  
                        return 5 
        if data["color"].iloc[-3]==1:  # Evening Star
            if data["color"].iloc[-1]==0:
                if data["upper_wick"].iloc[-3]+data["lower_wick"].iloc[-3]<data["abs_body"].iloc[-3]:
                    if data["upper_wick"].iloc[-1]+data["lower_wick"].iloc[-1]<data["abs_body"].iloc[-1]:
                        if data["abs_body"].iloc[-2]<0.2*(data["high"].iloc[-2]-data["low"].iloc[-2]):
                            if data["close"].iloc[-1]<(data["open"].iloc[-3]+data["close"].iloc[-3])/2:
                                return 6
    except (IndexError,KeyError):
        pass 
    return 0
def bullish_reversal(data:pd.DataFrame)->int:
    try:
        if data["lower_wick"].iloc[-1]>2*data["body"].iloc[-1]:  # Hammer
            if data["upper_wick"].iloc[-1]<0.2*data["lower_wick"].iloc[-1]:
                return 1
        if data["upper_wick"].iloc[-1]>2*data["body"].iloc[-1]:  # Inverted Hammer
            if data["lower_wick"].iloc[-1]<0.2*data["upper_wick"].iloc[-1]:
                return 2
        if data["color"].iloc[-2]==0:  # Bullish Engulfing
            if data["abs_body"].iloc[-2]>data["upper_wick"].iloc[-2]:
                if data["abs_body"].iloc[-2]>data["lower_wick"].iloc[-2]:
                    if data["color"].iloc[-1]==1:
                        if data["body"].iloc[-1]>data["upper_wick"].iloc[-1]:
                            if data["body"].iloc[-1]>data["lower_wick"].iloc[-1]:
                                if data["body"].iloc[-1]>data["body"].iloc[-2]:
                                    return 3
        closes=data["close"]
        if closes.iloc[-5]>closes.iloc[-4]>closes.iloc[-3]>closes.iloc[-2]>closes.iloc[-1]:  # Three Stars in the South
            if all(data["color"].iloc[-3:]==0): 
                return 4
        if data["color"].iloc[-1]==1 and data["color"].iloc[-2]==0:  # Tweezer Bottom
            if data["abs_body"].iloc[-1]>data["abs_body"].iloc[-2]:
                if 0.9*data["lower_wick"].iloc[-1]<data["lower_wick"].iloc[-2]<1.1*data["lower_wick"].iloc[-1]:
                    return 5
        if data["color"].iloc[-3]==0 and data["color"].iloc[-1]==1:  # Morning Star
            if data["abs_body"].iloc[-2]<0.1*data["abs_body"].iloc[-1]:
                if data["abs_body"].iloc[-2]<0.1*data["abs_body"].iloc[-3]:
                    return 6
    except (IndexError,KeyError):
        pass 
    return 0
# ====================================
# CANDLESTICK PATTERNS - CONTINUATION
# ====================================
def bearish_continuation(data:pd.DataFrame)->int:
    try:
        col=data["color"]
        ab=data["abs_body"]
        if col.iloc[-4]==0 and col.iloc[-3]==0 and col.iloc[-2]==0 and col.iloc[-1]==1:  # Bearish Three Line Strike 
            if ab.iloc[-4]>(ab.iloc[-3]+ab.iloc[-2]+ab.iloc[-1]):
                return 1
        if col.iloc[-5]==0 and col.iloc[-4]==1 and col.iloc[-3]==1 and col.iloc[-2]==1 and col.iloc[-1]==0:  # Falling Three Methods 
            if data["lower_wick"].iloc[-5]+data["upper_wick"].iloc[-5]<0.2*ab.iloc[-5]:
                if data["close"].iloc[-2]-data["open"].iloc[-4]<data["open"].iloc[-5]-data["close"].iloc[-5]:
                    if data["close"].iloc[-2]-data["open"].iloc[-4]<data["open"].iloc[-1]-data["close"].iloc[-1]:
                        if data["close"].iloc[-1]<data["close"].iloc[-5]:
                            return 2
        if col.iloc[-5]==0 and col.iloc[-4]==1 and col.iloc[-3]==1 and col.iloc[-2]==1 and col.iloc[-1]==0:  # Bearish Mat Hold
            if data["lower_wick"].iloc[-5]+data["upper_wick"].iloc[-5]<0.5*ab.iloc[-5]:
                if data["open"].iloc[-4]<data["close"].iloc[-5]:
                    if data["high"].iloc[-2]-data["low"].iloc[-4]<data["high"].iloc[-5]-data["low"].iloc[-5]:
                        return 3
    except (IndexError,KeyError):
        pass 
    return 0
def bullish_continuation(data:pd.DataFrame)->int:
        try:
            col=data['color']
            ab=data['abs_body']
            if col.iloc[-4]==1 and col.iloc[-3]==1 and col.iloc[-2]==1 and col.iloc[-1]==0:  # Bullish Three Line Strike 
                if ab.iloc[-1]>(ab.iloc[-4]+ab.iloc[-3]+ab.iloc[-2]):
                    return 1
            if col.iloc[-5]==1 and col.iloc[-4]==0 and col.iloc[-3]==0 and col.iloc[-2]==0 and col.iloc[-1]==1:  # Rising Three Methods 
                if data["open"].iloc[-4]-data["close"].iloc[-2]<data["close"].iloc[-5]-data["open"].iloc[-5]:
                    if data["open"].iloc[-4]-data["close"].iloc[-2]<data["close"].iloc[-1]-data["open"].iloc[-1]:
                        if data["close"].iloc[-1]>data["close"].iloc[-5]:
                            return 2 
        except (IndexError,KeyError):
            pass 
        return 0
# ============================================
# CANDLESTICK PATTERNS - HIGH CONVICTION TIER
# ============================================
def bearish_reversal_high(data:pd.DataFrame)->int:
    try:
        if data["close"].iloc[-3]<data["close"].iloc[-2]:  # Shooting Star
            if data["color"].iloc[-1]==0: 
                if data["upper_wick"].iloc[-1]>2*data["abs_body"].iloc[-1]:
                    if data["lower_wick"].iloc[-1]<0.2*data["upper_wick"].iloc[-1]:
                        return 1
        if all(data["close"].iloc[-6:-3].diff().dropna()>0):  # Advance Block
            if all(data["color"].iloc[-3:]==1):  
                if data["body"].iloc[-3]>data["body"].iloc[-2]>data["body"].iloc[-1]:
                    if data["upper_wick"].iloc[-2]>data["upper_wick"].iloc[-3]:
                        if data["upper_wick"].iloc[-1]>data["upper_wick"].iloc[-3]:
                            if data["upper_wick"].iloc[-1]>data["upper_wick"].iloc[-2]:
                                return 3
        if data["close"].iloc[-2]<data["close"].iloc[-1]:  # Hanging Man
            if data["color"].iloc[-1]==0:  
                if data["lower_wick"].iloc[-1]>2*data["abs_body"].iloc[-1]:
                    if data["upper_wick"].iloc[-1]<0.1*data["lower_wick"].iloc[-1]:
                        return 4
        if data["color"].iloc[-3]==1:  # Evening Star
            if data["color"].iloc[-1]==0:
                if data["upper_wick"].iloc[-3]+data["lower_wick"].iloc[-3]<data["abs_body"].iloc[-3]:
                    if data["upper_wick"].iloc[-1]+data["lower_wick"].iloc[-1]<data["abs_body"].iloc[-1]:
                        if data["abs_body"].iloc[-2]<0.2*(data["high"].iloc[-2]-data["low"].iloc[-2]):
                            if data["close"].iloc[-1]<(data["open"].iloc[-3]+data["close"].iloc[-3])/2:
                                return 6
    except (IndexError,KeyError):
        pass 
    return 0
def bullish_reversal_high(data):
    try:
        if data["upper_wick"].iloc[-1]>2*data["abs_body"].iloc[-1]:  # Inverted Hammer
            if data["lower_wick"].iloc[-1]<0.2*data["upper_wick"].iloc[-1]:
                return 2
        if data["close"].iloc[-3]>data["close"].iloc[-2]:  # Bullish Engulfing
            if data["color"].iloc[-2]==0 and data["color"].iloc[-1]==1:
                if data["abs_body"].iloc[-2]>data["upper_wick"].iloc[-2]:
                    if data["abs_body"].iloc[-2]>data["lower_wick"].iloc[-2]:
                        if data["body"].iloc[-1]>data["upper_wick"].iloc[-1]:
                            if data["body"].iloc[-1]>data["lower_wick"].iloc[-1]:
                                if data["body"].iloc[-1]>data["body"].iloc[-2]:
                                    return 3
        if data["color"].iloc[-1]==1 and data["color"].iloc[-2]==0:  # Tweezer Bottom
            if data["abs_body"].iloc[-1]>data["abs_body"].iloc[-2]:
                if 0.9*data["lower_wick"].iloc[-1]<data["lower_wick"].iloc[-2]<1.1*data["lower_wick"].iloc[-1]:
                    return 5
    except (IndexError,KeyError):
        pass 
    return 0
def bearish_continuation_high(data):
    try:
        col=data['color']
        ab=data['abs_body']
        if col.iloc[-5]==0 and col.iloc[-4]==1 and col.iloc[-3]==1 and col.iloc[-2]==1 and col.iloc[-1]==0:  # Bearish Mat Hold
                if data["lower_wick"].iloc[-5]+data["upper_wick"].iloc[-5]<0.5*ab.iloc[-5]:
                    if data["open"].iloc[-4]<data["close"].iloc[-5]:
                        if data["high"].iloc[-2]-data["low"].iloc[-4]<data["high"].iloc[-5]-data["low"].iloc[-5]:
                            return 3
        if col.iloc[-4]==1 and col.iloc[-3]==1 and col.iloc[-2]==1 and col.iloc[-1]==0:  # Bullish Three Line Strike 
            if ab.iloc[-1]>(ab.iloc[-4]+ab.iloc[-3]+ab.iloc[-2]):
                return 4
    except (IndexError,KeyError):
        pass 
    return 0
def bullish_continuation_high(data):
        try:
            col=data['color']
            ab=data['abs_body']
            if col.iloc[-4]==1 and col.iloc[-3]==1 and col.iloc[-2]==1 and col.iloc[-1]==0:  # Bullish Three Line Strike 
                if ab.iloc[-1]>(ab.iloc[-4]+ab.iloc[-3]+ab.iloc[-2]):
                    return 1
            if col.iloc[-5]==1 and col.iloc[-4]==0 and col.iloc[-3]==0 and col.iloc[-2]==0 and col.iloc[-1]==1:  # Rising Three Methods 
                if data["open"].iloc[-4]-data["close"].iloc[-2]<data["close"].iloc[-5]-data["open"].iloc[-5]:
                    if data["open"].iloc[-4]-data["close"].iloc[-2]<data["close"].iloc[-1]-data["open"].iloc[-1]:
                        if data["close"].iloc[-1]>data["close"].iloc[-5]:
                            return 2 
            if col.iloc[-5]==1 and col.iloc[-4]==0 and col.iloc[-3]==0 and col.iloc[-2]==0 and col.iloc[-1]==1:  # Bullish Mat Hold
                if data["high"].iloc[-4]-data["low"].iloc[-2]<data["high"].iloc[-5]-data["low"].iloc[-5]:
                    if data["close"].iloc[-1]>data["high"].iloc[-5]:
                        return 3
        except (IndexError,KeyError):
            pass 
        return 0
# ============================
# FVG HELPERS
# ============================
def unmitigated(data:pd.DataFrame):
    n=len(data)
    try:
        for i in range(1,n-16):
            lo=data['low'].iloc
            hi=data['high'].iloc
            cl=data['close'].iloc
            if lo[i+1]-hi[i-1]>25:
                mitigated=any(cl[j]<lo[i+1] for j   in range(i+2,min(i+8,n))) 
                if mitigated:
                    return 0,hi[i-1]
            elif lo[i-1]-hi[i+1]>25:
                mitigated=any(cl[j]>hi[i+1] for j in range(i+2,min(i+8,n)))
                if mitigated:
                    return 1,lo[i-1]
    except (IndexError,KeyError):
        pass
    return False,False 
def consolidating(data:pd.DataFrame):
    n=len(data)
    try:
        for i in range(1,n-24):
            lo=data['low'].iloc
            hi=data['high'].iloc
            ab=data['abs_body'].iloc
            if lo[i+1]-hi[i-1]>300:
                consolidating=False
                if ab[i+1]<50:
                    consolidating=True 
                if consolidating:
                    return 0,hi[i-1] 
            elif lo[i-1]-hi[i+1]>200:
                consolidating=False
                if ab[i+1]<25:
                    consolidating=True
                if consolidating:
                    return 1,hi[i+1]
    except (IndexError,KeyError):
        pass
    return False,False 
def inversion(data:pd.DataFrame):
    n=len(data)
    try:
        for i in range(1,n-12):
            lo=data['low'].iloc
            hi=data['high'].iloc
            if lo[i+1]-hi[i-1]>100:
                ifvg=False
                for j in range(i+4,i+12):
                    if lo[j-1]-hi[j+1]>200:
                        ifvg=True
                        break
                if ifvg:
                    return 0,hi[i-1] 
            elif lo[i-1]-hi[i+1]>100:
                ifvg=False
                for j in range(i+4,i+12):
                    if lo[j+1]-hi[j-1]>200:
                        ifvg=True 
                        break
                if ifvg:
                    return 1,hi[i+1]
    except (IndexError,KeyError):
        pass
    return False,False 
def reaction(data):
    n=len(data)
    try:
        for i in range(1,n-4):
            lo=data['low'].iloc
            hi=data['high'].iloc
            cl=data['close'].iloc
            if lo[i+1]-hi[i-1]>25:
                reaction=True  
                for j in range(i+2,i+4):
                    if cl[j]<hi[i-1]:
                        reaction=False
                if reaction:
                    return 0,hi[i-1]
            elif lo[i-1]-hi[i+1]>25:
                reaction=True 
                for j in range(i+2,i+4):
                    if cl[j]>lo[i-1]:
                        reaction=False
                if reaction:
                    return 1,hi[i+1]
    except (IndexError,KeyError):
        pass
    return False,False  
def support_resistance(data):
    n=len(data)
    try:
        for i in range(7,n-7):
            lo=data['low'].iloc
            hi=data['high'].iloc
            if lo[i+1]-hi[i-1]>25:
                support=True 
                for j in range(2,5):
                    if not (hi[i-1]<hi[i-j]<lo[i+1]):
                        support=False
                    if hi[i-j]>lo[i+1]:
                        support=False
                        break
                if support:
                    return 0,hi[i-1] 
            elif lo[i-1]-hi[i+1]>25:
                resistance=True
                for j in range(2,7):
                    if not (hi[i+1]<lo[i-j]<lo[i-1]):
                        resistance=False 
                    if lo[i-j]<hi[i+1]:
                        resistance=False
                        break
                if resistance:
                    return 1,hi[i+1]
    except (IndexError,KeyError):
        pass
    return False,False 
# ============================
# RISK MANAGEMENT 
# ============================
def calculate_position_size(entry:float,sl:float,size_mult:float=1.0)->float:
    effective_risk=RISK_PER_TRADE*max(0.0,size_mult)
    risk_amount=balance*effective_risk 
    risk_per_unit=(abs(entry-sl))
    if risk_per_unit==0 or effective_risk==0:
        return 0.0
    return risk_amount/risk_per_unit 
# =========================================
# ORDER EXECUTION (paper-trade by default)
# =========================================
def place_order(direction:str,entry:float,sl:float,tp:float,size_mult:float=1.0,tier_label:str="")->dict:
    global balance
    qty=calculate_position_size(entry,sl,size_mult)
    investment=qty*entry
    if investment>balance:
        print(f"[WARN] Insuficient balance ({balance:.2f}) < {investment:.2f}. Skipping.")
        return None
    if qty==0.0:
        print(f"[WARN] Zero qty computed (size_mult={size_mult}). Skipping.")
        return None
    pos={"direction":direction,"entry":entry,"sl":sl,"tp":tp,"qty":qty,"investment":investment,"entry_time":datetime.now().isoformat(),"source":"","rnn_bias":"",
         "size_mult":size_mult,"tier_label":tier_label}
    if not DRY_RUN:
        pass
    balance-=investment 
    with open(TRADE_LOG,"a",newline="") as f:
        csv.writer(f).writerow([pos["entry_time"],"ENTRY",direction,round(entry,2),"",round(qty,6),round(investment,2),"",round(balance,2)])
    tier_str=f"tier={tier_label}" if tier_label else ""
    print(f"[ORDER] {'[DRY]' if DRY_RUN else '[LIVE]'}"f"{direction.upper()} | entry={entry:.2f} | SL={sl:.2f} | TP={tp:.2f} | qty={qty:.6f} | size={size_mult:.2f}x"
          f"{tier_str}")
    log_signal(f"OPEN {direction.upper()} entry={entry:.2f} sl={sl:.2f} tp={tp:.2f} qty={qty:.6f} size_mult={size_mult:.2f}{tier_str}")
    return pos 
def close_position(exit_price:float,reason:str="signal"):
    global position,balance
    if position is None:
        return 
    direction=position["direction"]
    entry=position["entry"]
    qty=position["qty"]
    investment=position["investment"]
    exit_value=qty*exit_price
    if direction=="long":
        pnl=exit_value-investment
    else:
        pnl=investment-exit_value 
    balance+=investment+pnl
    if not DRY_RUN:
        pass
    pnl_pct=pnl/investment*100
    with open(TRADE_LOG,"a",newline="") as f:
        csv.writer(f).writerow([datetime.now().isoformat(),"EXIT",direction,round(entry,2),round(exit_price,2),round(qty,6),round(investment,2),round(pnl,2),
                                round(balance,2)])
    print(f"[CLOSE] {direction.upper()} @ {exit_price:.2f} | PnL={pnl:.2f} ({pnl_pct:+.2f}%) | reason={reason} | balance={balance:.2f}")
    log_signal(f"CLOSE {direction.upper()} exit={exit_price:.2f} pnl={pnl_pct:.2f}% reason={reason}")
    src_name=position.get("source","")
    rnn_b=position.get("rnn_bias","")
    won=reason=="TP"
    if src_name and src_name!="rnn":
        signal_outcome(src_name,won,pnl)
    if src_name=="rnn" and rnn_b in ("long","short"):
        rnn_outcome(rnn_b,won,pnl)
    position=None 
# ============================
# DAILY BTC PERFORMANCE LOG  
# ============================
def log_daily_btc_performance():
    global last_logged,start_balance
    yesterday=(datetime.now().date()-timedelta(days=1)).isoformat()
    if last_logged==yesterday:
        return 
    url=(f"https://api.twelvedata.com/time_series?symbol={TICKER_TD}&order=asc&timezone={TIMEZONE}&start_date={yesterday}T00:00:00&end_date={yesterday}T23:59:59"
        f"&interval=1min&outputsize=5000&apikey={API_KEY}")
    try:
        data=_http_session().get(url,timeout=15).json()
        if "values" not in data:
            print(f"[WARN] Daily BTC fetch failed: {data}")
            return
        df=pd.DataFrame(data["values"])
        df[["open","close"]]=df[["open","close"]].astype(float)
        btc_open=df.iloc[0]["open"]
        btc_close=df.iloc[-1]["close"]
        btc_ret=(btc_close-btc_open)/btc_open*100
        daily_pnl=balance-start_balance
        with open(DAILY_LOG,"a",newline="") as f:
            csv.writer(f).writerow([yesterday,round(btc_open,2),round(btc_close,2),round(btc_ret,2),round(btc_close-btc_open,2),round(balance,2),round(daily_pnl,2)])
            last_logged=yesterday
            start_balance=balance
            heartbeat("Daily BTC performance logged")
    except Exception as exc:
        print(f"[WARN] Daily BTC log error: {exc}")
# ========================================
# SLEEP UNTIL NEXT 30-MIN CANDLE BOUNDARY 
# ========================================
def sleep_until_next_candle():
    heartbeat("sleeping until next candle")
    now=datetime.now()
    minutes_past=now.minute%30
    seconds_past=now.second
    wait=(30-minutes_past)*60-seconds_past+5
    print(f"[SLEEP] Waiting {wait:.0f} s until next 30-min candle …")
    time.sleep(max(wait,5))
# ====================================
# POSITION MANAGEMENT - CHECK SL / TP
# ====================================
def check_exit(current_price:float)->bool:
    global position
    if position is None:
        return False
    direction=position["direction"]
    sl=position["sl"]
    tp=position["tp"]
    if direction=="long":
        if current_price>=tp:
            close_position(current_price,reason="TP")
            return True 
        if current_price<=sl:
            close_position(current_price,reason="SL")
            return True 
    elif direction=="short":
        if current_price<=tp:
            close_position(current_price,reason="TP")
            return True 
        if current_price>=sl:
            close_position(current_price,reason="SL")
            return True 
    return False 
# =======================================
# SIGNAL DETECTION (ordered by priority) 
# =======================================
_SIGNAL_PRIORITY=[(bearish_reversal,"short"),(bullish_reversal,"long"),(bearish_continuation,"short"),(bullish_continuation,"long"),(bearish_reversal_high,"short"),
                  (bullish_reversal_high,"long"),(bearish_continuation_high,"short"),(bullish_continuation_high,"long")]
_FVG_HELPERS=[("unmitigated",unmitigated),("consolidating",consolidating),("inversion",inversion),("reaction",reaction),("support_resistance",support_resistance)]
def detect_signal(df:pd.DataFrame)->tuple:
    for fn,direction in _SIGNAL_PRIORITY:
        try:
            result=fn(df)
        except Exception as exc:
            heartbeat(f"[ERR] {fn.__name__}: {exc}")
            continue
        if result:
            msg=(f"pattern={fn.__name__} id={result} direction={direction} price={df['close'].iloc[-1]:.2f}")
            print(f"[PATTERN] {fn.__name__} -> id={result} -> {direction}")
            log_signal(msg)
            return direction,fn.__name__,result,None 
    for name,fn in _FVG_HELPERS:
        try:
            result=fn(df)
        except Exception as exc:
            heartbeat(f"[ERR] {name}: {exc}")
            continue
        fvg_id,level=result
        if fvg_id is not False:
            direction="long" if fvg_id==0 else "short"
            msg=(f"FVG={name} direction={direction} level={level:.2f} price={df['close'].iloc[-1]:.2f}")
            print(f"[FVG] {name} -> {direction} @ level={level:.2f}")
            log_signal(msg)
            return direction,name,0,level 
    return "none","none",0,None 
# ============================
# MAIN LOOP 
# ============================
def run():
    global position,balance,start_balance,current_day
    print("="*62)
    print("  BTC/USD Live Trader + RNN Filter - Started")
    print(f"  DRY_RUN      : {DRY_RUN}")
    print(f"  Balance      : {balance:.2f}")
    print(f"  Risk/trade   : {RISK_PER_TRADE*100:.1f}%")
    print(f"  RR ratio     : {RR_RATIO}:1")
    print(f"  Max daily DD : {MAX_DAILY_DD*100:.1f}%")
    print(f"  RNN enabled  : {RNN_ENABLED}%")
    print(f"  Perf tracking: {_PERF_AVAILABLE}%")
    if RNN_ENABLED:
        tiers=[(f"{lo:.2f}-{hi:.2f}",d,f"{m:.2f}x") for lo,hi,d,m,_ in RNN_CONFIDENCE_TIERS if d!="skip"]
        print(f"RNN tiers: {len(RNN_CONFIDENCE_TIERS)} configured (dead zone 0.45-0.60)")
    print("="*62)
    heartbeat("trader started")  
    _load_rnn_model()   
    while True:
        try:
            heartbeat("loop start")
            today=datetime.now().date()
            if today!=current_day:
                log_daily_btc_performance()
                print_performance_report()
                start_balance=balance
                current_day=today
                print(f"[INFO] New trading day. Start balance = {start_balance:.2f}")
            if start_balance and balance<start_balance*(1-MAX_DAILY_DD):
                print(f"[HALT] Max daily drawdown hit ({(1-balance/start_balance)*100:.1f}%). Stopping for today.")
                sleep_until_next_candle()
                continue 
            df=get_latest_data()
            current_price=float(df["close"].iloc[-1])
            candle_time=str(df["datetime"].iloc[-1])
            pos_label="flat" if position is None else position["direction"].upper()
            print(f"\n{'-'*62}")
            print(f"[TICK] {datetime.now().strftime('%Y-%m-%d %H:%M')} | candle={candle_time} | price={current_price:,.2f} | balance={balance:,.2f} | "
                  f"position={pos_label}")
            heartbeat(f"data fetched price={current_price:.2f}")
            signal,source,pat_id,level="none","none",0,None
            rnn_bias,rnn_prob="uncertain",0.5
            rnn_size_mult=1.0
            rnn_tier=""
            if position is not None:
                exited=check_exit(current_price)
                if exited:
                    heartbeat("position closed by SL/TP")
                    sleep_until_next_candle()
                    continue
                try:
                    signal,source,pat_id,level=detect_signal(df)
                except Exception as exc:
                    heartbeat(f"detect_signal error: {exc}")
                    print(f"[ERROR] detect_signal failed: {exc}")
                if signal=="none":
                    try:
                        rnn_bias,rnn_prob,rnn_size_mult,rnn_tier=rnn_predict()
                        rnn_fired(rnn_bias,rnn_prob)
                        if rnn_bias!='uncertain':
                            signal=rnn_bias
                            source='rnn'
                            pat_id=0
                            level=None 
                            print (f"[RNN] No pattern - RNN fallback: {signal.upper()} | tier={rnn_tier} | (P(UP)={rnn_prob:.4f}) | size={rnn_size_mult:.2f}x")
                            log_signal(f"RNN fallback signal={signal} prob={rnn_prob:.4f} tier={rnn_tier} size_mult={rnn_size_mult:.2f}")
                        else:
                            print(f"[RNN] {rnn_tier} - P(UP)={rnn_prob:.4f} - staying flat.")
                            log_signal(f"RNN {rnn_tier} prob={rnn_prob:.4f}")
                    except Exception as exc:
                        heartbeat(f"rnn_predict error: {exc}")
                        print(f"[RNN] Error - no fallback signal: {exc}")
                else:
                    print(f"[RNN] Skipped - pattern already found ({source} -> {signal})")
                if position is not None and signal!="none" and signal!=position['direction']:
                    print(f"[FLIP] {signal.upper()} signal while in {position['direction'].upper()} - closing first.")
                    heartbeat(f"opposite signal flip: {position['direction']} -> {signal}")
                    close_position(current_price,reason=f"opposite_signal:{source}")
            entry_size_mult=rnn_size_mult if source=="rnn" else 1.0 
            entry_tier_label=rnn_tier if source=="rnn" else source 
            if position is None and signal!="none":
                if signal=="short":
                    entry=current_price
                    sl=float(df['high'].iloc[-1])*1.001
                    tp=entry-RR_RATIO*(sl-entry)
                    if tp>=entry:
                        print(f"[SKIP] Short TP ({tp:.2f}) >= entry ({entry:.2f}). Likely flat candle - skipping.")
                        heartbeat(f"skip short tp={tp:.2f} entry={entry:.2f}")
                        if source!="rnn":
                            signal_skipped(source)
                        else:
                            rnn_skipped(rnn_bias)
                    else:
                        new_pos=place_order("short",entry,sl,tp,size_mult=entry_size_mult,tier_label=entry_tier_label)
                        if new_pos:
                            new_pos["source"]=source
                            new_pos["rnn_bias"]=rnn_bias 
                            position=new_pos 
                            if source!="rnn":
                                signal_traded(source)
                            else:
                                rnn_traded(rnn_bias)
                            log_signal(f"ENTRY short entry={entry:.2f} sl={sl:.2f} tp={tp:.2f} source={source} rnn_prob={rnn_prob:.4f} tier={entry_tier_label}" 
                                       f"size_mult={entry_size_mult:.2f}")
                elif signal=="long":
                    entry=current_price
                    sl=float(df['low'].iloc[-1])*0.999
                    tp=entry+RR_RATIO*(entry-sl)
                    if tp<=entry:
                        print(f"[SKIP] Long TP ({tp:.2f}) <= entry ({entry:.2f}). Likely flat candle - skipping.")
                        heartbeat(f"skip long tp={tp:.2f} entry={entry:.2f}")
                        if source!="rnn":
                            signal_skipped(source)
                        else:
                            rnn_skipped(rnn_bias)
                    else:
                        new_pos=place_order("long",entry,sl,tp,size_mult=entry_size_mult,tier_label=entry_tier_label)
                        if new_pos:
                            new_pos["source"]=source
                            new_pos["rnn_bias"]=rnn_bias 
                            position=new_pos 
                            if source!="rnn":
                                signal_traded(source)
                            else:
                                rnn_traded(rnn_bias)
                            log_signal(f"ENTRY long entry={entry:.2f} sl={sl:.2f} tp={tp:.2f} source={source} rnn_prob={rnn_prob:.4f} tier={entry_tier_label} "
                                       f"size_mult={entry_size_mult:.2f}")
            elif position is None and signal=="none":
                print(f"[INFO] No signal this candle. Staying flat.")
                heartbeat("no signal")
            heartbeat("loop end - sleeping")
            sleep_until_next_candle()
        except KeyboardInterrupt:
            print("/n[EXIT] Keyboard interrupt received.")
            print_performance_report()
            if position is not None:
                print("[EXIT] Closing open position before shutdown …")
                try:
                    df=get_latest_data()
                    close_position(float(df["close"].iloc[-1]),reason="manual_exit")
                except Exception as exc:
                    print(f"[EXIT] Could not fetch price to close: {exc}")
            heartbeat("trader stopped by KeyboardInterrupt")
            break 
        except Exception as exc:
            tb=traceback.format_exc()
            ts=datetime.now().isoformat()
            msg=f"{ts} | {exc}\n{tb}\n{'='*40}\n"
            with open(CRASH_LOG,"a") as f:
                f.write(msg)
            print(f"[ERROR] {exc}")
            print(f"[ERROR] Full traceback written to {CRASH_LOG}")
            print(f"[ERROR] Retrying in 60 s …")
            time.sleep(60)
# ============================
# ENTRY POINT
# ============================
if __name__=="__main__":
    run()
