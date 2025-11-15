"""
Módulo de Ejecución de Órdenes
Apertura, cierre y gestión de posiciones
"""

import logging
from datetime import datetime
from typing import Dict, Optional
import ccxt
from .risk_management import Position, RiskManager

logger = logging.getLogger(__name__)


class ExecutionManager:
    """Gestiona la ejecución de órdenes en el exchange"""
    
    def __init__(
        self, 
        private_client: ccxt.Exchange,
        risk_manager: RiskManager,
        max_candles_in_trade: int = 48
    ):
        self.private_client = private_client
        self.risk_manager = risk_manager
        self.max_candles_in_trade = max_candles_in_trade
        self.open_positions: Dict[str, Position] = {}
    
    async def execute_trade(
        self,
        symbol: str,
        setup: Dict,
        balance: float,
        leverage: int,
        current_time: datetime,
        df_candles: Optional['pd.DataFrame'] = None
    ) -> Optional[Position]:
        """
        Ejecuta una orden de mercado y crea la posición.
        
        Args:
            symbol: Par de trading
            setup: Diccionario con info del setup
            balance: Balance disponible
            leverage: Apalancamiento
            current_time: Timestamp actual
            
        Returns:
            Position creada o None si falla
        """
        try:
            # Calcular tamaño de posición
            sizing = self.risk_manager.calculate_position_size(
                balance=balance,
                leverage=leverage,
                entry_price=setup['entry_price'],
                stop_loss=setup['stop_loss'],
                direction=setup['direction']
            )
            
            if not sizing['valid'] or sizing['size_base'] < 0.0001:
                logger.warning(f"Tamaño de posición inválido: {sizing}")
                return None
            
            # Calcular take profit
            if df_candles is not None:
                # Usar pools de liquidez si tenemos datos de velas
                atr = df_candles['atr'].iloc[-1] if 'atr' in df_candles.columns else None
                take_profit = self.risk_manager.calculate_take_profit_with_pools(
                    entry_price=setup['entry_price'],
                    stop_loss=sizing['stop_loss'],
                    direction=setup['direction'],
                    df_candles=df_candles,
                    atr=atr
                )
            else:
                # Fallback a método tradicional
                take_profit = self.risk_manager.calculate_take_profit(
                    entry_price=setup['entry_price'],
                    stop_loss=sizing['stop_loss'],
                    direction=setup['direction']
                )
            
            # Ejecutar orden de mercado
            side = 'buy' if setup['direction'] == 'LONG' else 'sell'
            
            # Obtener precio actual para simulación
            current_price = setup['entry_price']
            
            # Verificar si hay cliente privado (modo real vs simulación)
            if self.private_client is None:
                logger.warning(" MODO SIMULACIÓN: Sin cliente privado. Creando orden simulada.")
                order = {
                    'symbol': symbol,
                    'amount': sizing['size_base'],
                    'price': current_price,
                    'side': side,
                    'type': 'market',
                    'status': 'closed',
                    'filled': sizing['size_base']
                }
            else:
                order = self.private_client.create_order(
                    symbol=symbol,
                    type='market',
                    side=side,
                    amount=sizing['size_base'],
                    params={'reduceOnly': False}
                )
            
            # Crear posición
            position = Position(
                symbol=symbol,
                direction=setup['direction'],
                size_base=order['amount'],
                size_usd=sizing['size_usd'],
                entry_price=setup['entry_price'],
                entry_time=current_time.isoformat(),
                entry_idx=0,  # Se actualizará si es necesario
                stop_loss=sizing['stop_loss'],
                original_stop_loss=sizing['stop_loss'],
                take_profit=take_profit,
                margin_used=sizing['size_usd'] / leverage,
                setup_type=setup.get('setup_type', 'UNKNOWN')
            )
            
            self.open_positions[symbol] = position
            
            logger.info(
                f"✅ ORDEN EJECUTADA: {setup['direction']} "
                f"{position.size_base:.4f} {symbol} @ ${setup['entry_price']:.4f}"
            )
            
            # Establecer SL y TP en el exchange
            await self._set_sl_tp_orders(position)
            
            return position
            
        except Exception as e:
            logger.error(f"Error ejecutando trade para {symbol}: {e}", exc_info=True)
            return None
    
    async def _set_sl_tp_orders(self, position: Position):
        """Establece órdenes de SL y TP en el exchange"""
        try:
            if self.private_client is None:
                logger.info("🔄 MODO SIMULACIÓN: SL/TP manejados internamente")
                return
                
            side_to_close = 'sell' if position.direction == 'LONG' else 'buy'
            
            # Stop Loss
            self.private_client.create_order(
                symbol=position.symbol,
                type='stop_market',
                side=side_to_close,
                amount=position.size_base,
                price=position.stop_loss,
                params={'close_position': True}
            )
            
            # Take Profit
            self.private_client.create_order(
                symbol=position.symbol,
                type='limit',
                side=side_to_close,
                amount=position.size_base,
                price=position.take_profit,
                params={'close_position': True}
            )
            
            logger.debug(f"SL/TP establecidos para {position.symbol}")
            
        except Exception as e:
            logger.error(f"Error estableciendo SL/TP: {e}")
    
    async def update_stop_loss(self, position: Position, new_sl: float):
        """Actualiza el stop loss de una posición"""
        try:
            if self.private_client is None:
                logger.info("🔄 MODO SIMULACIÓN: SL actualizado internamente")
                position.stop_loss = new_sl
                return
                
            # Cancelar órdenes anteriores
            self.private_client.cancel_all_orders(position.symbol)
            
            # Actualizar SL en la posición
            position.stop_loss = new_sl
            
            # Establecer nuevo SL
            side_to_close = 'sell' if position.direction == 'LONG' else 'buy'
            
            self.private_client.create_order(
                symbol=position.symbol,
                type='stop_market',
                side=side_to_close,
                amount=position.size_base,
                price=new_sl,
                params={'close_position': True}
            )
            
            logger.info(f"SL actualizado para {position.symbol}: ${new_sl:.4f}")
            
        except Exception as e:
            logger.error(f"Error actualizando SL: {e}")
    
    async def close_position(
        self, 
        symbol: str, 
        reason: str,
        current_price: float
    ) -> Optional[Dict]:
        """
        Cierra una posición activa.
        
        Args:
            symbol: Par de trading
            reason: Razón del cierre
            current_price: Precio actual de cierre
            
        Returns:
            Dict con info del trade cerrado o None
        """
        position = self.open_positions.get(symbol)
        
        if not position:
            return None
        
        try:
            if self.private_client is None:
                logger.info("🔄 MODO SIMULACIÓN: Cierre simulado de posición")
            else:
                # Cancelar órdenes pendientes
                self.private_client.cancel_all_orders(symbol)
                
                # Ejecutar orden de cierre
                side_to_close = 'sell' if position.direction == 'LONG' else 'buy'
                
                close_order = self.private_client.create_order(
                    symbol=symbol,
                    type='market',
                    side=side_to_close,
                    amount=position.size_base,
                    params={'reduceOnly': True}
                )
            
            # Calcular PnL
            if position.direction == 'LONG':
                pnl = (current_price - position.entry_price) * position.size_base
            else:
                pnl = (position.entry_price - current_price) * position.size_base
            
            pnl_pct = (pnl / position.margin_used) * 100 if position.margin_used > 0 else 0
            
            # Registro del trade
            trade_log = {
                'symbol': symbol,
                'direction': position.direction,
                'entry_price': position.entry_price,
                'exit_price': current_price,
                'entry_time': position.entry_time,
                'exit_time': datetime.now().isoformat(),
                'size_base': position.size_base,
                'size_usd': position.size_usd,
                'stop_loss': position.stop_loss,
                'take_profit': position.take_profit,
                'pnl': pnl,
                'pnl_pct': pnl_pct,
                'exit_reason': reason,
                'setup_type': position.setup_type
            }
            
            # Eliminar posición
            del self.open_positions[symbol]
            
            logger.info(
                f"🔒 POSICIÓN CERRADA: {symbol} | {reason} | "
                f"PnL: ${pnl:.2f} ({pnl_pct:.2f}%)"
            )
            
            return trade_log
            
        except Exception as e:
            logger.error(f"Error cerrando posición {symbol}: {e}", exc_info=True)
            return None
    
    def check_exit_conditions(
        self,
        symbol: str,
        current_candle: 'pd.Series',
        candles_in_trade: int
    ) -> Optional[tuple[str, float]]:
        """
        Verifica condiciones de salida (SL, TP, Time Limit).
        
        Args:
            symbol: Par de trading
            current_candle: Vela actual
            candles_in_trade: Número de velas desde entrada
            
        Returns:
            (razón, precio) si debe cerrar, None si no
        """
        position = self.open_positions.get(symbol)
        
        if not position:
            return None
        
        # Verificar Stop Loss
        if position.direction == 'LONG':
            if current_candle['low'] <= position.stop_loss:
                return ('Stop Loss', position.stop_loss)
        else:  # SHORT
            if current_candle['high'] >= position.stop_loss:
                return ('Stop Loss', position.stop_loss)
        
        # Verificar Take Profit
        if position.direction == 'LONG':
            if current_candle['high'] >= position.take_profit:
                return ('Take Profit', position.take_profit)
        else:  # SHORT
            if current_candle['low'] <= position.take_profit:
                return ('Take Profit', position.take_profit)
        
        # Verificar Time Limit
        if candles_in_trade >= self.max_candles_in_trade:
            return ('Time Limit', float(current_candle['close']))
        
        return None