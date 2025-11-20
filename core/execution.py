"""
Módulo de Ejecución de Órdenes
Apertura, cierre y gestión de posiciones
"""

import logging
from datetime import datetime
from typing import Dict, Optional
import ccxt
import json
import os
import pandas as pd
from .risk_management import Position, RiskManager

logger = logging.getLogger(__name__)


class ExecutionManager:
    """Gestiona la ejecución de órdenes en el exchange"""
    
    def __init__(
        self, 
        private_client: ccxt.Exchange,
        risk_manager: RiskManager,
        strategy=None,
        max_candles_in_trade: int = 48
    ):
        self.private_client = private_client
        self.risk_manager = risk_manager
        self.strategy = strategy  # Instancia de SMCStrategy para pool detection
        self.max_candles_in_trade = max_candles_in_trade
        self.open_positions: Dict[str, Position] = {}
        # Intentar cargar estado persistido de posiciones (si existe)
        try:
            persisted = self._load_live_positions()
            if persisted:
                for symbol, pdata in persisted.get('smc_positions', {}).items():
                    if pdata:
                        try:
                            pos = Position(
                                symbol=symbol,
                                direction=pdata.get('direction', 'LONG'),
                                size_base=float(pdata.get('size_base', 0.0)),
                                size_usd=float(pdata.get('size_usd', 0.0)),
                                entry_price=float(pdata.get('entry_price', 0.0)),
                                entry_time=pdata.get('entry_time', datetime.now().isoformat()),
                                entry_idx=int(pdata.get('entry_idx', 0)),
                                stop_loss=float(pdata.get('stop_loss', 0.0)),
                                original_stop_loss=float(pdata.get('original_stop_loss', pdata.get('stop_loss', 0.0))),
                                take_profit=float(pdata.get('take_profit', 0.0)),
                                liquidation_price=float(pdata.get('liquidation_price', 0.0)),
                                margin_used=float(pdata.get('margin_used', 0.0)),
                                is_copy=bool(pdata.get('is_copy', False)),
                                setup_type=pdata.get('setup_type', 'UNKNOWN')
                            )
                            self.open_positions[symbol] = pos
                        except Exception:
                            continue
        except Exception:
            pass

    def _state_file_path(self) -> str:
        """Ruta absoluta al archivo de estado live_positions_state.json"""
        # Ubicar la carpeta raíz (dos niveles arriba de core)
        base_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
        return os.path.join(base_dir, 'config', 'live_positions_state.json')

    def _load_live_positions(self) -> Optional[dict]:
        """Carga el JSON de estado de posiciones si existe."""
        try:
            path = self._state_file_path()
            if not os.path.exists(path):
                return None
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return None

    async def _persist_live_positions(self):
        """Persiste el estado actual de open_positions a JSON (escritura atómica)."""
        try:
            path = self._state_file_path()
            data = {
                'smc_positions': {},
                'copy_positions': {}
            }

            for symbol, pos in self.open_positions.items():
                pdata = {
                    'direction': pos.direction,
                    'size_base': pos.size_base,
                    'size_usd': pos.size_usd,
                    'entry_price': pos.entry_price,
                    'entry_time': pos.entry_time,
                    'entry_idx': pos.entry_idx,
                    'stop_loss': pos.stop_loss,
                    'original_stop_loss': pos.original_stop_loss,
                    'take_profit': pos.take_profit,
                    'liquidation_price': getattr(pos, 'liquidation_price', 0.0),
                    'margin_used': getattr(pos, 'margin_used', 0.0),
                    'is_copy': getattr(pos, 'is_copy', False),
                    'setup_type': getattr(pos, 'setup_type', 'UNKNOWN')
                }
                if pdata['is_copy']:
                    data['copy_positions'][symbol] = pdata
                else:
                    data['smc_positions'][symbol] = pdata

            # Asegurar que todos los símbolos existan en el archivo (mantener consistencia)
            # Si el archivo ya existe, leer su estructura para conservar keys vacías
            existing = {}
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        existing = json.load(f) or {}
                except Exception:
                    existing = {}

            # Garantizar claves por defecto
            for k in ('smc_positions', 'copy_positions'):
                if k not in existing:
                    existing[k] = {}

            # Merge: conservar símbolos del archivo original si no están en data
            for k in ('smc_positions', 'copy_positions'):
                for sym, val in existing.get(k, {}).items():
                    if sym not in data[k]:
                        data[k][sym] = val

            # Escritura atómica: escribir a tmp y reemplazar
            tmp_path = path + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            os.replace(tmp_path, path)
        except Exception as e:
            logger.error(f"Error persisting live positions: {e}")

    async def _persist_live_reports(self):
        """Genera/actualiza archivos Excel por símbolo en la carpeta reports/.

        Cada archivo será `reports/live_report_SMC_{SYMBOL}.xlsx` y contendrá una hoja
        con la información de la posición actualmente en `self.open_positions`.
        """
        try:
            base_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
            reports_dir = os.path.join(base_dir, 'reports')
            os.makedirs(reports_dir, exist_ok=True)

            for symbol, pos in self.open_positions.items():
                try:
                    # Construir dataframe de una fila con los campos relevantes
                    row = {
                        'symbol': pos.symbol,
                        'direction': pos.direction,
                        'entry_price': pos.entry_price,
                        'entry_time': pos.entry_time,
                        'size_base': pos.size_base,
                        'size_usd': pos.size_usd,
                        'stop_loss': pos.stop_loss,
                        'original_stop_loss': pos.original_stop_loss,
                        'take_profit': pos.take_profit,
                        'margin_used': getattr(pos, 'margin_used', 0.0),
                        'is_copy': getattr(pos, 'is_copy', False),
                        'setup_type': getattr(pos, 'setup_type', 'UNKNOWN')
                    }

                    df = pd.DataFrame([row])
                    filename = os.path.join(reports_dir, f"live_report_SMC_{symbol.replace('/','')}.xlsx")

                    # Escribir Excel (sobrescribe si existe)
                    with pd.ExcelWriter(filename, engine='xlsxwriter') as writer:
                        df.to_excel(writer, sheet_name='LivePosition', index=False)
                except Exception as e:
                    logger.error(f"Error generando reporte para {symbol}: {e}")

        except Exception as e:
            logger.error(f"Error en persistencia de reportes: {e}")
    
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
            
            # Usar take_profit del setup (ya calculado con mínimo 15%)
            take_profit = setup.get('take_profit')
            
            # Si no hay TP en setup, calcularlo con método dinámico
            if take_profit is None:
                if df_candles is not None:
                    tp_result = self.risk_manager.calculate_dynamic_take_profit(
                        entry_price=setup['entry_price'],
                        stop_loss=sizing['stop_loss'],
                        direction=setup['direction'],
                        df=df_candles
                    )
                else:
                    tp_result = self.risk_manager.calculate_dynamic_take_profit(
                        entry_price=setup['entry_price'],
                        stop_loss=sizing['stop_loss'],
                        direction=setup['direction'],
                        df=None
                    )
                take_profit = tp_result['take_profit']
            
            # NUEVO: Intentar detectar pools de liquidación como TP (si hay > 1% de distancia)
            if self.strategy is not None and df_candles is not None:
                pool_tp = self.strategy.calculate_tp_with_liquidity_pools(
                    entry_price=setup['entry_price'],
                    direction=setup['direction'],
                    df=df_candles,
                    original_tp=take_profit
                )
                if pool_tp is not None:
                    take_profit = pool_tp  # Usar pool como TP si es válido
            
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
            # Persistir estado en archivo
            await self._persist_live_positions()
            # Actualizar reportes Excel en reports/
            await self._persist_live_reports()

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
                # Persistir cambio de SL
                await self._persist_live_positions()
                # Actualizar reportes Excel
                await self._persist_live_reports()
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
            # Persistir cambio de SL
            await self._persist_live_positions()
            
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
            # Persistir estado en archivo
            await self._persist_live_positions()
            # Actualizar reportes Excel
            await self._persist_live_reports()

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