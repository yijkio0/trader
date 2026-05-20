import numpy as np 
import pandas as pd 
def build_features_30min(data):
    data['color']=np.where(data['close']>data['open'],1,0)
    data['open']=pd.to_numeric(data['open'])
    data['close']=pd.to_numeric(data['close'])
    data['body']=abs(data['open']-data['close'])
    data['high']=pd.to_numeric(data['high'])
    data['low']=pd.to_numeric(data['low'])
    data['upper_wick']=np.where(data['color']==1,data['high']-data['close'],data['high']-data['open'])
    data['lower_wick']=np.where(data['color']==1,data['open']-data['low'],data['close']-data['low'])
    data['body_to_range']=data['body']/(data['high']-data['low']+1e-10)
    data['upper_wick_ratio']=data['upper_wick']/(data['high']-data['low']+1e-10)
    data['lower_wick_ratio']=data['lower_wick']/(data['high']-data['low']+1e-10)
    data['price_change']=data['close'].pct_change()
    data['price_change_2']=data['close'].pct_change(2)
    data['price_change_4']=data['close'].pct_change(4)
    data['sma_5']=data['close'].rolling(5).mean()
    data['sma_10']=data['close'].rolling(10).mean()
    data['sma_20']=data['close'].rolling(20).mean()
    data['close_to_sma5']=(data['close']-data['sma_5'])/data['sma_5']
    data['close_to_sma10']=(data['close']-data['sma_10'])/data['sma_10']
    data['volatility']=data['close'].rolling(10).std()
    delta=data['close'].diff()
    gain=(delta.where(delta>0,0)).rolling(14).mean()
    loss=(-delta.where(delta<0,0)).rolling(14).mean()
    rs=gain/(loss+1e-10)
    data['rsi']=100-(100/(1+rs))
    data=data.dropna()
    data=data.reset_index(drop=True)
    return data