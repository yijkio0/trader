import numpy as np 
import pandas as pd 
def calculate_atr(data,period):
    tr1=data['high']-data['low']
    tr2=abs(data['high']-data['close'].shift(1))
    tr3=abs(data['low']-data['close'].shift(1))
    data['TR']=pd.concat([tr1,tr2,tr3],axis=1).max(axis=1)
    data['ATR']=data['TR'].ewm(alpha=1/period,adjust=False).mean()
    return data['ATR'] 
def build_features_4hrs(data):
    data['color']=np.where(data['close']>data['open'],1,0)
    data['open']=pd.to_numeric(data['open'])
    data['close']=pd.to_numeric(data['close'])
    data['high']=pd.to_numeric(data['high'])
    data['low']=pd.to_numeric(data['low'])  
    data['price_change']=data['close'].pct_change()
    data['price_change_2']=data['close'].pct_change(2)
    data['price_change_4']=data['close'].pct_change(4)
    data['sma_5']=data['close'].rolling(5).mean()
    data['sma_10']=data['close'].rolling(10).mean()
    data['sma_20']=data['close'].rolling(20).mean()
    data['close_to_sma5']=(data['close']-data['sma_5'])/data['sma_5']
    data['close_to_sma10']=(data['close']-data['sma_10'])/data['sma_10']
    data['close_to_sma20']=(data['close']-data['sma_20'])/data['sma_20']
    period=10
    data['atr_10']=calculate_atr(data,period)
    period=20
    data['atr_20']=calculate_atr(data,period)
    data['retatr_10']=data['price_change']/data['atr_10']
    data['retatr_20']=data['price_change']/data['atr_20']
    data['volatility']=data['close'].rolling(10).std()
    delta=data['close'].diff()
    gain=delta.where(delta>0,0).rolling(14).mean()
    loss=delta.where(delta<0,0).rolling(14).mean()
    rs=gain/(loss+1e-10)
    data['rsi']=100-(100/(1+rs))
    data['rsi_slope']=data['rsi']-data['rsi'].shift(3)
    data=data.dropna()
    data=data.reset_index(drop=True)
    return data 