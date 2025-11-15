import ccxt
import pandas as pd
import numpy as np
from datetime import datetime
import logging
import time
import matplotlib.pyplot as plt
from scipy.signal import argrelextrema
import xlsxwriter
import json
import os
from typing import Dict, Optional, List

# --- Configuración del Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class SmartMoneyScalpingBacktest:
    """
    Motor de backtesting para un solo símbolo.
    Versión actualizada con:
    1. Stop Loss Estructural (Corrección N1)
    2. Filtro de Tendencia MTF (MACD 1H)
    3. Lógica de FVG con Memoria (Mitigación 50%)
    4. Validación de Margen (Corrección N3)
    """
    def __init__(self, symbol: str, initial_balance: float = 30.0, symbol_config: dict = None):
        # Corregido a binanceusdm para consistencia con el bot en vivo
        self.exchange = ccxt.binanceusdm({'enableRateLimit': True, 'options': {'defaultType': 'future'}})
        self.symbol = symbol
        self.timeframe = '15m' # Timeframe de ejecución
        
        self.df = None # type: Optional[pd.DataFrame]
        self.df_1h = None # type: Optional[pd.DataFrame]
        
        self.initial_balance = initial_balance
        self.balance = initial_balance
        
        self.symbol_config = symbol_config or {}
        
        # Parámetros de la estrategia
        self.structure_lookback = 20
        self.risk_reward_ratio = self.symbol_config.get('risk_reward_ratio', 2)
        self.leverage = 20 # Este valor será sobrescrito por el MultiSymbolBacktest
        self.risk_per_trade_pct = 0.05 #riesgo por trade
        self.max_candles_in_trade = 48 # AUMENTADO (12 horas)
        
        self.pool_lookback_bars = 192  # 48h de velas de 15m
        self.equal_tol = 0.0003  # 0.03%
        self.min_rr = 1.5
        
        self.enable_structural_stop = self.symbol_config.get('enable_structural_stop', True)
        self.tp_percentage = self.symbol_config.get('tp_percentage', 2.0)
        self.sl_percentage = self.symbol_config.get('sl_percentage', 1.0) # Ya no se usa para SL
        
        # Historial
        self.trades = []
        self.equity_curve = [{'time': None, 'balance': self.initial_balance}]
        self.active_trade = None
        self.pnl_callback = None  # type: ignore
        
        # Parámetros MACD (para cálculo)
        self.macd_fast = 12
        self.macd_slow = 26
        self.macd_signal = 9
        # El filtro de 15M se deshabilita, se usará MTF
        self.enable_macd_filter = False 

    def _fetch_data(self, timeframe: str, days=5) -> Optional[pd.DataFrame]:
        """Descarga datos históricos genérica."""
        try:
            logger.info(f"Descargando datos para {self.symbol} ({timeframe}) de los últimos {days} días...")
            limit = 1000
            
            # Calcular 'since' basado en el timeframe para asegurar suficientes datos
            msec_in_day = 86400000
            if timeframe == '1h':
                msec_needed = msec_in_day * days
            elif timeframe == '4h':
                msec_needed = msec_in_day * days * 4 # Pedir más días para 4H
            else: # 15m
                msec_needed = msec_in_day * days
                
            since = self.exchange.milliseconds() - msec_needed
            
            all_ohlcv = []
            
            fetch_since = since
            while fetch_since < self.exchange.milliseconds():
                ohlcv = self.exchange.fetch_ohlcv(self.symbol, timeframe, since=fetch_since, limit=limit)
                if not ohlcv: break
                all_ohlcv.extend(ohlcv)
                fetch_since = ohlcv[-1][0] + 1
                if len(all_ohlcv) > 0 and len(all_ohlcv) % 20000 == 0: 
                    logger.info(f"Descargadas {len(all_ohlcv)} velas ({timeframe})...")
            
            if not all_ohlcv:
                logger.warning(f"No se encontraron datos para {self.symbol} ({timeframe})")
                return None

            df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df = df.drop_duplicates(subset=['timestamp'], keep='last')
            
            # Convertir a UTC y luego a la zona horaria deseada (UTC-3)
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert('Etc/GMT+3')
            df.set_index('timestamp', inplace=True)
            df = df.astype(float)
            
            logger.info(f"✅ Descarga completa ({timeframe}): {len(df)} velas desde {df.index[0]}.")
            return df
        except Exception as e:
            logger.error(f"❌ Error descargando datos ({timeframe}): {e}")
            return None

    def prepare_all_data(self, days=5) -> bool:
        """Descarga todos los timeframes, calcula indicadores y los fusiona."""
        logger.info(f"--- Preparando datos MTF para {self.symbol} ---")
        
        # 1. Descargar todos los dataframes
        self.df = self._fetch_data(self.timeframe, days=days) # 15m
        self.df_1h = self._fetch_data('1h', days=days)
        
        if self.df is None or self.df.empty or self.df_1h is None or self.df_1h.empty:
            logger.error(f"Faltan datos de algún timeframe para {self.symbol}. Abortando.")
            return False

        # 2. Calcular MACD en 1H
        logger.info(f"Calculando MACD 1H para {self.symbol}...")
        self.df_1h = self.compute_macd(self.df_1h)

        # 3. Fusionar MACD de 1H al dataframe principal (15m)
        logger.info(f"Fusionando datos MTF para {self.symbol}...")
        
        df_1h_macd = self.df_1h[['macd']].rename(columns={'macd': 'macd_1h'})

        self.df = pd.merge_asof(
            self.df.sort_index(), 
            df_1h_macd.sort_index(), 
            left_index=True, 
            right_index=True, 
            direction='backward'
        )
        
        # 4. Calcular patrones (FVG, Memoria, Swings) en 15m
        self.find_patterns_and_mitigation()
        
        # 5. Calcular indicadores de 15m
        self.compute_atr()
        self.df = self.compute_macd(self.df) # Calcular MACD 15m (por si se usa en el futuro)
        
        # 6. Limpiar NaNs de MTF al inicio
        self.df.dropna(subset=['macd_1h'], inplace=True)
        
        logger.info(f"✅ Datos MTF listos para {self.symbol}. Velas usables: {len(self.df)}")
        return True

    def find_patterns_and_mitigation(self):
        """
        Identifica la estructura del mercado, barridos de liquidez, FVGs
        y calcula la mitigación basada en el 50% (lógica del bot en vivo).
        """
        logger.info(f"Identificando patrones SMC (Swings, FVG, Mitigación 50%) para {self.symbol}...")
        df = self.df 
        
        # 1. Calcular Estructura (min/max)
        n = self.structure_lookback
        df['min'] = df.iloc[argrelextrema(df.low.values, np.less_equal, order=n)[0]]['low']
        df['max'] = df.iloc[argrelextrema(df.high.values, np.greater_equal, order=n)[0]]['high']

        # 2. Calcular FVGs y 50% Midpoint
        df['is_fvg_bullish'] = False
        df['is_fvg_bearish'] = False
        df['fvg_bull_high'], df['fvg_bull_low'] = np.nan, np.nan
        df['fvg_bear_high'], df['fvg_bear_low'] = np.nan, np.nan
        df['fvg_bull_mid'] = np.nan
        df['fvg_bear_mid'] = np.nan
        
        is_fvg_bullish_np = np.zeros(len(df), dtype=bool)
        is_fvg_bearish_np = np.zeros(len(df), dtype=bool)
        fvg_bull_low_np = np.full(len(df), np.nan)
        fvg_bull_high_np = np.full(len(df), np.nan)
        fvg_bull_mid_np = np.full(len(df), np.nan)
        fvg_bear_low_np = np.full(len(df), np.nan)
        fvg_bear_high_np = np.full(len(df), np.nan)
        fvg_bear_mid_np = np.full(len(df), np.nan)

        lows = df['low'].values
        highs = df['high'].values

        for i in range(2, len(df)): 
            if lows[i] > highs[i-2]:
                is_fvg_bullish_np[i-1] = True
                low_edge = highs[i-2]
                high_edge = lows[i]
                fvg_bull_low_np[i-1] = low_edge
                fvg_bull_high_np[i-1] = high_edge
                fvg_bull_mid_np[i-1] = low_edge + (high_edge - low_edge) * 0.5
            if highs[i] < lows[i-2]:
                is_fvg_bearish_np[i-1] = True
                low_edge = highs[i]
                high_edge = lows[i-2]
                fvg_bear_low_np[i-1] = low_edge
                fvg_bear_high_np[i-1] = high_edge
                fvg_bear_mid_np[i-1] = low_edge + (high_edge - low_edge) * 0.5
        
        df['is_fvg_bullish'] = is_fvg_bullish_np
        df['is_fvg_bearish'] = is_fvg_bearish_np
        df['fvg_bull_low'] = fvg_bull_low_np
        df['fvg_bull_high'] = fvg_bull_high_np
        df['fvg_bull_mid'] = fvg_bull_mid_np
        df['fvg_bear_low'] = fvg_bear_low_np
        df['fvg_bear_high'] = fvg_bear_high_np
        df['fvg_bear_mid'] = fvg_bear_mid_np
        
        # 3. Calcular Mitigación (50% FVG)
        df['is_mitigated'] = False
        is_mitigated_np = np.zeros(len(df), dtype=bool)

        bull_fvg_indices = df.index[df['is_fvg_bullish']]
        bear_fvg_indices = df.index[df['is_fvg_bearish']]

        logger.debug(f"[{self.symbol}] Calculando mitigación (50% FVG) para {len(bull_fvg_indices)} FVG alcistas y {len(bear_fvg_indices)} FVG bajistas...")

        for fvg_idx_time in bull_fvg_indices:
            fvg_iloc = df.index.get_loc(fvg_idx_time)
            fvg_mid_price = df['fvg_bull_mid'].iloc[fvg_iloc]
            if fvg_iloc + 1 < len(df):
                future_lows = df['low'].values[fvg_iloc + 1:]
                if (future_lows <= fvg_mid_price).any():
                    is_mitigated_np[fvg_iloc] = True

        for fvg_idx_time in bear_fvg_indices:
            fvg_iloc = df.index.get_loc(fvg_idx_time)
            fvg_mid_price = df['fvg_bear_mid'].iloc[fvg_iloc]
            if fvg_iloc + 1 < len(df):
                future_highs = df['high'].values[fvg_iloc + 1:]
                if (future_highs >= fvg_mid_price).any():
                    is_mitigated_np[fvg_iloc] = True
        
        df['is_mitigated'] = is_mitigated_np
        self.df = df 
        logger.info(f"[{self.symbol}] Patrones SMC y mitigación calculados.")

    def compute_atr(self, window: int = 14):
        """Calcula ATR simple para umbrales de proximidad."""
        high = self.df['high']
        low = self.df['low']
        close = self.df['close']
        prev_close = close.shift(1)
        tr = pd.concat([
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs()
        ], axis=1).max(axis=1)
        self.df['atr'] = tr.rolling(window=window, min_periods=window).mean()

    def compute_macd(self, df: pd.DataFrame, fast: int = None, slow: int = None, signal: int = None) -> pd.DataFrame:
        """Calcula MACD clásico (EMA fast/slow + signal) sobre un DataFrame."""
        if df.empty: return df
        df_copy = df.copy()
        if fast is None: fast = self.macd_fast
        if slow is None: slow = self.macd_slow
        if signal is None: signal = self.macd_signal
        close = df_copy['close']
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        macd_signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        macd_hist = macd_line - macd_signal_line
        df_copy['macd'] = macd_line
        df_copy['macd_signal'] = macd_signal_line
        df_copy['macd_hist'] = macd_hist
        return df_copy

    def _binsize(self, ref_price: float, tol: float) -> float:
        return max(1e-8, ref_price * tol)

    def build_liquidity_pools(self, i: int, lookback: int = None, tol: float = None):
        """Construye pools de liquidez como concentración de niveles en una ventana."""
        if lookback is None: lookback = self.pool_lookback_bars
        if tol is None: tol = self.equal_tol
        start = max(0, i - lookback)
        window = self.df.iloc[start:i]
        if window.empty: return []

        mid_price = float(window['close'].iloc[-1])
        binsize = self._binsize(mid_price, tol)
        step = 5.0 if mid_price < 5000 else 10.0
        pools = {}

        def add(price: float, score: float):
            if price is None or np.isnan(price): return
            bucket = round(price / binsize)
            level = bucket * binsize
            pools[level] = pools.get(level, 0.0) + score

        # Equal highs/lows
        highs = window['high'].values; lows = window['low'].values
        for arr, base_score in ((highs, 3.0), (lows, 3.0)):
            buckets = {}
            for p in arr:
                b = round(p / binsize)
                buckets[b] = buckets.get(b, 0) + 1
            for b, cnt in buckets.items():
                if cnt >= 2: add(b * binsize, base_score * cnt)

        # Swings
        swing_highs = window['max'].dropna().values if 'max' in window.columns else []
        swing_lows = window['min'].dropna().values if 'min' in window.columns else []
        for p in swing_highs: add(float(p), 4.0)
        for p in swing_lows: add(float(p), 4.0)

        # FVG borders (usando las columnas precalculadas)
        for p in window['fvg_bull_high'].dropna().values: add(float(p), 2.5)
        for p in window['fvg_bear_low'].dropna().values: add(float(p), 2.5)
        
        # Niveles redondos
        wmin = float(window['low'].min()); wmax = float(window['high'].max())
        if step > 0:
            lvl = (np.floor(wmin / step) * step)
            while lvl <= wmax:
                hits = ((np.abs(window['high'] - lvl) <= binsize) | (np.abs(window['low'] - lvl) <= binsize)).sum()
                if hits >= 1: add(lvl, 0.5 * hits)
                lvl += step

        levels = [{'price': float(k), 'score': float(v)} for k, v in pools.items()]
        levels.sort(key=lambda x: (-x['score'], x['price']))
        return levels

    def select_target_pool(self, i: int, direction: str, entry_price: float, sl_price: float, pools: list):
        """Elige el pool objetivo."""
        if not pools: return None
        atr = self.df['atr'].iloc[i-1] if 'atr' in self.df.columns and i-1 >= 0 and not pd.isna(self.df['atr'].iloc[i-1]) else np.nan
        max_dist = 1.5 * atr if not np.isnan(atr) and atr is not None else None

        if direction == 'LONG':
            candidates = [p for p in pools if p['price'] > entry_price]
            candidates.sort(key=lambda p: (-p['score'], abs(p['price'] - entry_price)))
            if max_dist is not None:
                within = [p for p in candidates if (p['price'] - entry_price) <= max_dist]
                if within: return within[0]['price']
            return candidates[0]['price'] if candidates else None
        else:
            candidates = [p for p in pools if p['price'] < entry_price]
            candidates.sort(key=lambda p: (-p['score'], abs(p['price'] - entry_price)))
            if max_dist is not None:
                within = [p for p in candidates if (entry_price - p['price']) <= max_dist]
                if within: return within[0]['price']
            return candidates[0]['price'] if candidates else None

    def _run_simulation(self):
        """Ejecuta el bucle de simulación (anteriormente 'run_backtest')."""
        logger.info(f"Simulando trades para {self.symbol}...")
        
        # Empezar desde un índice seguro donde todos los datos (incluido MTF) sean válidos
        start_idx = self.structure_lookback + 2
        if start_idx >= len(self.df):
            logger.error(f"No hay suficientes datos para {self.symbol} después de la preparación.")
            return

        i = start_idx
        while i < len(self.df):
            if self.active_trade:
                exit_info = self.manage_active_trade(i)
                if exit_info:
                    i = exit_info['exit_idx'] + 1
                    continue
            
            # --- PRIORIDAD MODIFICADA ---
            
            # Prioridad 1: FVG con Memoria
            if self.check_fvg_memory_long(i):
                i += 1
                continue
            elif self.check_fvg_memory_short(i):
                i += 1
                continue

            # Prioridad 2: Barrido (Sweep) + FVG Inmediato
            elif self.check_long_setup(i):
                i += 1
                continue
            elif self.check_short_setup(i):
                i += 1
                continue
            
            i += 1
        
        logger.info(f"=== SIMULACIÓN COMPLETADA PARA {self.symbol} ===")

    def _check_mtf_filter(self, i: int, direction: str) -> bool:
        """Función helper para chequear el filtro MTF (1H) en el backtest."""
        # Usamos i-1 para usar datos de la vela CERRADA anterior como filtro
        if i-1 < 0: return False
        
        if 'macd_1h' not in self.df.columns:
            logger.warning(f"[{self.symbol}] No hay datos de MACD 1H para filtrar.")
            return False
            
        macd_1h = self.df['macd_1h'].iloc[i-1] 
        
        if pd.isna(macd_1h):
            logger.warning(f"[{self.symbol}] Valor de MACD 1H es NaN en la vela {i-1}.")
            return False
        
        if direction == 'LONG':
            if not (macd_1h > 0):
                # logger.debug(f"[{self.symbol}] Setup LONG ignorado por filtro MTF (1H: {macd_1h:.2f})")
                return False
        elif direction == 'SHORT':
            if not (macd_1h < 0):
                # logger.debug(f"[{self.symbol}] Setup SHORT ignorado por filtro MTF (1H: {macd_1h:.2f})")
                return False
                
        return True # Filtro pasado

    def check_long_setup(self, i):
        """Setup de Barrido (Sweep) LONG + Filtro MTF 1H"""
        recent_lows = self.df['min'].iloc[i-50:i].dropna()
        if len(recent_lows) < 2 or recent_lows.iloc[-1] >= recent_lows.iloc[-2]: return False
        
        try: sweep_idx = self.df.index.get_loc(recent_lows.index[-1])
        except KeyError: return False
        if i - sweep_idx > 12: return False
        
        fvg_window = self.df.iloc[sweep_idx:i]
        bullish_fvgs = fvg_window[fvg_window['is_fvg_bullish']]
        if not bullish_fvgs.empty:
            
            # --- CORRECCIÓN LÓGICA UNIFICADA (ENTRADA AL 50%) ---
            fvg_mid_price = bullish_fvgs['fvg_bull_mid'].iloc[-1] # Usar el 50%
            
            if self.df['low'].iloc[i] <= fvg_mid_price: # Comprobar toque al 50%
                if not self._check_mtf_filter(i, 'LONG'):
                    return False
                
                entry_price = float(fvg_mid_price) # Entrar al 50%
                # --- FIN CORRECCIÓN ---
                
                liquidity_level = float(recent_lows.iloc[-1]) # SL Estructural
                pools = self.build_liquidity_pools(i, lookback=self.pool_lookback_bars, tol=self.equal_tol)
                tp_pool = self.select_target_pool(i, 'LONG', entry_price, liquidity_level, pools)
                self.open_trade(i, 'LONG', entry_price, liquidity_level, tp_override=tp_pool)
                return True
        return False

    def check_short_setup(self, i):
        """Setup de Barrido (Sweep) SHORT + Filtro MTF 1H"""
        recent_highs = self.df['max'].iloc[i-50:i].dropna()
        if len(recent_highs) < 2 or recent_highs.iloc[-1] <= recent_highs.iloc[-2]: return False

        try: sweep_idx = self.df.index.get_loc(recent_highs.index[-1])
        except KeyError: return False
        if i - sweep_idx > 12: return False

        fvg_window = self.df.iloc[sweep_idx:i]
        bearish_fvgs = fvg_window[fvg_window['is_fvg_bearish']]
        if not bearish_fvgs.empty:
            
            # --- CORRECCIÓN LÓGICA UNIFICADA (ENTRADA AL 50%) ---
            fvg_mid_price = bearish_fvgs['fvg_bear_mid'].iloc[-1] # Usar el 50%
            
            if self.df['high'].iloc[i] >= fvg_mid_price: # Comprobar toque al 50%
                if not self._check_mtf_filter(i, 'SHORT'):
                    return False

                entry_price = float(fvg_mid_price) # Entrar al 50%
                # --- FIN CORRECCIÓN ---
                
                liquidity_level = float(recent_highs.iloc[-1]) # SL Estructural
                pools = self.build_liquidity_pools(i, lookback=self.pool_lookback_bars, tol=self.equal_tol)
                tp_pool = self.select_target_pool(i, 'SHORT', entry_price, liquidity_level, pools)
                self.open_trade(i, 'SHORT', entry_price, liquidity_level, tp_override=tp_pool)
                return True
        return False
    # --- NUEVAS FUNCIONES DE SETUP (FVG MEMORY) ---
    
    def check_fvg_memory_long(self, i: int) -> bool:
        """Setup de Retorno a FVG Alcista (Memoria) + Filtro MTF 1H"""
        df = self.df
        candle = df.iloc[i]
        
        df_slice = df.iloc[:i]
        unmitigated_bull_fvgs = df_slice[
            (df_slice['is_fvg_bullish'] == True) & (df_slice['is_mitigated'] == False)
        ]
        if unmitigated_bull_fvgs.empty: return False

        touching_fvgs = unmitigated_bull_fvgs[
            candle['low'] <= unmitigated_bull_fvgs['fvg_bull_high']
        ]
        if touching_fvgs.empty: return False

        fvg_to_trade = touching_fvgs.iloc[-1]
        
        entry_price = float(fvg_to_trade['fvg_bull_high'])
        # Stop loss un poco más abajo del borde del FVG (buffer de ~0.05%)
        fvg_low = float(fvg_to_trade['fvg_bull_low'])
        sl_buffer = fvg_low * 0.0005  # 0.05% del precio
        liquidity_level = fvg_low - sl_buffer 

        # --- NUEVO FILTRO MTF (1H) ---
        if not self._check_mtf_filter(i, 'LONG'):
            return False
        # --- FIN FILTRO MTF ---
        
        pools = self.build_liquidity_pools(i, lookback=self.pool_lookback_bars, tol=self.equal_tol)
        tp_pool = self.select_target_pool(i, 'LONG', entry_price, liquidity_level, pools)
        
        logger.debug(f" [{self.symbol}] Setup FVG CON MEMORIA (LONG) detectado en {df.index[i]}. FVG de {fvg_to_trade.name}")
        self.open_trade(i, 'LONG', entry_price, liquidity_level, tp_override=tp_pool)
        return True

    def check_fvg_memory_short(self, i: int) -> bool:
        """Setup de Retorno a FVG Bajista (Memoria) + Filtro MTF 1H"""
        df = self.df
        candle = df.iloc[i]
        
        df_slice = df.iloc[:i]
        unmitigated_bear_fvgs = df_slice[
            (df_slice['is_fvg_bearish'] == True) & (df_slice['is_mitigated'] == False)
        ]
        if unmitigated_bear_fvgs.empty: return False

        touching_fvgs = unmitigated_bear_fvgs[
            candle['high'] >= unmitigated_bear_fvgs['fvg_bear_low']
        ]
        if touching_fvgs.empty: return False

        fvg_to_trade = touching_fvgs.iloc[-1]
        
        entry_price = float(fvg_to_trade['fvg_bear_low'])
        # Stop loss un poco más arriba del borde del FVG (buffer de ~0.05%)
        fvg_high = float(fvg_to_trade['fvg_bear_high'])
        sl_buffer = fvg_high * 0.0005  # 0.05% del precio
        liquidity_level = fvg_high + sl_buffer  # SL Estructural

        # --- NUEVO FILTRO MTF (1H) ---
        if not self._check_mtf_filter(i, 'SHORT'):
            return False
        # --- FIN FILTRO MTF ---
        
        pools = self.build_liquidity_pools(i, lookback=self.pool_lookback_bars, tol=self.equal_tol)
        tp_pool = self.select_target_pool(i, 'SHORT', entry_price, liquidity_level, pools)
        
        logger.debug(f"💡 [{self.symbol}] Setup FVG CON MEMORIA (SHORT) detectado en {df.index[i]}. FVG de {fvg_to_trade.name}")
        self.open_trade(i, 'SHORT', entry_price, liquidity_level, tp_override=tp_pool)
        return True

    # --- FIN NUEVAS FUNCIONES DE SETUP ---

    def open_trade(self, entry_idx, direction, entry_price, liquidity_level, tp_override=None):
        """
        Abre una nueva posición. 
        USA STOP LOSS ESTRUCTURAL (liquidity_level).
        Incluye fix para RIESGO CERO.
        Incluye fix para VALIDACIÓN DE MARGEN (N3).
        """
        
        # --- CORRECCIÓN N1: USAR STOP LOSS ESTRUCTURAL ---
        stop_loss_price = liquidity_level
        original_stop_loss_price = stop_loss_price
            
        if direction == 'LONG':
            risk_per_unit = entry_price - stop_loss_price
            default_tp = entry_price * (1 + self.tp_percentage / 100) # TP por %
        else: # SHORT
            risk_per_unit = stop_loss_price - entry_price
            default_tp = entry_price * (1 - self.tp_percentage / 100) # TP por %
        # --- FIN CORRECCIÓN N1 ---

        # --- CORRECCIÓN N2: BUG DE RIESGO-CERO (¡NUEVO!) ---
        # Definir un riesgo mínimo (ej: 0.1% del precio de entrada) para evitar
        # que un SL estructural idéntico a la entrada genere un tamaño de posición infinito.
        min_risk_as_price = entry_price * 0.001 # 0.1% Mínimo SL
        
        if risk_per_unit < min_risk_as_price:
            logger.debug(f"[{self.symbol}] Trade en {self.df.index[entry_idx]}: Riesgo estructural ({risk_per_unit:.5f}) es demasiado bajo. Ajustando a {min_risk_as_price:.5f}.")
            risk_per_unit = min_risk_as_price
            
            # Recalcular el SL basado en este nuevo riesgo mínimo
            if direction == 'LONG':
                stop_loss_price = entry_price - risk_per_unit
            else:
                stop_loss_price = entry_price + risk_per_unit
            
            # Guardar el nuevo SL ajustado
            original_stop_loss_price = stop_loss_price
        # --- FIN CORRECCIÓN N2 ---

        if risk_per_unit <= 0: # Esta validación sigue siendo importante
            logger.debug(f"[{self.symbol}] Trade ignorado: Riesgo inválido ({risk_per_unit})")
            return

        take_profit_price = default_tp
        if tp_override is not None:
            rr = 0.0
            if risk_per_unit > 0:
                if direction == 'LONG':
                    if tp_override > entry_price:
                        rr = (tp_override - entry_price) / risk_per_unit
                else:
                    if tp_override < entry_price:
                        rr = (entry_price - tp_override) / risk_per_unit
            if rr >= self.min_rr:
                take_profit_price = tp_override

        capital_to_risk = self.balance * self.risk_per_trade_pct
        
        # 1. Calcular tamaño en activo base (ej: ETH)
        position_size_base = capital_to_risk / risk_per_unit
        
        # 2. Calcular tamaño nocional en USD (ej: USDT)
        position_size_usd = position_size_base * entry_price

        # --- NUEVA CORRECCIÓN N3: VALIDACIÓN DE MARGEN ---
        # El apalancamiento (ej: 15) se establece en el MultiSymbolBacktest.
        # La posición nocional MÁXIMA que la cuenta puede abrir es balance * apalancamiento.
        max_notional_position_usd = self.balance * self.leverage
        
        if position_size_usd > max_notional_position_usd:
            logger.warning(f"[{self.symbol}] Trade en {self.df.index[entry_idx]}: El tamaño de posición nocional calculado (${position_size_usd:,.2f}) "
                           f"supera el máximo permitido por el apalancamiento (${max_notional_position_usd:,.2f}). "
                           f"Reduciendo tamaño al máximo.")
            
            # Reducir el tamaño nocional al máximo permitido
            position_size_usd = max_notional_position_usd
            # Recalcular el tamaño en el activo base correspondiente
            position_size_base = position_size_usd / entry_price
        # --- FIN CORRECCIÓN N3 ---

        self.active_trade = {
            'entry_idx': entry_idx, 
            'direction': direction, 
            'entry_price': entry_price,
            'sl_price': stop_loss_price,
            'original_sl_price': original_stop_loss_price, 
            'tp_price': take_profit_price,
            'position_size_base': position_size_base, # Tamaño en activo base (ej: ETH)
            'position_size_usd': position_size_usd     # Tamaño nocional en USD (ej: USDT)
        }


    def manage_active_trade(self, current_idx):
        """Gestiona la salida de un trade activo (TP, SL, TimeLimit) para la vela actual."""
        trade = self.active_trade
        i = current_idx
        
        if i <= trade['entry_idx']: return None
                
        candle = self.df.iloc[i]
        exit_reason, pnl, exit_price = None, 0, 0

        if trade['direction'] == 'LONG':
            if self.enable_structural_stop:
                # Mirar estructura desde la entrada HASTA la vela anterior (i-1)
                recent_structure_lows = self.df['min'].iloc[trade['entry_idx']:i].dropna()
                if not recent_structure_lows.empty:
                    new_protective_stop = recent_structure_lows.iloc[-1]
                    if new_protective_stop > trade['sl_price']:
                        trade['sl_price'] = new_protective_stop
            
            if candle['high'] >= trade['tp_price']:
                exit_reason, exit_price = 'Take Profit', trade['tp_price']
            elif candle['low'] <= trade['sl_price']:
                exit_price = trade['sl_price']
                exit_reason = 'Stop Loss' if trade['sl_price'] == trade['original_sl_price'] else 'Stop Estructural (Trailing)'

        else:  # SHORT
            if self.enable_structural_stop:
                recent_structure_highs = self.df['max'].iloc[trade['entry_idx']:i].dropna()
                if not recent_structure_highs.empty:
                    new_protective_stop = recent_structure_highs.iloc[-1]
                    if new_protective_stop < trade['sl_price']:
                        trade['sl_price'] = new_protective_stop
            
            if candle['low'] <= trade['tp_price']:
                exit_reason, exit_price = 'Take Profit', trade['tp_price']
            elif candle['high'] >= trade['sl_price']:
                exit_price = trade['sl_price']
                exit_reason = 'Stop Loss' if trade['sl_price'] == trade['original_sl_price'] else 'Stop Estructural (Trailing)'
        
        if not exit_reason and (i - trade['entry_idx']) >= self.max_candles_in_trade:
            exit_reason, exit_price = 'Time Limit', candle['close']

        if exit_reason:
            # PnL se calcula en _log_and_close_trade usando position_size_base
            self._log_and_close_trade(trade, i, exit_price, exit_reason, pnl) # pnl se pasa como 0 y se recalcula dentro
            return {'exit_idx': i}
        
        return None 

    def _log_and_close_trade(self, trade, exit_idx, exit_price, exit_reason, pnl):
        """Función auxiliar para registrar un trade con todos los detalles."""
        
        # --- CÁLCULO DE PNL MODIFICADO ---
        # El PnL ahora debe usar position_size_base
        pnl = (exit_price - trade['entry_price']) * trade['position_size_base'] if trade['direction'] == 'LONG' else (trade['entry_price'] - exit_price) * trade['position_size_base']
        # --- FIN MODIFICACIÓN ---

        trade_log = {
            'entry_time': self.df.index[trade['entry_idx']],
            'exit_time': self.df.index[exit_idx],
            'direction': trade['direction'],
            'entry_price': trade['entry_price'],
            'exit_price': exit_price,
            'stop_loss': trade['sl_price'],
            'original_sl_price': trade.get('original_sl_price', trade['sl_price']),
            'take_profit': trade['tp_price'],
            'pnl': pnl,
            'position_size_base': trade.get('position_size_base', None), # ej: 0.1 ETH
            'position_size_usd': trade.get('position_size_usd', None),   # ej: 300 USDT
            'exit_reason': exit_reason
        }
        
        # --- CORRECCIÓN ---
        # self.balance += pnl  <-- ¡ELIMINADA!
        self.trades.append(trade_log)
        # self.equity_curve.append({'time': self.df.index[exit_idx], 'balance': self.balance}) <-- ¡ELIMINADA!
        # --- FIN CORRECCIÓN ---
        
        self.active_trade = None
        if callable(getattr(self, 'pnl_callback', None)):
            try:
                # Informar al orquestador (que SÍ actualizará el balance)
                self.pnl_callback(self.symbol, pnl, trade_log)
            except Exception as _:
                pass

    def run_backtest(self, days=5):
        """Prepara todos los datos MTF y luego corre la simulación."""
        data_ready = self.prepare_all_data(days=days)
        if not data_ready or self.df is None: 
            logger.error(f"No se pudieron preparar los datos para {self.symbol}. Abortando backtest.")
            return

        self._run_simulation()
        logger.info(f"=== BACKTEST COMPLETADO PARA {self.symbol} ===")
        self.print_results()
        # self.plot_results() # Descomentar para ver gráfico

    def print_results(self):
        """Imprime un resumen de los resultados del backtest."""
        if not self.trades:
            logger.warning(f"No se generaron trades para {self.symbol}.")
            return
            
        trades_df = pd.DataFrame(self.trades)
        total_trades = len(trades_df)
        wins = trades_df[trades_df['pnl'] > 0]
        losses = trades_df[trades_df['pnl'] <= 0]
        win_rate = (len(wins) / total_trades) * 100 if total_trades > 0 else 0
        
        equity_df = pd.DataFrame(self.equity_curve).dropna().set_index('time')
        max_drawdown = 0
        if not equity_df.empty:
            equity_df['peak'] = equity_df['balance'].cummax()
            equity_df['drawdown_pct'] = (equity_df['balance'] - equity_df['peak']) / equity_df['peak']
            max_drawdown = equity_df['drawdown_pct'].min()

        profit_factor = 0
        if not losses.empty and losses['pnl'].sum() != 0:
            profit_factor = wins['pnl'].sum() / abs(losses['pnl'].sum())
        elif not wins.empty and losses.empty:
            profit_factor = 999.99

        logger.info(f"Resultados para {self.symbol}:")
        logger.info(f"  Balance Final: ${self.balance:,.2f}")
        logger.info(f"  Retorno: {(self.balance/self.initial_balance - 1)*100:,.2f}%")
        logger.info(f"  Total Trades: {total_trades}")
        logger.info(f"  Win Rate: {win_rate:.2f}%")
        logger.info(f"  Profit Factor: {profit_factor:.2f}")
        logger.info(f"  Max Drawdown: {max_drawdown*100:.2f}%")
        logger.info(f"  Ganancia Prom.: ${wins['pnl'].mean():,.2f}")
        logger.info(f"  Pérdida Prom.: ${losses['pnl'].mean():,.2f}")

    def plot_results(self):
        """Grafica la curva de equity."""
        if len(self.equity_curve) <= 1: return
        
        equity_df = pd.DataFrame(self.equity_curve).dropna().set_index('time')
        plt.style.use('seaborn-v0_8-whitegrid')
        fig, ax = plt.subplots(figsize=(14, 7))
        ax.plot(equity_df.index, equity_df['balance'], label='Balance', color='#0077b6', linewidth=1.5)
        ax.fill_between(equity_df.index, self.initial_balance, equity_df['balance'], where=(equity_df['balance'] >= self.initial_balance), color='#2ca02c', alpha=0.3, interpolate=True)
        ax.set_title(f'Curva de Equity - SMC MTF - {self.symbol}', fontsize=16, fontweight='bold')
        filename = f"equity_curve_SMC_MTF_{self.symbol.replace('/', '_')}.png"
        plt.savefig(filename, dpi=300)
        logger.info(f"Gráfico guardado como: {filename}")
        plt.show()

    def save_excel_report(self, trades_df: pd.DataFrame):
        # Esta función es llamada por la clase MultiSymbolBacktest
        pass


class MultiSymbolBacktest:
    """
    Orquestador de backtesting multi-símbolo con gestión de balance global
    y lógica de concurrencia.
    """
    def __init__(self, symbols: list[str], initial_balance: float = 1000.0, symbol_configs: dict = None):
        self.symbols = [s.replace('USDT', '/USDT') if 'USDT' in s and '/' not in s else s for s in symbols]
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.max_concurrent = 4
        self.symbol_leverage = {
            'ETH/USDT': 15,
            'HYPE/USDT': 15,
            'SOL/USDT': 15,
            'SUI/USDT': 15,
        }
        self.symbol_configs = symbol_configs or {}
        self.engines: dict[str, SmartMoneyScalpingBacktest] = {}
        self.concurrent_open = 0
        self.combined_equity = [{'time': None, 'balance': self.initial_balance}]
        self.all_trades: list[dict] = []

    def _pnl_sink(self, symbol: str, pnl: float, trade_log: dict):
        """Callback que recibe PnL de motores individuales y actualiza el balance global."""
        self.balance += pnl
        self.all_trades.append({'symbol': symbol, **trade_log})
        self.combined_equity.append({'time': trade_log['exit_time'], 'balance': self.balance})
        self.concurrent_open = sum(1 for e in self.engines.values() if e.active_trade is not None)
    
    def get_total_margin_used(self) -> float:
        """Calcula el margen total usado por todas las posiciones abiertas."""
        total_margin = 0.0
        for eng in self.engines.values():
            if eng.active_trade is not None:
                # Calcular margen usado: (tamaño nocional / apalancamiento)
                position_size_usd = eng.active_trade.get('position_size_usd', 0.0)
                leverage = eng.leverage
                if leverage > 0:
                    margin = position_size_usd / leverage
                    total_margin += margin
        return total_margin

    def _fetch_and_prepare(self, days=5):
        """Prepara los datos MTF para todos los símbolos."""
        logger.info("--- Iniciando preparación de datos Multi-Símbolo ---")
        for s in self.symbols:
            symbol_key = s.replace('/', '')
            symbol_config = self.symbol_configs.get(symbol_key, {})
            
            eng = SmartMoneyScalpingBacktest(symbol=s, initial_balance=self.balance, symbol_config=symbol_config)
            
            # --- ASIGNAR APALANCAMIENTO AL MOTOR ---
            eng.leverage = self.symbol_leverage.get(s, eng.leverage)
            # --- FIN ASIGNACIÓN ---
            
            # --- MODIFICADO: Llamar a la nueva función de preparación ---
            data_ready = eng.prepare_all_data(days=days)
            # --- FIN MODIFICACIÓN ---

            if not data_ready or eng.df is None or eng.df.empty:
                logger.warning(f"Sin datos para {s}, se omite.")
                continue
                
            eng.pnl_callback = self._pnl_sink
            self.engines[s] = eng
            
            logger.info(f"Configuración para {s}: Apalancamiento={eng.leverage}, Stop estructural={eng.enable_structural_stop}, TP%={eng.tp_percentage}")

    def run(self, days=5):
        """Ejecuta el backtest multi-símbolo."""
        self._fetch_and_prepare(days=days)
        if not self.engines:
            logger.error("No hay símbolos con datos para backtest multisímbolo.")
            return

        all_ts = sorted(set().union(*[set(eng.df.index) for eng in self.engines.values()]))

        def loc_idx(eng: SmartMoneyScalpingBacktest, ts):
            try:
                return eng.df.index.get_loc(ts)
            except KeyError:
                return None

        logger.info(f"Multisímbolo: {len(self.engines)} símbolos; {len(all_ts)} timestamps.")

        for ts in all_ts:
            # 1) Gestionar posiciones abiertas
            for s, eng in self.engines.items():
                i = loc_idx(eng, ts)
                if i is None: continue
                if eng.active_trade:
                    eng.manage_active_trade(i) 

            # 2) Recalcular concurrencia
            self.concurrent_open = sum(1 for e in self.engines.values() if e.active_trade is not None)

            # 3) Buscar nuevas entradas respetando límite global
            for s, eng in self.engines.items():
                if self.concurrent_open >= self.max_concurrent:
                    break
                
                i = loc_idx(eng, ts)
                if i is None or i < eng.structure_lookback + 2:
                    continue
                if eng.active_trade:
                    continue

                # --- CORRECCIÓN: USAR BALANCE LIBRE (BALANCE - MARGEN USADO) ---
                total_margin_used = self.get_total_margin_used()
                free_balance = self.balance - total_margin_used
                eng.balance = free_balance if free_balance > 0 else 0
                # --- FIN CORRECCIÓN ---
                
                # --- ASIGNAR APALANCAMIENTO (REDUNDANTE PERO SEGURO) ---
                # El apalancamiento es necesario ANTES de llamar a open_trade
                eng.leverage = self.symbol_leverage.get(s, eng.leverage)
                # --- FIN ASIGNACIÓN ---
                
                opened = False
                long_opened = False
                
                # --- LÓGICA DE PRIORIDAD ACTUALIZADA ---
                if eng.check_fvg_memory_long(i):
                    opened = True
                    long_opened = True
                elif eng.check_fvg_memory_short(i):
                    opened = True
                elif eng.check_long_setup(i):
                    opened = True
                    long_opened = True
                elif eng.check_short_setup(i):
                    opened = True
                # --- FIN LÓGICA DE PRIORIDAD ---

                if opened:
                    # eng.leverage = self.symbol_leverage.get(s, eng.leverage) # Movido arriba
                    self.concurrent_open += 1
                    
                    # Lógica de correlación ETH -> SOL
                    if long_opened and s == 'ETH/USDT' and 'SOL/USDT' in self.engines:
                        sol_eng = self.engines['SOL/USDT']
                        sol_i = loc_idx(sol_eng, ts)
                        
                        if (sol_i is not None and 
                            sol_i >= sol_eng.structure_lookback + 2 and 
                            not sol_eng.active_trade and 
                            self.concurrent_open < self.max_concurrent):
                            
                            # Usar balance libre para SOL también
                            total_margin_used_sol = self.get_total_margin_used()
                            free_balance_sol = self.balance - total_margin_used_sol
                            sol_eng.balance = free_balance_sol if free_balance_sol > 0 else 0
                            sol_eng.leverage = self.symbol_leverage.get('SOL/USDT', sol_eng.leverage)
                            
                            current_price = float(sol_eng.df['close'].iloc[sol_i])
                            atr_value = sol_eng.df['atr'].iloc[sol_i-1] if 'atr' in sol_eng.df.columns and not pd.isna(sol_eng.df['atr'].iloc[sol_i-1]) else current_price * 0.02
                            stop_loss = current_price - (atr_value * 1.5) # SL basado en ATR
                            
                            sol_eng.open_trade(sol_i, 'LONG', current_price, stop_loss) 
                            self.concurrent_open += 1
                            logger.info(f"🔗 ETH Long detectado -> Abriendo Long automático en SOL a ${current_price:.4f}")

        self._print_results()
        self._plot_combined_equity()
        self._save_excel_report_multi()

    def _print_results(self):
        print("\n" + "="*60)
        print("RESULTADOS SMC MULTI-SÍMBOLO (MTF 1H + FVG Memory)")
        print("="*60)
        print(f"Balance Final:      ${self.balance:,.2f} | Retorno: {(self.balance/self.initial_balance - 1)*100:,.2f}%")
        total_trades = len(self.all_trades)
        wins = [t for t in self.all_trades if t['pnl'] > 0]
        win_rate = (len(wins) / total_trades) * 100 if total_trades > 0 else 0
        print(f"Total de Trades:    {total_trades}")
        print(f"Win Rate:           {win_rate:.2f}%")
        
        eq_df = pd.DataFrame(self.combined_equity).dropna().set_index('time')
        max_drawdown = 0
        if not eq_df.empty:
            eq_df['peak'] = eq_df['balance'].cummax()
            eq_df['drawdown_pct'] = (eq_df['balance'] - eq_df['peak']) / eq_df['peak']
            max_drawdown = eq_df['drawdown_pct'].min()
        print(f"Max Drawdown:       {max_drawdown*100:.2f}%")
        print("="*60)

    def _plot_combined_equity(self):
        eq_df = pd.DataFrame(self.combined_equity).dropna().set_index('time')
        if eq_df.empty: return
        plt.style.use('seaborn-v0_8-whitegrid')
        fig, ax = plt.subplots(figsize=(14, 7))
        ax.plot(eq_df.index, eq_df['balance'], label='Balance', color='#0077b6', linewidth=1.5)
        ax.fill_between(eq_df.index, self.initial_balance, eq_df['balance'], where=(eq_df['balance'] >= self.initial_balance), color='#2ca02c', alpha=0.3, interpolate=True)
        ax.set_title('Curva de Equity - SMC Multisímbolo (MTF 1H + FVG Memory)', fontsize=16, fontweight='bold')
        filename = 'equity_curve_SMC_multisymbol_MTF.png'
        plt.savefig(filename, dpi=300)
        logger.info(f"Gráfico combinado guardado como: {filename}")
        plt.show()

    def _save_excel_report_multi(self):
        trades_df = pd.DataFrame(self.all_trades)
        filename = f"report_SMC_MULTI_MTF_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        logger.info(f"Generando reporte Excel multisímbolo: {filename}")

        with pd.ExcelWriter(filename, engine='xlsxwriter') as writer:
            workbook = writer.book
            header_format = workbook.add_format({'bold': True, 'bg_color': '#D3D3D3', 'border': 1, 'align': 'center'})
            money_format = workbook.add_format({'num_format': '$#,##0.00'})
            percent_format = workbook.add_format({'num_format': '0.00%'})

            # --- Hoja 1: Resumen General ---
            summary_sheet = workbook.add_worksheet('Resumen General')
            total_trades = len(trades_df)
            wins = trades_df[trades_df['pnl'] > 0]
            losses = trades_df[trades_df['pnl'] <= 0]
            win_rate = (len(wins) / total_trades) if total_trades > 0 else 0
            
            eq_df = pd.DataFrame(self.combined_equity).dropna().set_index('time')
            max_drawdown = 0
            if not eq_df.empty:
                eq_df['peak'] = eq_df['balance'].cummax()
                eq_df['drawdown_pct'] = (eq_df['balance'] - eq_df['peak']) / eq_df['peak']
                max_drawdown = eq_df['drawdown_pct'].min()

            profit_factor = 0
            if not losses.empty and losses['pnl'].sum() != 0:
                profit_factor = wins['pnl'].sum() / abs(losses['pnl'].sum())
            elif not wins.empty and losses.empty:
                profit_factor = 999.99 
            
            summary_data = {
                "Balance Inicial": self.initial_balance,
                "Balance Final": self.balance,
                "Retorno Total": (self.balance / self.initial_balance) - 1,
                "P/L Neto": trades_df['pnl'].sum() if not trades_df.empty else 0,
                "Max Drawdown": max_drawdown if not np.isnan(max_drawdown) and not np.isinf(max_drawdown) else 0,
                "Total de Trades": total_trades,
                "Win Rate": win_rate if not np.isnan(win_rate) and not np.isinf(win_rate) else 0,
                "Profit Factor": profit_factor if not np.isnan(profit_factor) and not np.isinf(profit_factor) else 0,
                "Ganancia Promedio": wins['pnl'].mean() if not wins.empty and not np.isnan(wins['pnl'].mean()) else 0,
                "Pérdida Promedio": losses['pnl'].mean() if not losses.empty and not np.isnan(losses['pnl'].mean()) else 0
            }
            summary_sheet.write_row('A1', ['Métrica', 'Valor'], header_format)
            row = 1
            for key, value in summary_data.items():
                summary_sheet.write(row, 0, key)
                fmt = money_format
                if "Retorno" in key or "Rate" in key or "Drawdown" in key: fmt = percent_format
                elif "Trades" in key or "Factor" in key: fmt = None
                summary_sheet.write(row, 1, value, fmt)
                row += 1
            summary_sheet.set_column('A:A', 25); summary_sheet.set_column('B:B', 18)

            if trades_df.empty:
                logger.warning("No hay trades para generar el resto del reporte Excel.")
                return

            # --- Hoja 2: Todos los Trades ---
            trades_df_excel = trades_df.copy()
            trades_df_excel['entry_time'] = pd.to_datetime(trades_df_excel['entry_time']).dt.tz_localize(None)
            trades_df_excel['exit_time'] = pd.to_datetime(trades_df_excel['exit_time']).dt.tz_localize(None)
            trades_df_excel.to_excel(writer, sheet_name='Todos los Trades', index=False)

            # --- Hojas de Análisis Detallado ---
            
            # Desempeño por Días
            daily_sheet = workbook.add_worksheet('Desempeño por Días')
            daily_perf = trades_df.copy()
            daily_perf['date'] = pd.to_datetime(daily_perf['entry_time']).dt.tz_localize(None).dt.date
            daily_agg = daily_perf.groupby('date').agg(total_pnl=('pnl', 'sum')).reset_index()
            daily_sheet.write_row('A1', ['Fecha', 'P/L Total'], header_format)
            for r_idx, r in daily_agg.iterrows():
                daily_sheet.write(r_idx + 1, 0, r['date'].strftime('%Y-%m-%d'))
                daily_sheet.write(r_idx + 1, 1, r['total_pnl'], money_format)
            chart = workbook.add_chart({'type': 'column'})
            chart.add_series({'name': 'P/L Diario', 'categories': f"='Desempeño por Días'!$A$2:$A${len(daily_agg)+1}", 'values': f"='Desempeño por Días'!$B$2:$B${len(daily_agg)+1}"})
            daily_sheet.insert_chart('D2', chart)

            # Desempeño por Horas
            hourly_sheet = workbook.add_worksheet('Desempeño por Horas')
            hourly_perf = trades_df.copy()
            hourly_perf['hour'] = pd.to_datetime(hourly_perf['entry_time']).dt.tz_localize(None).dt.hour
            hourly_agg = hourly_perf.groupby('hour').agg(total_pnl=('pnl', 'sum')).reset_index()
            hourly_sheet.write_row('A1', ['Hora', 'P/L Total'], header_format)
            for r_idx, r in hourly_agg.iterrows():
                hourly_sheet.write(r_idx + 1, 0, r['hour'])
                hourly_sheet.write(r_idx + 1, 1, r['total_pnl'], money_format)
            chart = workbook.add_chart({'type': 'column'})
            chart.add_series({'name': 'P/L por Hora', 'categories': f"='Desempeño por Horas'!$A$2:$A${len(hourly_agg)+1}", 'values': f"='Desempeño por Horas'!$B$2:$B${len(hourly_agg)+1}"})
            hourly_sheet.insert_chart('D2', chart)

            # Desempeño por Día Semana
            weekday_sheet = workbook.add_worksheet('Desempeño por Día Semana')
            weekday_perf = trades_df.copy()
            weekday_perf['weekday'] = pd.to_datetime(weekday_perf['entry_time']).dt.tz_localize(None).dt.weekday
            day_names = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo']
            weekday_agg = weekday_perf.groupby('weekday').agg(total_pnl=('pnl', 'sum')).reindex(range(7)).fillna(0).reset_index()
            weekday_agg['weekday'] = weekday_agg['weekday'].map(lambda x: day_names[x])
            weekday_sheet.write_row('A1', ['Día', 'P/L Total'], header_format)
            for r_idx, r in weekday_agg.iterrows():
                weekday_sheet.write(r_idx + 1, 0, r['weekday'])
                weekday_sheet.write(r_idx + 1, 1, r['total_pnl'], money_format)
            chart = workbook.add_chart({'type': 'column'})
            chart.add_series({'name': 'P/L por Día', 'categories': "='Desempeño por Día Semana'!$A$2:$A$8", 'values': "='Desempeño por Día Semana'!$B$2:$B$8"})
            weekday_sheet.insert_chart('D2', chart)

            # Resumen por Símbolo
            if 'symbol' in trades_df.columns:
                df_rr = trades_df.copy()
                
                # --- FUNCIÓN DE RR MODIFICADA ---
                def compute_rr(row):
                    try:
                        pos_size = float(row.get('position_size_base')) # Usar el tamaño en activo base
                        orig_sl = float(row.get('original_sl_price'))
                        entry = float(row['entry_price'])
                        pnl_total = float(row['pnl'])
                        
                        if pos_size is None or pos_size == 0: return np.nan
                        
                        per_unit_pnl = pnl_total / pos_size
                        if row['direction'] == 'LONG': risk_per_unit = entry - orig_sl
                        else: risk_per_unit = orig_sl - entry
                        
                        if risk_per_unit <= 0: return np.nan
                        
                        return per_unit_pnl / risk_per_unit
                    except Exception: return np.nan
                # --- FIN FUNCIÓN DE RR MODIFICADA ---
                
                df_rr['rr'] = df_rr.apply(compute_rr, axis=1)

                sym_summary_sheet = workbook.add_worksheet('Resumen por Símbolo')
                sym_summary_sheet.write_row('A1', ['Símbolo', 'P/L Neto', 'Trades', 'Win Rate', 'Profit Factor', 'Avg Win', 'Avg Loss', 'RR Medio', 'RR Mediano'], header_format)
                r = 1
                for sym, df_sym in df_rr.groupby('symbol'):
                    wins_sym = df_sym[df_sym['pnl'] > 0]
                    losses_sym = df_sym[df_sym['pnl'] <= 0]
                    total_sym = len(df_sym)
                    win_rate_sym = (len(wins_sym) / total_sym) if total_sym > 0 else 0
                    if not losses_sym.empty and losses_sym['pnl'].sum() != 0:
                        pf_sym = wins_sym['pnl'].sum() / abs(losses_sym['pnl'].sum())
                    elif not wins_sym.empty and losses_sym.empty: pf_sym = 999.99  
                    else: pf_sym = 0
                    avg_win = wins_sym['pnl'].mean() if not wins_sym.empty else 0
                    avg_loss = losses_sym['pnl'].mean() if not losses_sym.empty else 0
                    sym_summary_sheet.write(r, 0, sym)
                    sym_summary_sheet.write(r, 1, df_sym['pnl'].sum(), money_format)
                    sym_summary_sheet.write(r, 2, total_sym)
                    sym_summary_sheet.write(r, 3, win_rate_sym, percent_format)
                    sym_summary_sheet.write(r, 4, pf_sym)
                    sym_summary_sheet.write(r, 5, avg_win, money_format)
                    sym_summary_sheet.write(r, 6, avg_loss, money_format)
                    
                    rr_mean = df_sym['rr'].mean()
                    rr_median = df_sym['rr'].median()
                    rr_mean = rr_mean if not np.isnan(rr_mean) and not np.isinf(rr_mean) else 0
                    rr_median = rr_median if not np.isnan(rr_median) and not np.isinf(rr_median) else 0
                    
                    sym_summary_sheet.write(r, 7, rr_mean)
                    sym_summary_sheet.write(r, 8, rr_median)
                    r += 1
                sym_summary_sheet.set_column('A:A', 14); sym_summary_sheet.set_column('B:I', 14)

                # Trades por Símbolo
                for sym, df_sym in df_rr.groupby('symbol'):
                    sheet_name = f"Trades_{str(sym).replace('/', '_')}"
                    sheet_name = sheet_name[:31]
                    ws = workbook.add_worksheet(sheet_name)
                    df_x = df_sym.copy()
                    df_x['entry_time'] = pd.to_datetime(df_x['entry_time']).dt.tz_localize(None)
                    df_x['exit_time'] = pd.to_datetime(df_x['exit_time']).dt.tz_localize(None)
                    headers = list(df_x.columns)
                    ws.write_row('A1', headers, header_format)
                    for ridx, row_data in enumerate(df_x.itertuples(index=False), start=2):
                        for cidx, val in enumerate(row_data, start=1):
                            # Sanitizar valores NaN/Inf
                            if isinstance(val, (int, float)):
                                if np.isnan(val) or np.isinf(val):
                                    val = 0
                            ws.write(ridx-1, cidx-1, val)

        logger.info("✅ Reporte Excel multisímbolo guardado.")

# --- Configuración y Ejecución del Backtest ---
if __name__ == "__main__":
    script_dir = os.path.dirname(os.path.realpath(__file__))
    config_filename = 'cofigETHBTC.json'
    config_path = os.path.join(script_dir, config_filename)

    symbols_cfg = ['ETHUSDT', 'SOLUSDT', 'HYPEUSDT']
    initial_balance_cfg = 30.0
    symbol_configs_cfg = {}
    max_concurrent_cfg = 4
    
    try:
        with open(config_path, 'r') as f:
            cfg = json.load(f)
            symbols_cfg = cfg.get('symbols', symbols_cfg)
            initial_balance_cfg = float(cfg.get('initial_balance', initial_balance_cfg))
            symbol_configs_cfg = cfg.get('symbol_configs', {})
            max_concurrent_cfg = cfg.get('max_concurrent_open', max_concurrent_cfg)
            
        logger.info(f"Configuración cargada desde {config_filename}")
        logger.info(f"Símbolos: {symbols_cfg}")
        logger.info(f"Balance inicial: ${initial_balance_cfg}")
        logger.info(f"Configuraciones por símbolo: {len(symbol_configs_cfg)} símbolos configurados")
        
    except FileNotFoundError:
        logger.warning(f"No se encontró '{config_filename}'. Usando configuración por defecto: {symbols_cfg}")
    except Exception as e:
        logger.error(f"Error leyendo '{config_filename}': {e}")

    # Ejecutar el Backtest Multisímbolo
    msb = MultiSymbolBacktest(symbols=symbols_cfg, initial_balance=initial_balance_cfg, symbol_configs=symbol_configs_cfg)
    msb.max_concurrent = max_concurrent_cfg
    msb.run(days=30) # Correr backtest de 30 días