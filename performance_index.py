import json 
import math
import os 
import time 
from datetime import datetime
from typing import Optional 
_DIR=os.path.dirname(os.path.abspath(__file__))
_PATH=os.path.join(_DIR,"performance_index.json")
_TMP=_PATH+".tmp"
_CANDLE_SOURCES=["bearish_reversal","bullish_reversal","bearish_continuation","bullish_continuation","bearish_reversal_high","bullish_reversal_high",
                "bearish_continuation_high","bullish_continuation_high"]
_FVG_SOURCES=["unmitigated","consolidating","inversion","reaction","support_resistance"]
_RNN_BIASES=["long","short","uncertain"]
ALL_SOURCES=_CANDLE_SOURCES+_FVG_SOURCES
# ==========================
# INTERNAL HELPERS
# ==========================
def _blank_signal_entry()->dict:
    return {"fired":0,"traded":0,"wins":0,"losses":0,"skipped":0,"pnl_total":0.0,"pnl_list":[],"last_fired":None,"last_result":None}
def _blank_rnn_entry()->dict:
    return {"fired":0,"traded":0,"wins":0,"losses":0,"skippd":0,"pnl_total":0.0,"pnl_list":[],"last_fired":None,"last_result":None}
def _load()->dict:
    if os.path.exists(_PATH):
        try:
            with open(_PATH,"r",encoding="utf-8") as f:
                store=json.load(f)
            for src in ALL_SOURCES:
                store["signals"].setdefault(src,_blank_signal_entry())
            for bias in _RNN_BIASES:
                store["rnn"].setdefault(bias,_blank_rnn_entry())
            return store
        except (json.JSONDecodeError,KeyError):
            pass
    store={
        "signals":{src:_blank_signal_entry() for src in ALL_SOURCES},
        "rnn":{bias:_blank_rnn_entry() for bias in _RNN_BIASES},
        "meta":{
            "created_at":datetime.now().isoformat(),
            "updated_at":datetime.now().isoformat(),
            "total_trades":0
        }
    }
    _save(store)
    return store 
def _save(store:dict):
    store["meta"]["updated_at"]=datetime.now().isoformat()
    with open(_TMP,"w",encoding="utf-8") as f:
        json.dump(store,f,indent=2)
    os.replace(_TMP,_PATH)
def _ts()->str:
    return datetime.now().isoformat()
def _win_rate(entry:dict)->Optional[float]:
    closed=entry["wins"]+entry["losses"]
    return (entry["wins"]/closed*100) if closed else None 
def _avg_pnl(entry:dict)->Optional[float]:
    closed=entry["wins"]+entry["losses"]
    return (entry["pnl_total"]/closed) if closed else None 
def _sharpe(entry:dict)->Optional[float]:
    pnl=entry.get("pnl_list",[])
    if len(pnl)<2:
        return None 
    mean=sum(pnl)/len(pnl)
    variance=sum((x-mean)**2 for x in pnl)/len(pnl)
    std=math.sqrt(variance)
    return (mean/std) if std else None 
def _performance_score(entry:dict)->Optional[float]:
    closed=entry["wins"]+entry["losses"]
    if closed==0:
        return None 
    wr=_win_rate(entry)
    avg=_avg_pnl(entry)
    if wr is None or avg is None:
        return None 
    wr_component=wr-50.0
    pnl_sign=1.0 if avg>=0 else -1.0
    confidence=math.tanh(closed/20.0)
    return round(wr_component*pnl_sign*confidence*2,2)
# ========================================
# PUBLIC API - called from live_trader.py
# ========================================
def signal_fired(source:str):
    store=_load()
    if source not in store["signals"]:
        store["signals"][source]=_blank_signal_entry()
    e=store["signals"][source]
    e["fired"]+=1
    e["last_fired"]=_ts()
    e["last_result"]="open"
    _save(store)
def signal_traded(source:str):
    store=_load()
    if source not in store["signals"]:
        store["signals"][source]=_blank_signal_entry()
    store["signals"][source]["traded"]+=1
    _save(store)
def signal_skipped(source:str):
    store=_load()
    if source not in store["signals"]:
        store["signals"][source]=_blank_signal_entry()
    e=store["signals"][source]
    e["skipped"]+=1
    e["last_result"]="skip"
    _save(store)
def signal_outcome(source:str,won:bool,pnl:float):
    store=_load()
    if source not in store["signals"]:
        store["signals"][source]=_blank_signal_entry()
    e=store["signals"][source]
    if won:
        e["wins"]+=1
        e["last_result"]="win"
    else:
        e["losses"]+=1
        e["last_result"]="loss"
    e["pnl_total"]+=pnl
    e["pnl_list"].append(pnl)
    store["meta"]["total_trades"]+=1
    _save(store)
def rnn_fired(bias:str,prob:float):
    store=_load()
    if bias not in store["rnn"]:
        store["rnn"][bias]=_blank_rnn_entry()
    e=store["rnn"][bias]
    e["fired"]+=1
    e["last_fired"]=_ts()
    e.setdefault("prob_sum",0.0)
    e["prob_sum"]=e.get("prob_sum",0.0)+prob
    _save(store)
def rnn_traded(bias:str):
    store=_load()
    if bias not in store["rnn"]:
        store["rnn"][bias]=_blank_rnn_entry()
    store["rnn"][bias]["traded"]+=1
    _save(store)
def rnn_skipped(bias:str):
    store=_load()
    if bias not in store["rnn"]:
        store["rnn"][bias]=_blank_rnn_entry()
    e=store["rnn"][bias]
    e["skipped"]+=1
    e["last_result"]="skip"
    _save(store)
def rnn_outcome(bias:str,won:bool,pnl:float):
    store=_load()
    if bias not in store["rnn"]:
        store["rnn"][bias]=_blank_rnn_entry()
    e=store["rnn"][bias]
    if won:
        e["wins"]+=1
        e["last_result"]="win"
    else:
        e["losses"]+=1
        e["last_result"]="loss"
    e["pnl_total"]+=pnl
    e["pnl_list"].append(pnl)
    store["meta"]["total_trades"]+=1
    _save(store)
# ========================
# REPORTING 
# ========================
def get_performance_dict()->dict:
    store=_load()
    out={"signals":[],"rnn":[],"meta":store["meta"]}
    for src,e in store["signals"].items():
        cat="candlestick" if src in _CANDLE_SOURCES else "fvg"
        out["signals"].append({
            "source":src,
            "category":cat,
            "fired":e["fired"],
            "traded":e["traded"],
            "wins":e["wins"],
            "losses":e["losses"],
            "skipped":e["skipped"],
            "win_rate":round(_win_rate(e),1) if _win_rate(e) is not None else None,
            "avg_pnl":round(_avg_pnl(e),2) if _avg_pnl(e) is not None else None,
            "pnl_total":round(e["pnl_total"],2),
            "sharpe":round(_sharpe(e),3) if _sharpe(e) is not None else None,
            "score":_performance_score(e),
            "last_fired":e["last_fired"],
            "last_result":e["last_result"]
        })
    for bias,e in store["rnn"].items():
        fired=e.get("fired",0)
        p_sum=e.get("prob_sum",0.0)
        out["rnn"].append({
            "bias":bias,
            "fired":e["fired"],
            "traded":e.get("traded",0),
            "wins":e.get("wins",0),
            "losses":e.get("losses",0),
            "skipped":e.get("skipped",0),
            "win_rate":round(_win_rate(e),1) if _win_rate(e) is not None else None,
            "avg_pnl":round(_avg_pnl(e),2) if _avg_pnl(e) is not None else None,
            "pnl_total":round(e.get("pnl_total",0.0),2),
            "sharpe":round(_sharpe(e),3) if _sharpe(e) is not None else None,
            "score":_performance_score(e),
            "avg_prob":round(p_sum/fired,4) if fired else None,
            "last_fired":e.get("last_fired"),
            "last_result":e.get("last_result")
        })
    out["signals"].sort(key=lambda x:x["score"] if x["score"] is not None else -999,reverse=True)
    return out 
def print_performance_report():
    d=get_performance_dict()
    header=lambda t: print(f"\n{'='*62}\n {t}\n{'='*62}")
    row=lambda *cols:print(" "+" ".join(str(c).ljust(w) for c,w in zip(cols,[26,6,6,5,6,6,8,8,7,8])))
    header("CANDLESTICK PATTERN PERFORMANCE")
    row("Source","Fired","Trade","Win","Loss","Skip","WinRate%","AvgPnL","Sharpe","Score")
    print(" "+"-"*58)
    for s in d["signals"]:
        if s["category"]!="candlestick":
            continue
        row(s["source"],s["fired"],s["traded"],s["wins"],s["losses"],s["skipped"],
            f"{s['win_rate']:.1f}" if s["win_rate"] is not None else "-",
            f"{s['avg_pnl']:.2f}" if s["avg_pnl"] is not None else "-",
            f"{s['sharpe']:.1f}" if s["sharpe"] is not None else "-",
            f"{s['score']:.1f}" if s["score"] is not None else "-",)
    header("FVG SIGNAL PERFORMANCE")
    row("Source","Fired","Trade","Win","Loss","Skip","WinRate%","AvgPnL","Sharpe","Score")
    print(" "+"-"*58)
    for s in d["signals"]:
        if s["category"]!="candlestick":
            continue
        row(s["source"],s["fired"],s["traded"],s["wins"],s["losses"],s["skipped"],
            f"{s['win_rate']:.1f}" if s["win_rate"] is not None else "-",
            f"{s['avg_pnl']:.2f}" if s["avg_pnl"] is not None else "-",
            f"{s['sharpe']:.1f}" if s["sharpe"] is not None else "-",
            f"{s['score']:.1f}" if s["score"] is not None else "-",)
    header("RNN MODEL PERFORMANCE")
    row("Bias","Fired","Trade","Win","Loss","Skip","WinRate%","AvgPnL","AvgProb","Score")
    print(" "+"-"*58)
    for s in d["rnn"]:
        row(s["bias"],s["fired"],s["traded"],s["wins"],s["losses"],s["skipped"],
            f"{s['win_rate']:.1f}" if s["win_rate"] is not None else "-",
            f"{s['avg_pnl']:.2f}" if s["avg_pnl"] is not None else "-",
            f"{s['avg_prob']:.1f}" if s["avg_prob"] is not None else "-",
            f"{s['score']:.1f}" if s["score"] is not None else "-",)
    print(f"\n  Total trades tracked: {d['meta']['total_trades']}")
    print(f"  Last updated: {d['meta']['updated_at']}")
    print()