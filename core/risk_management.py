"""
Módulo de Gestión de Riesgo
Cálculo de tamaño de posición, SL/TP, y validación de margen
"""

import logging
from typing import Dict, Optional
from dataclasses import dataclass

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
        risk_reward_ratio: float = 2.0
    ):
        self.risk_per_trade_pct = risk_per_trade_pct
        self.min_risk_as_pct = min_risk_as_pct
        self.risk_reward_ratio = risk_reward_ratio
    
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