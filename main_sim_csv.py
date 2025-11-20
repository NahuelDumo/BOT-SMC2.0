#!/usr/bin/env python3
"""
Simulación usando datos CSV (sin descargar desde API).
Esto permite verificar que el BOT genera las mismas señales que el backtest
usando datos históricos congelados.
"""

import os
import sys
import json
import logging
import pandas as pd
import numpy as np
from datetime import datetime
from scipy.signal import argrelextrema

# Agregar la ruta del proyecto
sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config', 'cofigETHBTC.json')
DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
REPORTS_DIR = os.path.join(os.path.dirname(__file__), 'reports')
os.makedirs(REPORTS_DIR, exist_ok=True)

# Leer configuración
with open(CONFIG_PATH, 'r') as f:
    cfg = json.load(f)
    symbols = cfg.get('symbols', ['ETHUSDT', 'SOLUSDT', 'HYPEUSDT', 'SUIUSDT'])
    initial_balance = float(cfg.get('initial_balance', 30.0))
    symbol_configs = cfg.get('symbol_configs', {})

logger.info("=" * 70)
logger.info("SIMULACIÓN DESDE ARCHIVOS CSV (Sin descargar desde API)")
logger.info("=" * 70)
logger.info(f"Símbolos: {symbols}")
logger.info(f"Balance Inicial: ${initial_balance:.2f}")
logger.info(f"Directorio de datos: {DATA_DIR}")

# ============================================================================
# CARGAR DATOS DESDE CSV
# ============================================================================

symbol_data_csv = {}

for symbol_name in symbols:
    # Convertir ETHUSDT -> ETH/USDT
    symbol_display = symbol_name.replace('USDT', '/USDT')
    
    csv_15m = os.path.join(DATA_DIR, f"{symbol_name}_15m_130d.csv")
    csv_1h = os.path.join(DATA_DIR, f"{symbol_name}_1h_130d.csv")
    
    if not os.path.exists(csv_15m) or not os.path.exists(csv_1h):
        logger.warning(f"❌ Archivos CSV faltantes para {symbol_name}")
        logger.warning(f"   15m: {csv_15m} {'✅' if os.path.exists(csv_15m) else '❌'}")
        logger.warning(f"   1h:  {csv_1h} {'✅' if os.path.exists(csv_1h) else '❌'}")
        continue
    
    logger.info(f"Cargando {symbol_display}...")
    
    # Cargar CSV
    df_15m = pd.read_csv(csv_15m, parse_dates=['timestamp'])
    df_1h = pd.read_csv(csv_1h, parse_dates=['timestamp'])
    
    # Convertir timestamp a índice
    df_15m.set_index('timestamp', inplace=True)
    df_1h.set_index('timestamp', inplace=True)
    
    logger.info(f"  ✅ 15m: {len(df_15m)} velas")
    logger.info(f"  ✅ 1h:  {len(df_1h)} velas")
    
    symbol_data_csv[symbol_display] = {
        'df_15m': df_15m,
        'df_1h': df_1h,
    }

if not symbol_data_csv:
    logger.error("❌ No se cargaron datos CSV. Abortando.")
    sys.exit(1)

logger.info(f"✅ Datos CSV cargados para {len(symbol_data_csv)} símbolos")

# ============================================================================
# FUNCIONES DE CÁLCULO (Copiadas de backtest.py)
# ============================================================================

def compute_macd(df, fast=12, slow=26, signal=9):
    """Calcula MACD."""
    close = df['close']
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    macd_signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    
    df_copy = df.copy()
    df_copy['macd'] = macd_line
    df_copy['macd_signal'] = macd_signal_line
    return df_copy

def _binsize(price, tol=0.0003):
    """Calcula el tamaño del bucket para scoring de pools."""
    return max(0.01, price * tol)

def build_liquidity_pools(df_slice, i, mid_price, lookback=192, tol=0.0003):
    """Construye pools de liquidez como concentración de niveles en una ventana."""
    start = max(0, i - lookback)
    window = df_slice.iloc[start:i]
    if window.empty:
        return []

    binsize = _binsize(mid_price, tol)
    step = 5.0 if mid_price < 5000 else 10.0
    pools = {}

    def add(price, score):
        if price is None or np.isnan(price):
            return
        bucket = round(price / binsize)
        level = bucket * binsize
        pools[level] = pools.get(level, 0.0) + score

    # Equal highs/lows
    highs = window['high'].values
    lows = window['low'].values
    for arr, base_score in ((highs, 3.0), (lows, 3.0)):
        buckets = {}
        for p in arr:
            b = round(p / binsize)
            buckets[b] = buckets.get(b, 0) + 1
        for b, cnt in buckets.items():
            if cnt >= 2:
                add(b * binsize, base_score * cnt)

    # Swings
    swing_highs = window['max'].dropna().values if 'max' in window.columns else []
    swing_lows = window['min'].dropna().values if 'min' in window.columns else []
    for p in swing_highs:
        add(float(p), 4.0)
    for p in swing_lows:
        add(float(p), 4.0)

    # FVG borders
    for p in window['fvg_bull_high'].dropna().values:
        add(float(p), 2.5)
    for p in window['fvg_bear_low'].dropna().values:
        add(float(p), 2.5)

    # Round levels
    wmin = float(window['low'].min())
    wmax = float(window['high'].max())
    if step > 0:
        lvl = (np.floor(wmin / step) * step)
        while lvl <= wmax:
            hits = ((np.abs(window['high'] - lvl) <= binsize) | (np.abs(window['low'] - lvl) <= binsize)).sum()
            if hits >= 1:
                add(lvl, 0.5 * hits)
            lvl += step

    levels = [{'price': float(k), 'score': float(v)} for k, v in pools.items()]
    levels.sort(key=lambda x: (-x['score'], x['price']))
    return levels

def select_target_pool(direction, entry_price, sl_price, pools):
    """Elige el pool objetivo basado en dirección y scoring."""
    if not pools:
        return entry_price * 1.02 if direction == 'LONG' else entry_price * 0.98

    if direction == 'LONG':
        candidates = [p for p in pools if p['price'] > entry_price]
        candidates.sort(key=lambda p: (-p['score'], abs(p['price'] - entry_price)))
        # Validar distancia mínima (1%)
        if candidates:
            best = candidates[0]
            if best['price'] >= entry_price * 1.01:
                return best['price']
        return entry_price * 1.02
    else:  # SHORT
        candidates = [p for p in pools if p['price'] < entry_price]
        candidates.sort(key=lambda p: (-p['score'], abs(p['price'] - entry_price)))
        # Validar distancia mínima (1%)
        if candidates:
            best = candidates[0]
            if best['price'] <= entry_price * 0.99:
                return best['price']
        return entry_price * 0.98

def find_patterns(df):
    """Identifica FVGs, mitigación y estructuras."""
    structure_lookback = 20
    df_copy = df.copy()
    
    # Calcular estructura (swings)
    df_copy['min'] = df_copy.iloc[argrelextrema(df_copy.low.values, np.less_equal, order=structure_lookback)[0]]['low']
    df_copy['max'] = df_copy.iloc[argrelextrema(df_copy.high.values, np.greater_equal, order=structure_lookback)[0]]['high']
    
    # Inicializar columnas FVG
    n = len(df_copy)
    is_fvg_bullish_np = np.zeros(n, dtype=bool)
    is_fvg_bearish_np = np.zeros(n, dtype=bool)
    fvg_bull_low_np = np.full(n, np.nan)
    fvg_bull_high_np = np.full(n, np.nan)
    fvg_bull_mid_np = np.full(n, np.nan)
    fvg_bear_low_np = np.full(n, np.nan)
    fvg_bear_high_np = np.full(n, np.nan)
    fvg_bear_mid_np = np.full(n, np.nan)
    
    lows = df_copy['low'].values
    highs = df_copy['high'].values
    
    # Detectar FVGs
    for i in range(2, n):
        # FVG Bullish
        if lows[i] > highs[i-2]:
            is_fvg_bullish_np[i-1] = True
            low_edge = highs[i-2]
            high_edge = lows[i]
            fvg_bull_low_np[i-1] = low_edge
            fvg_bull_high_np[i-1] = high_edge
            fvg_bull_mid_np[i-1] = low_edge + (high_edge - low_edge) * 0.5
        
        # FVG Bearish
        if highs[i] < lows[i-2]:
            is_fvg_bearish_np[i-1] = True
            low_edge = highs[i]
            high_edge = lows[i-2]
            fvg_bear_low_np[i-1] = low_edge
            fvg_bear_high_np[i-1] = high_edge
            fvg_bear_mid_np[i-1] = low_edge + (high_edge - low_edge) * 0.5
    
    df_copy['is_fvg_bullish'] = is_fvg_bullish_np
    df_copy['is_fvg_bearish'] = is_fvg_bearish_np
    df_copy['fvg_bull_low'] = fvg_bull_low_np
    df_copy['fvg_bull_high'] = fvg_bull_high_np
    df_copy['fvg_bull_mid'] = fvg_bull_mid_np
    df_copy['fvg_bear_low'] = fvg_bear_low_np
    df_copy['fvg_bear_high'] = fvg_bear_high_np
    df_copy['fvg_bear_mid'] = fvg_bear_mid_np
    
    # Calcular mitigación (50% FVG)
    is_mitigated_np = np.zeros(n, dtype=bool)
    
    bull_fvg_indices = df_copy.index[df_copy['is_fvg_bullish']]
    for fvg_idx_time in bull_fvg_indices:
        fvg_iloc = df_copy.index.get_loc(fvg_idx_time)
        fvg_mid_price = df_copy['fvg_bull_mid'].iloc[fvg_iloc]
        if fvg_iloc + 1 < n:
            future_lows = df_copy['low'].values[fvg_iloc + 1:]
            if (future_lows <= fvg_mid_price).any():
                is_mitigated_np[fvg_iloc] = True
    
    bear_fvg_indices = df_copy.index[df_copy['is_fvg_bearish']]
    for fvg_idx_time in bear_fvg_indices:
        fvg_iloc = df_copy.index.get_loc(fvg_idx_time)
        fvg_mid_price = df_copy['fvg_bear_mid'].iloc[fvg_iloc]
        if fvg_iloc + 1 < n:
            future_highs = df_copy['high'].values[fvg_iloc + 1:]
            if (future_highs >= fvg_mid_price).any():
                is_mitigated_np[fvg_iloc] = True
    
    df_copy['is_mitigated'] = is_mitigated_np
    
    return df_copy

# ============================================================================
# PREPARAR DATOS PARA LA SIMULACIÓN
# ============================================================================

logger.info("\nPreparando datos MTF para simulación...")

for symbol_display, data in symbol_data_csv.items():
    df_15m = data['df_15m']
    df_1h = data['df_1h']
    
    # Calcular MACD 1H
    logger.info(f"  {symbol_display}: Calculando MACD 1H...")
    df_1h = compute_macd(df_1h)
    
    # Calcular FVG y patrones en 15m
    logger.info(f"  {symbol_display}: Identificando patrones FVG...")
    df_15m = find_patterns(df_15m)
    
    # Fusionar MACD 1H al dataframe 15m
    logger.info(f"  {symbol_display}: Fusionando MTF...")
    df_1h_macd = df_1h[['macd']].rename(columns={'macd': 'macd_1h'})
    df_15m = pd.merge_asof(
        df_15m.sort_index(),
        df_1h_macd.sort_index(),
        left_index=True,
        right_index=True,
        direction='backward'
    )
    
    # Limpiar NaNs
    df_15m.dropna(subset=['macd_1h'], inplace=True)
    
    symbol_data_csv[symbol_display] = {
        'df_15m': df_15m,
        'df_1h': df_1h,
    }
    
    logger.info(f"  ✅ {symbol_display}: {len(df_15m)} velas usables")

# ============================================================================
# EJECUTAR SIMULACIÓN MULTI-SÍMBOLO
# ============================================================================

logger.info("\n" + "=" * 70)
logger.info("INICIANDO SIMULACIÓN")
logger.info("=" * 70)

balance = initial_balance
trades = []
concurrent_open = 0
active_trades = {}
max_concurrent = 4
symbol_leverage = {
    'ETH/USDT': 15,
    'SOL/USDT': 15,
    'HYPE/USDT': 15,
    'SUI/USDT': 15,
}

# Obtener todos los timestamps en orden
all_timestamps = sorted(set().union(*[set(data['df_15m'].index) for data in symbol_data_csv.values()]))
logger.info(f"Total timestamps: {len(all_timestamps)}")

# Loop principal de simulación
for ts_idx, ts in enumerate(all_timestamps):
    # Log cada 1000 timestamps
    if ts_idx % 1000 == 0:
        logger.info(f"Procesando timestamp {ts_idx}/{len(all_timestamps)} - Posiciones abiertas: {concurrent_open}/{max_concurrent}")
    
    # 1. GESTIONAR POSICIONES ABIERTAS (CON SL TRAILING)
    for symbol_display in list(active_trades.keys()):
        if symbol_display not in symbol_data_csv:
            continue
        
        data = symbol_data_csv[symbol_display]
        df_15m = data['df_15m']
        
        try:
            loc = df_15m.index.get_loc(ts)
        except KeyError:
            continue
        
        candle = df_15m.iloc[loc]
        trade = active_trades[symbol_display]
        
        candles_in_trade = loc - trade['entry_idx']
        exit_triggered = False
        exit_reason = None
        exit_price = None
        
        # ============================================================================
        # ACTUALIZAR SL CON TRAILING STOP (Basado en Swings Recientes)
        # ============================================================================
        if candles_in_trade >= 4:  # Después de 1 hora (4 velas de 15m)
            if trade['direction'] == 'LONG':
                # Para LONG: actualizar SL al swing low más reciente si está arriba del SL actual
                recent_lows = df_15m['min'].iloc[max(0, loc-20):loc].dropna()
                if not recent_lows.empty:
                    recent_low = float(recent_lows.iloc[-1])
                    if recent_low > trade['sl_price']:
                        trade['sl_price'] = recent_low
            else:  # SHORT
                # Para SHORT: actualizar SL al swing high más reciente si está abajo del SL actual
                recent_highs = df_15m['max'].iloc[max(0, loc-20):loc].dropna()
                if not recent_highs.empty:
                    recent_high = float(recent_highs.iloc[-1])
                    if recent_high < trade['sl_price']:
                        trade['sl_price'] = recent_high
        
        # ============================================================================
        # VERIFICAR TP/SL
        # ============================================================================
        if trade['direction'] == 'LONG':
            if candle['high'] >= trade['tp_price']:
                exit_triggered = True
                exit_reason = 'Take Profit'
                exit_price = trade['tp_price']
            elif candle['low'] <= trade['sl_price']:
                exit_triggered = True
                exit_reason = 'Stop Loss'
                exit_price = trade['sl_price']
        else:  # SHORT
            if candle['low'] <= trade['tp_price']:
                exit_triggered = True
                exit_reason = 'Take Profit'
                exit_price = trade['tp_price']
            elif candle['high'] >= trade['sl_price']:
                exit_triggered = True
                exit_reason = 'Stop Loss'
                exit_price = trade['sl_price']
        
        # Verificar time limit (48 velas = 12 horas)
        if candles_in_trade >= 48:
            exit_triggered = True
            exit_reason = 'Time Limit'
            exit_price = float(candle['close'])
        
        if exit_triggered:
            # Calcular PnL
            if trade['direction'] == 'LONG':
                pnl = (exit_price - trade['entry_price']) * trade['size_base']
            else:
                pnl = (trade['entry_price'] - exit_price) * trade['size_base']
            
            trades.append({
                'symbol': symbol_display,
                'direction': trade['direction'],
                'entry_price': trade['entry_price'],
                'exit_price': exit_price,
                'entry_time': trade['entry_time'],
                'exit_time': ts,
                'size_base': trade['size_base'],
                'size_usd': trade['size_usd'],
                'stop_loss': trade['sl_price'],
                'take_profit': trade['tp_price'],
                'pnl': pnl,
                'exit_reason': exit_reason,
                'setup_type': trade['setup_type']
            })
            
            balance += pnl
            del active_trades[symbol_display]
            concurrent_open -= 1
    
    # 2. BUSCAR NUEVAS ENTRADAS
    for symbol_display, data in symbol_data_csv.items():
        if concurrent_open >= max_concurrent:
            break
        
        if symbol_display in active_trades:
            continue
        
        df_15m = data['df_15m']
        
        try:
            loc = df_15m.index.get_loc(ts)
        except KeyError:
            continue
        
        if loc < 50:
            continue
        
        df_slice = df_15m.iloc[:loc+1]
        current_price = float(df_slice.iloc[-1]['close'])
        macd_1h = float(df_slice.iloc[-1]['macd_1h']) if not pd.isna(df_slice.iloc[-1]['macd_1h']) else 0
        
        setup = None
        
        # FVG MEMORY LONG
        if not setup:
            unmitigated = df_slice.iloc[:-1][
                (df_slice.iloc[:-1]['is_fvg_bullish'] == True) &
                (df_slice.iloc[:-1]['is_mitigated'] == False)
            ]
            if not unmitigated.empty and macd_1h > 0:
                touching = unmitigated[
                    (current_price >= unmitigated['fvg_bull_low']) &
                    (current_price <= unmitigated['fvg_bull_high'])
                ]
                if not touching.empty:
                    fvg = touching.iloc[-1]
                    entry_price = float(fvg['fvg_bull_high'])
                    sl_price = float(fvg['fvg_bull_low']) - (float(fvg['fvg_bull_low']) * 0.0005)
                    
                    # Calcular TP con pools
                    pools = build_liquidity_pools(df_slice, loc, current_price, lookback=192, tol=0.0003)
                    tp_price = select_target_pool('LONG', entry_price, sl_price, pools)
                    
                    setup = {
                        'direction': 'LONG',
                        'entry_price': entry_price,
                        'sl_price': sl_price,
                        'tp_price': tp_price,
                        'setup_type': 'FVG_MEMORY'
                    }
        
        # FVG MEMORY SHORT
        if not setup:
            unmitigated = df_slice.iloc[:-1][
                (df_slice.iloc[:-1]['is_fvg_bearish'] == True) &
                (df_slice.iloc[:-1]['is_mitigated'] == False)
            ]
            if not unmitigated.empty and macd_1h < 0:
                touching = unmitigated[
                    (current_price >= unmitigated['fvg_bear_low']) &
                    (current_price <= unmitigated['fvg_bear_high'])
                ]
                if not touching.empty:
                    fvg = touching.iloc[-1]
                    entry_price = float(fvg['fvg_bear_low'])
                    sl_price = float(fvg['fvg_bear_high']) + (float(fvg['fvg_bear_high']) * 0.0005)
                    
                    # Calcular TP con pools
                    pools = build_liquidity_pools(df_slice, loc, current_price, lookback=192, tol=0.0003)
                    tp_price = select_target_pool('SHORT', entry_price, sl_price, pools)
                    
                    setup = {
                        'direction': 'SHORT',
                        'entry_price': entry_price,
                        'sl_price': sl_price,
                        'tp_price': tp_price,
                        'setup_type': 'FVG_MEMORY'
                    }
        
        # SWEEP LONG
        if not setup:
            recent_lows = df_slice['min'].iloc[-50:].dropna()
            if len(recent_lows) >= 2 and recent_lows.iloc[-1] < recent_lows.iloc[-2]:
                try:
                    sweep_idx = df_slice.index.get_loc(recent_lows.index[-1])
                except KeyError:
                    sweep_idx = None
                
                if sweep_idx is not None and loc - sweep_idx <= 12 and macd_1h > 0:
                    fvg_window = df_slice.iloc[sweep_idx:loc]
                    bullish_fvgs = fvg_window[fvg_window['is_fvg_bullish'] == True]
                    if not bullish_fvgs.empty:
                        fvg = bullish_fvgs.iloc[-1]
                        if current_price >= fvg['fvg_bull_low'] and current_price <= fvg['fvg_bull_high']:
                            entry_price = float(fvg['fvg_bull_mid'])
                            sl_price = float(recent_lows.iloc[-1])
                            
                            # Calcular TP con pools
                            pools = build_liquidity_pools(df_slice, loc, current_price, lookback=192, tol=0.0003)
                            tp_price = select_target_pool('LONG', entry_price, sl_price, pools)
                            
                            setup = {
                                'direction': 'LONG',
                                'entry_price': entry_price,
                                'sl_price': sl_price,
                                'tp_price': tp_price,
                                'setup_type': 'SWEEP'
                            }
        
        # SWEEP SHORT
        if not setup:
            recent_highs = df_slice['max'].iloc[-50:].dropna()
            if len(recent_highs) >= 2 and recent_highs.iloc[-1] > recent_highs.iloc[-2]:
                try:
                    sweep_idx = df_slice.index.get_loc(recent_highs.index[-1])
                except KeyError:
                    sweep_idx = None
                
                if sweep_idx is not None and loc - sweep_idx <= 12 and macd_1h < 0:
                    fvg_window = df_slice.iloc[sweep_idx:loc]
                    bearish_fvgs = fvg_window[fvg_window['is_fvg_bearish'] == True]
                    if not bearish_fvgs.empty:
                        fvg = bearish_fvgs.iloc[-1]
                        if current_price >= fvg['fvg_bear_low'] and current_price <= fvg['fvg_bear_high']:
                            entry_price = float(fvg['fvg_bear_mid'])
                            sl_price = float(recent_highs.iloc[-1])
                            
                            # Calcular TP con pools
                            pools = build_liquidity_pools(df_slice, loc, current_price, lookback=192, tol=0.0003)
                            tp_price = select_target_pool('SHORT', entry_price, sl_price, pools)
                            
                            setup = {
                                'direction': 'SHORT',
                                'entry_price': entry_price,
                                'sl_price': sl_price,
                                'tp_price': tp_price,
                                'setup_type': 'SWEEP'
                            }
        
        # Ejecutar trade si hay setup válido
        if setup:
            risk_per_unit = abs(setup['entry_price'] - setup['sl_price'])
            if risk_per_unit > 0:
                capital_to_risk = balance * 0.05
                size_base = capital_to_risk / risk_per_unit
                size_usd = size_base * setup['entry_price']
                leverage = symbol_leverage.get(symbol_display, 15)
                max_notional = balance * leverage
                
                if size_usd > max_notional:
                    size_usd = max_notional
                    size_base = size_usd / setup['entry_price']
                
                active_trades[symbol_display] = {
                    'direction': setup['direction'],
                    'entry_price': setup['entry_price'],
                    'sl_price': setup['sl_price'],
                    'tp_price': setup['tp_price'],
                    'size_base': size_base,
                    'size_usd': size_usd,
                    'entry_time': ts,
                    'entry_idx': loc,
                    'setup_type': setup['setup_type']
                }
                
                concurrent_open += 1
                
                # CORRELACIÓN ETH → SOL
                if symbol_display == 'ETH/USDT' and setup['direction'] == 'LONG' and 'SOL/USDT' not in active_trades:
                    # Abrir SOL LONG automáticamente
                    sol_data = symbol_data_csv.get('SOL/USDT')
                    if sol_data:
                        sol_df = sol_data['df_15m']
                        try:
                            sol_loc = sol_df.index.get_loc(ts)
                            sol_price = float(sol_df.iloc[sol_loc]['close'])
                            sol_macd_1h = float(sol_df.iloc[sol_loc]['macd_1h']) if not pd.isna(sol_df.iloc[sol_loc]['macd_1h']) else 0
                            
                            # Verificar que SOL cumpla condiciones
                            if concurrent_open < max_concurrent and sol_macd_1h > 0:
                                # Usar mismo TP% que ETH
                                sol_entry = sol_price
                                sol_tp = sol_price * 1.02
                                sol_sl = sol_price * 0.99
                                
                                sol_risk = abs(sol_entry - sol_sl)
                                if sol_risk > 0:
                                    sol_capital = balance * 0.05
                                    sol_size_base = sol_capital / sol_risk
                                    sol_size_usd = sol_size_base * sol_entry
                                    sol_leverage = symbol_leverage.get('SOL/USDT', 15)
                                    sol_max_notional = balance * sol_leverage
                                    
                                    if sol_size_usd > sol_max_notional:
                                        sol_size_usd = sol_max_notional
                                        sol_size_base = sol_size_usd / sol_entry
                                    
                                    active_trades['SOL/USDT'] = {
                                        'direction': 'LONG',
                                        'entry_price': sol_entry,
                                        'sl_price': sol_sl,
                                        'tp_price': sol_tp,
                                        'size_base': sol_size_base,
                                        'size_usd': sol_size_usd,
                                        'entry_time': ts,
                                        'entry_idx': sol_loc,
                                        'setup_type': 'CORRELATION'
                                    }
                                    concurrent_open += 1
                        except KeyError:
                            pass

# ============================================================================
# GENERAR REPORTE
# ============================================================================

if trades:
    trades_df = pd.DataFrame(trades)
    
    # Calcular métricas
    total_trades = len(trades_df)
    wins = trades_df[trades_df['pnl'] > 0]
    losses = trades_df[trades_df['pnl'] <= 0]
    win_rate = (len(wins) / total_trades * 100) if total_trades > 0 else 0
    
    profit_factor = 0
    if not losses.empty and losses['pnl'].sum() != 0:
        profit_factor = wins['pnl'].sum() / abs(losses['pnl'].sum())
    elif not wins.empty and losses.empty:
        profit_factor = 999.99
    
    logger.info("\n" + "=" * 70)
    logger.info("RESULTADOS DE LA SIMULACIÓN CSV")
    logger.info("=" * 70)
    logger.info(f"Balance Inicial: ${initial_balance:.2f}")
    logger.info(f"Balance Final: ${balance:.2f}")
    logger.info(f"Retorno: {(balance/initial_balance - 1)*100:.2f}%")
    logger.info(f"Total Trades: {total_trades}")
    logger.info(f"Win Rate: {win_rate:.2f}%")
    logger.info(f"Profit Factor: {profit_factor:.2f}")
    logger.info(f"Ganancia Promedio: ${wins['pnl'].mean():.2f}" if not wins.empty else "Ganancia Promedio: $0.00")
    logger.info(f"Pérdida Promedio: ${losses['pnl'].mean():.2f}" if not losses.empty else "Pérdida Promedio: $0.00")
    logger.info("=" * 70)
    
    # Exportar Excel
    filename = os.path.join(REPORTS_DIR, f"sim_csv_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
    
    with pd.ExcelWriter(filename, engine='xlsxwriter') as writer:
        workbook = writer.book
        header_format = workbook.add_format({'bold': True, 'bg_color': '#D3D3D3', 'border': 1})
        money_format = workbook.add_format({'num_format': '$#,##0.00'})
        percent_format = workbook.add_format({'num_format': '0.00%'})
        
        # Hoja resumen
        summary_sheet = workbook.add_worksheet('Resumen')
        summary_data = {
            'Balance Inicial': initial_balance,
            'Balance Final': balance,
            'Retorno Total': (balance / initial_balance) - 1,
            'Total Trades': total_trades,
            'Win Rate': win_rate / 100,
            'Profit Factor': profit_factor,
        }
        summary_sheet.write_row('A1', ['Métrica', 'Valor'], header_format)
        row = 1
        for key, value in summary_data.items():
            summary_sheet.write(row, 0, key)
            if 'Retorno' in key or 'Rate' in key:
                summary_sheet.write(row, 1, value, percent_format)
            elif 'Balance' in key or 'Promedio' in key:
                summary_sheet.write(row, 1, value, money_format)
            else:
                summary_sheet.write(row, 1, value)
            row += 1
        
        # Hoja todos los trades
        trades_df_export = trades_df.copy()
        trades_df_export['entry_time'] = pd.to_datetime(trades_df_export['entry_time']).dt.tz_localize(None)
        trades_df_export['exit_time'] = pd.to_datetime(trades_df_export['exit_time']).dt.tz_localize(None)
        trades_df_export.to_excel(writer, sheet_name='Todos los Trades', index=False)
    
    logger.info(f"✅ Reporte guardado en: {filename}")
else:
    logger.warning("❌ No se generaron trades en la simulación.")
