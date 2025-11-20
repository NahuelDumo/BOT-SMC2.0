import ccxt
import pandas as pd
import os
import json
from datetime import datetime

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config', 'cofigETHBTC.json')
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
os.makedirs(DATA_DIR, exist_ok=True)

# Leer configuración igual que el backtest
with open(CONFIG_PATH, 'r') as f:
    cfg = json.load(f)
    symbols = cfg.get('symbols', ['ETHUSDT', 'SOLUSDT', 'HYPEUSDT', 'SUIUSDT'])
    days = int(cfg.get('days', 130))

exchange = ccxt.binanceusdm({'enableRateLimit': True, 'options': {'defaultType': 'future'}})

def fetch_and_save(symbol, timeframe, days):
    print(f"Descargando {symbol} {timeframe} ({days} días)...")
    msec_in_day = 86400000
    since = exchange.milliseconds() - msec_in_day * days
    all_ohlcv = []
    limit = 1000
    fetch_since = since
    while fetch_since < exchange.milliseconds():
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=fetch_since, limit=limit)
        if not ohlcv:
            break
        all_ohlcv.extend(ohlcv)
        fetch_since = ohlcv[-1][0] + 1
        if len(all_ohlcv) > 0 and len(all_ohlcv) % 20000 == 0:
            print(f"Descargadas {len(all_ohlcv)} velas ({timeframe})...")
    if not all_ohlcv:
        print(f"No se encontraron datos para {symbol} ({timeframe})")
        return
    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
    df.set_index('timestamp', inplace=True)
    df = df.astype(float)
    filename = os.path.join(DATA_DIR, f"{symbol.replace('/', '')}_{timeframe}_{days}d.csv")
    df.to_csv(filename)
    print(f"✅ Guardado: {filename} ({len(df)} velas)")

for symbol in symbols:
    symbol_ccxt = symbol if '/' in symbol else symbol.replace('USDT', '/USDT')
    fetch_and_save(symbol_ccxt, '15m', days)
    fetch_and_save(symbol_ccxt, '1h', days)

print("Descarga de datos históricos completada.")
