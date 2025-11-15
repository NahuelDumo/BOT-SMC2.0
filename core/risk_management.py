"""
Módulo de Gestión de Riesgo
Cálculo de tamaño de posición, SL/TP, y validación de margen
"""

import logging
from typing import Dict, Optional, List
from dataclasses import dataclass
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """Estructura para almacenar datos de una posición"""
    symbol: str
    direction: str
    size_base: float  # Tamaño en activo base (ej: ETH)
    size_usd: float   # Tamaño nocional en USD
    entry_price: float
    entry_time: str
    entry_idx: int
    stop_loss: float
    original_stop_loss: float
    take_profit: float
    liquidation_price: float = 0.0
    margin_used: float = 0.0
    is_copy: bool = False
    setup_type: str = 'UNKNOWN'


class RiskManager:
    """Gestiona el riesgo de las operaciones"""
    
    def __init__(
        self, 
        risk_per_trade_pct: float = 1.0,
        min_risk_as_pct: float = 0.1,
        risk_reward_ratio: float = 2.0,
        pool_lookback_bars: int = 192,
        equal_tol: float = 0.0003
    ):
        self.risk_per_trade_pct = risk_per_trade_pct
        self.min_risk_as_pct = min_risk_as_pct
        self.risk_reward_ratio = risk_reward_ratio
        self.pool_lookback_bars = pool_lookback_bars
        self.equal_tol = equal_tol
    
    def calculate_position_size(
        self,
        balance: float,
        leverage: int,
        entry_price: float,
        stop_loss: float,
        direction: str
    ) -> Dict[str, float]:
        """
        Calcula el tamaño de la posición basado en riesgo fijo.
        
        IMPORTANTE: Implementa las MISMAS validaciones que el backtest:
        1. Riesgo mínimo (evitar riesgo cero)
        2. Validación de margen (N3)
        
        Args:
            balance: Balance disponible
            leverage: Apalancamiento
            entry_price: Precio de entrada
            stop_loss: Precio de stop loss
            direction: 'LONG' o 'SHORT'
            
        Returns:
            Dict con size_base, size_usd, y stop_loss ajustado
        """
        # Calcular riesgo por unidad
        if direction == 'LONG':
            risk_per_unit = entry_price - stop_loss
        else:  # SHORT
            risk_per_unit = stop_loss - entry_price
        
        # CORRECCIÓN N2: Validar riesgo mínimo (evitar riesgo cero)
        min_risk_as_price = entry_price * (self.min_risk_as_pct / 100)
        
        if risk_per_unit < min_risk_as_price:
            logger.debug(
                f"Riesgo estructural ({risk_per_unit:.5f}) es muy bajo. "
                f"Ajustando a {min_risk_as_price:.5f}"
            )
            risk_per_unit = min_risk_as_price
            
            # Recalcular SL basado en nuevo riesgo mínimo
            if direction == 'LONG':
                stop_loss = entry_price - risk_per_unit
            else:
                stop_loss = entry_price + risk_per_unit
        
        # Validar que el riesgo sea positivo
        if risk_per_unit <= 0:
            logger.warning(f"Riesgo inválido: {risk_per_unit}")
            return {
                'size_base': 0.0,
                'size_usd': 0.0,
                'stop_loss': stop_loss,
                'valid': False
            }
        
        # Calcular capital a arriesgar
        capital_to_risk = balance * (self.risk_per_trade_pct / 100)
        
        # Calcular tamaño en activo base
        position_size_base = capital_to_risk / risk_per_unit
        
        # Calcular tamaño nocional en USD
        position_size_usd = position_size_base * entry_price
        
        # CORRECCIÓN N3: Validación de margen
        max_notional_position_usd = balance * leverage
        
        if position_size_usd > max_notional_position_usd:
            logger.warning(
                f"Tamaño nocional (${position_size_usd:,.2f}) supera "
                f"el máximo permitido (${max_notional_position_usd:,.2f}). "
                f"Reduciendo al máximo."
            )
            
            # Reducir al máximo permitido
            position_size_usd = max_notional_position_usd
            position_size_base = position_size_usd / entry_price
        
        return {
            'size_base': position_size_base,
            'size_usd': position_size_usd,
            'stop_loss': stop_loss,
            'valid': True
        }
    
    def calculate_take_profit(
        self,
        entry_price: float,
        stop_loss: float,
        direction: str,
        tp_override: Optional[float] = None
    ) -> float:
        """
        Calcula el precio de take profit.
        
        Args:
            entry_price: Precio de entrada
            stop_loss: Precio de stop loss
            direction: 'LONG' o 'SHORT'
            tp_override: TP manual (opcional)
            
        Returns:
            Precio de take profit
        """
        if tp_override is not None:
            # Validar que el TP tenga sentido
            if direction == 'LONG' and tp_override > entry_price:
                return tp_override
            elif direction == 'SHORT' and tp_override < entry_price:
                return tp_override
        
        # Calcular TP basado en R:R
        if direction == 'LONG':
            risk = entry_price - stop_loss
            take_profit = entry_price + (risk * self.risk_reward_ratio)
        else:  # SHORT
            risk = stop_loss - entry_price
            take_profit = entry_price - (risk * self.risk_reward_ratio)
        
        return take_profit
    
    def update_trailing_stop(
        self,
        position: Position,
        current_price: float,
        df_swings: 'pd.DataFrame'
    ) -> Optional[float]:
        """
        Actualiza el stop loss con trailing estructural.
        
        IMPORTANTE: Usa la misma lógica que el backtest.
        
        Args:
            position: Posición activa
            current_price: Precio actual
            df_swings: DataFrame con swings detectados
            
        Returns:
            Nuevo stop loss o None si no hay cambio
        """
        if df_swings.empty:
            return None
        
        if position.direction == 'LONG':
            # Buscar swing lows desde la entrada hasta ahora
            recent_lows = df_swings['min'].dropna()
            
            if recent_lows.empty:
                return None
            
            # Tomar el swing low más reciente
            new_protective_stop = float(recent_lows.iloc[-1])
            
            # Solo actualizar si es más favorable (más alto)
            if new_protective_stop > position.stop_loss:
                logger.info(
                    f"Trailing stop activado: {position.stop_loss:.4f} "
                    f"→ {new_protective_stop:.4f}"
                )
                return new_protective_stop
        
        else:  # SHORT
            # Buscar swing highs desde la entrada hasta ahora
            recent_highs = df_swings['max'].dropna()
            
            if recent_highs.empty:
                return None
            
            # Tomar el swing high más reciente
            new_protective_stop = float(recent_highs.iloc[-1])
            
            # Solo actualizar si es más favorable (más bajo)
            if new_protective_stop < position.stop_loss:
                logger.info(
                    f"Trailing stop activado: {position.stop_loss:.4f} "
                    f"→ {new_protective_stop:.4f}"
                )
                return new_protective_stop
        
        return None
    
    def check_breakeven(
        self,
        position: Position,
        current_price: float
    ) -> Optional[float]:
        """
        Verifica si se debe mover el SL a breakeven.
        
        Mueve el SL a entrada + pequeño buffer cuando se alcanza 1R.
        
        Args:
            position: Posición activa
            current_price: Precio actual
            
        Returns:
            Nuevo stop loss a breakeven o None
        """
        risk = abs(position.entry_price - position.original_stop_loss)
        
        if risk <= 0:
            return None
        
        if position.direction == 'LONG':
            # Si el precio alcanzó 1R de ganancia
            if current_price >= position.entry_price + risk:
                breakeven_sl = position.entry_price * 1.0001  # Buffer de 0.01%
                
                # Solo actualizar si es mejor que el SL actual
                if breakeven_sl > position.stop_loss:
                    logger.info(
                        f"Breakeven activado: {position.stop_loss:.4f} "
                        f"→ {breakeven_sl:.4f}"
                    )
                    return breakeven_sl
        
        else:  # SHORT
            # Si el precio alcanzó 1R de ganancia
            if current_price <= position.entry_price - risk:
                breakeven_sl = position.entry_price * 0.9999  # Buffer de 0.01%
                
                # Solo actualizar si es mejor que el SL actual
                if breakeven_sl < position.stop_loss:
                    logger.info(
                        f"Breakeven activado: {position.stop_loss:.4f} "
                        f"→ {breakeven_sl:.4f}"
                    )
                    return breakeven_sl
        
        return None
    
    def _binsize(self, ref_price: float, tol: float) -> float:
        """Calcula el tamaño del bin para agrupar niveles de precio"""
        return max(1e-8, ref_price * tol)
    
    def build_liquidity_pools(self, df: pd.DataFrame, lookback: int = None, tol: float = None) -> List[Dict]:
        """
        Construye pools de liquidez como concentración de niveles en una ventana.
        
        Args:
            df: DataFrame con datos OHLCV y columnas adicionales (swings, FVGs)
            lookback: Número de velas hacia atrás a analizar
            tol: Tolerancia para agrupar niveles similares
            
        Returns:
            Lista de pools ordenados por score (mayor concentración)
        """
        if lookback is None: 
            lookback = self.pool_lookback_bars
        if tol is None: 
            tol = self.equal_tol
            
        if len(df) < lookback:
            return []
            
        window = df.tail(lookback)
        if window.empty:
            return []

        mid_price = float(window['close'].iloc[-1])
        binsize = self._binsize(mid_price, tol)
        step = 5.0 if mid_price < 5000 else 10.0
        pools = {}

        def add(price: float, score: float):
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

        # Swings (si existen las columnas)
        if 'max' in window.columns:
            swing_highs = window['max'].dropna().values
            for p in swing_highs:
                add(float(p), 4.0)
                
        if 'min' in window.columns:
            swing_lows = window['min'].dropna().values
            for p in swing_lows:
                add(float(p), 4.0)

        # FVG borders (si existen las columnas)
        if 'fvg_bull_high' in window.columns:
            for p in window['fvg_bull_high'].dropna().values:
                add(float(p), 2.5)
        if 'fvg_bear_low' in window.columns:
            for p in window['fvg_bear_low'].dropna().values:
                add(float(p), 2.5)
        
        # Niveles redondos
        wmin = float(window['low'].min())
        wmax = float(window['high'].max())
        if step > 0:
            lvl = (np.floor(wmin / step) * step)
            while lvl <= wmax:
                hits = ((np.abs(window['high'] - lvl) <= binsize) | 
                       (np.abs(window['low'] - lvl) <= binsize)).sum()
                if hits >= 1:
                    add(lvl, 0.5 * hits)
                lvl += step

        levels = [{'price': float(k), 'score': float(v)} for k, v in pools.items()]
        levels.sort(key=lambda x: (-x['score'], x['price']))
        return levels
    
    def select_target_pool(self, entry_price: float, direction: str, sl_price: float, 
                          pools: List[Dict], atr: float = None) -> Optional[float]:
        """
        Elige el pool objetivo para el take profit.
        
        Args:
            entry_price: Precio de entrada
            direction: 'LONG' o 'SHORT'
            sl_price: Precio de stop loss
            pools: Lista de pools generados por build_liquidity_pools
            atr: Valor ATR para filtrar por distancia máxima
            
        Returns:
            Precio del pool seleccionado o None
        """
        if not pools:
            return None
            
        # Calcular TP mínimo basado en R:R para asegurar ganancia mínima
        min_rr_tp = self.calculate_take_profit(entry_price, sl_price, direction)
        
        max_dist = 1.5 * atr if atr is not None else None

        if direction == 'LONG':
            # Filtrar pools que estén por encima del entry Y por encima del TP mínimo
            candidates = [p for p in pools if p['price'] > entry_price and p['price'] >= min_rr_tp]
            
            if not candidates:
                # Si no hay pools que cumplan el R:R mínimo, usar TP tradicional
                return None
                
            candidates.sort(key=lambda p: (-p['score'], abs(p['price'] - entry_price)))
            
            if max_dist is not None:
                within = [p for p in candidates if (p['price'] - entry_price) <= max_dist]
                if within:
                    return within[0]['price']
                    
            return candidates[0]['price'] if candidates else None
            
        else:  # SHORT
            # Filtrar pools que estén por debajo del entry Y por debajo del TP mínimo
            candidates = [p for p in pools if p['price'] < entry_price and p['price'] <= min_rr_tp]
            
            if not candidates:
                # Si no hay pools que cumplan el R:R mínimo, usar TP tradicional
                return None
                
            candidates.sort(key=lambda p: (-p['score'], abs(p['price'] - entry_price)))
            
            if max_dist is not None:
                within = [p for p in candidates if (entry_price - p['price']) <= max_dist]
                if within:
                    return within[0]['price']
                    
            return candidates[0]['price'] if candidates else None
    
    def calculate_take_profit_with_pools(
        self,
        entry_price: float,
        stop_loss: float,
        direction: str,
        df_candles: pd.DataFrame,
        tp_override: Optional[float] = None,
        atr: Optional[float] = None
    ) -> float:
        """
        Calcula TP usando pools de liquidez (como el backtest).
        
        Args:
            entry_price: Precio de entrada
            stop_loss: Precio de stop loss
            direction: 'LONG' o 'SHORT'
            df_candles: DataFrame con velas recientes e indicadores
            tp_override: TP manual (opcional)
            atr: Valor ATR para filtrar distancia
            
        Returns:
            Precio de take profit
        """
        if tp_override is not None:
            # Validar que el TP tenga sentido
            if direction == 'LONG' and tp_override > entry_price:
                return tp_override
            elif direction == 'SHORT' and tp_override < entry_price:
                return tp_override
        
        # Construir pools de liquidez
        pools = self.build_liquidity_pools(df_candles)
        
        if pools:
            # Seleccionar pool objetivo
            tp_pool = self.select_target_pool(entry_price, direction, stop_loss, pools, atr)
            
            if tp_pool is not None:
                logger.info(f"🎯 TP basado en pool de liquidez: ${tp_pool:.4f}")
                return tp_pool
        
        # Fallback a R:R tradicional si no hay pools
        logger.debug("No se encontraron pools de liquidez, usando R:R tradicional")
        return self.calculate_take_profit(entry_price, stop_loss, direction)