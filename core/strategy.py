"""
Módulo de Estrategia de Trading SMC
Validación de setups y filtros MTF
"""

import logging
import pandas as pd
import numpy as np
from typing import Optional, Dict

logger = logging.getLogger(__name__)


class SMCStrategy:
    """Implementa la estrategia SMC con filtros MTF"""
    
    def __init__(self):
        self.macd_threshold = 1e-6  # Umbral para considerar MACD como alcista/bajista
    
    def calculate_tp_with_liquidity_pools(
        self,
        entry_price: float,
        direction: str,
        df: pd.DataFrame,
        original_tp: Optional[float] = None
    ) -> Optional[float]:
        """
        Busca un pool de liquidación relevante (con concentración significativa) 
        que esté a más de 1% de distancia del entry. Si existe, lo usa como TP.
        
        Args:
            entry_price: Precio de entrada (ej: 136.75)
            direction: 'LONG' o 'SHORT'
            df: DataFrame con datos OHLCV (últimas 50 velas para detectar pools)
            original_tp: TP original calculado (para comparar)
            
        Returns:
            Precio del pool si es válido (>1% de distancia), None si no hay pool relevante
        """
        if df.empty or len(df) < 50:
            return None
        
        try:
            # Usar últimas 50 velas para detectar pools de liquidación
            lookback = min(50, len(df))
            window = df.tail(lookback)
            
            # Encontrar velas con volumen significativo (potenciales pools)
            volume_mean = window['volume'].mean()
            volume_std = window['volume'].std()
            
            # Umbral: media + 1 std (zonas de alto volumen)
            high_volume_threshold = volume_mean + volume_std
            high_volume_candles = window[window['volume'] >= high_volume_threshold]
            
            if high_volume_candles.empty:
                return None
            
            # Agrupar precios de cierre cercanos (detectar clusters de liquidez)
            pools = {}
            tolerance = entry_price * 0.001  # 0.1% tolerancia para agrupar precios
            
            for idx, row in high_volume_candles.iterrows():
                level = float(row['close'])
                volume = float(row['volume'])
                
                # Buscar pool existente cercano
                found = False
                for pool_level in pools.keys():
                    if abs(pool_level - level) < tolerance:
                        pools[pool_level] += volume
                        found = True
                        break
                
                if not found:
                    pools[level] = volume
            
            if not pools:
                return None
            
            # Filtrar pools con concentración RELEVANTE
            # (volumen acumulado debe ser > 15% del máximo volumen visto)
            max_volume = window['volume'].max()
            volume_threshold = max_volume * 0.15
            
            significant_pools = {p: v for p, v in pools.items() if v >= volume_threshold}
            
            if not significant_pools:
                return None
            
            # Buscar el pool más cercano que esté a > 1% de distancia del entry
            best_pool = None
            min_distance_pct = float('inf')
            
            if direction == 'LONG':
                # Para LONG: buscar pools ARRIBA del entry (resistencia)
                for pool_price, pool_volume in significant_pools.items():
                    if pool_price > entry_price:  # Pool debe estar arriba
                        distance_pct = ((pool_price - entry_price) / entry_price) * 100
                        
                        # Pool válido si está a > 1% de distancia y es el más cercano
                        if distance_pct > 1.0 and distance_pct < min_distance_pct:
                            best_pool = pool_price
                            min_distance_pct = distance_pct
            
            elif direction == 'SHORT':
                # Para SHORT: buscar pools DEBAJO del entry (soporte)
                for pool_price, pool_volume in significant_pools.items():
                    if pool_price < entry_price:  # Pool debe estar debajo
                        distance_pct = ((entry_price - pool_price) / entry_price) * 100
                        
                        # Pool válido si está a > 1% de distancia y es el más cercano
                        if distance_pct > 1.0 and distance_pct < min_distance_pct:
                            best_pool = pool_price
                            min_distance_pct = distance_pct
            
            if best_pool is not None:
                logger.info(
                    f"✅ {direction}: Pool de liquidación relevante detectado como TP: ${best_pool:.4f} "
                    f"({min_distance_pct:.2f}% de distancia desde entry ${entry_price:.4f})"
                    + (f" (TP original era ${original_tp:.4f})" if original_tp else "")
                )
                return best_pool
        
        except Exception as e:
            logger.debug(f"Error calculando TP con pools de liquidación: {e}")
        
        return None
    
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
            # MÁS ESTRICTO: Exigir MACD claramente negativo para SHORT
            # Rechazar si MACD >= -0.0001 (demasiado cercano a cero o positivo)
            if macd_1h >= -0.0001:
                logger.debug(f"Setup SHORT rechazado por filtro MTF (MACD 1H: {macd_1h:.4f} es muy débil)")
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
            
            # CORREGIDO: Verificar si el PRECIO ACTUAL (en tiempo real) toca el rango del FVG
            # NO usar la vela actual completa, SOLO el precio actual
            touching_fvgs = unmitigated_fvgs[
                (current_price >= unmitigated_fvgs['fvg_bull_low']) &
                (current_price <= unmitigated_fvgs['fvg_bull_high'])
            ]
            
            # ELIMINADO: Lógica de proximidad - SOLO se permite toque directo
            # La entrada debe ser SI y SOLO SI el precio actual toca el FVG
            
            if touching_fvgs.empty:
                return None
            
            # Tomar el FVG más reciente
            fvg = touching_fvgs.iloc[-1]
            
            # VALIDAR tamaño mínimo del FVG (>0.1% del precio)
            fvg_size = float(fvg['fvg_bull_high']) - float(fvg['fvg_bull_low'])
            fvg_size_pct = (fvg_size / entry_price) * 100
            
            if fvg_size_pct < 0.1:
                logger.debug(
                    f"❌ FVG alcista RECHAZADO por tamaño muy pequeño: {fvg_size_pct:.4f}% "
                    f"(mínimo: 0.1%)"
                )
                return None
            
            # ENTRADA AL BORDE DEL FVG (como en backtest)
            entry_price = float(fvg['fvg_bull_high'])
            
            # SL con buffer debajo del borde inferior del FVG (0.05% como en backtest)
            fvg_low = float(fvg['fvg_bull_low'])
            sl_buffer = fvg_low * 0.0005  # 0.05% de distancia (backtest config)
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
            
            # CORREGIDO: Verificar si el PRECIO ACTUAL (en tiempo real) toca el rango del FVG
            # NO usar la vela actual completa, SOLO el precio actual
            touching_fvgs = unmitigated_fvgs[
                (current_price >= unmitigated_fvgs['fvg_bear_low']) &
                (current_price <= unmitigated_fvgs['fvg_bear_high'])
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
            
            # VALIDAR tamaño mínimo del FVG (>0.1% del precio)
            fvg_size = float(fvg['fvg_bear_high']) - float(fvg['fvg_bear_low'])
            fvg_size_pct = (fvg_size / entry_price) * 100
            
            if fvg_size_pct < 0.1:
                logger.debug(
                    f"❌ FVG bajista RECHAZADO por tamaño muy pequeño: {fvg_size_pct:.4f}% "
                    f"(mínimo: 0.1%)"
                )
                return None
            
            # ENTRADA AL BORDE DEL FVG (como en backtest)
            entry_price = float(fvg['fvg_bear_low'])
            
            # SL con buffer arriba del borde superior del FVG (0.05% como en backtest)
            fvg_high = float(fvg['fvg_bear_high'])
            sl_buffer = fvg_high * 0.0005  # 0.05% de distancia (backtest config)
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
            entry_price = float(fvg['fvg_bull_mid'])
            
            # Verificar que el PRECIO ACTUAL (en tiempo real) toca el 50% del FVG
            if not (current_price >= fvg['fvg_bull_low'] and current_price <= fvg['fvg_bull_high']):
                return None
            
            # SL en el nivel de liquidez barrido (swing low)
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
            entry_price = float(fvg['fvg_bear_mid'])
            
            # Verificar que el PRECIO ACTUAL (en tiempo real) toca el 50% del FVG
            if not (current_price >= fvg['fvg_bear_low'] and current_price <= fvg['fvg_bear_high']):
                return None
            
            # SL en el nivel de liquidez barrido (swing high)
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