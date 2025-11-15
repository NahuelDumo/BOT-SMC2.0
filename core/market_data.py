"""
Módulo de Gestión de Datos de Mercado
Descarga y actualiza datos OHLCV para múltiples timeframes
"""

import logging
import pandas as pd
import numpy as np
from typing import Dict, Optional
import ccxt

logger = logging.getLogger(__name__)


class MarketDataManager:
    """Gestiona la descarga y actualización de datos de mercado"""
    
    def __init__(self, public_client: ccxt.Exchange):
        self.public_client = public_client
        self.dfs: Dict[str, pd.DataFrame] = {}  # 15m data
        self.dfs_1h: Dict[str, pd.DataFrame] = {}  # 1h data
        
    async def update_market_data(self, symbol: str) -> bool:
        """
        Actualiza los dataframes de 15m y 1h para un símbolo.
        
        Args:
            symbol: Par de trading (ej: 'ETH/USDT')
            
        Returns:
            bool: True si la actualización fue exitosa
        """
        try:
            # Descargar datos de 15m
            limit_15m = 1000
            ohlcv_15m = self.public_client.fetch_ohlcv(
                symbol, 
                timeframe='15m', 
                limit=limit_15m
            )
            
            df_15m = pd.DataFrame(
                ohlcv_15m, 
                columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
            )
            df_15m['timestamp'] = pd.to_datetime(
                df_15m['timestamp'], 
                unit='ms'
            ).dt.tz_localize('UTC').dt.tz_convert('Etc/GMT+3')
            df_15m.set_index('timestamp', inplace=True)
            self.dfs[symbol] = df_15m
            
            # Descargar datos de 1h
            limit_1h = 200
            ohlcv_1h = self.public_client.fetch_ohlcv(
                symbol, 
                timeframe='1h', 
                limit=limit_1h
            )
            
            df_1h = pd.DataFrame(
                ohlcv_1h, 
                columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
            )
            df_1h['timestamp'] = pd.to_datetime(
                df_1h['timestamp'], 
                unit='ms'
            ).dt.tz_localize('UTC').dt.tz_convert('Etc/GMT+3')
            df_1h.set_index('timestamp', inplace=True)
            self.dfs_1h[symbol] = df_1h
            
            logger.info(
                f"📊 Datos actualizados para {symbol} "
                f"(15m: {len(df_15m)}, 1h: {len(df_1h)})"
            )
            return True
            
        except Exception as e:
            logger.error(f"Error actualizando datos para {symbol}: {e}")
            return False
    
    def get_dataframe(self, symbol: str, timeframe: str = '15m') -> Optional[pd.DataFrame]:
        """
        Obtiene el DataFrame para un símbolo y timeframe.
        
        Args:
            symbol: Par de trading
            timeframe: '15m' o '1h'
            
        Returns:
            DataFrame o None si no existe
        """
        if timeframe == '15m':
            return self.dfs.get(symbol)
        elif timeframe == '1h':
            return self.dfs_1h.get(symbol)
        else:
            logger.warning(f"Timeframe {timeframe} no soportado")
            return None
    
    def compute_indicators(self, symbol: str) -> bool:
        """
        Calcula todos los indicadores necesarios (MACD, ATR).
        
        Args:
            symbol: Par de trading
            
        Returns:
            bool: True si el cálculo fue exitoso
        """
        try:
            # Calcular MACD en 1H
            df_1h = self.dfs_1h.get(symbol)
            if df_1h is None or df_1h.empty:
                logger.warning(f"No hay datos de 1H para calcular MACD: {symbol}")
                return False
            
            self.dfs_1h[symbol] = self._compute_macd(df_1h)
            
            # Fusionar MACD de 1H al DataFrame de 15m
            df_15m = self.dfs.get(symbol)
            if df_15m is None or df_15m.empty:
                logger.warning(f"No hay datos de 15m para {symbol}")
                return False
            
            df_1h_macd = self.dfs_1h[symbol][['macd']].rename(
                columns={'macd': 'macd_1h'}
            )
            
            self.dfs[symbol] = pd.merge_asof(
                df_15m.sort_index(),
                df_1h_macd.sort_index(),
                left_index=True,
                right_index=True,
                direction='backward'
            )
            
            # Calcular ATR en 15m
            self.dfs[symbol] = self._compute_atr(self.dfs[symbol])
            
            # Limpiar NaNs de MTF
            self.dfs[symbol].dropna(subset=['macd_1h'], inplace=True)
            
            logger.debug(f"✅ Indicadores calculados para {symbol}")
            return True
            
        except Exception as e:
            logger.error(f"Error calculando indicadores para {symbol}: {e}")
            return False
    
    @staticmethod
    def _compute_macd(
        df: pd.DataFrame, 
        fast: int = 12, 
        slow: int = 26, 
        signal: int = 9
    ) -> pd.DataFrame:
        """Calcula MACD clásico (EMA fast/slow + signal)"""
        if df.empty:
            return df
        
        df_copy = df.copy()
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
    
    @staticmethod
    def _compute_atr(df: pd.DataFrame, window: int = 14) -> pd.DataFrame:
        """Calcula ATR (Average True Range)"""
        if df.empty:
            return df
        
        df_copy = df.copy()
        high = df_copy['high']
        low = df_copy['low']
        close = df_copy['close']
        prev_close = close.shift(1)
        
        tr = pd.concat([
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs()
        ], axis=1).max(axis=1)
        
        df_copy['atr'] = tr.rolling(window=window, min_periods=window).mean()
        
        return df_copy