#!/usr/bin/env python3
"""Hybrid market monitor + short-window predictor.

Uses quote/candle data as the monitoring layer, then emits only when the
short-term forecast changes enough to matter.
"""
import argparse, json, math, os, subprocess, time, urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR=Path(__file__).resolve().parent
SYMBOL=os.environ.get('QAP_SYMBOL','SNDK').upper()
STATE_PATH=Path(os.environ.get('QAP_STATE_PATH', str(BASE_DIR / 'state' / f'{SYMBOL.lower()}_state.json'))).expanduser()
ENV_PATH=Path(os.environ.get('QAP_ENV_PATH', str(BASE_DIR / '.env'))).expanduser()
TECH_TTL=15*60
FORECAST_WINDOW='15m-1h'
FORECAST_HISTORY_LIMIT=360
FORECAST_HISTORY_INTERVAL=5*60
EVAL_HORIZONS=(15*60,30*60,60*60)
EVAL_MOVE_THRESHOLD=0.7
ALERT_COOLDOWN_BY_SEVERITY={1:1800,2:1200,3:900,4:600,5:420,6:300}
BENCHMARKS=('SPY','QQQ','SMH')
BJ_TZ=ZoneInfo('Asia/Shanghai')
NY_TZ=ZoneInfo('America/New_York')

def current_symbol():
    return SYMBOL

def configure(symbol=None, state_path=None, env_path=None):
    global SYMBOL, STATE_PATH, ENV_PATH
    if symbol:
        SYMBOL=str(symbol).upper()
    if env_path:
        ENV_PATH=Path(env_path).expanduser()
    if state_path:
        STATE_PATH=Path(state_path).expanduser()
    else:
        configured=os.environ.get('QAP_STATE_PATH')
        if configured:
            STATE_PATH=Path(configured).expanduser()
        elif STATE_PATH.name == 'sndk_hf_state.json' or STATE_PATH.name.endswith('_state.json'):
            STATE_PATH=BASE_DIR / 'state' / f'{SYMBOL.lower()}_state.json'

def parse_args(argv=None):
    parser=argparse.ArgumentParser(
        description='Free-data market monitor, predictor, deduplicated alert emitter.')
    parser.add_argument('--symbol', default=os.environ.get('QAP_SYMBOL', SYMBOL),
                        help='Ticker symbol to monitor, default: SNDK or QAP_SYMBOL.')
    parser.add_argument('--env-file', default=os.environ.get('QAP_ENV_PATH', str(ENV_PATH)),
                        help='Path to .env file with data-provider API keys.')
    parser.add_argument('--state-file', default=os.environ.get('QAP_STATE_PATH'),
                        help='Path to persistent JSON state. Defaults to state/<symbol>_state.json.')
    parser.add_argument('--print-config', action='store_true',
                        help='Print resolved symbol and paths, then exit.')
    return parser.parse_args(argv)

def market_phase_info(now=None):
    now=now or time.time()
    ny=datetime.fromtimestamp(now, NY_TZ)
    minutes=ny.hour*60+ny.minute
    if ny.weekday() >= 5:
        return {'phase':'closed','label':'休市','session':'休市'}
    if 4*60 <= minutes < 9*60+30:
        return {'phase':'premarket','label':'盘前','session':'盘前'}
    if 9*60+30 <= minutes < 10*60:
        return {'phase':'open_30m','label':'开盘30分钟','session':'正常交易'}
    if 10*60 <= minutes < 11*60:
        return {'phase':'open_90m','label':'开盘30-90分钟','session':'正常交易'}
    if 11*60 <= minutes < 15*60:
        return {'phase':'midday','label':'盘中','session':'正常交易'}
    if 15*60 <= minutes < 16*60:
        return {'phase':'power_hour','label':'尾盘1小时','session':'正常交易'}
    if 16*60 <= minutes < 20*60:
        return {'phase':'afterhours','label':'盘后','session':'盘后'}
    return {'phase':'closed','label':'休市','session':'休市'}


def beijing_market_cadence(now=None):
    """Return polling cadence in seconds for the current US regular session.

    Cron may fire every minute, but this gate decides whether a tick should
    actually touch market-data APIs. Beijing 21:30-00:00 is the user's high
    priority opening window; after that, the cadence progressively slows.
    """
    now=now or time.time()
    bj=datetime.fromtimestamp(now, BJ_TZ)
    phase=market_phase_info(now)
    if phase['phase'] == 'closed':
        return None, '非美股可监控时段'
    if phase['phase'] == 'premarket':
        return 600, '盘前低频监控'
    if phase['phase'] == 'afterhours':
        return 900, '盘后低频监控'
    bj_minutes=bj.hour*60+bj.minute
    if 21*60+30 <= bj_minutes < 24*60:
        return 60, '北京21:30-00:00开盘主监控'
    if 0 <= bj_minutes < 60:
        return 120, '北京00:00-01:00降频监控'
    if 60 <= bj_minutes < 150:
        return 300, '北京01:00-02:30降频监控'
    return 600, '北京02:30后低频监控'


def should_poll_market(s):
    now=time.time()
    cadence, reason=beijing_market_cadence(now)
    if cadence is None:
        return False, reason, None
    try:
        last=float(s.get('last_poll_epoch') or 0)
    except Exception:
        last=0
    elapsed=now-last
    if elapsed < cadence:
        return False, f'{reason}，未到{cadence//60 or 1}分钟节流间隔', cadence
    return True, reason, cadence


def load_dotenv():
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            if line and not line.strip().startswith('#') and '=' in line:
                k,v=line.split('=',1); os.environ.setdefault(k.strip(), v.strip())

def get_json(url, timeout=25):
    last=None
    for _ in range(3):
        try:
            out=subprocess.check_output(['curl','-4','-sS','--connect-timeout','8','--max-time',str(timeout),'-A','Mozilla/5.0',url],stderr=subprocess.DEVNULL,timeout=timeout+5)
            txt=out.decode('utf-8','replace').strip()
            if not txt: raise RuntimeError('empty response')
            return json.loads(txt)
        except Exception as e:
            last=e; time.sleep(2)
    raise RuntimeError(f'get_json failed after retries: {last}')

def get_text(url, timeout=20):
    out=subprocess.check_output(['curl','-4','-sS','--connect-timeout','8','--max-time',str(timeout),'-A','Mozilla/5.0',url],stderr=subprocess.DEVNULL,timeout=timeout+5)
    return out.decode('utf-8','replace').strip()

def fmp_json(url, timeout=20, label='FMP'):
    raw=get_text(url, timeout)
    low=raw.lower()
    if not raw:
        raise RuntimeError(f'{label} empty response')
    if not raw.startswith(('[','{')):
        if 'invalid api key' in low or 'invalid apikey' in low:
            raise RuntimeError('FMP invalid API key')
        if any(x in low for x in ('premium', 'current subscription', 'current plan', 'subscription', 'not available')):
            raise RuntimeError(f'{label} unavailable on current plan (FMP)')
        raise RuntimeError(f'{label} returned non-json: '+raw[:160])
    data=json.loads(raw)
    if isinstance(data, dict):
        msg=str(data.get('Error Message') or data.get('error') or data.get('message') or '')
        msg_low=msg.lower()
        if 'invalid api key' in msg_low or 'invalid apikey' in msg_low:
            raise RuntimeError('FMP invalid API key')
        if msg and any(x in msg_low for x in ('premium', 'current subscription', 'current plan', 'subscription', 'not available')):
            raise RuntimeError(f'{label} unavailable on current plan (FMP)')
    return data

def finnhub_quote():
    symbol=current_symbol()
    key=os.environ.get('FINNHUB_API_KEY')
    if not key: raise RuntimeError('FINNHUB_API_KEY missing')
    url='https://finnhub.io/api/v1/quote?symbol='+urllib.parse.quote(symbol)+'&token='+urllib.parse.quote(key)
    q=get_json(url,20)
    if not q or not q.get('c'): raise RuntimeError('Finnhub quote returned no price: '+str(q)[:200])
    return {'price':float(q['c']),'day_pct':float(q.get('dp') or 0),'ts':int(q.get('t') or time.time()),'source':'Finnhub',
            'day_high':float(q.get('h') or 0),'day_low':float(q.get('l') or 0),'open':float(q.get('o') or 0),'prev_close':float(q.get('pc') or 0)}

def twelve_quote():
    symbol=current_symbol()
    key=os.environ.get('TWELVE_DATA_API_KEY')
    if not key: raise RuntimeError('TWELVE_DATA_API_KEY missing')
    url='https://api.twelvedata.com/quote?symbol='+urllib.parse.quote(symbol)+'&apikey='+urllib.parse.quote(key)
    q=get_json(url,25)
    if q.get('code') or q.get('status')=='error': raise RuntimeError('Twelve quote error: '+str(q)[:200])
    return {'price':float(q['close']),'day_pct':float(q.get('percent_change') or 0),'ts':int(q.get('last_quote_at') or q.get('timestamp') or time.time()),'source':'Twelve Data',
            'day_high':float(q.get('high') or 0),'day_low':float(q.get('low') or 0),'open':float(q.get('open') or 0),'prev_close':float(q.get('previous_close') or 0)}

def fmp_quote():
    symbol=current_symbol()
    key=os.environ.get('FMP_API_KEY')
    if not key: raise RuntimeError('FMP_API_KEY missing')
    url='https://financialmodelingprep.com/stable/quote?symbol='+urllib.parse.quote(symbol)+'&apikey='+urllib.parse.quote(key)
    data=fmp_json(url,20,'FMP quote')
    q=(data[0] if isinstance(data,list) and data else data if isinstance(data,dict) else None)
    if not q: raise RuntimeError('FMP quote returned no data: '+str(data)[:160])
    price=float(q.get('price') or q.get('close') or 0)
    if not price: raise RuntimeError('FMP quote returned no price: '+str(q)[:160])
    prev=float(q.get('previousClose') or q.get('previous_close') or q.get('prevClose') or 0)
    day_pct=q.get('changesPercentage')
    if day_pct is None and prev:
        day_pct=(price/prev-1)*100
    return {'price':price,'day_pct':float(day_pct or 0),'ts':int(q.get('timestamp') or time.time()),'source':'FMP',
            'day_high':float(q.get('dayHigh') or q.get('high') or 0),'day_low':float(q.get('dayLow') or q.get('low') or 0),
            'open':float(q.get('open') or 0),'prev_close':prev}

def yahoo_quote():
    symbol=current_symbol()
    url=f'https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range=1d&interval=1m&includePrePost=true'
    data=get_json(url,20)
    res=(data.get('chart',{}).get('result') or [])
    if not res: raise RuntimeError('Yahoo quote error: '+str(data.get('chart',{}).get('error')))
    r=res[0]; meta=r.get('meta') or {}; q=(r.get('indicators',{}).get('quote') or [{}])[0]
    ts_list=r.get('timestamp') or []; closes=q.get('close') or []
    highs=q.get('high') or []; lows=q.get('low') or []
    last=None
    for ts,c in zip(reversed(ts_list), reversed(closes)):
        if c is not None:
            last=(int(ts),float(c)); break
    if not last: raise RuntimeError('Yahoo quote returned no price')
    ts,price=last
    prev=float(meta.get('previousClose') or meta.get('chartPreviousClose') or 0)
    day_pct=(price/prev-1)*100 if prev else 0
    valid_highs=[float(x) for x in highs if x is not None]
    valid_lows=[float(x) for x in lows if x is not None]
    return {'price':price,'day_pct':day_pct,'ts':ts,'source':'Yahoo',
            'day_high':max(valid_highs) if valid_highs else 0,
            'day_low':min(valid_lows) if valid_lows else 0,
            'open':float(meta.get('regularMarketOpen') or 0),'prev_close':prev}

def quote_quality(q, now=None):
    now=now or time.time()
    phase=market_phase_info(now)
    score=100; issues=[]
    price=float(q.get('price') or 0)
    ts=float(q.get('ts') or 0)
    age=now-ts if ts else 999999
    if price <= 0:
        score-=100; issues.append('missing_price')
    if phase['phase'] in ('open_30m','open_90m','midday','power_hour'):
        if age > 20*60:
            score-=45; issues.append('stale_regular_quote')
        elif age > 5*60:
            score-=15; issues.append('quote_age_gt_5m')
    elif phase['phase'] in ('premarket','afterhours'):
        if age > 90*60:
            score-=25; issues.append('stale_extended_quote')
    high=float(q.get('day_high') or 0); low=float(q.get('day_low') or 0)
    if high and low and high < low:
        score-=35; issues.append('bad_day_range')
    if high and price > high*1.05:
        score-=20; issues.append('price_above_day_high')
    if low and price < low*0.95:
        score-=20; issues.append('price_below_day_low')
    prev=float(q.get('prev_close') or 0)
    if prev and abs((price/prev-1)*100) > 35:
        score-=10; issues.append('large_prev_close_gap')
    return {
        'score':max(0,score),
        'issues':issues,
        'age_seconds':age if age < 999999 else None,
        'phase':phase['phase'],
        'phase_label':phase['label'],
    }

def attach_quote_quality(q, now=None):
    q=dict(q)
    q['quality']=quote_quality(q, now)
    return q

def quote():
    # Quote checks are high-frequency: use Finnhub first so Twelve Data credits
    # stay reserved for lower-frequency K-line/technical refreshes. If the
    # primary quote is stale or internally inconsistent, automatically fall
    # through to other free sources.
    candidates=[]; errors=[]
    for getter in (finnhub_quote, fmp_quote, twelve_quote, yahoo_quote):
        try:
            q=attach_quote_quality(getter())
            candidates.append(q)
            if q['quality']['score'] >= 70:
                break
        except Exception as e:
            errors.append(f'{getter.__name__}: {str(e)[:160]}')
    if not candidates:
        raise RuntimeError('all quote sources failed: '+'; '.join(errors))
    best=max(candidates, key=lambda x:x.get('quality',{}).get('score',0))
    best=dict(best)
    best['candidates']=[{
        'source':c.get('source'),
        'price':c.get('price'),
        'ts':c.get('ts'),
        'score':c.get('quality',{}).get('score'),
        'issues':c.get('quality',{}).get('issues'),
    } for c in candidates]
    if errors:
        best['source_errors']=errors[-3:]
    return best

def twelve_candles(interval, outputsize=120, symbol=None):
    symbol=symbol or current_symbol()
    key=os.environ.get('TWELVE_DATA_API_KEY')
    if not key: raise RuntimeError('TWELVE_DATA_API_KEY missing')
    url=('https://api.twelvedata.com/time_series?symbol='+urllib.parse.quote(symbol)+'&interval='+urllib.parse.quote(interval)+
         '&outputsize='+str(outputsize)+'&apikey='+urllib.parse.quote(key))
    data=get_json(url,30)
    if data.get('status')!='ok' or not data.get('values'):
        raise RuntimeError('Twelve time_series error: '+str(data)[:220])
    rows=[]
    for v in reversed(data['values']):
        dt=v['datetime']
        # Twelve returns exchange-local time. Timestamp only used for display ordering; indicators unaffected.
        try:
            if len(dt)==10: ts=int(datetime.fromisoformat(dt+' 16:00:00').replace(tzinfo=timezone.utc).timestamp())
            else: ts=int(datetime.fromisoformat(dt).replace(tzinfo=timezone.utc).timestamp())
        except Exception:
            ts=0
        rows.append((ts,float(v['close']),float(v['high']),float(v['low']),int(float(v.get('volume') or 0)),dt))
    return rows

def yahoo_chart(interval='15m', symbol=None):
    symbol=symbol or current_symbol()
    rng={'15m':'10d','60m':'3mo','1d':'6mo'}[interval]
    url=f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={rng}&interval={interval}&includePrePost=false'
    data=get_json(url,25); res=(data.get('chart',{}).get('result') or [])
    if not res: raise RuntimeError('Yahoo chart error: '+str(data.get('chart',{}).get('error')))
    r=res[0]; q=(r.get('indicators',{}).get('quote') or [{}])[0]; rows=[]
    for ts,c,h,l,v in zip(r.get('timestamp') or [],q.get('close') or [],q.get('high') or [],q.get('low') or [],q.get('volume') or []):
        if c is not None and h is not None and l is not None and v is not None:
            rows.append((int(ts),float(c),float(h),float(l),int(v),datetime.fromtimestamp(int(ts),timezone.utc).strftime('%Y-%m-%d %H:%M')))
    return rows

def fmp_eod(symbol=None, outputsize=160):
    symbol=symbol or current_symbol()
    key=os.environ.get('FMP_API_KEY')
    if not key: raise RuntimeError('FMP_API_KEY missing')
    url='https://financialmodelingprep.com/stable/historical-price-eod/full?symbol='+urllib.parse.quote(symbol)+'&apikey='+urllib.parse.quote(key)
    data=fmp_json(url,25,'FMP EOD')
    rows_data=data.get('historical') if isinstance(data,dict) else data
    if not isinstance(rows_data,list) or not rows_data:
        raise RuntimeError('FMP EOD returned no rows: '+str(data)[:160])
    rows=[]
    for v in reversed(rows_data[:outputsize]):
        dt=v.get('date') or v.get('label')
        if not dt: continue
        ts=int(datetime.fromisoformat(dt+' 16:00:00').replace(tzinfo=timezone.utc).timestamp())
        rows.append((ts,float(v.get('close') or 0),float(v.get('high') or 0),float(v.get('low') or 0),int(float(v.get('volume') or 0)),dt))
    if not rows: raise RuntimeError('FMP EOD parsed no rows')
    return rows

def alpha_daily(symbol=None, outputsize=160):
    symbol=symbol or current_symbol()
    key=os.environ.get('ALPHA_VANTAGE_API_KEY')
    if not key: raise RuntimeError('ALPHA_VANTAGE_API_KEY missing')
    url=('https://www.alphavantage.co/query?function=TIME_SERIES_DAILY&symbol='+
         urllib.parse.quote(symbol)+'&outputsize=compact&apikey='+urllib.parse.quote(key))
    data=get_json(url,25)
    series=data.get('Time Series (Daily)') if isinstance(data,dict) else None
    if not series:
        raise RuntimeError('Alpha Vantage daily returned no series: '+str(data)[:160])
    rows=[]
    for dt,v in sorted(series.items())[-outputsize:]:
        ts=int(datetime.fromisoformat(dt+' 16:00:00').replace(tzinfo=timezone.utc).timestamp())
        rows.append((ts,float(v.get('4. close') or 0),float(v.get('2. high') or 0),float(v.get('3. low') or 0),int(float(v.get('5. volume') or 0)),dt))
    return rows

def candles(interval, symbol=None):
    symbol=symbol or current_symbol()
    # Intraday still prefers Twelve/Yahoo. Daily data can use FMP/Alpha as
    # low-frequency free fallbacks without burning intraday quota.
    if interval == '1day':
        for getter in (twelve_candles, fmp_eod, alpha_daily):
            try:
                if getter is twelve_candles:
                    return getter(interval, symbol=symbol)
                return getter(symbol=symbol)
            except Exception:
                pass
        return yahoo_chart('1d', symbol=symbol)
    try: return twelve_candles(interval, symbol=symbol)
    except Exception:
        ymap={'15min':'15m','1h':'60m'}
        return yahoo_chart(ymap[interval], symbol=symbol)

def ema(vals,n):
    out=[None]*len(vals)
    if len(vals)<n: return out
    a=2/(n+1); prev=sum(vals[:n])/n; out[n-1]=prev
    for i in range(n,len(vals)):
        prev=vals[i]*a+prev*(1-a); out[i]=prev
    return out

def rsi(vals,n=14):
    out=[None]*len(vals)
    if len(vals)<=n: return out
    ag=sum(max(vals[i]-vals[i-1],0) for i in range(1,n+1))/n
    al=sum(max(vals[i-1]-vals[i],0) for i in range(1,n+1))/n
    out[n]=100 if al==0 else 100-100/(1+ag/al)
    for i in range(n+1,len(vals)):
        d=vals[i]-vals[i-1]; ag=(ag*(n-1)+max(d,0))/n; al=(al*(n-1)+max(-d,0))/n
        out[i]=100 if al==0 else 100-100/(1+ag/al)
    return out

def macd(vals):
    e12=ema(vals,12); e26=ema(vals,26); line=[None if a is None or b is None else a-b for a,b in zip(e12,e26)]
    valid=[x for x in line if x is not None]; sigv=ema(valid,9); sig=[None]*len(vals); j=0
    for i,x in enumerate(line):
        if x is not None: sig[i]=sigv[j]; j+=1
    return line,sig

def sma(vals,n):
    out=[None]*len(vals)
    for i in range(n-1,len(vals)):
        out[i]=sum(vals[i-n+1:i+1])/n
    return out

def bollinger(vals,n=20,k=2):
    if len(vals)<n: return None,None,None,None
    window=vals[-n:]
    mid=sum(window)/n
    var=sum((x-mid)**2 for x in window)/n
    std=math.sqrt(var)
    upper=mid+k*std; lower=mid-k*std
    width=(upper-lower)/mid*100 if mid else None
    return upper,mid,lower,width

def atr_rows(rows,n=14):
    if len(rows)<=n: return None
    trs=[]
    for i in range(1,len(rows)):
        _,c,h,l,_,_=rows[i]; pc=rows[i-1][1]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    return sum(trs[-n:])/n if len(trs)>=n else None

def kdj(rows,n=9):
    if len(rows)<n: return None,None,None
    k=d=50.0
    for i in range(n-1,len(rows)):
        win=rows[i-n+1:i+1]
        low=min(r[3] for r in win); high=max(r[2] for r in win); close=rows[i][1]
        rsv=50.0 if high==low else (close-low)/(high-low)*100
        k=2/3*k+1/3*rsv
        d=2/3*d+1/3*k
    j=3*k-2*d
    return k,d,j

def mfi(rows,n=14):
    if len(rows)<=n: return None
    pos=neg=0.0
    prev_tp=(rows[-n-1][2]+rows[-n-1][3]+rows[-n-1][1])/3
    for row in rows[-n:]:
        _,c,h,l,v,_=row
        tp=(h+l+c)/3
        mf=tp*v
        if tp>prev_tp: pos+=mf
        elif tp<prev_tp: neg+=mf
        prev_tp=tp
    if neg==0: return 100.0
    ratio=pos/neg
    return 100-100/(1+ratio)

def obv_slope(rows,n=5):
    if len(rows)<n+1: return None
    obv=[0]
    for i in range(1,len(rows)):
        c=rows[i][1]; pc=rows[i-1][1]; v=rows[i][4]
        obv.append(obv[-1]+v if c>pc else obv[-1]-v if c<pc else obv[-1])
    return obv[-1]-obv[-1-n]

def summarize(rows):
    c=[r[1] for r in rows]; ml,ms=macd(c)
    highs=[r[2] for r in rows]; lows=[r[3] for r in rows]; vols=[r[4] for r in rows]
    bb_upper,bb_mid,bb_lower,bb_width=bollinger(c,20,2)
    k_val,d_val,j_val=kdj(rows,9)
    return {'close':c[-1],'prev_close':c[-2] if len(c)>1 else c[-1],
            'ema20':ema(c,20)[-1],'ema50':ema(c,50)[-1],'rsi14':rsi(c,14)[-1],
            'macd':ml[-1],'macd_sig':ms[-1],
            'atr14':atr_rows(rows,14),
            'bb_upper':bb_upper,'bb_mid':bb_mid,'bb_lower':bb_lower,'bb_width':bb_width,
            'kdj_k':k_val,'kdj_d':d_val,'kdj_j':j_val,
            'mfi14':mfi(rows,14),'obv_slope5':obv_slope(rows,5),
            'high20':max(highs[-20:]),'low20':min(lows[-20:]),
            'prev_high20':max(highs[-21:-1]) if len(highs)>=21 else max(highs[:-1] or highs),
            'prev_low20':min(lows[-21:-1]) if len(lows)>=21 else min(lows[:-1] or lows),
            'last3_lows':lows[-3:],'last3_closes':c[-3:],'last3_highs':highs[-3:],
            'last_volume':vols[-1],'avg20_volume':(sum(vols[-20:])/min(20,len(vols))) if vols else 0,
            'label':rows[-1][5]}

def vwap(rows):
    if not rows: return None
    day=str(rows[-1][5])[:10]; pv=vv=0
    for _,c,h,l,v,label in rows:
        if str(label).startswith(day) and v>0:
            pv+=((h+l+c)/3)*v; vv+=v
    return pv/vv if vv else None

def load_state():
    try: return json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    except Exception: return {}

def save_state(s):
    STATE_PATH.parent.mkdir(parents=True,exist_ok=True); STATE_PATH.write_text(json.dumps(s,ensure_ascii=False,indent=2))

def forecast_signature(alert_category, alert_severity, forecast, reasons):
    return '|'.join([
        alert_category,
        str(alert_severity),
        forecast['primary'],
        forecast['confidence'],
        forecast.get('prob_bucket',''),
        forecast.get('level_state',''),
        forecast.get('daily_key',''),
        '|'.join(reasons[:3]),
    ])

def semantic_session(daily_key):
    return (daily_key or '').split('|',1)[0]

def forecast_semantic_key(alert_category, alert_severity, forecast):
    return '|'.join([
        alert_category,
        str(alert_severity),
        forecast.get('primary',''),
        forecast.get('level_state',''),
        semantic_session(forecast.get('daily_key')),
    ])

def previous_semantic_key(s):
    key=s.get('last_forecast_semantic_key')
    if key:
        return key
    return '|'.join([
        str(s.get('last_alert_category') or ''),
        str(s.get('last_alert_severity') or ''),
        str(s.get('last_forecast_primary') or ''),
        str(s.get('last_forecast_level_state') or ''),
        semantic_session(s.get('last_forecast_daily_key')),
    ])

def alert_stage(alert_category):
    if alert_category in ('EXTREME_RISK','RISK_UPGRADE','SELL_EXIT','SELL_REDUCE'):
        return 'risk_control'
    if alert_category in ('BOTTOM_WATCH','REBOUND_CONFIRM'):
        return 'rebound_watch'
    if alert_category in ('TAKE_PROFIT','TRAILING_STOP'):
        return 'profit_protect'
    if alert_category == 'ABNORMAL_MOVE':
        return 'abnormal_move'
    if alert_category == 'FORECAST_SHIFT':
        return 'forecast_shift'
    return 'watch'

def forecast_state_key(alert_category, alert_severity, forecast):
    return '|'.join([
        alert_stage(alert_category),
        str(alert_severity),
        forecast.get('primary',''),
        forecast.get('level_state',''),
        semantic_session(forecast.get('daily_key')),
    ])

def slim_indicators(row):
    keys=('close','prev_close','ema20','ema50','rsi14','macd','macd_sig','atr14',
          'bb_upper','bb_mid','bb_lower','bb_width','kdj_j','mfi14','obv_slope5',
          'high20','low20','prev_high20','prev_low20','last_volume','avg20_volume','label')
    return {k:row.get(k) for k in keys if isinstance(row,dict) and k in row}

def slim_daily(daily):
    keys=('ret3','ret5','ret20','support3','support5','support20','resistance3',
          'resistance5','resistance20','near_support','near_resistance',
          'below_support_count','gap_pct','day_range_pct','volume_ratio',
          'atr_pct','atr_pctile','rel_vs_smh_5d','rel_vs_qqq_5d','rel_vs_spy_5d',
          'session','phase','phase_label','news_risk')
    out={k:daily.get(k) for k in keys if isinstance(daily,dict) and k in daily}
    if isinstance(daily,dict) and isinstance(daily.get('relative'),dict):
        out['relative']=daily.get('relative')
    return out

def pct_move(new_price, old_price):
    try:
        return (float(new_price)/float(old_price)-1)*100 if old_price else None
    except Exception:
        return None

def directional_score(primary, move):
    if move is None:
        return None
    if primary == '下行延续':
        return 1.0 if move <= -EVAL_MOVE_THRESHOLD else 0.0 if move >= EVAL_MOVE_THRESHOLD else 0.5
    if primary == '反弹修复':
        return 1.0 if move >= EVAL_MOVE_THRESHOLD else 0.0 if move <= -EVAL_MOVE_THRESHOLD else 0.5
    if primary == '高波动横盘':
        return 1.0 if abs(move) < EVAL_MOVE_THRESHOLD else 0.0
    return None

def outcome_bucket(primary, move):
    if move is None:
        return 'pending'
    score=directional_score(primary, move)
    if score == 1.0:
        return 'hit'
    if score == 0.0:
        return 'miss'
    return 'flat'

def update_forecast_evaluations(s, current_price=None, now=None):
    now=now or time.time()
    history=s.get('forecast_history')
    if not isinstance(history,list):
        return
    changed=False
    for entry in history:
        try:
            epoch=float(entry.get('epoch') or 0)
            base=float(entry.get('price') or 0)
        except Exception:
            continue
        if not epoch or not base:
            continue
        forecast=entry.get('forecast') or {}
        primary=forecast.get('primary')
        if primary not in ('下行延续','反弹修复','高波动横盘'):
            continue
        outcomes=entry.setdefault('outcomes',{})
        for horizon in EVAL_HORIZONS:
            key=f'{horizon//60}m'
            if key in outcomes and outcomes[key].get('status') == 'final':
                continue
            if now < epoch+horizon:
                continue
            move=pct_move(current_price, base)
            if move is None:
                continue
            outcomes[key]={
                'status':'final',
                'evaluated_epoch':now,
                'evaluated_price':current_price,
                'move_pct':move,
                'bucket':outcome_bucket(primary, move),
                'directional_score':directional_score(primary, move),
            }
            changed=True
    if changed:
        s['forecast_metrics']=compute_forecast_metrics(history)

def compute_forecast_metrics(history):
    by_primary={}
    overall={'count':0,'score_sum':0.0,'hit':0,'miss':0,'flat':0,'brier_sum':0.0}
    for entry in history[-FORECAST_HISTORY_LIMIT:]:
        forecast=entry.get('forecast') or {}
        primary=forecast.get('primary')
        probs=forecast.get('probs') or {}
        outcomes=entry.get('outcomes') or {}
        out=outcomes.get('60m') or outcomes.get('30m') or outcomes.get('15m')
        if not out or out.get('status') != 'final':
            continue
        score=out.get('directional_score')
        if score is None:
            continue
        bucket=out.get('bucket') or 'flat'
        p=probs.get(primary)
        try:
            prob=float(p)/100 if p is not None else 0.5
        except Exception:
            prob=0.5
        target=1.0 if bucket == 'hit' else 0.0 if bucket == 'miss' else 0.5
        brier=(prob-target)**2
        for target_dict in (overall, by_primary.setdefault(primary,{'count':0,'score_sum':0.0,'hit':0,'miss':0,'flat':0,'brier_sum':0.0})):
            target_dict['count']+=1
            target_dict['score_sum']+=float(score)
            target_dict[bucket]=target_dict.get(bucket,0)+1
            target_dict['brier_sum']+=brier
    def finalize(d):
        if d.get('count'):
            d['avg_score']=round(d['score_sum']/d['count'],3)
            d['hit_rate']=round(d.get('hit',0)/d['count'],3)
            d['avg_brier']=round(d['brier_sum']/d['count'],4)
        d.pop('score_sum',None); d.pop('brier_sum',None)
        return d
    return {'overall':finalize(overall),'by_primary':{k:finalize(v) for k,v in by_primary.items()}}

def record_forecast_history(s, *, price, day_pct, q, m15, h1, d1, daily, vw,
                            risk_score, bottom_score, profit_score,
                            alert_category, alert_severity, thesis, reasons,
                            forecast, event, emitted=False, suppressed_reason=None):
    now=time.time()
    sig=forecast_signature(alert_category, alert_severity, forecast, reasons)
    history=s.get('forecast_history')
    if not isinstance(history,list):
        history=[]
    last=history[-1] if history else {}
    try:
        last_epoch=float(last.get('epoch') or 0)
    except Exception:
        last_epoch=0
    changed=sig != last.get('signature')
    periodic=(now-last_epoch) >= FORECAST_HISTORY_INTERVAL
    if not (emitted or changed or periodic or not history):
        return
    entry={
        'epoch':now,
        'iso_bj':datetime.fromtimestamp(now, ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds'),
        'iso_ny':datetime.fromtimestamp(now, ZoneInfo('America/New_York')).isoformat(timespec='seconds'),
        'trading_day':datetime.fromtimestamp(now, ZoneInfo('America/New_York')).strftime('%Y-%m-%d'),
        'event':event,
        'emitted':bool(emitted),
        'suppressed_reason':suppressed_reason,
        'signature':sig,
        'semantic_key':forecast_semantic_key(alert_category, alert_severity, forecast),
        'state_key':forecast_state_key(alert_category, alert_severity, forecast),
        'price':price,
        'day_pct':day_pct,
        'source':q.get('source'),
        'quote_quality':q.get('quality'),
        'alert_category':alert_category,
        'alert_severity':alert_severity,
        'thesis':thesis,
        'reasons':reasons[:5],
        'forecast':{
            'window':forecast.get('window'),
            'primary':forecast.get('primary'),
            'confidence':forecast.get('confidence'),
            'probs':forecast.get('probs'),
            'ranked':forecast.get('ranked'),
            'confirm':forecast.get('confirm'),
            'invalid':forecast.get('invalid'),
            'supports':forecast.get('supports'),
            'daily_notes':forecast.get('daily_notes'),
            'level_state':forecast.get('level_state'),
            'prob_bucket':forecast.get('prob_bucket'),
            'daily_key':forecast.get('daily_key'),
            'price_gap_vwap':forecast.get('price_gap_vwap'),
            'price_gap_ema20':forecast.get('price_gap_ema20'),
            'price_gap_low20':forecast.get('price_gap_low20'),
        },
        'scores':{'risk':risk_score,'bottom':bottom_score,'profit':profit_score},
        'levels':{
            'vwap':vw,
            'm15_low20':m15.get('low20') if isinstance(m15,dict) else None,
            'm15_high20':m15.get('high20') if isinstance(m15,dict) else None,
        },
        'tech':{
            'm15':slim_indicators(m15),
            'h1':slim_indicators(h1),
            'd1':slim_indicators(d1),
            'daily':slim_daily(daily),
        },
        'market_phase':{
            'phase':daily.get('phase') if isinstance(daily,dict) else None,
            'label':daily.get('phase_label') if isinstance(daily,dict) else None,
            'session':daily.get('session') if isinstance(daily,dict) else None,
        },
    }
    history.append(entry)
    cutoff=now-3*24*3600
    cleaned=[]
    for item in history:
        try:
            item_epoch=float(item.get('epoch') or 0)
        except Exception:
            item_epoch=0
        if item_epoch >= cutoff:
            cleaned.append(item)
    s['forecast_history']=cleaned[-FORECAST_HISTORY_LIMIT:]

def fmt(x): return 'n/a' if x is None or (isinstance(x,float) and math.isnan(x)) else f'{x:.2f}'

def fmt_pct(x): return 'n/a' if x is None else f'{x:+.2f}%'

def pct_change(vals, n):
    if len(vals) <= n or not vals[-1-n]:
        return None
    return (vals[-1] / vals[-1-n] - 1) * 100

def percentile_rank(values, current):
    vals=[v for v in values if v is not None]
    if not vals or current is None:
        return None
    return sum(1 for v in vals if v <= current) / len(vals) * 100

def market_session(now=None):
    now=now or datetime.now(ZoneInfo('America/New_York'))
    minutes=now.hour*60+now.minute
    if now.weekday() >= 5:
        return '休市'
    if 4*60 <= minutes < 9*60+30:
        return '盘前'
    if 9*60+30 <= minutes < 16*60:
        return '正常交易'
    if 16*60 <= minutes < 20*60:
        return '盘后'
    return '休市'

def daily_context(rows, price, q, old=None):
    old=old or {}
    if not rows or len(rows) < 25:
        return old or {}
    closes=[r[1] for r in rows]; highs=[r[2] for r in rows]; lows=[r[3] for r in rows]; vols=[r[4] for r in rows]
    atr14=atr_rows(rows,14)
    atr_series=[]
    for i in range(15,len(rows)+1):
        atr_series.append(atr_rows(rows[:i],14))
    vol20=sum(vols[-20:])/min(20,len(vols)) if vols else None
    prev_close=q.get('prev_close') or (closes[-2] if len(closes)>1 else None)
    open_price=q.get('open') or None
    gap_pct=(open_price/prev_close-1)*100 if open_price and prev_close else None
    day_range_pct=(q.get('day_high')-q.get('day_low'))/price*100 if q.get('day_high') and q.get('day_low') and price else None
    rel={}
    bench=[]
    for symbol in BENCHMARKS:
        try:
            b_rows=candles('1day', symbol=symbol)
            b_closes=[r[1] for r in b_rows]
            r3=pct_change(b_closes,3); r5=pct_change(b_closes,5); r20=pct_change(b_closes,20)
            rel[symbol]={'ret3':r3,'ret5':r5,'ret20':r20}
            bench.append((symbol,r5))
        except Exception:
            rel[symbol]=old.get('relative',{}).get(symbol,{})
    ret3=pct_change(closes,3); ret5=pct_change(closes,5); ret20=pct_change(closes,20)
    smh5=rel.get('SMH',{}).get('ret5')
    qqq5=rel.get('QQQ',{}).get('ret5')
    spy5=rel.get('SPY',{}).get('ret5')
    rel_vs_smh=ret5-smh5 if ret5 is not None and smh5 is not None else None
    rel_vs_qqq=ret5-qqq5 if ret5 is not None and qqq5 is not None else None
    rel_vs_spy=ret5-spy5 if ret5 is not None and spy5 is not None else None
    support_candidates=[min(lows[-n:]) for n in (3,5,20) if len(lows)>=n]
    resistance_candidates=[max(highs[-n:]) for n in (3,5,20) if len(highs)>=n]
    below_support=[x for x in support_candidates if price < x*0.995]
    near_support=[x for x in support_candidates if abs(price/x-1)*100 <= 1.2]
    near_resistance=[x for x in resistance_candidates if abs(price/x-1)*100 <= 1.2 or (price < x and abs(price/x-1)*100 <= 2.0)]
    volume_ratio=vols[-1]/vol20 if vol20 else None
    atr_pct=atr14/price*100 if atr14 and price else None
    atr_pctile=percentile_rank(atr_series[-60:], atr14)
    news_risk=market_news_context(old.get('news_risk',{}))
    return {
        'ret3':ret3,'ret5':ret5,'ret20':ret20,
        'support3':support_candidates[0] if len(support_candidates)>0 else None,
        'support5':support_candidates[1] if len(support_candidates)>1 else None,
        'support20':support_candidates[2] if len(support_candidates)>2 else None,
        'resistance3':resistance_candidates[0] if len(resistance_candidates)>0 else None,
        'resistance5':resistance_candidates[1] if len(resistance_candidates)>1 else None,
        'resistance20':resistance_candidates[2] if len(resistance_candidates)>2 else None,
        'near_support':near_support[:2],
        'near_resistance':near_resistance[:2],
        'below_support_count':len(below_support),
        'gap_pct':gap_pct,
        'day_range_pct':day_range_pct,
        'volume_ratio':volume_ratio,
        'atr_pct':atr_pct,
        'atr_pctile':atr_pctile,
        'relative':rel,
        'rel_vs_smh_5d':rel_vs_smh,
        'rel_vs_qqq_5d':rel_vs_qqq,
        'rel_vs_spy_5d':rel_vs_spy,
        'news_risk':news_risk,
        'session':market_session(),
    }

def market_news_context(old=None):
    symbol=current_symbol()
    old=old or {}
    now=time.time()
    try:
        if old and now-float(old.get('updated_epoch') or 0) < 6*3600:
            return old
    except Exception:
        pass
    items=[]; errors=[]
    key=os.environ.get('MARKETAUX_API_KEY')
    if key:
        try:
            url=('https://api.marketaux.com/v1/news/all?symbols='+urllib.parse.quote(symbol)+
                 '&language=en&limit=5&api_token='+urllib.parse.quote(key))
            data=get_json(url,20)
            for item in data.get('data') or []:
                title=item.get('title') or ''
                desc=item.get('description') or ''
                sentiment=item.get('sentiment')
                items.append({'source':'Marketaux','title':title[:180],'sentiment':sentiment,'text':(title+' '+desc)[:500]})
        except Exception as e:
            errors.append('marketaux: '+str(e)[:120])
    fmp_key=os.environ.get('FMP_API_KEY')
    if fmp_key and len(items) < 3:
        try:
            url='https://financialmodelingprep.com/stable/news/stock?symbols='+urllib.parse.quote(symbol)+'&limit=5&apikey='+urllib.parse.quote(fmp_key)
            data=fmp_json(url,20,'FMP news')
            for item in (data if isinstance(data,list) else []):
                title=item.get('title') or ''
                text=item.get('text') or item.get('content') or ''
                items.append({'source':'FMP','title':title[:180],'sentiment':None,'text':(title+' '+text)[:500]})
        except Exception as e:
            if 'current plan' not in str(e).lower() and 'premium' not in str(e).lower():
                errors.append('fmp_news: '+str(e)[:120])
    negative_words=('downgrade','lawsuit','investigation','miss','cut','weak','risk','halt','recall','plunge','bearish','sell')
    positive_words=('upgrade','beat','strong','raise','bullish','buy','approval','record','surge','growth')
    neg=pos=0
    headlines=[]
    for item in items[:8]:
        text=(item.get('text') or item.get('title') or '').lower()
        neg+=sum(1 for w in negative_words if w in text)
        pos+=sum(1 for w in positive_words if w in text)
        if item.get('title'):
            headlines.append({'source':item.get('source'), 'title':item.get('title')})
    score=neg-pos
    return {
        'updated_epoch':now,
        'headline_count':len(items),
        'negative_hits':neg,
        'positive_hits':pos,
        'risk_score':score,
        'headlines':headlines[:5],
        'errors':errors[-3:],
    }

def ensure_tech(s):
    now=time.time(); old=s.get('tech') or {}
    if old and now-old.get('updated_epoch',0)<TECH_TTL: return old
    r15=candles('15min')
    tech={'updated_epoch':now,'m15':summarize(r15),'vwap':vwap(r15),'source':'TwelveData/Yahoo'}
    try: tech['h1']=summarize(candles('1h'))
    except Exception: tech['h1']=old.get('h1',{})
    try:
        d_rows=candles('1day')
        tech['d1']=summarize(d_rows)
        price=(r15[-1][1] if r15 else tech['d1'].get('close'))
        tech['daily']=daily_context(d_rows, price, {'open':tech['d1'].get('open'), 'prev_close':tech['d1'].get('prev_close')}, old.get('daily',{}))
    except Exception:
        tech['d1']=old.get('d1',{})
        tech['daily']=old.get('daily',{})
    s['tech']=tech; return tech

def trend(t):
    if t.get('ema20') and t.get('ema50'):
        if t['close']>t['ema20']>t['ema50']: return '偏强'
        if t['close']<t['ema20']<t['ema50']: return '偏弱'
    return '震荡'

def phase_adjust_scores(phase, risk_score, bottom_score, profit_score, risk_evidence, bottom_evidence, profit_evidence):
    p=phase.get('phase') if isinstance(phase,dict) else 'closed'
    if p == 'closed':
        risk_score=max(0,risk_score-3)
        bottom_score=max(0,bottom_score-2)
        profit_score=max(0,profit_score-2)
        risk_evidence.append('休市阶段降低技术信号权重')
    elif p == 'premarket':
        risk_score=max(0,risk_score-1)
        bottom_score=max(0,bottom_score-1)
        profit_score=max(0,profit_score-1)
        risk_evidence.append('盘前阶段降低低成交量信号权重')
    elif p == 'open_30m':
        risk_score+=1
        profit_score+=1
        risk_evidence.append('开盘30分钟提高风险确认权重')
    elif p == 'open_90m':
        if risk_score >= bottom_score:
            risk_score+=1
            risk_evidence.append('开盘30-90分钟延续风险权重较高')
        else:
            bottom_score+=1
            bottom_evidence.append('开盘30-90分钟反转信号开始可参考')
    elif p == 'midday':
        if abs(risk_score-bottom_score) <= 2:
            risk_score=max(0,risk_score-1)
            bottom_score=max(0,bottom_score-1)
            profit_score=max(0,profit_score-1)
    elif p == 'power_hour':
        risk_score+=1
        profit_score+=1
        risk_evidence.append('尾盘阶段提高风控权重')
    elif p == 'afterhours':
        risk_score=max(0,risk_score-2)
        bottom_score=max(0,bottom_score-1)
        profit_score=max(0,profit_score-1)
        risk_evidence.append('盘后阶段降低技术信号权重')
    return risk_score,bottom_score,profit_score

def title_action(alert_category, alert_severity):
    mapping = {
        'EXTREME_RISK': ('极端风险观察', '建议动作：下跌进入极端波动区，避免加仓；若持有杠杆产品，优先确认可承受的最大回撤。'),
        'RISK_UPGRADE': ('强风险升级', '建议动作：风险比上一档进一步扩大，继续避免加仓；若已持有，重点看仓位是否过重。'),
        'SELL_EXIT': ('卖出/离场观察', '建议动作：技术面出现强风险，优先考虑停止加仓；若已持仓，关注减仓/止损。'),
        'SELL_REDUCE': ('卖出/减仓观察', '建议动作：技术面转弱，若已持仓，关注减仓或收紧止损；未持仓不追。'),
        'BOTTOM_WATCH': ('抄底观察', '建议动作：出现止跌尝试，但不是直接梭哈信号；若要参与，优先小仓试探并等回踩不破。'),
        'REBOUND_CONFIRM': ('反弹确认观察', '建议动作：短线修复增强；若前面低位有仓，可观察能否延续到下一压力位。'),
        'TAKE_PROFIT': ('止盈/降杠杆观察', '建议动作：反弹接近压力区或短线过热；若持有杠杆产品，可考虑分批止盈/降低杠杆。'),
        'TRAILING_STOP': ('移动止盈观察', '建议动作：反弹后开始转弱；若已有盈利或减亏，可以考虑锁定部分成果。'),
        'ABNORMAL_MOVE': ('异常波动提醒', '建议动作：价格波动异常，先打开盘面确认，不急着追涨杀跌。'),
        'FORECAST_SHIFT': ('预测路径变化', '建议动作：先按新主路径设观察位；等验证价/失效价触发后再提高动作强度。'),
        'INFO': ('技术面观察', '建议动作：只作观察。'),
    }
    return mapping.get(alert_category, mapping.get('INFO'))


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def pct_diff(price, level):
    return (price / level - 1) * 100 if price and level else None


def normalize_probs(scores):
    cleaned={k:max(3.0,float(v)) for k,v in scores.items()}
    total=sum(cleaned.values()) or 1.0
    probs={k:int(round(v/total*100)) for k,v in cleaned.items()}
    drift=100-sum(probs.values())
    leader=max(probs, key=probs.get)
    probs[leader]+=drift
    return probs


def forecast_card(*, price, day_pct, risk_score, bottom_score, profit_score,
                  below_vwap, below_ema20, below_ema50, reclaim_vwap,
                  reclaim_ema20, oversold, very_oversold, kdj_oversold,
                  kdj_hot, mfi_outflow, mfi_inflow, obv_weak, obv_strong,
                  high_volatility, closes_rising, higher_low_try, macd_weak,
                  h1_state, d1_state, vw, ema20, ema50, low20, prev_high20,
                  day_low, day_high, bb_lower, bb_mid, bb_upper,
                  bounce_from_low, pullback_from_high, pressure_hits, daily):
    """Turn monitored state into an auditable short-window forecast."""
    downside=18 + risk_score*7
    if day_pct <= -10: downside += 10
    elif day_pct <= -5: downside += 5
    if below_vwap: downside += 5
    if below_ema20: downside += 6
    if below_ema50: downside += 5
    if h1_state == '偏弱': downside += 6
    if d1_state == '偏弱': downside += 4
    if macd_weak: downside += 4
    if mfi_outflow: downside += 4
    if obv_weak: downside += 4
    if high_volatility and day_pct < 0: downside += 4
    if reclaim_vwap: downside -= 8
    if reclaim_ema20: downside -= 8
    if closes_rising and higher_low_try: downside -= 5
    if daily.get('ret3') is not None and daily['ret3'] <= -8: downside += 5
    if daily.get('ret5') is not None and daily['ret5'] <= -12: downside += 6
    if daily.get('ret20') is not None and daily['ret20'] <= -20: downside += 5
    if daily.get('below_support_count',0) >= 2: downside += 8
    if daily.get('gap_pct') is not None and daily['gap_pct'] <= -4: downside += 5
    if daily.get('volume_ratio') is not None and daily['volume_ratio'] >= 1.8 and day_pct < 0: downside += 5
    if daily.get('rel_vs_smh_5d') is not None and daily['rel_vs_smh_5d'] <= -6: downside += 4

    rebound=14 + bottom_score*8
    if very_oversold: rebound += 5
    elif oversold: rebound += 3
    if kdj_oversold: rebound += 3
    if bounce_from_low >= 5: rebound += 8
    elif bounce_from_low >= 3: rebound += 4
    if higher_low_try: rebound += 4
    if closes_rising: rebound += 5
    if reclaim_vwap: rebound += 8
    if reclaim_ema20: rebound += 8
    if mfi_inflow: rebound += 4
    if obv_strong: rebound += 4
    if below_vwap and below_ema20: rebound -= 10
    if risk_score >= 8: rebound -= 7
    if daily.get('near_support'): rebound += 5
    if daily.get('ret3') is not None and daily['ret3'] <= -10 and bounce_from_low >= 3: rebound += 3
    if daily.get('gap_pct') is not None and daily['gap_pct'] >= 3 and day_pct > 0: rebound += 4
    if daily.get('rel_vs_smh_5d') is not None and daily['rel_vs_smh_5d'] >= 4: rebound += 4

    chop=26
    if high_volatility: chop += 7
    if pressure_hits: chop += 7
    if abs(day_pct) < 4: chop += 5
    if abs(risk_score-bottom_score) <= 2: chop += 8
    if bb_mid and bb_lower and bb_upper and bb_lower < price < bb_upper:
        chop += 5
    if kdj_hot or mfi_outflow: chop += 2
    if risk_score >= 9 or bottom_score >= 8: chop -= 7
    if daily.get('atr_pctile') is not None and daily['atr_pctile'] >= 80: chop += 7
    if daily.get('near_resistance') and daily.get('near_support'): chop += 5
    if daily.get('session') in ('盘前','盘后'): chop += 5

    probs=normalize_probs({
        '下行延续': clamp(downside, 5, 95),
        '反弹修复': clamp(rebound, 5, 95),
        '高波动横盘': clamp(chop, 5, 95),
    })
    ranked=sorted(probs.items(), key=lambda kv: kv[1], reverse=True)
    primary, primary_prob=ranked[0]
    secondary, secondary_prob=ranked[1]
    gap=primary_prob-secondary_prob
    confidence='高' if primary_prob >= 50 and gap >= 15 else '中' if primary_prob >= 42 and gap >= 8 else '低'

    supports=[]
    daily_notes=[]
    if daily.get('ret3') is not None or daily.get('ret5') is not None or daily.get('ret20') is not None:
        daily_notes.append(f'3/5/20日涨跌：{fmt_pct(daily.get("ret3"))} / {fmt_pct(daily.get("ret5"))} / {fmt_pct(daily.get("ret20"))}')
    if daily.get('gap_pct') is not None:
        daily_notes.append(f'跳空缺口：{fmt_pct(daily.get("gap_pct"))}')
    if daily.get('volume_ratio') is not None:
        daily_notes.append(f'日线量比：{daily["volume_ratio"]:.2f}x')
    if daily.get('atr_pctile') is not None:
        daily_notes.append(f'ATR分位：{daily["atr_pctile"]:.0f}%')
    if daily.get('rel_vs_smh_5d') is not None:
        daily_notes.append(f'5日相对SMH：{fmt_pct(daily.get("rel_vs_smh_5d"))}')
    if daily.get('session'):
        daily_notes.append(f'时段：{daily["session"]}')
    if primary == '下行延续':
        supports=(['风险分仍占优'] if risk_score >= bottom_score else [])
        if below_vwap: supports.append('仍在VWAP下方')
        if below_ema20: supports.append('未收回15m趋势线')
        if h1_state == '偏弱': supports.append('1小时趋势偏弱')
        if daily.get('ret5') is not None and daily['ret5'] <= -12: supports.append('5日级别跌幅较深')
        if daily.get('below_support_count',0) >= 2: supports.append('跌破多条日线支撑')
        if daily.get('rel_vs_smh_5d') is not None and daily['rel_vs_smh_5d'] <= -6: supports.append('相对半导体板块明显弱势')
        if mfi_outflow or obv_weak: supports.append('量价/资金未修复')
        confirm_candidates=[x for x in [low20, day_low, bb_lower, daily.get('support3'), daily.get('support5'), daily.get('support20')] if x]
        invalidate_candidates=[x for x in [vw, ema20, bb_mid, daily.get('resistance3'), daily.get('resistance5')] if x and x > price]
        confirm=min(confirm_candidates) if confirm_candidates else price*0.985
        invalidate=min(invalidate_candidates) if invalidate_candidates else price*1.015
        trigger=f'跌破/守不住 {fmt(confirm)}，下行路径继续验证'
        invalid=f'重新站上 {fmt(invalidate)} 并维持，当前下行预测失效'
        level_state='confirmed' if price <= confirm else 'invalidated' if price >= invalidate else 'pending'
    elif primary == '反弹修复':
        supports=(['反弹分开始压过风险分'] if bottom_score >= risk_score else [])
        if bounce_from_low >= 3: supports.append(f'低点反弹{bounce_from_low:.2f}%')
        if closes_rising: supports.append('短线连续收高')
        if higher_low_try: supports.append('出现更高低点')
        if reclaim_vwap or reclaim_ema20: supports.append('已收回关键短线位')
        if daily.get('near_support'): supports.append('贴近日线支撑区')
        if daily.get('rel_vs_smh_5d') is not None and daily['rel_vs_smh_5d'] >= 4: supports.append('相对半导体板块转强')
        confirm_candidates=[x for x in [vw, ema20, bb_mid, prev_high20, daily.get('resistance3'), daily.get('resistance5')] if x and x > price]
        invalidate_candidates=[x for x in [day_low, low20, bb_lower, daily.get('support3'), daily.get('support5')] if x]
        confirm=min(confirm_candidates) if confirm_candidates else price*1.018
        invalidate=max([x for x in invalidate_candidates if x < price], default=price*0.985)
        trigger=f'放量站上 {fmt(confirm)}，反弹修复路径确认度提高'
        invalid=f'跌回 {fmt(invalidate)} 下方，反弹预测失效'
        level_state='confirmed' if price >= confirm else 'invalidated' if price <= invalidate else 'pending'
    else:
        supports=[]
        if pressure_hits: supports.append('价格贴近压力/参考位：'+'、'.join(pressure_hits[:3]))
        if high_volatility: supports.append('波动率高，容易拉扯')
        if abs(risk_score-bottom_score) <= 2: supports.append('多空分差不大')
        if bb_lower and bb_upper: supports.append(f'布林区间 {fmt(bb_lower)}-{fmt(bb_upper)}')
        if daily.get('session') in ('盘前','盘后'): supports.append(f'{daily["session"]}信号降低成交量权重')
        lower=max([x for x in [day_low, low20, bb_lower, daily.get('support3'), daily.get('support5')] if x and x < price], default=price*0.985)
        upper=min([x for x in [vw, ema20, prev_high20, bb_upper, daily.get('resistance3'), daily.get('resistance5')] if x and x > price], default=price*1.015)
        trigger=f'向上突破 {fmt(upper)} 或向下跌破 {fmt(lower)}，横盘路径结束'
        invalid='出现连续两根15m收盘同向突破区间，横盘预测失效'
        level_state='break_up' if price >= upper else 'break_down' if price <= lower else 'pending'

    if not supports:
        supports.append('当前路径由综合评分主导，单项证据不强')

    prob_bucket='|'.join(f'{k}:{int(v/5)*5}' for k,v in ranked)
    daily_key='|'.join([
        daily.get('session') or '',
        str(round(daily.get('ret5') or 0,1)),
        str(round(daily.get('rel_vs_smh_5d') or 0,1)),
        str(round(daily.get('gap_pct') or 0,1)),
        str(daily.get('below_support_count',0)),
    ])
    return {
        'window': FORECAST_WINDOW,
        'probs': probs,
        'ranked': ranked,
        'primary': primary,
        'secondary': secondary,
        'confidence': confidence,
        'supports': supports[:4],
        'daily_notes': daily_notes[:6],
        'confirm': trigger,
        'invalid': invalid,
        'level_state': level_state,
        'prob_bucket': prob_bucket,
        'daily_key': daily_key,
        'price_gap_vwap': pct_diff(price, vw),
        'price_gap_ema20': pct_diff(price, ema20),
        'price_gap_low20': pct_diff(price, low20),
    }


def should_emit(s, *, alert_category, alert_severity, price, reasons, forecast):
    if alert_severity <= 0:
        return False, 'no actionable severity'
    prev_sig=s.get('last_forecast_signature')
    signature=forecast_signature(alert_category, alert_severity, forecast, reasons)
    semantic_key=forecast_semantic_key(alert_category, alert_severity, forecast)
    prev_semantic=previous_semantic_key(s)
    state_key=forecast_state_key(alert_category, alert_severity, forecast)
    prev_state=s.get('last_alert_state_key')
    prev_price=s.get('last_alert_price')
    prev_primary=s.get('last_forecast_primary')
    price_move=abs((price/prev_price-1)*100) if prev_price else 999
    path_changed=forecast['primary'] != prev_primary
    severity_changed=alert_severity != s.get('last_alert_severity')
    category_changed=alert_category != s.get('last_alert_category')
    prev_level=s.get('last_forecast_level_state')
    level_changed=forecast.get('level_state') != prev_level
    material_move=price_move >= (2.0 if alert_severity <= 2 else 3.0 if alert_severity <= 4 else 4.0)
    session=semantic_session(forecast.get('daily_key'))
    same_semantic=semantic_key == prev_semantic
    structural_change=any([path_changed, severity_changed, category_changed, level_changed])
    state_changed=state_key != prev_state

    if signature == prev_sig:
        return False, 'same forecast signature'
    if not state_changed and not material_move:
        return False, 'same alert state'
    if session == '休市' and same_semantic and not material_move:
        return False, 'same resting-session forecast'
    if same_semantic and not material_move:
        return False, 'same semantic forecast'
    if not any([state_changed, structural_change, material_move]):
        return False, 'no actionable forecast change'
    return True, 'emit'


def main(argv=None):
    args=parse_args(argv)
    configure(symbol=args.symbol, state_path=args.state_file, env_path=args.env_file)
    if args.print_config:
        print(json.dumps({
            'symbol': SYMBOL,
            'env_file': str(ENV_PATH),
            'state_file': str(STATE_PATH),
        }, ensure_ascii=False, indent=2))
        return
    load_dotenv(); s=load_state()
    phase=market_phase_info()
    should_poll, poll_reason, poll_cadence=should_poll_market(s)
    if not should_poll:
        s['last_skip_epoch']=time.time()
        s['last_skip_reason']=poll_reason
        save_state(s)
        return
    s['last_poll_epoch']=time.time()
    s['last_poll_reason']=poll_reason
    if poll_cadence:
        s['last_poll_cadence_seconds']=poll_cadence
    save_state(s)
    try:
        q=quote(); tech=ensure_tech(s)
    except Exception as e:
        # Data-source outages should not spam the user or wake an LLM. Keep the
        # no-agent cron healthy and remember the failure for inspection.
        s['last_data_error_epoch']=time.time()
        s['last_data_error']=str(e)[:500]
        save_state(s)
        return
    update_forecast_evaluations(s, q.get('price'), time.time())
    price=q['price']; day_pct=q['day_pct']; prev=s.get('last_price')
    s['last_quote_quality']=q.get('quality')
    s['last_quote_candidates']=q.get('candidates')
    s['last_market_phase']=phase
    if (q.get('quality') or {}).get('score',100) < 50:
        s['last_data_error_epoch']=time.time()
        s['last_data_error']='low quote quality: '+json.dumps(q.get('quality'), ensure_ascii=False)
        save_state(s)
        return
    m15=tech.get('m15',{}); h1=tech.get('h1',{}); d1=tech.get('d1',{}); daily=tech.get('daily',{}); vw=tech.get('vwap')
    if q.get('open') and q.get('prev_close'):
        daily=dict(daily)
        daily['gap_pct']=(q['open']/q['prev_close']-1)*100
        daily['session']=phase.get('session') or market_session()
        daily['phase']=phase.get('phase')
        daily['phase_label']=phase.get('label')
    low20=m15.get('low20'); prev_high20=m15.get('prev_high20') or m15.get('high20')
    ema20=m15.get('ema20'); ema50=m15.get('ema50'); rsi14=m15.get('rsi14')
    macd_v=m15.get('macd'); macd_s=m15.get('macd_sig')
    atr14=m15.get('atr14'); bb_lower=m15.get('bb_lower'); bb_mid=m15.get('bb_mid'); bb_upper=m15.get('bb_upper'); bb_width=m15.get('bb_width')
    kdj_j=m15.get('kdj_j'); mfi14=m15.get('mfi14'); obv_slope=m15.get('obv_slope5')
    last3_lows=m15.get('last3_lows') or []; last3_closes=m15.get('last3_closes') or []
    day_low=q.get('day_low') or 0; day_high=q.get('day_high') or 0; prev_close=q.get('prev_close') or 0

    def pct_to(level): return (price/level-1)*100 if level else None
    below_vwap = bool(vw and price < vw * 0.995)
    below_ema20 = bool(ema20 and price < ema20 * 0.995)
    below_ema50 = bool(ema50 and price < ema50 * 0.995)
    reclaim_vwap = bool(vw and price > vw * 1.005)
    reclaim_ema20 = bool(ema20 and price > ema20 * 1.003)
    oversold = bool(rsi14 is not None and rsi14 < 30)
    very_oversold = bool(rsi14 is not None and rsi14 < 22)
    kdj_oversold = bool(kdj_j is not None and kdj_j < 20)
    kdj_hot = bool(kdj_j is not None and kdj_j > 90)
    mfi_outflow = bool(mfi14 is not None and mfi14 < 35)
    mfi_inflow = bool(mfi14 is not None and mfi14 > 55)
    mfi_hot = bool(mfi14 is not None and mfi14 > 80)
    obv_weak = bool(obv_slope is not None and obv_slope < 0)
    obv_strong = bool(obv_slope is not None and obv_slope > 0)
    near_lower_band = bool(bb_lower and price <= bb_lower * 1.01)
    reclaim_lower_band = bool(bb_lower and price > bb_lower * 1.02)
    high_volatility = bool(atr14 and price and atr14 / price > 0.025)
    bounce_from_low = (price/day_low-1)*100 if day_low else 0
    pullback_from_high = (price/day_high-1)*100 if day_high else 0
    higher_low_try = len(last3_lows) >= 2 and last3_lows[-1] > last3_lows[-2]
    closes_rising = len(last3_closes) >= 3 and last3_closes[-1] > last3_closes[-2] > last3_closes[-3]
    macd_weak = bool(macd_v is not None and macd_s is not None and macd_v < macd_s)
    h1_state=trend(h1); d1_state=trend(d1)

    # Composite technical scoring. The script now decides from combined evidence, not one indicator.
    risk_score=0; risk_evidence=[]
    if day_pct <= -15: risk_score+=4; risk_evidence.append('日内跌幅超过15%')
    elif day_pct <= -10: risk_score+=3; risk_evidence.append('日内跌幅超过10%')
    elif day_pct <= -5: risk_score+=1; risk_evidence.append('日内跌幅超过5%')
    if below_vwap: risk_score+=1; risk_evidence.append('价格低于VWAP')
    if below_ema20: risk_score+=1; risk_evidence.append('价格低于15m趋势线')
    if below_ema50: risk_score+=1; risk_evidence.append('价格低于15m中期线')
    if low20 and price < low20 * 0.992: risk_score+=2; risk_evidence.append('有效跌破短线支撑')
    elif low20 and price < low20 * 0.997: risk_score+=1; risk_evidence.append('轻微跌破短线支撑')
    if h1_state == '偏弱': risk_score+=1; risk_evidence.append('1小时趋势偏弱')
    if d1_state == '偏弱': risk_score+=1; risk_evidence.append('日线趋势偏弱')
    if daily.get('ret5') is not None and daily['ret5'] <= -12: risk_score+=2; risk_evidence.append('5日跌幅较深')
    if daily.get('ret20') is not None and daily['ret20'] <= -20: risk_score+=1; risk_evidence.append('20日结构偏弱')
    if daily.get('below_support_count',0) >= 2: risk_score+=2; risk_evidence.append('跌破多条日线支撑')
    if daily.get('gap_pct') is not None and daily['gap_pct'] <= -4: risk_score+=1; risk_evidence.append('向下跳空缺口')
    if daily.get('volume_ratio') is not None and daily['volume_ratio'] >= 1.8 and day_pct < 0: risk_score+=1; risk_evidence.append('日线放量下跌')
    if daily.get('rel_vs_smh_5d') is not None and daily['rel_vs_smh_5d'] <= -6: risk_score+=1; risk_evidence.append('相对SMH明显弱势')
    news_risk=daily.get('news_risk') or {}
    if news_risk.get('risk_score',0) >= 2:
        risk_score+=1; risk_evidence.append('新闻/公告风险偏负面')
    elif news_risk.get('risk_score',0) <= -2:
        bottom_score_bonus_from_news=1
    else:
        bottom_score_bonus_from_news=0
    if macd_weak: risk_score+=1; risk_evidence.append('短线动能仍弱')
    if mfi_outflow: risk_score+=1; risk_evidence.append('资金流偏弱')
    if obv_weak: risk_score+=1; risk_evidence.append('量能趋势偏弱')
    if high_volatility and day_pct < 0: risk_score+=1; risk_evidence.append('波动率处于高位')

    bottom_score=0; bottom_evidence=[]
    if oversold: bottom_score+=1; bottom_evidence.append('短线超卖')
    if very_oversold: bottom_score+=1; bottom_evidence.append('超卖程度较深')
    if kdj_oversold: bottom_score+=1; bottom_evidence.append('KDJ进入超卖区')
    if near_lower_band: bottom_score+=1; bottom_evidence.append('接近布林带下轨')
    if daily.get('near_support'): bottom_score+=1; bottom_evidence.append('靠近日线支撑区')
    if daily.get('ret3') is not None and daily['ret3'] <= -10 and bounce_from_low >= 3: bottom_score+=1; bottom_evidence.append('多日急跌后出现日内反抽')
    if daily.get('rel_vs_smh_5d') is not None and daily['rel_vs_smh_5d'] >= 4: bottom_score+=1; bottom_evidence.append('相对SMH转强')
    if bottom_score_bonus_from_news:
        bottom_score+=bottom_score_bonus_from_news; bottom_evidence.append('新闻/公告风险偏正面')
    if reclaim_lower_band: bottom_score+=1; bottom_evidence.append('脱离布林带下轨')
    if bounce_from_low >= 3: bottom_score+=2; bottom_evidence.append(f'从日内低点反弹{bounce_from_low:.2f}%')
    if bounce_from_low >= 5: bottom_score+=1; bottom_evidence.append('反弹幅度达到确认观察区')
    if higher_low_try: bottom_score+=1; bottom_evidence.append('出现更高低点尝试')
    if closes_rising: bottom_score+=1; bottom_evidence.append('连续收高')
    if reclaim_vwap: bottom_score+=2; bottom_evidence.append('重新站上VWAP')
    if reclaim_ema20: bottom_score+=2; bottom_evidence.append('重新站上15m趋势线')
    if mfi_inflow: bottom_score+=1; bottom_evidence.append('资金流改善')
    if obv_strong: bottom_score+=1; bottom_evidence.append('量能开始支持反弹')
    if below_vwap and below_ema20: bottom_score-=2; bottom_evidence.append('但仍未站回关键参考位')

    pressure_hits=[]
    for name, level in [('VWAP', vw), ('15m趋势线', ema20), ('15m中期线', ema50), ('前收', prev_close), ('短线压力', prev_high20)]:
        if level and abs(pct_to(level)) <= 0.8:
            pressure_hits.append(name)
    profit_score=0; profit_evidence=[]
    if bounce_from_low >= 4: profit_score+=2; profit_evidence.append(f'低位反弹{bounce_from_low:.2f}%')
    if pressure_hits: profit_score+=2; profit_evidence.append('接近压力区：' + '、'.join(pressure_hits[:3]))
    if daily.get('near_resistance'): profit_score+=1; profit_evidence.append('接近日线压力区')
    if daily.get('gap_pct') is not None and daily['gap_pct'] >= 5 and daily.get('volume_ratio',0) >= 1.5: profit_score+=1; profit_evidence.append('跳空高开且量能放大')
    if rsi14 is not None and rsi14 >= 65: profit_score+=1; profit_evidence.append('短线偏热')
    if kdj_hot: profit_score+=1; profit_evidence.append('KDJ短线过热')
    if mfi_hot: profit_score+=1; profit_evidence.append('资金流进入过热区')
    if bb_upper and price >= bb_upper * 0.99: profit_score+=1; profit_evidence.append('接近布林带上轨')
    if h1_state != '偏强' or d1_state != '偏强': profit_score+=1; profit_evidence.append('大周期未完全转强')
    if prev and vw and prev > vw and price < vw * 0.995 and bounce_from_low >= 4:
        profit_score+=2; profit_evidence.append('反弹后跌回VWAP下方')

    risk_score,bottom_score,profit_score=phase_adjust_scores(
        phase, risk_score, bottom_score, profit_score,
        risk_evidence, bottom_evidence, profit_evidence)

    # Decision: take the highest-quality actionable interpretation.
    alert_severity=0; alert_category='INFO'; thesis='继续观察'; reasons=[]
    if risk_score >= 10:
        alert_severity=6; alert_category='EXTREME_RISK'; thesis='综合判断：极端风险，当前重点不是抄底，而是控制杠杆和回撤。'; reasons=risk_evidence[:5]
    elif risk_score >= 7:
        alert_severity=5; alert_category='RISK_UPGRADE'; thesis='综合判断：强风险仍占优，暂不适合主动抄底。'; reasons=risk_evidence[:5]
    elif profit_score >= 5 and bounce_from_low >= 4:
        alert_severity=3; alert_category='TAKE_PROFIT'; thesis='综合判断：反弹接近压力区，更适合考虑止盈/降杠杆，而不是追买。'; reasons=profit_evidence[:5]
    elif bottom_score >= 6 and risk_score <= 6:
        alert_severity=2; alert_category='REBOUND_CONFIRM'; thesis='综合判断：止跌修复较明显，可进入反弹确认观察。'; reasons=bottom_evidence[:5]
    elif bottom_score >= 4 and risk_score <= 7:
        alert_severity=2; alert_category='BOTTOM_WATCH'; thesis='综合判断：有抄底观察条件，但仍需小仓/回踩确认。'; reasons=bottom_evidence[:5]
    elif risk_score >= 4:
        alert_severity=4 if (low20 and price < low20 * 0.992) else 3
        alert_category='SELL_EXIT' if alert_severity==4 else 'SELL_REDUCE'
        thesis='综合判断：弱势仍未解除，重点是防守，不是猜底。'; reasons=risk_evidence[:5]
    elif abs(day_pct) >= 5:
        alert_severity=1; alert_category='ABNORMAL_MOVE'; thesis='综合判断：波动异常，但还没有形成新的可操作阶段。'; reasons=[f'日内波动{day_pct:+.2f}%']

    forecast=forecast_card(
        price=price, day_pct=day_pct, risk_score=risk_score, bottom_score=bottom_score,
        profit_score=profit_score, below_vwap=below_vwap, below_ema20=below_ema20,
        below_ema50=below_ema50, reclaim_vwap=reclaim_vwap, reclaim_ema20=reclaim_ema20,
        oversold=oversold, very_oversold=very_oversold, kdj_oversold=kdj_oversold,
        kdj_hot=kdj_hot, mfi_outflow=mfi_outflow, mfi_inflow=mfi_inflow,
        obv_weak=obv_weak, obv_strong=obv_strong, high_volatility=high_volatility,
        closes_rising=closes_rising, higher_low_try=higher_low_try, macd_weak=macd_weak,
        h1_state=h1_state, d1_state=d1_state, vw=vw, ema20=ema20, ema50=ema50,
        low20=low20, prev_high20=prev_high20, day_low=day_low, day_high=day_high,
        bb_lower=bb_lower, bb_mid=bb_mid, bb_upper=bb_upper,
        bounce_from_low=bounce_from_low, pullback_from_high=pullback_from_high,
        pressure_hits=pressure_hits, daily=daily)

    if alert_severity == 0:
        primary_prob=forecast['probs'].get(forecast['primary'],0)
        prev_primary=s.get('last_forecast_primary')
        prev_conf=s.get('last_forecast_confidence')
        if primary_prob >= 48 and (forecast['primary'] != prev_primary or forecast['confidence'] != prev_conf):
            alert_severity=1
            alert_category='FORECAST_SHIFT'
            thesis=f'预测判断：未来{forecast["window"]}主路径切换为{forecast["primary"]}。'
            reasons=forecast['supports'][:3]

    current_sig=forecast_signature(alert_category, alert_severity, forecast, reasons)
    s['last_price']=price
    s['last_quote_epoch']=time.time()
    s['last_forecast_seen']={
        'epoch':time.time(),
        'price':price,
        'primary':forecast['primary'],
        'confidence':forecast['confidence'],
        'probs':forecast['probs'],
        'prob_bucket':forecast.get('prob_bucket'),
        'level_state':forecast.get('level_state'),
        'daily_key':forecast.get('daily_key'),
        'semantic_key':forecast_semantic_key(alert_category, alert_severity, forecast),
        'state_key':forecast_state_key(alert_category, alert_severity, forecast),
        'market_phase':phase,
        'quote_quality':q.get('quality'),
    }
    if alert_severity == 0:
        record_forecast_history(
            s, price=price, day_pct=day_pct, q=q, m15=m15, h1=h1, d1=d1, daily=daily, vw=vw,
            risk_score=risk_score, bottom_score=bottom_score, profit_score=profit_score,
            alert_category=alert_category, alert_severity=alert_severity, thesis=thesis,
            reasons=reasons, forecast=forecast, event='watch', emitted=False)
    save_state(s)
    if alert_severity == 0:
        return

    emit, emit_reason=should_emit(
        s, alert_category=alert_category, alert_severity=alert_severity,
        price=price, reasons=reasons, forecast=forecast)
    if not emit:
        s['last_suppressed_reason']=emit_reason
        record_forecast_history(
            s, price=price, day_pct=day_pct, q=q, m15=m15, h1=h1, d1=d1, daily=daily, vw=vw,
            risk_score=risk_score, bottom_score=bottom_score, profit_score=profit_score,
            alert_category=alert_category, alert_severity=alert_severity, thesis=thesis,
            reasons=reasons, forecast=forecast, event='suppressed',
            emitted=False, suppressed_reason=emit_reason)
        save_state(s)
        return

    s['last_alert_category']=alert_category
    s['last_alert_severity']=alert_severity
    s['last_alert_price']=price
    s['last_alert_epoch']=time.time()
    s['last_forecast_primary']=forecast['primary']
    s['last_forecast_confidence']=forecast['confidence']
    s['last_forecast_probs']=forecast['probs']
    s['last_forecast_prob_bucket']=forecast.get('prob_bucket')
    s['last_forecast_level_state']=forecast.get('level_state')
    s['last_forecast_daily_key']=forecast.get('daily_key')
    s['last_forecast_signature']=current_sig
    s['last_forecast_semantic_key']=forecast_semantic_key(alert_category, alert_severity, forecast)
    s['last_alert_state_key']=forecast_state_key(alert_category, alert_severity, forecast)
    s['last_signal_key']=s['last_forecast_signature']
    record_forecast_history(
        s, price=price, day_pct=day_pct, q=q, m15=m15, h1=h1, d1=d1, daily=daily, vw=vw,
        risk_score=risk_score, bottom_score=bottom_score, profit_score=profit_score,
        alert_category=alert_category, alert_severity=alert_severity, thesis=thesis,
        reasons=reasons, forecast=forecast, event='emitted', emitted=True)
    save_state(s)

    title, action = title_action(alert_category, alert_severity)
    trend15 = '强' if (reclaim_vwap and reclaim_ema20) else '弱' if (below_vwap and below_ema20) else '震荡'
    context=[]
    if day_low: context.append(f'距日内低点反弹{bounce_from_low:.2f}%')
    if day_high: context.append(f'距日内高点{pullback_from_high:.2f}%')
    if rsi14 is not None: context.append('短线超卖' if rsi14 < 30 else '短线未超卖')
    score_line=f'综合评分：风险{risk_score} / 抄底{bottom_score} / 止盈{profit_score}'
    probs=forecast['probs']
    ranked='；'.join([f'{name}{prob}%' for name,prob in forecast['ranked']])
    gaps=[]
    if forecast['price_gap_vwap'] is not None: gaps.append(f'距VWAP {forecast["price_gap_vwap"]:+.2f}%')
    if forecast['price_gap_ema20'] is not None: gaps.append(f'距15m趋势线 {forecast["price_gap_ema20"]:+.2f}%')
    if forecast['price_gap_low20'] is not None: gaps.append(f'距短线支撑 {forecast["price_gap_low20"]:+.2f}%')
    multi=[]
    if daily.get('ret3') is not None or daily.get('ret5') is not None or daily.get('ret20') is not None:
        multi.append(f'3/5/20日 {fmt_pct(daily.get("ret3"))}/{fmt_pct(daily.get("ret5"))}/{fmt_pct(daily.get("ret20"))}')
    if daily.get('gap_pct') is not None: multi.append(f'跳空 {fmt_pct(daily.get("gap_pct"))}')
    if daily.get('volume_ratio') is not None: multi.append(f'日线量比 {daily["volume_ratio"]:.2f}x')
    if daily.get('atr_pctile') is not None: multi.append(f'ATR分位 {daily["atr_pctile"]:.0f}%')
    if daily.get('rel_vs_smh_5d') is not None: multi.append(f'相对SMH5日 {fmt_pct(daily.get("rel_vs_smh_5d"))}')
    if daily.get('session'): multi.append(f'时段 {daily["session"]}')
    lines=[f'{SYMBOL} {title}', thesis, action,
           f'当前价: {fmt(price)}  日内: {day_pct:+.2f}%  报价源: {q["source"]}',
           f'状态摘要：15分钟偏{trend15}；1小时{h1_state}；日线{d1_state}；' + '，'.join(context[:3]),
           ('多周期：' + '；'.join(multi)) if multi else '多周期：n/a',
           f'预测窗口：{forecast["window"]}；主路径：{forecast["primary"]}（置信度{forecast["confidence"]}）',
           f'路径概率：{ranked}',
           f'验证条件：{forecast["confirm"]}',
           f'失效条件：{forecast["invalid"]}',
           ('位置偏离：' + '；'.join(gaps)) if gaps else '位置偏离：n/a',
           score_line,
           '关键依据:']+[f'- {r}' for r in reasons]+[
           '预测依据:']+[f'- {r}' for r in forecast['supports']]+[
           '下次提醒：预测主路径/置信度变化、关键价位被验证或失效、或价格出现实质位移时再提醒。']
    print('\n'.join(lines))

if __name__=='__main__': main()
