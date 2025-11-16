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
        
        MODIFICADO: Más permisivo para SHORT con FVGs fuertes
        - LONG: MACD > 0 (tendencia alcista)
        - SHORT: MACD < 0 o MACD ligeramente positivo si hay FVG fuerte
        
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
            # MÁS PERMISIVO: Permitir longs si MACD > 0 o si está cerca de 0
            # pero hay fuerte evidencia alcista (FVG no mitigado)
            if macd_1h < -0.0001:  # Solo rechazar si MACD es claramente negativo
                logger.debug(f"Setup LONG rechazado por filtro MTF (MACD 1H: {macd_1h:.4f})")
                return False
            else:
                logger.info(f"✅ Setup LONG permitido (MACD 1H: {macd_1h:.4f})")
        
        elif direction == 'SHORT':
            # MÁS PERMISIVO: Permitir shorts si MACD < 0 o si está cerca de 0
            # pero hay fuerte evidencia bajista (FVG no mitigado)
            if macd_1h > 0.0001:  # Solo rechazar si MACD es claramente positivo
                logger.debug(f"Setup SHORT rechazado por filtro MTF (MACD 1H: {macd_1h:.4f})")
                return False
            else:
                logger.info(f"✅ Setup SHORT permitido (MACD 1H: {macd_1h:.4f})")
        
        return True
    
    def check_fvg_memory_setup(
        self, 
        df: pd.DataFrame, 
        direction: str,
        current_price: Optional[float] = None
    ) -> Optional[Dict]:
        """
        Detecta setup de FVG con Memoria (retorno a FVG no mitigado).
        
        IMPORTANTE: Usa el precio actual en tiempo real, no el close de la vela.
        
        Args:
            df: DataFrame con FVGs detectados
            direction: 'LONG' o 'SHORT'
            current_price: Precio actual del mercado (si None, usa close de vela)
            
        Returns:
            Dict con info del setup o None
        """
        if df.empty or len(df) < 2:
            return None
        
        # Vela actual (última)
        current_candle = df.iloc[-1]
        
        # Precio actual: usar ticker en tiempo real o fallback a close
        if current_price is None:
            current_price = float(current_candle['close'])
        
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
            
            # Verificar si la vela actual toca el rango del FVG
            touching_fvgs = unmitigated_fvgs[
                (current_candle['low'] <= unmitigated_fvgs['fvg_bull_high']) &
                (current_candle['high'] >= unmitigated_fvgs['fvg_bull_low'])
            ]
            
            # ELIMINADO: Lógica de proximidad - SOLO se permite toque directo
            # La entrada debe ser SI y SOLO SI el precio actual toca el FVG
            
            if touching_fvgs.empty:
                return None
            
            # Tomar el FVG más reciente
            fvg = touching_fvgs.iloc[-1]
            
            # ENTRADA AL PRECIO ACTUAL DE MERCADO EN TIEMPO REAL
            entry_price = current_price
            
            # SL un poco debajo del borde inferior del FVG
            fvg_low = float(fvg['fvg_bull_low'])
            sl_buffer = fvg_low * 0.0005
            stop_loss = fvg_low - sl_buffer
            
            logger.info(
                f"🎯 FVG LONG detectado: Entry={entry_price:.2f} (precio actual), "
                f"SL={stop_loss:.2f}, FVG=[{fvg_low:.2f}, {fvg['fvg_bull_high']:.2f}]"
            )
            
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
                logger.debug("❌ No hay FVGs bajistas no mitigados")
                return None
            
            # FIX: Verificar si la vela actual toca el rango del FVG
            # ANTES era: high >= mid AND low <= high (MUY RESTRICTIVO)
            # AHORA es: high >= low AND low <= high (toca cualquier parte del FVG)
            touching_fvgs = unmitigated_fvgs[
                (current_candle['high'] >= unmitigated_fvgs['fvg_bear_low']) &
                (current_candle['low'] <= unmitigated_fvgs['fvg_bear_high'])
            ]
            
            if touching_fvgs.empty:
                logger.debug(
                    f"❌ Vela actual NO toca FVGs bajistas. "
                    f"High: {current_candle['high']:.2f}, "
                    f"Low: {current_candle['low']:.2f}"
                )
                # Log de FVGs disponibles para debugging
                for idx, fvg_row in unmitigated_fvgs.iterrows():
                    logger.debug(
                        f"  FVG disponible: [{fvg_row['fvg_bear_low']:.2f} - {fvg_row['fvg_bear_high']:.2f}]"
                    )
                return None
            
            # Tomar el FVG más reciente
            fvg = touching_fvgs.iloc[-1]
            
            # ENTRADA AL PRECIO ACTUAL DE MERCADO EN TIEMPO REAL
            entry_price = current_price
            
            # SL un poco arriba del borde superior del FVG
            fvg_high = float(fvg['fvg_bear_high'])
            sl_buffer = fvg_high * 0.0005
            stop_loss = fvg_high + sl_buffer
            
            logger.info(
                f"🎯 FVG SHORT detectado: Entry={entry_price:.2f} (precio actual), "
                f"SL={stop_loss:.2f}, FVG=[{fvg['fvg_bear_low']:.2f}, {fvg_high:.2f}]"
            )
            
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
        direction: str,
        current_price: Optional[float] = None
    ) -> Optional[Dict]:
        """
        Detecta setup de Barrido (Sweep) de liquidez + FVG.
        
        IMPORTANTE: Usa el precio actual en tiempo real.
        
        Args:
            df: DataFrame con swings y FVGs
            direction: 'LONG' o 'SHORT'
            current_price: Precio actual del mercado (si None, usa close de vela)
            
        Returns:
            Dict con info del setup o None
        """
        if df.empty or len(df) < 50:
            return None
        
        current_candle = df.iloc[-1]
        
        # Precio actual: usar ticker en tiempo real o fallback a close
        if current_price is None:
            current_price = float(current_candle['close'])
        
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
            
            # ENTRADA AL PRECIO ACTUAL DE MERCADO EN TIEMPO REAL
            entry_price = current_price
            
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
            
            # ENTRADA AL PRECIO ACTUAL DE MERCADO EN TIEMPO REAL
            entry_price = current_price
            
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
        setup: Dict,
        levels: Optional[Dict] = None
    ) -> Optional[Dict]:
        """
        Valida un setup completo (entrada + filtro MTF + niveles).
        
        Args:
            df: DataFrame con todos los datos
            setup: Diccionario con info del setup
            levels: Niveles de soporte/resistencia (opcional)
            
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