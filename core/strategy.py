"""
Módulo de Estrategia de Trading SMC
Validación de setups y filtros MTF
"""

import logging
import pandas as pd
from typing import Optional, Dict

logger = logging.getLogger(__name__)


class SMCStrategy:
    """Implementa la estrategia SMC con filtros MTF"""
    
    def __init__(self):
        self.macd_threshold = 1e-6  # Umbral para considerar MACD como alcista/bajista
    
    def check_mtf_filter(self, df: pd.DataFrame, direction: str) -> bool:
        """
        Verifica el filtro Multi-Time Frame (MACD 1H).
        
        CRÍTICO: Usa la MISMA lógica que el backtest.
        - Usa la vela CERRADA anterior (i-1)
        - MACD > 0 → Tendencia ALCISTA
        - MACD < 0 → Tendencia BAJISTA
        
        Args:
            df: DataFrame con macd_1h calculado
            direction: 'LONG' o 'SHORT'
            
        Returns:
            bool: True si el filtro MTF es favorable
        """
        if df.empty or len(df) < 2:
            return False
        
        if 'macd_1h' not in df.columns:
            logger.warning("No hay datos de MACD 1H para filtrar")
            return False
        
        # Usar la vela CERRADA anterior (i-2 porque i-1 es la actual)
        macd_1h = df['macd_1h'].iloc[-2]
        
        if pd.isna(macd_1h):
            logger.debug("Valor de MACD 1H es NaN")
            return False
        
        # Validar filtro según dirección
        if direction == 'LONG':
            if macd_1h <= self.macd_threshold:
                logger.debug(f"Setup LONG rechazado por filtro MTF (MACD 1H: {macd_1h:.4f})")
                return False
        
        elif direction == 'SHORT':
            if macd_1h >= -self.macd_threshold:
                logger.debug(f"Setup SHORT rechazado por filtro MTF (MACD 1H: {macd_1h:.4f})")
                return False
        
        return True
    
    def check_fvg_memory_setup(
        self, 
        df: pd.DataFrame, 
        direction: str
    ) -> Optional[Dict]:
        """
        Detecta setup de FVG con Memoria (retorno a FVG no mitigado).
        
        IMPORTANTE: Entrada al 50% del FVG (como en el backtest).
        
        Args:
            df: DataFrame con FVGs detectados
            direction: 'LONG' o 'SHORT'
            
        Returns:
            Dict con info del setup o None
        """
        if df.empty or len(df) < 2:
            return None
        
        # Vela actual (última)
        current_candle = df.iloc[-1]
        
        # FVGs históricos (excluir velas actuales)
        df_historical = df.iloc[:-1]
        
        if direction == 'LONG':
            # Buscar FVGs alcistas no mitigados
            unmitigated_fvgs = df_historical[
                (df_historical['is_fvg_bullish'] == True) & 
                (df_historical['is_mitigated'] == False)
            ]
            
            if unmitigated_fvgs.empty:
                return None
            
            # Verificar si la vela actual toca el 50% de algún FVG
            touching_fvgs = unmitigated_fvgs[
                (current_candle['low'] <= unmitigated_fvgs['fvg_bull_mid']) &
                (current_candle['high'] >= unmitigated_fvgs['fvg_bull_low'])
            ]
            
            if touching_fvgs.empty:
                return None
            
            # Tomar el FVG más reciente
            fvg = touching_fvgs.iloc[-1]
            
            # ENTRADA AL 50% DEL FVG (como en backtest)
            entry_price = float(fvg['fvg_bull_mid'])
            
            # SL un poco debajo del borde inferior del FVG
            fvg_low = float(fvg['fvg_bull_low'])
            sl_buffer = fvg_low * 0.0005
            stop_loss = fvg_low - sl_buffer
            
            return {
                'direction': 'LONG',
                'entry_price': entry_price,
                'stop_loss': stop_loss,
                'setup_type': 'FVG_MEMORY',
                'fvg_index': df.index.get_loc(fvg.name)
            }
        
        elif direction == 'SHORT':
            # Buscar FVGs bajistas no mitigados
            unmitigated_fvgs = df_historical[
                (df_historical['is_fvg_bearish'] == True) & 
                (df_historical['is_mitigated'] == False)
            ]
            
            if unmitigated_fvgs.empty:
                return None
            
            # Verificar si la vela actual toca el 50% de algún FVG
            touching_fvgs = unmitigated_fvgs[
                (current_candle['high'] >= unmitigated_fvgs['fvg_bear_mid']) &
                (current_candle['low'] <= unmitigated_fvgs['fvg_bear_high'])
            ]
            
            if touching_fvgs.empty:
                return None
            
            # Tomar el FVG más reciente
            fvg = touching_fvgs.iloc[-1]
            
            # ENTRADA AL 50% DEL FVG (como en backtest)
            entry_price = float(fvg['fvg_bear_mid'])
            
            # SL un poco arriba del borde superior del FVG
            fvg_high = float(fvg['fvg_bear_high'])
            sl_buffer = fvg_high * 0.0005
            stop_loss = fvg_high + sl_buffer
            
            return {
                'direction': 'SHORT',
                'entry_price': entry_price,
                'stop_loss': stop_loss,
                'setup_type': 'FVG_MEMORY',
                'fvg_index': df.index.get_loc(fvg.name)
            }
        
        return None
    
    def check_sweep_setup(
        self, 
        df: pd.DataFrame, 
        direction: str
    ) -> Optional[Dict]:
        """
        Detecta setup de Barrido (Sweep) de liquidez + FVG.
        
        IMPORTANTE: Entrada al 50% del FVG (como en backtest).
        
        Args:
            df: DataFrame con swings y FVGs
            direction: 'LONG' o 'SHORT'
            
        Returns:
            Dict con info del setup o None
        """
        if df.empty or len(df) < 50:
            return None
        
        if direction == 'LONG':
            # Buscar barrido de swing lows
            recent_lows = df['min'].iloc[-50:].dropna()
            
            if len(recent_lows) < 2:
                return None
            
            # Verificar que el último swing low es menor al anterior (barrido)
            if recent_lows.iloc[-1] >= recent_lows.iloc[-2]:
                return None
            
            try:
                sweep_idx = df.index.get_loc(recent_lows.index[-1])
            except KeyError:
                return None
            
            current_idx = len(df) - 1
            
            # Verificar que el barrido es reciente (< 12 velas)
            if current_idx - sweep_idx > 12:
                return None
            
            # Buscar FVG alcista después del barrido
            fvg_window = df.iloc[sweep_idx:current_idx]
            bullish_fvgs = fvg_window[fvg_window['is_fvg_bullish'] == True]
            
            if bullish_fvgs.empty:
                return None
            
            # Tomar el FVG más reciente
            fvg = bullish_fvgs.iloc[-1]
            
            # ENTRADA AL 50% DEL FVG (como en backtest)
            fvg_mid_price = fvg['fvg_bull_mid']
            
            # Verificar que la vela actual toca el 50% del FVG
            current_candle = df.iloc[-1]
            if not (current_candle['low'] <= fvg_mid_price <= current_candle['high']):
                return None
            
            entry_price = float(fvg_mid_price)
            
            # SL en el nivel de liquidez barrido
            stop_loss = float(recent_lows.iloc[-1])
            
            return {
                'direction': 'LONG',
                'entry_price': entry_price,
                'stop_loss': stop_loss,
                'setup_type': 'SWEEP',
                'sweep_level': stop_loss
            }
        
        elif direction == 'SHORT':
            # Buscar barrido de swing highs
            recent_highs = df['max'].iloc[-50:].dropna()
            
            if len(recent_highs) < 2:
                return None
            
            # Verificar que el último swing high es mayor al anterior (barrido)
            if recent_highs.iloc[-1] <= recent_highs.iloc[-2]:
                return None
            
            try:
                sweep_idx = df.index.get_loc(recent_highs.index[-1])
            except KeyError:
                return None
            
            current_idx = len(df) - 1
            
            # Verificar que el barrido es reciente (< 12 velas)
            if current_idx - sweep_idx > 12:
                return None
            
            # Buscar FVG bajista después del barrido
            fvg_window = df.iloc[sweep_idx:current_idx]
            bearish_fvgs = fvg_window[fvg_window['is_fvg_bearish'] == True]
            
            if bearish_fvgs.empty:
                return None
            
            # Tomar el FVG más reciente
            fvg = bearish_fvgs.iloc[-1]
            
            # ENTRADA AL 50% DEL FVG (como en backtest)
            fvg_mid_price = fvg['fvg_bear_mid']
            
            # Verificar que la vela actual toca el 50% del FVG
            current_candle = df.iloc[-1]
            if not (current_candle['low'] <= fvg_mid_price <= current_candle['high']):
                return None
            
            entry_price = float(fvg_mid_price)
            
            # SL en el nivel de liquidez barrido
            stop_loss = float(recent_highs.iloc[-1])
            
            return {
                'direction': 'SHORT',
                'entry_price': entry_price,
                'stop_loss': stop_loss,
                'setup_type': 'SWEEP',
                'sweep_level': stop_loss
            }
        
        return None
    
    def validate_setup(
        self, 
        df: pd.DataFrame, 
        setup: Dict
    ) -> Optional[Dict]:
        """
        Valida un setup completo (entrada + filtro MTF).
        
        Args:
            df: DataFrame con todos los datos
            setup: Diccionario con info del setup
            
        Returns:
            Setup validado o None
        """
        if setup is None:
            return None
        
        # Verificar filtro MTF
        if not self.check_mtf_filter(df, setup['direction']):
            return None
        
        # Setup validado
        return setup