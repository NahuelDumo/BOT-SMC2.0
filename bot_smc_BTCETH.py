import asyncio
import json
import logging
import os
from dataclasses import dataclass, asdict  # <-- AÑADIDO
from datetime import datetime
from typing import Dict, Optional, List
import time 

import ccxt
from bitunix import BitunixClient
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from wallet_tracker import WalletTracker

import numpy as np
import pandas as pd
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment
from scipy.signal import argrelextrema
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError, NetworkError

# --- Configuración del Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('smc_trading_bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


@dataclass
class Position:
    """Estructura para almacenar el estado de una posición."""
    symbol: str
    direction: str
    size_base: float      # Tamaño en el activo base (ej: 1.39 SOL)
    size_usd: float       # Tamaño nocional en USD (ej: 223.67 USDT)
    entry_price: float
    entry_time: datetime
    entry_idx: int          
    stop_loss: float        
    original_stop_loss: float 
    take_profit: float      
    liquidation_price: float
    margin_used: float
    is_copy: bool = False   


class SmartMoneyLiveBot:
    """
    Bot de trading SMC multi-símbolo con persistencia de balance.
    LÓGICA IDÉNTICA AL BACKTEST (FVG, MTF, SL Estructural, Validación N2 y N3)
    """

    def __init__(
        self,
        wallets: List[Dict[str, str]],  
        telegram_token: str,
        telegram_chat_id: str,
        symbols: List[str] = ['BTCUSDT'],
        initial_balance: float = 50.0,
    ) -> None:
        self.symbols = symbols
        self.timeframe = '15m' # Timeframe de ejecución
        self.candle_limit = 300 # Límite para 15m
        self.candle_limit_1h = 100 # Límite para 1H
        self.refresh_seconds = 15

        # --- Parámetros de Estrategia (Idénticos al Backtest) ---
        self.structure_lookback = 20
        self.risk_reward_ratio = 2.0     
        self.leverage = 20 # Valor por defecto, se sobrescribe por símbolo
        self.risk_per_trade_pct = 0.05   
        self.max_candles_in_trade = 48 
        self.pool_lookback_bars = 192    
        
        # --- ¡NUEVO! FILTRO DE ANTIGÜEDAD PARA TRADING ---
        # El bot SOLO operará FVGs con memoria de las últimas 96 velas (24h)
        self.fvg_memory_max_age_bars = 96 
        
        self.equal_tol = 0.0003        
        self.min_rr = 1.5              
        
        self.enable_macd_filter = False # Desactivado (usamos MTF)
        self.macd_fast = 12
        self.macd_slow = 26
        self.macd_signal = 9

        # Telegram
        self.telegram_bot = Bot(token=telegram_token)
        self.telegram_chat_id = telegram_chat_id
        
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.trades: list[Dict] = []
        
        self.excel_filenames: Dict[str, str] = {}
        
        # --- DataFrames MTF (15m, 1H, 4H) ---
        self.dfs: Dict[str, Optional[pd.DataFrame]] = {s: None for s in self.symbols} # 15m
        self.dfs_1h: Dict[str, Optional[pd.DataFrame]] = {s: None for s in self.symbols} # 1H
        self.dfs_4h: Dict[str, Optional[pd.DataFrame]] = {s: None for s in self.symbols} # 4H (AÑADIDO)
        # --- Fin DataFrames ---

        self.positions: Dict[str, Optional[Position]] = {s: None for s in self.symbols}
        self.copy_positions: Dict[str, Optional[Position]] = {s: None for s in self.symbols}
        
        self.positions_persistence_file = 'live_positions_state.json'
        self._load_persistent_positions()
        
        self.last_signal_times: Dict[str, Optional[pd.Timestamp]] = {s: None for s in self.symbols}
        self.ccxt_symbols: Dict[str, str] = {s: s.replace('USDT', '/USDT') for s in self.symbols}
        
        self.position_close_times: Dict[str, Optional[datetime]] = {s: None for s in self.symbols}
        self.position_cooldown_seconds = 60  
        
        self.failed_entry_prices: Dict[str, list] = {s: [] for s in self.symbols}  
        self.failed_entry_tolerance = 0.001  
        
        # Sistema de cooldown para alertas de error
        self.last_error_alert_time: Dict[str, Optional[datetime]] = {}
        self.error_alert_cooldown_seconds = 300  # 5 minutos entre alertas del mismo error
        
        self.wallet_tracker: Optional[WalletTracker] = None
        self.copy_trading_enabled: bool = True  
        
        self.last_update_id = 0
        self.command_handlers = {
            '/wallet': self.handle_wallet_command,
            '/copy_on': self.handle_copy_on_command,
            '/copy_off': self.handle_copy_off_command,
            '/copy_status': self.handle_copy_status_command,
            '/fvg': self.handle_fvg_command,
            '/add_symbol': self.handle_add_symbol_command,
        }
        
        # Estado para el flujo conversacional de /add_symbol
        self.pending_symbol_add: Dict[str, any] = {}
        
        self.max_concurrent_open = 4
        self.leverage_per_symbol: Dict[str, int] = {
            'BTCUSDT': 30, 'ETHUSDT': 15, 'HYPEUSDT': 15, 'SOLUSDT': 15,
        }
        
        self.symbol_configs: Dict[str, Dict] = {}
        
        last_known_balance = None
        last_trade_time = pd.Timestamp(0, tz='UTC') 

        for symbol in self.symbols:
            filename = f"live_report_SMC_{symbol.replace('/', '_')}.xlsx"
            self.excel_filenames[symbol] = filename
            balance_from_file, time_from_file = self._initialize_excel(filename) 
            if balance_from_file is not None and time_from_file > last_trade_time:
                last_known_balance = balance_from_file
                last_trade_time = time_from_file
                
        if last_known_balance is not None:
            self.balance = last_known_balance
            logger.info(f"✅ Balance restaurado desde el último trade en Excel: ${self.balance:,.2f}")
        else:
            logger.info(f"ℹ️ Iniciando con balance de configuración (no se encontraron trades): ${self.balance:,.2f}")

        logger.info("Conectando a Bitunix para operaciones...")
        self.wallets = wallets
        self.clients: Dict[str, BitunixClient] = {}
        
        for wallet in wallets:
            if wallet.get('enabled', True):
                wallet_name = wallet.get('name', 'Unnamed')
                try:
                    client = BitunixClient(
                        api_key=wallet['api_key'],
                        api_secret=wallet['api_secret']
                    )
                    self.clients[wallet_name] = client
                    logger.info(f"✅ Wallet '{wallet_name}' conectada exitosamente")
                except Exception as e:
                    logger.error(f"❌ Error conectando wallet '{wallet_name}': {e}")
        
        if not self.clients:
            raise ValueError("No se pudo conectar ninguna wallet. Verifica las credenciales.")
        
        self.client = list(self.clients.values())[0]

        logger.info("Configurando ccxt (Binance Futures) para obtener datos históricos...")
        self.data_exchange = ccxt.binanceusdm({
            'enableRateLimit': True,
            'options': {'defaultType': 'future'},
            'timeout': 30000,  # 30 segundos de timeout
            'rateLimit': 1200  # Esperar más entre requests
        })
        
        self.is_running: bool = False

    # --- Métodos de Reporte en Excel ---
    
    def _initialize_excel(self, filename: str) -> (Optional[float], pd.Timestamp):
        min_timestamp = pd.Timestamp(0, tz='UTC')
        if not os.path.exists(filename):
            wb = Workbook()
            ws_trades = wb.active
            ws_trades.title = "Trades"
            headers = [
                'Fecha Entrada', 'Hora Entrada', 'Fecha Salida', 'Hora Salida', 'Dirección', # 1-5
                'Precio Entrada', 'Precio Salida', 'Stop Loss', 'Take Profit', 'Liquidación', # 6-10
                'Tamaño (Activo)', 'Tamaño (USD)', 'Margen Usado', 'P/L USD', 'Razón Salida', 'Balance' # 11-16 (ACTUALIZADO)
            ]
            for col, header in enumerate(headers, 1):
                cell = ws_trades.cell(row=1, column=col, value=header)
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
                cell.alignment = Alignment(horizontal="center")
            wb.save(filename)
            logger.info(f"📊 Nuevo archivo Excel creado: {filename}")
            return None, min_timestamp
        else:
            logger.info(f"📊 Usando archivo Excel existente: {filename}")
            try:
                wb = load_workbook(filename)
                ws = wb["Trades"]
                if ws.max_row <= 1:
                    logger.info("    ↳ Archivo existente pero sin trades. Usando balance inicial.")
                    return None, min_timestamp
                
                # Leer la última fila válida
                last_balance_cell_col = 16 # Columna P
                last_balance = ws.cell(row=ws.max_row, column=last_balance_cell_col).value
                last_exit_date_str = ws.cell(row=ws.max_row, column=3).value
                last_exit_time_str = ws.cell(row=ws.max_row, column=4).value
                
                if not last_exit_date_str or not last_exit_time_str:
                    logger.info("    ↳ El último trade en el archivo aún está abierto. Buscando el anterior...")
                    # Buscar la última fila *cerrada*
                    for row in range(ws.max_row - 1, 1, -1):
                        last_balance = ws.cell(row=row, column=last_balance_cell_col).value
                        last_exit_date_str = ws.cell(row=row, column=3).value
                        last_exit_time_str = ws.cell(row=row, column=4).value
                        if last_exit_date_str and last_exit_time_str:
                            break # Encontramos la última cerrada
                    if not last_exit_date_str or not last_exit_time_str:
                         logger.info("    ↳ No se encontraron trades cerrados. Usando balance inicial.")
                         return None, min_timestamp

                last_timestamp = pd.Timestamp(f"{last_exit_date_str} {last_exit_time_str}", tz='UTC')
                if isinstance(last_balance, (int, float)):
                    logger.info(f"    ↳ Último balance leído de Excel: ${last_balance:,.2f} (del {last_timestamp})")
                    return float(last_balance), last_timestamp
                else:
                    logger.warning(f"    ↳ No se pudo leer el último balance de la fila {ws.max_row}. Valor: {last_balance}")
                    return None, min_timestamp
            except Exception as e:
                logger.error(f"Error al leer el archivo Excel existente {filename}: {e}", exc_info=True)
                return None, min_timestamp

    def _save_trade_to_excel(self, trade: Dict) -> None:
        try:
            symbol = trade['symbol']
            filename = self.excel_filenames[symbol]
            wb = load_workbook(filename)
            ws = wb["Trades"]
            entry_time = trade['entry_time']
            exit_time = trade['exit_time']
            row_data = [
                entry_time.strftime('%Y-%m-%d'), entry_time.strftime('%H:%M:%S'),
                exit_time.strftime('%Y-%m-%d'), exit_time.strftime('%H:%M:%S'),
                trade['direction'], trade['entry_price'], trade['exit_price'],
                trade['stop_loss'], trade['take_profit'], trade['liquidation_price'],
                trade['position_size_base'], # Columna 11 (Activo)
                trade['position_size_usd'],  # Columna 12 (USD)
                trade['margin_used'],      # Columna 13
                trade['pnl'],              # Columna 14
                trade['exit_reason'],      # Columna 15
                self.balance               # Columna 16
            ]
            ws.append(row_data)
            
            pnl_cell = ws.cell(row=ws.max_row, column=14) # Columna N
            if trade['pnl'] > 0:
                pnl_cell.font = Font(color="00B050", bold=True)
            elif trade['pnl'] < 0:
                pnl_cell.font = Font(color="FF0000", bold=True)
            
            wb.save(filename)
            logger.info(f"📊 Trade de {symbol} guardado en {filename}.")
        except Exception as e:
            logger.error(f"Error al guardar en Excel: {e}", exc_info=True)

    # --- Métodos de Notificación por Telegram ---
    async def send_telegram_message(self, message: str) -> None:
        try:
            await self.telegram_bot.send_message(chat_id=self.telegram_chat_id, text=message, parse_mode='HTML')
        except TelegramError as e:
            logger.error(f"Error al enviar mensaje de Telegram: {e}")
    
    async def listen_telegram_commands(self):
        """Escucha comandos de Telegram y callbacks de botones en background."""
        logger.info("🎧 Iniciando escucha de comandos de Telegram...")
        
        retry_delay = 2  # Delay inicial en segundos
        max_retry_delay = 60  # Delay máximo en segundos
        consecutive_errors = 0
        
        while self.is_running:
            try:
                updates = await self.telegram_bot.get_updates(
                    offset=self.last_update_id + 1,
                    timeout=30  # Aumentado de 10 a 30 segundos
                )
                
                # Reset error tracking on successful request
                consecutive_errors = 0
                retry_delay = 2
                
                for update in updates:
                    self.last_update_id = update.update_id
                    
                    if update.callback_query:
                        callback = update.callback_query
                        chat_id = str(callback.message.chat_id)
                        
                        if chat_id != self.telegram_chat_id:
                            logger.warning(f"Callback recibido de chat no autorizado: {chat_id}")
                            continue
                        
                        callback_data = callback.data
                        message_id = callback.message.message_id
                        
                        logger.info(f"🔘 Botón presionado: {callback_data}")
                        
                        try:
                            await self.telegram_bot.answer_callback_query(callback.id)
                        except:
                            pass
                        
                        if callback_data == "refresh_wallet":
                            await self.handle_wallet_command(message_id=message_id)
                    
                    elif update.message and update.message.text:
                        text = update.message.text.strip()
                        chat_id = str(update.message.chat_id)
                        
                        if chat_id != self.telegram_chat_id:
                            logger.warning(f"Comando recibido de chat no autorizado: {chat_id}")
                            continue
                        
                        logger.info(f"📨 Comando recibido: {text}")
                        
                        # Verificar si hay un flujo conversacional activo
                        if self.pending_symbol_add.get('active'):
                            await self.process_symbol_add_response(text)
                            continue
                        
                        command_parts = text.split()
                        command = command_parts[0]
                        args = command_parts[1:]
                        
                        handler = self.command_handlers.get(command)
                        if handler:
                            try:
                                if command == '/fvg':
                                    await handler(args) # Pasar args
                                elif command == '/add_symbol':
                                    await handler(args)
                                else:
                                    await handler()
                            except Exception as e:
                                logger.error(f"Error ejecutando comando {command}: {e}", exc_info=True)
                                await self.send_telegram_message(
                                    f"🚨 Error ejecutando comando {command}: {str(e)}"
                                )
                
                await asyncio.sleep(2)  
                
            except NetworkError as e:
                # Network errors are transient - use exponential backoff
                consecutive_errors += 1
                logger.warning(
                    f"⚠️ Error de red en Telegram (intento {consecutive_errors}): {e}. "
                    f"Reintentando en {retry_delay}s..."
                )
                await asyncio.sleep(retry_delay)
                # Exponential backoff with cap
                retry_delay = min(retry_delay * 2, max_retry_delay)
                
            except TelegramError as e:
                # Other Telegram errors - log and continue with moderate delay
                consecutive_errors += 1
                logger.error(f"Error de Telegram: {e}", exc_info=True)
                await asyncio.sleep(10)
                
            except Exception as e:
                # Unexpected errors - log with full traceback
                consecutive_errors += 1
                logger.error(f"Error inesperado en listener de comandos de Telegram: {e}", exc_info=True)
                await asyncio.sleep(5)

    async def notify_entry(self, pos: Position, successful_wallets: List[str] = None, failed_wallets: List[str] = None) -> None:
        pnl_target = 0.0
        if pos.take_profit > 0 and pos.size_base > 0:
             pnl_target = (pos.take_profit - pos.entry_price) * pos.size_base if pos.direction == 'LONG' else (pos.entry_price - pos.take_profit) * pos.size_base
        
        trade_type = "COPY" if pos.is_copy else "SMC"
        
        msg = (
            f"✅ <b>POSICIÓN ABIERTA ({trade_type})</b>\n\n"
            f"📊 Par: {pos.symbol}\n"
            f"📈 Dirección: <b>{pos.direction}</b>\n"
            f"💰 Precio entrada: ${pos.entry_price:,.4f}\n"
            f"📏 Tamaño: {pos.size_base:.4f} ({pos.symbol.replace('USDT', '')})\n"
            f"💲 Valor Nocional: ${pos.size_usd:,.2f} (USDT)\n"
            f"💵 Margen: ${pos.margin_used:,.2f}\n"
        )
        
        if not pos.is_copy:
            msg += (
                f"🎯 Take Profit: ${pos.take_profit:,.4f} (+${pnl_target:,.2f})\n"
                f"🛑 Stop Loss: ${pos.stop_loss:,.4f}\n"
            )
        
        msg += f"⚠️ Liquidación: ${pos.liquidation_price:,.4f}\n"
        
        await self.send_telegram_message(msg)
    
    async def notify_entry_per_wallet(self, pos: Position, wallet_name: str) -> None:
        """Notifica la apertura de una posición en una wallet específica."""
        pnl_target = 0
        if not pos.is_copy and pos.take_profit > 0 and pos.size_base > 0:
            pnl_target = (pos.take_profit - pos.entry_price) * pos.size_base if pos.direction == 'LONG' else (pos.entry_price - pos.take_profit) * pos.size_base
        
        trade_type = "COPY TRADING" if pos.is_copy else "SMC"
        
        msg = (
            f"✅ <b>POSICIÓN ABIERTA EN WALLET: {wallet_name}</b>\n\n"
            f"🏦 Wallet: <b>{wallet_name}</b>\n"
            f"📊 Par: {pos.symbol}\n"
            f"📈 Dirección: <b>{pos.direction}</b>\n"
            f"🎯 Tipo: {trade_type}\n"
            f"💰 Precio entrada: ${pos.entry_price:,.4f}\n"
            f"📏 Tamaño: {pos.size_base:.4f} ({pos.symbol.replace('USDT', '')})\n"
            f"💲 Valor Nocional: ${pos.size_usd:,.2f} (USDT)\n"
            f"💵 Margen: ${pos.margin_used:,.2f}\n"
        )
        
        if not pos.is_copy:
            msg += (
                f"🎯 Take Profit: ${pos.take_profit:,.4f} (+${pnl_target:,.2f})\n"
                f"🛑 Stop Loss: ${pos.stop_loss:,.4f}\n"
            )
        
        msg += f"⚠️ Liquidación: ${pos.liquidation_price:,.4f}"
        
        await self.send_telegram_message(msg)

    async def notify_exit(self, trade: Dict) -> None:
        emoji = "🟢" if trade['pnl'] > 0 else "🔴"
        msg = (
            f"{emoji} <b>POSICIÓN CERRADA (SMC)</b>\n\n"
            f"📊 Par: {trade['symbol']}\n"
            f"📈 Dirección: {trade['direction']}\n"
            f"💰 Entrada: ${trade['entry_price']:,.4f} | Salida: ${trade['exit_price']:,.4f}\n"
            f"💵 P/L: <b>${trade['pnl']:,.2f}</b>\n"
            f"📝 Razón: {trade['exit_reason']}\n"
            f"💼 Balance: ${self.balance:,.2f}"
        )
        await self.send_telegram_message(msg)

    # --- Métodos de Persistencia ---
    
    def _load_persistent_positions(self) -> None:
        if not os.path.exists(self.positions_persistence_file):
            logger.info("No se encontró archivo de estado de posiciones. Empezando de cero.")
            return
        try:
            with open(self.positions_persistence_file, 'r') as f:
                persistent_data = json.load(f)
            smc_reloaded = 0
            copy_reloaded = 0
            smc_positions = persistent_data.get('smc_positions', persistent_data)
            for symbol, pos_data in list(smc_positions.items()):
                if symbol in self.symbols and pos_data is not None:
                    # --- COMPATIBILIDAD CON DATACLASS ANTIGUO ---
                    if 'size' in pos_data:
                        pos_data['size_base'] = pos_data.pop('size')
                        if pos_data['entry_price'] > 0:
                            pos_data['size_usd'] = pos_data['size_base'] * pos_data['entry_price']
                        else:
                            pos_data['size_usd'] = 0
                    # --- FIN COMPATIBILIDAD ---
                    
                    pos_data.setdefault('is_copy', False)
                    pos_data['entry_time'] = datetime.fromisoformat(pos_data['entry_time'])
                    try:
                        fields_to_float = ['size_base', 'size_usd', 'entry_price', 'stop_loss', 
                                           'original_stop_loss', 'take_profit', 'liquidation_price', 'margin_used']
                        for field in fields_to_float:
                             pos_data[field] = float(pos_data.get(field, 0.0)) # Usar .get con default
                            
                        self.positions[symbol] = Position(**pos_data)
                        smc_reloaded += 1
                        logger.info(f"🔄 Posición SMC para {symbol} ({pos_data['direction']}) recargada.")
                    except (TypeError, KeyError) as e:
                        logger.error(f"Error al recrear Position SMC para {symbol}: {e}")
                        self.positions[symbol] = None
                        
            copy_positions = persistent_data.get('copy_positions', {})
            for symbol, pos_data in list(copy_positions.items()):
                if symbol in self.symbols and pos_data is not None:
                    # --- COMPATIBILIDAD CON DATACLASS ANTIGUO ---
                    if 'size' in pos_data:
                        pos_data['size_base'] = pos_data.pop('size')
                        if pos_data['entry_price'] > 0:
                            pos_data['size_usd'] = pos_data['size_base'] * pos_data['entry_price']
                        else:
                            pos_data['size_usd'] = 0
                    # --- FIN COMPATIBILIDAD ---
                    
                    pos_data.setdefault('is_copy', True)
                    pos_data['entry_time'] = datetime.fromisoformat(pos_data['entry_time'])
                    try:
                        fields_to_float = ['size_base', 'size_usd', 'entry_price', 'stop_loss', 
                                           'original_stop_loss', 'take_profit', 'liquidation_price', 'margin_used']
                        for field in fields_to_float:
                            pos_data[field] = float(pos_data.get(field, 0.0))
                            
                        self.copy_positions[symbol] = Position(**pos_data)
                        copy_reloaded += 1
                        logger.info(f"🔄 Posición COPY para {symbol} ({pos_data['direction']}) recargada.")
                    except (TypeError, KeyError) as e:
                        logger.error(f"Error al recrear Position COPY para {symbol}: {e}")
                        self.copy_positions[symbol] = None
            if smc_reloaded > 0 or copy_reloaded > 0:
                logger.info(f"✅ Se recargaron {smc_reloaded} posiciones SMC y {copy_reloaded} posiciones COPY.")
        except json.JSONDecodeError:
            logger.error(f"Error al decodificar JSON desde {self.positions_persistence_file}.")
            if os.path.exists(self.positions_persistence_file):
                try: os.remove(self.positions_persistence_file)
                except OSError as e: logger.error(f"No se pudo eliminar el archivo JSON corrupto: {e}")
        except Exception as e:
            logger.error(f"Error inesperado al cargar estado de posiciones: {e}", exc_info=True)
            if os.path.exists(self.positions_persistence_file):
                try: os.remove(self.positions_persistence_file)
                except OSError as e: logger.error(f"No se pudo eliminar el archivo JSON tras error de carga: {e}")

    async def _save_persistent_positions(self) -> None:
        logger.debug("Guardando estado de posiciones persistentes (SMC + COPY)...")
        temp_file = self.positions_persistence_file + ".tmp"
        try:
            persistent_data = {'smc_positions': {}, 'copy_positions': {}}
            for symbol, pos in self.positions.items():
                if pos is not None:
                    pos_dict = asdict(pos)
                    if isinstance(pos_dict['entry_time'], datetime):
                        pos_dict['entry_time'] = pos_dict['entry_time'].isoformat()
                    else:
                        logger.error(f"Tipo inesperado para entry_time en posición SMC {symbol}: {type(pos_dict['entry_time'])}.")
                        continue
                    persistent_data['smc_positions'][symbol] = pos_dict
                else:
                    persistent_data['smc_positions'][symbol] = None
            for symbol, pos in self.copy_positions.items():
                if pos is not None:
                    pos_dict = asdict(pos)
                    if isinstance(pos_dict['entry_time'], datetime):
                        pos_dict['entry_time'] = pos_dict['entry_time'].isoformat()
                    else:
                        logger.error(f"Tipo inesperado para entry_time en posición COPY {symbol}: {type(pos_dict['entry_time'])}.")
                        continue
                    persistent_data['copy_positions'][symbol] = pos_dict
                else:
                    persistent_data['copy_positions'][symbol] = None
            with open(temp_file, 'w') as f:
                json.dump(persistent_data, f, indent=4)
            os.replace(temp_file, self.positions_persistence_file) 
            logger.debug(f"✅ Estado de posiciones guardado (SMC + COPY) en {self.positions_persistence_file}.")
        except Exception as e:
            logger.error(f"Error crítico al guardar estado de posiciones en {self.positions_persistence_file}: {e}", exc_info=True)
            if os.path.exists(temp_file):
                try: os.remove(temp_file)
                except OSError as ose: logger.error(f"No se pudo eliminar archivo temporal {temp_file} tras error: {ose}")
    

    # --- Métodos de Obtención de Datos (MTF) ---
    
    async def _fetch_market_data(self, symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
        """Función base para descargar datos de mercado HASTA LA FECHA ACTUAL."""
        ccxt_symbol = self.ccxt_symbols[symbol]
        logger.info(f"🔄 [{symbol}] Actualizando datos de mercado ({timeframe}, {limit} velas)...")
        try:
            # Descargar las últimas N velas SIN especificar 'since'
            # Esto garantiza que siempre obtenemos las velas más recientes hasta el momento actual
            ohlcv = await asyncio.to_thread(
                self.data_exchange.fetch_ohlcv,
                ccxt_symbol, timeframe, limit=limit
            )
            if not ohlcv:
                logger.error(f"[{symbol}] No se pudieron descargar datos ({timeframe}).")
                return None

            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df = df.drop_duplicates(subset=['timestamp'], keep='last')
            
            # --- CORRECCIÓN DE ZONA HORARIA ---
            # Convertir a UTC y luego a UTC-3 (igual que el backtest)
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert('Etc/GMT+3')
            # --- FIN CORRECCIÓN ---
            
            df.set_index('timestamp', inplace=True)
            df = df.sort_index()
            df = df.astype(float)
            
            if len(df) < 50: # Necesitamos suficientes datos para MACD
                logger.warning(f"[{symbol}] No se recibieron suficientes datos ({timeframe}, {len(df)} velas).")
                return None
                
            return df
        except Exception as e:
            logger.error(f"[{symbol}] Error inesperado actualizando datos ({timeframe}): {e}", exc_info=True)
            return None

    async def update_market_data_1h_4h(self, symbol: str) -> bool:
        """Actualiza los datos de 1H y 4H y calcula solo el MACD."""
        df_1h = await self._fetch_market_data(symbol, '1h', self.candle_limit_1h)
        if df_1h is None: return False
        
        self.dfs_1h[symbol] = self.compute_macd(df_1h)
        
        # Para 4H, podemos usar el mismo límite, ccxt manejará la descarga
        df_4h = await self._fetch_market_data(symbol, '4h', self.candle_limit_1h) 
        if df_4h is None: return False
        
        self.dfs_4h[symbol] = self.compute_macd(df_4h) # <-- CORREGIDO
        
        logger.info(f"✅ [{symbol}] Datos MTF (1H, 4H) y MACD actualizados.")
        return True

    async def update_market_data(self, symbol: str) -> bool:
        """
        Actualiza los datos de 15M y calcula todos los patrones SMC.
        LÓGICA ORIGINAL: Separa velas cerradas de vela en vivo para evitar falsos positivos.
        """
        df = await self._fetch_market_data(symbol, self.timeframe, self.candle_limit)
        if df is None: return False

        # --- SEPARAR VELAS CERRADAS DE VELA EN VIVO ---
        df_closed = df.iloc[:-1].copy()  # Todas las velas cerradas
        df_current = df.iloc[[-1]].copy()  # Solo la vela en vivo

        # 1. Calcular Estructura (min/max) en velas CERRADAS
        n = self.structure_lookback
        if len(df_closed) > n:
            min_indices = argrelextrema(df_closed.low.values, np.less_equal, order=n)[0]
            max_indices = argrelextrema(df_closed.high.values, np.greater_equal, order=n)[0]
            df_closed['min'] = np.nan
            df_closed['max'] = np.nan
            
            valid_min_indices_loc = df_closed.index[min_indices[min_indices < len(df_closed)]]
            valid_max_indices_loc = df_closed.index[max_indices[max_indices < len(df_closed)]]
            if not valid_min_indices_loc.empty:
                df_closed.loc[valid_min_indices_loc, 'min'] = df_closed.loc[valid_min_indices_loc, 'low']
            if not valid_max_indices_loc.empty:
                df_closed.loc[valid_max_indices_loc, 'max'] = df_closed.loc[valid_max_indices_loc, 'high']
        else:
            df_closed['min'] = np.nan
            df_closed['max'] = np.nan

        # 2. Calcular FVGs SÓLO en velas cerradas
        df_closed['is_fvg_bullish'] = False
        df_closed['is_fvg_bearish'] = False
        df_closed['fvg_bull_high'], df_closed['fvg_bull_low'] = np.nan, np.nan
        df_closed['fvg_bear_high'], df_closed['fvg_bear_low'] = np.nan, np.nan
        df_closed['fvg_bull_mid'] = np.nan
        df_closed['fvg_bear_mid'] = np.nan
        
        # Patrón de 3 velas: [i-2], [i-1], [i]
        for i in range(2, len(df_closed)):
            # FVG Alcista: low[i] > high[i-2]
            if df_closed['low'].iloc[i] > df_closed['high'].iloc[i-2]:
                df_closed.loc[df_closed.index[i-1], 'is_fvg_bullish'] = True
                low_edge = df_closed['high'].iloc[i-2]
                high_edge = df_closed['low'].iloc[i]
                df_closed.loc[df_closed.index[i-1], 'fvg_bull_low'] = low_edge
                df_closed.loc[df_closed.index[i-1], 'fvg_bull_high'] = high_edge
                df_closed.loc[df_closed.index[i-1], 'fvg_bull_mid'] = low_edge + (high_edge - low_edge) * 0.5

            # FVG Bajista: high[i] < low[i-2]
            if df_closed['high'].iloc[i] < df_closed['low'].iloc[i-2]:
                df_closed.loc[df_closed.index[i-1], 'is_fvg_bearish'] = True
                low_edge = df_closed['high'].iloc[i]
                high_edge = df_closed['low'].iloc[i-2]
                df_closed.loc[df_closed.index[i-1], 'fvg_bear_low'] = low_edge
                df_closed.loc[df_closed.index[i-1], 'fvg_bear_high'] = high_edge
                df_closed.loc[df_closed.index[i-1], 'fvg_bear_mid'] = low_edge + (high_edge - low_edge) * 0.5
        
        # 3. Calcular Mitigación (50% FVG) SOLO en velas cerradas
        df_closed['is_mitigated'] = False
        bull_fvg_indices = df_closed[df_closed['is_fvg_bullish']].index
        bear_fvg_indices = df_closed[df_closed['is_fvg_bearish']].index

        for fvg_idx_time in bull_fvg_indices:
            try:
                fvg_row = df_closed.loc[fvg_idx_time]
                fvg_mid_price = fvg_row['fvg_bull_mid']
                fvg_iloc = df_closed.index.get_loc(fvg_idx_time)
                
                if fvg_iloc + 1 < len(df_closed):
                    future_candles = df_closed.iloc[fvg_iloc + 1:]
                    if (future_candles['low'] <= fvg_mid_price).any():
                        df_closed.loc[fvg_idx_time, 'is_mitigated'] = True
            except Exception as e:
                logger.warning(f"[{symbol}] Error calculando mitigación bull FVG en {fvg_idx_time}: {e}")

        for fvg_idx_time in bear_fvg_indices:
            try:
                fvg_row = df_closed.loc[fvg_idx_time]
                fvg_mid_price = fvg_row['fvg_bear_mid']
                fvg_iloc = df_closed.index.get_loc(fvg_idx_time)
                
                if fvg_iloc + 1 < len(df_closed):
                    future_candles = df_closed.iloc[fvg_iloc + 1:]
                    if (future_candles['high'] >= fvg_mid_price).any():
                        df_closed.loc[fvg_idx_time, 'is_mitigated'] = True
            except Exception as e:
                logger.warning(f"[{symbol}] Error calculando mitigación bear FVG en {fvg_idx_time}: {e}")

        # 4. Añadir columnas vacías a la vela actual
        df_current['min'], df_current['max'] = np.nan, np.nan
        df_current['is_fvg_bullish'] = False
        df_current['is_fvg_bearish'] = False
        df_current['fvg_bull_high'], df_current['fvg_bull_low'] = np.nan, np.nan
        df_current['fvg_bear_high'], df_current['fvg_bear_low'] = np.nan, np.nan
        df_current['is_mitigated'] = False
        df_current['fvg_bull_mid'], df_current['fvg_bear_mid'] = np.nan, np.nan
        
        # 5. Unir los DataFrames
        df_final = pd.concat([df_closed, df_current])
        
        # 6. Calcular ATR y MACD en el DF final
        df_final = self.compute_atr(df_final)
        df_final = self.compute_macd(df_final)
        
        # 7. Fusionar datos de 1H
        df_1h = self.dfs_1h.get(symbol)
        if df_1h is None or df_1h.empty:
            logger.warning(f"[{symbol}] Faltan datos de 1H para la fusión. El filtro MTF fallará.")
            df_final['macd_1h'] = np.nan
        else:
            df_1h_macd = df_1h[['macd']].rename(columns={'macd': 'macd_1h'})
            df_final = pd.merge_asof(
                df_final.sort_index(), 
                df_1h_macd.sort_index(), 
                left_index=True, 
                right_index=True, 
                direction='backward'
            )

        self.dfs[symbol] = df_final
        logger.info(f"✅ [{symbol}] Datos ({self.timeframe}) y patrones SMC actualizados ({len(df_closed)} cerradas + 1 en vivo).")
        return True

    # ... (Funciones de Indicadores: compute_atr, compute_macd) ...
    def compute_atr(self, df: pd.DataFrame, window: int = 14) -> pd.DataFrame:
        if df.empty: return df
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

    def compute_macd(self, df: pd.DataFrame, fast: int = None, slow: int = None, signal: int = None):
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

    # ... (Funciones de Liquidez: _binsize, build_liquidity_pools, select_target_pool) ...
    def _binsize(self, ref_price: float, tol: float) -> float:
        return max(1e-8, ref_price * tol)

    def build_liquidity_pools(self, df: pd.DataFrame, i: int, lookback: int = None, tol: float = None):
        if lookback is None: lookback = self.pool_lookback_bars
        if tol is None: tol = self.equal_tol
        start = max(0, i - lookback)
        window = df.iloc[start:i] 
        if window.empty: return []

        mid_price = float(window['close'].iloc[-1])
        binsize = self._binsize(mid_price, tol)
        step = 5.0 if mid_price < 5000 else 10.0 
        pools: Dict[float, float] = {}

        def add(price: float, score: float):
            if price is None or np.isnan(price): return
            bucket = round(price / binsize)
            level = bucket * binsize
            pools[level] = pools.get(level, 0.0) + score

        highs = window['high'].values; lows = window['low'].values
        for arr, base in ((highs, 3.0), (lows, 3.0)):
            counts: Dict[int, int] = {}
            for p in arr:
                b = round(p / binsize)
                counts[b] = counts.get(b, 0) + 1
            for b, cnt in counts.items():
                if cnt >= 2: add(b * binsize, base * cnt)

        if 'max' in window.columns:
            for p in window['max'].dropna().values: add(float(p), 4.0)
        if 'min' in window.columns:
            for p in window['min'].dropna().values: add(float(p), 4.0)
        
        if 'fvg_bull_high' in window.columns:
            for p in window['fvg_bull_high'].dropna().values: add(float(p), 2.5)
        if 'fvg_bear_low' in window.columns:
            for p in window['fvg_bear_low'].dropna().values: add(float(p), 2.5)

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

    def select_target_pool(self, df: pd.DataFrame, i: int, direction: str, entry_price: float, pools: List[Dict]):
        if not pools: return None
        atr_val = df['atr'].iloc[i-1] if 'atr' in df.columns and i-1 >= 0 and not pd.isna(df['atr'].iloc[i-1]) else np.nan
        max_dist = 1.5 * atr_val if not np.isnan(atr_val) else None

        if direction == 'LONG':
            candidates = [p for p in pools if p['price'] > entry_price]
            candidates.sort(key=lambda p: (-p['score'], abs(p['price'] - entry_price)))
            if max_dist is not None:
                within = [p for p in candidates if (p['price'] - entry_price) <= max_dist]
                if within: return within[0]['price']
            return candidates[0]['price'] if candidates else None
        else: # SHORT
            candidates = [p for p in pools if p['price'] < entry_price]
            candidates.sort(key=lambda p: (-p['score'], abs(p['price'] - entry_price)))
            if max_dist is not None:
                within = [p for p in candidates if (entry_price - p['price']) <= max_dist]
                if within: return within[0]['price']
            return candidates[0]['price'] if candidates else None

    # --- Lógica de Setups (AHORA CON FILTRO MTF 1H) ---
    
    def _check_mtf_filter(self, symbol: str, i: int, direction: str) -> bool:
        """Función helper para chequear el filtro MTF."""
        df = self.dfs[symbol]
        if df is None or 'macd_1h' not in df.columns:
            logger.warning(f"[{symbol}] No hay datos de MACD 1H para filtrar.")
            return False
            
        # Usar i-1 (vela cerrada anterior) para el filtro, igual que el backtest
        if i-1 < 0: return False
        macd_1h = df['macd_1h'].iloc[i-1] 
        
        if pd.isna(macd_1h):
            logger.warning(f"[{symbol}] Valor de MACD 1H es NaN en la vela {i-1}.")
            return False
        
        if direction == 'LONG':
            if not (macd_1h > 0):
                logger.debug(f"[{symbol}] Setup LONG ignorado por filtro MTF (1H: {macd_1h:.2f})")
                return False
        elif direction == 'SHORT':
            if not (macd_1h < 0):
                logger.debug(f"[{symbol}] Setup SHORT ignorado por filtro MTF (1H: {macd_1h:.2f})")
                return False
                
        return True # Filtro pasado

    # --- ¡INICIO DE CÓDIGO CORREGIDO (ERROR DE DELAY)! ---

    def check_long_setup(self, symbol: str, i: int) -> bool:
        df = self.dfs[symbol]
        if df is None or 'min' not in df.columns or 'is_fvg_bullish' not in df.columns: return False
        
        candle = df.iloc[i]
        
        recent_lows = df['min'].iloc[max(0, i-50):i].dropna()
        if len(recent_lows) < 2 or recent_lows.iloc[-1] >= recent_lows.iloc[-2]: return False
        try:
            sweep_idx = df.index.get_loc(recent_lows.index[-1])
        except KeyError: return False
        if i - sweep_idx > 12: return False
        
        fvg_window = df.iloc[sweep_idx:i]
        bullish_fvgs = fvg_window[fvg_window['is_fvg_bullish']]
        if bullish_fvgs.empty: return False
        
        # --- LÓGICA DE ENTRADA CORREGIDA ---
        fvg_to_trade = bullish_fvgs.iloc[-1]
        fvg_mid_price = fvg_to_trade['fvg_bull_mid']
        fvg_low_price = fvg_to_trade['fvg_bull_low'] # <-- Límite inferior
        current_price = candle['close']

        # El precio actual debe estar DENTRO del FVG (o al menos por debajo del 50%)
        if (current_price <= fvg_mid_price) and (current_price >= fvg_low_price):
            if not self._check_mtf_filter(symbol, i, 'LONG'):
                return False
            
            entry_price = float(current_price) # ¡USAR PRECIO ACTUAL!
            # --- FIN CORRECCIÓN ---
            
            # Stop loss un poco más abajo del nivel estructural (buffer de ~0.05%)
            structural_low = float(recent_lows.iloc[-1])
            sl_buffer = structural_low * 0.0005  # 0.05% del precio
            liquidity_level = structural_low - sl_buffer  # SL Estructural
            pools = self.build_liquidity_pools(df, i, lookback=self.pool_lookback_bars, tol=self.equal_tol)
            tp_pool = self.select_target_pool(df, i, 'LONG', entry_price, pools)
            
            logger.info(f"🔍 [{symbol}] Setup LONG (Sweep) detectado en vela {df.index[i]}. Entry (Mercado)=${entry_price:.4f}, SL=${liquidity_level:.4f}")
            asyncio.create_task(self.open_position(symbol, i, 'LONG', entry_price, liquidity_level, tp_override=tp_pool))
            return True
        return False

    def check_short_setup(self, symbol: str, i: int) -> bool:
        df = self.dfs[symbol]
        if df is None or 'max' not in df.columns or 'is_fvg_bearish' not in df.columns: return False
        
        candle = df.iloc[i]
        
        recent_highs = df['max'].iloc[max(0, i-50):i].dropna()
        if len(recent_highs) < 2 or recent_highs.iloc[-1] <= recent_highs.iloc[-2]: return False
        try:
            sweep_idx = df.index.get_loc(recent_highs.index[-1])
        except KeyError: return False
        if i - sweep_idx > 12: return False
        
        fvg_window = df.iloc[sweep_idx:i]
        bearish_fvgs = fvg_window[fvg_window['is_fvg_bearish']]
        if bearish_fvgs.empty: return False
        
        # --- LÓGICA DE ENTRADA CORREGIDA ---
        fvg_to_trade = bearish_fvgs.iloc[-1]
        fvg_mid_price = fvg_to_trade['fvg_bear_mid']
        fvg_high_price = fvg_to_trade['fvg_bear_high'] # <-- Límite superior
        current_price = candle['close']

        # El precio actual debe estar DENTRO del FVG (o al menos por encima del 50%)
        if (current_price >= fvg_mid_price) and (current_price <= fvg_high_price):
            if not self._check_mtf_filter(symbol, i, 'SHORT'):
                return False

            entry_price = float(current_price) # ¡USAR PRECIO ACTUAL!
            # --- FIN CORRECCIÓN ---
            
            # Stop loss un poco más arriba del nivel estructural (buffer de ~0.05%)
            structural_high = float(recent_highs.iloc[-1])
            sl_buffer = structural_high * 0.0005  # 0.05% del precio
            liquidity_level = structural_high + sl_buffer  # SL Estructural
            pools = self.build_liquidity_pools(df, i, lookback=self.pool_lookback_bars, tol=self.equal_tol)
            tp_pool = self.select_target_pool(df, i, 'SHORT', entry_price, pools)
            
            logger.info(f"🔍 [{symbol}] Setup SHORT (Sweep) detectado en vela {df.index[i]}. Entry (Mercado)=${entry_price:.4f}, SL=${liquidity_level:.4f}")
            asyncio.create_task(self.open_position(symbol, i, 'SHORT', entry_price, liquidity_level, tp_override=tp_pool))
            return True
        return False

    def check_fvg_memory_long(self, symbol: str, i: int) -> bool:
        df = self.dfs[symbol]
        if df is None or 'is_fvg_bullish' not in df.columns or 'is_mitigated' not in df.columns:
            return False
        candle = df.iloc[i]
        
        # Aplicar filtro de antigüedad (si no lo has hecho)
        fvg_max_age = getattr(self, 'fvg_memory_max_age_bars', 96) 
        start_idx = max(0, i - fvg_max_age)
        df_slice = df.iloc[start_idx:i]
        
        unmitigated_bull_fvgs = df_slice[
            (df_slice['is_fvg_bullish'] == True) & (df_slice['is_mitigated'] == False)
        ]
        if unmitigated_bull_fvgs.empty: return False

        current_price = candle['close']

        # --- LÓGICA DE ENTRADA CORREGIDA ---
        # Iterar en los FVGs (del más nuevo al más viejo)
        for fvg_time, fvg_row in unmitigated_bull_fvgs.iloc[::-1].iterrows():
            
            # El precio actual DEBE estar DENTRO del FVG para una entrada a mercado
            if (current_price <= fvg_row['fvg_bull_high']) and (current_price >= fvg_row['fvg_bull_low']):
                
                entry_price = float(current_price) # ¡Usar precio actual!
                # Stop loss un poco más abajo del borde del FVG (buffer de ~0.05%)
                fvg_low = float(fvg_row['fvg_bull_low'])
                sl_buffer = fvg_low * 0.0005  # 0.05% del precio
                liquidity_level = fvg_low - sl_buffer  # SL Estructural (fondo del FVG)

                # --- FILTRO MTF (1H) ---
                if not self._check_mtf_filter(symbol, i, 'LONG'):
                    return False
                # --- FIN FILTRO MTF ---
                
                pools = self.build_liquidity_pools(df, i, lookback=self.pool_lookback_bars, tol=self.equal_tol)
                tp_pool = self.select_target_pool(df, i, 'LONG', entry_price, pools)
                
                logger.info(f"💡 [{symbol}] Setup FVG CON MEMORIA (LONG) detectado.")
                logger.info(f"    FVG de vela: {fvg_time}")
                logger.info(f"    Entrada (Mercado): ${entry_price:.4f}, SL (base): ${liquidity_level:.4f}, TP_pool={tp_pool}")
                
                asyncio.create_task(self.open_position(symbol, i, 'LONG', entry_price, liquidity_level, tp_override=tp_pool))
                return True # Salir después de encontrar el primer trade válido

        # Si el loop termina, no se encontró ningún FVG que el precio actual esté tocando
        return False

    def check_fvg_memory_short(self, symbol: str, i: int) -> bool:
        df = self.dfs[symbol]
        if df is None or 'is_fvg_bearish' not in df.columns or 'is_mitigated' not in df.columns:
            return False
        candle = df.iloc[i]
        
        # Aplicar filtro de antigüedad (si no lo has hecho)
        fvg_max_age = getattr(self, 'fvg_memory_max_age_bars', 96)
        start_idx = max(0, i - fvg_max_age)
        df_slice = df.iloc[start_idx:i]

        unmitigated_bear_fvgs = df_slice[
            (df_slice['is_fvg_bearish'] == True) & (df_slice['is_mitigated'] == False)
        ]
        if unmitigated_bear_fvgs.empty: return False

        current_price = candle['close']

        # --- LÓGICA DE ENTRADA CORREGIDA ---
        # Iterar en los FVGs (del más nuevo al más viejo)
        for fvg_time, fvg_row in unmitigated_bear_fvgs.iloc[::-1].iterrows():
            
            # El precio actual DEBE estar DENTRO del FVG para una entrada a mercado
            if (current_price >= fvg_row['fvg_bear_low']) and (current_price <= fvg_row['fvg_bear_high']):
                
                entry_price = float(current_price) # ¡Usar precio actual!
                # Stop loss un poco más arriba del borde del FVG (buffer de ~0.05%)
                fvg_high = float(fvg_row['fvg_bear_high'])
                sl_buffer = fvg_high * 0.0005  # 0.05% del precio
                liquidity_level = fvg_high + sl_buffer  # SL Estructural (techo del FVG)

                # --- FILTRO MTF (1H) ---
                if not self._check_mtf_filter(symbol, i, 'SHORT'):
                    return False
                # --- FIN FILTRO MTF ---
                
                pools = self.build_liquidity_pools(df, i, lookback=self.pool_lookback_bars, tol=self.equal_tol)
                tp_pool = self.select_target_pool(df, i, 'SHORT', entry_price, pools)
                
                logger.info(f"💡 [{symbol}] Setup FVG CON MEMORIA (SHORT) detectado.")
                logger.info(f"    FVG de vela: {fvg_time}")
                logger.info(f"    Entrada (Mercado): ${entry_price:.4f}, SL (base): ${liquidity_level:.4f}, TP_pool={tp_pool}")

                asyncio.create_task(self.open_position(symbol, i, 'SHORT', entry_price, liquidity_level, tp_override=tp_pool))
                return True # Salir después de encontrar el primer trade válido
        
        # Si el loop termina, no se encontró ningún FVG que el precio actual esté tocando
        return False
    
    # --- ¡FIN DE CÓDIGO CORREGIDO (ERROR DE DELAY)! ---


    # --- Gestión de Órdenes (SINCRONIZADA CON BACKTEST N2 y N3) ---
    
    def get_total_margin_used(self) -> float:
        """Calcula el margen total usado por todas las posiciones abiertas (SMC + COPY)."""
        total_margin = 0.0
        
        # Sumar margen de posiciones SMC
        for pos in self.positions.values():
            if pos is not None:
                total_margin += pos.margin_used
        
        # Sumar margen de posiciones COPY
        for pos in self.copy_positions.values():
            if pos is not None:
                total_margin += pos.margin_used
        
        return total_margin
    
    async def open_position(self, symbol: str, entry_idx: int, direction: str, 
                            entry_price: float, 
                            liquidity_level: float, # <-- ESTE ES EL STOP ESTRUCTURAL
                            tp_override: Optional[float] = None, 
                            is_copy: bool = False):
        
        df = self.dfs[symbol]
        if df is None: return
        
        # --- Validación de Cooldown ---
        if self.position_close_times.get(symbol):
            time_since_close = (datetime.now() - self.position_close_times[symbol]).total_seconds()
            if time_since_close < self.position_cooldown_seconds:
                logger.warning(f"[{symbol}] Cooldown activo. No se puede abrir posición durante {int(self.position_cooldown_seconds - time_since_close)}s más.")
                return
            else:
                self.position_close_times[symbol] = None
        
        # --- Validación de Entradas Fallidas ---
        if not is_copy and symbol in self.failed_entry_prices:
            for failed_price in self.failed_entry_prices[symbol]:
                price_diff_pct = abs(entry_price - failed_price) / failed_price
                if price_diff_pct < self.failed_entry_tolerance:
                    logger.warning(f"[{symbol}] ⚠️ Precio de entrada ${entry_price:.4f} ya falló antes (${failed_price:.4f}). Ignorando setup.")
                    return
        
        symbol_config = self.symbol_configs.get(symbol, {})
        tp_percentage = symbol_config.get('tp_percentage', 2.0) 
        
        # Obtener apalancamiento específico del símbolo
        leverage = self.leverage_per_symbol.get(symbol, self.leverage)
        
        # --- CORRECCIÓN: CALCULAR BALANCE LIBRE (BALANCE - MARGEN USADO) ---
        total_margin_used = self.get_total_margin_used()
        free_balance = self.balance - total_margin_used
        
        if free_balance <= 0:
            logger.warning(f"[{symbol}] No hay balance libre disponible. Balance: ${self.balance:,.2f}, Margen usado: ${total_margin_used:,.2f}")
            
            # Verificar cooldown de alerta
            error_key = f"no_balance_{symbol}"
            now = datetime.now()
            last_alert = self.last_error_alert_time.get(error_key)
            
            if last_alert is None or (now - last_alert).total_seconds() >= self.error_alert_cooldown_seconds:
                await self.send_telegram_message(
                    f"⚠️ <b>Sin Balance Libre</b>\n\n"
                    f"{symbol} {direction}\n"
                    f"Balance: ${self.balance:,.2f}\n"
                    f"Margen usado: ${total_margin_used:,.2f}\n"
                    f"Libre: ${free_balance:,.2f}\n\n"
                    f"<i>Esta alerta no se repetirá por 5 minutos</i>"
                )
                self.last_error_alert_time[error_key] = now
            return
        
        capital_to_risk = free_balance * self.risk_per_trade_pct
        if capital_to_risk <= 0:
            logger.error(f"[{symbol}] Capital a riesgo es cero o negativo (${capital_to_risk:,.2f}).")
            return
        
        logger.info(f"[{symbol}] 💰 Balance: ${self.balance:,.2f} | Margen usado: ${total_margin_used:,.2f} | Libre: ${free_balance:,.2f} | Riesgo (10%): ${capital_to_risk:,.2f}")
        # --- FIN CORRECCIÓN ---

        stop_loss_price = 0.0
        take_profit_price = 0.0
        original_stop_loss_price = 0.0
        risk_per_unit = 0.0 
        position_size_base = 0.0
        position_size_usd = 0.0
        log_prefix = "SMC"

        if not is_copy:
            logger.info(f"[{symbol}] Calculando parámetros para trade {log_prefix}...")

            # --- LÓGICA DE CÁLCULO DE TAMAÑO (IDÉNTICA AL BACKTEST) ---
            
            # --- CORRECCIÓN N1: USAR STOP LOSS ESTRUCTURAL ---
            stop_loss_price = liquidity_level
            original_stop_loss_price = stop_loss_price
                    
            if direction == 'LONG':
                risk_per_unit = entry_price - stop_loss_price
                default_tp = entry_price * (1 + tp_percentage / 100)
            else: # SHORT
                risk_per_unit = stop_loss_price - entry_price
                default_tp = entry_price * (1 - tp_percentage / 100)
            # --- FIN CORRECCIÓN N1 ---

            # --- CORRECCIÓN N2: BUG DE RIESGO-CERO ---
            min_risk_as_price = entry_price * 0.001 # 0.1% Mínimo SL
            
            if risk_per_unit < min_risk_as_price:
                logger.debug(f"[{symbol}] Trade en {df.index[entry_idx]}: Riesgo estructural ({risk_per_unit:.5f}) es demasiado bajo. Ajustando a {min_risk_as_price:.5f}.")
                risk_per_unit = min_risk_as_price
                
                if direction == 'LONG':
                    stop_loss_price = entry_price - risk_per_unit
                else:
                    stop_loss_price = entry_price + risk_per_unit
                
                original_stop_loss_price = stop_loss_price
            # --- FIN CORRECCIÓN N2 ---

            if risk_per_unit <= 0:
                logger.warning(f"[{symbol}] Ignorando señal SMC, riesgo por unidad inválido ({risk_per_unit:.4f}) con SL estructural a {stop_loss_price:.4f}. Entrada: {entry_price}")
                return

            # --- Lógica de TP (Idéntica al Backtest) ---
            take_profit_price = default_tp
            if tp_override is not None:
                rr = 0.0
                if risk_per_unit > 0: 
                    if direction == 'LONG':
                        if tp_override > entry_price: 
                            rr = (tp_override - entry_price) / risk_per_unit
                    else: # SHORT
                        if tp_override < entry_price: 
                            rr = (entry_price - tp_override) / risk_per_unit
                
                if rr >= self.min_rr:
                    take_profit_price = tp_override
                    logger.info(f"[{symbol}] Usando TP de Pool de Liquidez: ${take_profit_price:,.4f} (R:R {rr:.2f}x >= {self.min_rr:.2f}x)")
                else:
                    logger.info(f"[{symbol}] TP de Pool (${tp_override:,.4f}, R:R {rr:.2f}x < {self.min_rr:.2f}x) ignorado. Usando TP por % ({tp_percentage}%): ${take_profit_price:,.4f}")
            else:
                logger.info(f"[{symbol}] No hay TP de Pool. Usando TP por % ({tp_percentage}%): ${take_profit_price:,.4f}")

            # --- Lógica de Tamaño (Idéntica al Backtest) ---
            position_size_base = capital_to_risk / risk_per_unit
            position_size_usd = position_size_base * entry_price

            # --- CORRECCIÓN N3: VALIDACIÓN DE MARGEN ---
            max_notional_position_usd = self.balance * leverage
            
            if position_size_usd > max_notional_position_usd:
                logger.warning(f"[{symbol}] Trade en {df.index[entry_idx]}: El tamaño de posición nocional calculado (${position_size_usd:,.2f}) "
                                f"supera el máximo permitido por el apalancamiento (${max_notional_position_usd:,.2f}). "
                                f"Reduciendo tamaño al máximo.")
                
                position_size_usd = max_notional_position_usd
                position_size_base = position_size_usd / entry_price
            # --- FIN CORRECCIÓN N3 ---

        else:
            # Lógica para Copy Trading (se mantiene, pero ahora usa N3 implícitamente)
            log_prefix = "COPY"
            logger.info(f"[{symbol}] Calculando parámetros para trade {log_prefix}...")
            stop_loss_price = 0.0
            take_profit_price = 0.0
            original_stop_loss_price = 0.0
            
            # El tamaño se basa en el margen, no en el riesgo
            # Usar balance libre en lugar del balance total
            margin_to_use = free_balance * self.risk_per_trade_pct
            
            if entry_price <= 0 or leverage <= 0:
                 logger.error(f"[{symbol}] Precio de entrada ({entry_price}) o apalancamiento ({leverage}) inválido para calcular tamaño de copy trade.")
                 return

            position_size_usd = margin_to_use * leverage
            position_size_base = position_size_usd / entry_price
            
            logger.info(f"[{symbol}] Tamaño para COPY trade: {position_size_base:.4f} (Nocional: ${position_size_usd:.2f}, Margen: ${margin_to_use:.2f}, Lev: {leverage}x)")
            
            if position_size_base <= 0:
                logger.error(f"[{symbol}] Tamaño calculado para COPY trade es inválido ({position_size_base:.4f}). No se puede abrir.")
                return

        # --- FIN LÓGICA DE CÁLCULO DE TAMAÑO ---

        # Calcular margen y liquidación (común para ambos)
        margin_used = (position_size_usd / leverage) if leverage > 0 else position_size_usd
        
        liquidation_price = 0.0
        if leverage > 0:
            liquidation_pct = (1 / leverage) * 0.95 # Asumir 95% para seguridad
            liquidation_price = entry_price * (1 - liquidation_pct) if direction == 'LONG' else entry_price * (1 + liquidation_pct)
        else:
            logger.warning(f"[{symbol}] Apalancamiento es 0, no se puede calcular precio de liquidación.")

        try:
            entry_time_dt = df.index[entry_idx].to_pydatetime()
            if entry_time_dt.tzinfo is None:
                entry_time_dt = entry_time_dt.replace(tzinfo=pd.Timestamp(0, tz='UTC').tzinfo)
            pos = Position(
                symbol=symbol, direction=direction, 
                size_base=position_size_base, # NUEVO
                size_usd=position_size_usd,   # NUEVO
                entry_price=entry_price,
                entry_time=entry_time_dt, entry_idx=entry_idx, 
                stop_loss=stop_loss_price,
                original_stop_loss=original_stop_loss_price, 
                take_profit=take_profit_price,
                liquidation_price=liquidation_price, 
                margin_used=margin_used, 
                is_copy=is_copy
            )
        except Exception as e:
            logger.error(f"Error creando objeto Position para {symbol}: {e}", exc_info=True)
            return

        if is_copy:
            self.copy_positions[symbol] = pos
            logger.info(f"💾 Posición COPY guardada en copy_positions[{symbol}]")
        else:
            self.positions[symbol] = pos
            self.last_signal_times[symbol] = df.index[entry_idx]
            logger.info(f"💾 Posición SMC guardada en positions[{symbol}]")
        
        await self._save_persistent_positions() 

        logger.info(f"📢 [{symbol}] Señal {log_prefix} {direction}: Tamaño {position_size_base:.4f} @ ${entry_price:.4f}")
        logger.info(f"   SL: ${stop_loss_price:.4f}, TP: ${take_profit_price:.4f}, Liq: ${liquidation_price:.4f}, Margen: ${margin_used:.2f}, Valor: ${position_size_usd:,.2f}")

        successful_wallets = []
        failed_wallets = []
        
        for wallet_name, client in self.clients.items():
            try:
                logger.info(f"[{symbol}] Enviando orden {log_prefix} {direction} a wallet '{wallet_name}'...")
                order_params = {
                    'symbol': symbol, 'side': 'buy' if direction == 'LONG' else 'sell',
                    'order_type': 'market', 
                    'quantity': position_size_base, # Usar tamaño en activo base
                }
                if not is_copy:
                    order_params['stop_loss'] = stop_loss_price
                    order_params['take_profit'] = take_profit_price
                
                # SIMULACIÓN (Comentado para producción)
                logger.warning(f"[{symbol}] SIMULACIÓN: Orden {log_prefix} {direction} para '{wallet_name}' NO enviada.")
                successful_wallets.append(wallet_name)
                
                # PRODUCCIÓN (Descomentar para real)
                # order_result = await client.place_order(**order_params)
                # logger.info(f"[{symbol}] ✅ Orden {log_prefix} enviada a '{wallet_name}': {order_result}")
                # successful_wallets.append(wallet_name)
                
            except Exception as e:
                logger.error(f"[{symbol}] ❌ ERROR enviando orden a '{wallet_name}': {e}", exc_info=True)
                failed_wallets.append(wallet_name)
        
        if successful_wallets:
            wallets_str = ", ".join(successful_wallets)
            logger.info(f"[{symbol}] ✅ Orden {log_prefix} exitosa en: {wallets_str}")
            await self.notify_entry(pos) 
        else:
            logger.error(f"[{symbol}] ❌ Todas las wallets fallaron al abrir posición")
            await self.send_telegram_message(
                f"🚨 <b>¡ERROR DE ORDEN!</b>\n\n"
                f"No se pudo abrir {symbol} {direction} en ninguna wallet.\n"
                f"Wallets fallidas: {', '.join(failed_wallets)}"
            )
            if is_copy: self.copy_positions[symbol] = None
            else: self.positions[symbol] = None
            await self._save_persistent_positions()
            return

    async def close_position(self, symbol: str, exit_price: float, reason: str = "Manual") -> None:
        pos = self.positions.get(symbol)
        is_copy_trade = False
        if not pos:
            pos = self.copy_positions.get(symbol)
            is_copy_trade = True
            if not pos:
                logger.warning(f"[{symbol}] No hay posición activa (SMC o COPY) para cerrar.")
                return
        
        try:
            # --- CÁLCULO DE PNL SINCRONIZADO ---
            if pos.direction == 'LONG':
                pnl = (exit_price - pos.entry_price) * pos.size_base
            else: # SHORT
                pnl = (pos.entry_price - exit_price) * pos.size_base
            
            self.balance += pnl
            
            exit_time = pd.Timestamp.now(tz='UTC')
            trade_record = {
                'symbol': symbol, 'entry_time': pos.entry_time, 'exit_time': exit_time,
                'direction': pos.direction, 'entry_price': pos.entry_price, 'exit_price': exit_price,
                'stop_loss': pos.stop_loss, 'take_profit': pos.take_profit,
                'liquidation_price': pos.liquidation_price, 
                'position_size_base': pos.size_base, # NUEVO
                'position_size_usd': pos.size_usd,   # NUEVO
                'margin_used': pos.margin_used, 'pnl': pnl, 'exit_reason': reason
            }
            
            if not is_copy_trade:
                self._save_trade_to_excel(trade_record)
                await self.notify_exit(trade_record)
            else:
                logger.info(f"ℹ️ Posición COPY cerrada: {symbol} {direction}. PnL: ${pnl:,.2f}")
                await self.send_telegram_message(
                    f"ℹ️ <b>POSICIÓN COPY CERRADA</b>\n\n"
                    f"📊 Par: {symbol}\n"
                    f"📈 Dirección: {pos.direction}\n"
                    f"💰 Entrada: ${pos.entry_price:,.4f} | Salida: ${exit_price:,.4f}\n"
                    f"💵 P/L: <b>${pnl:,.2f}</b> (Estimado)\n"
                    f"📝 Razón: {reason}"
                )

            
            if is_copy_trade:
                self.copy_positions[symbol] = None
                logger.info(f"[{symbol}] Posición COPY cerrada y limpiada de copy_positions")
            else:
                self.positions[symbol] = None
                self.position_close_times[symbol] = datetime.now()
                logger.info(f"[{symbol}] Cooldown activado: no se abrirán nuevas posiciones SMC durante {self.position_cooldown_seconds}s")
                
                if "Stop Loss" in reason and pnl < 0:
                    if symbol not in self.failed_entry_prices:
                        self.failed_entry_prices[symbol] = []
                    self.failed_entry_prices[symbol].append(pos.entry_price)
                    if len(self.failed_entry_prices[symbol]) > 5:
                        self.failed_entry_prices[symbol].pop(0)
                    logger.warning(f"[{symbol}] 🚫 Precio de entrada ${pos.entry_price:.4f} registrado como FALLIDO. ")
            
            await self._save_persistent_positions()
            
            if not is_copy_trade:
                logger.info(f"[{symbol}] Posición SMC cerrada exitosamente. PnL: ${pnl:,.2f}, Balance: ${self.balance:,.2f}")
        
        except Exception as e:
            logger.error(f"[{symbol}] Error al cerrar posición: {e}", exc_info=True)
            await self.send_telegram_message(f"🚨 Error crítico al cerrar {symbol}: {e}")
    
    async def manage_position(self, symbol: str, idx: int, is_copy: bool = False):
        if is_copy:
            pos = self.copy_positions.get(symbol)
            if not pos: return 
            logger.debug(f"[{symbol}] Posición COPY activa, esperando señal de cierre de wallet.")
            return
        else:
            pos = self.positions.get(symbol)
            if not pos: return 

        df = self.dfs[symbol]
        if df is None: 
            logger.warning(f"[{symbol}] No hay DataFrame disponible para gestionar posición SMC.")
            return
        
        symbol_config = self.symbol_configs.get(symbol, {})
        enable_structural_stop = symbol_config.get('enable_structural_stop', True)
        
        if idx < 0 or idx >= len(df):
            logger.error(f"[{symbol}] Índice {idx} fuera de rango para DataFrame de tamaño {len(df)}. No se puede gestionar.")
            return
            
        candle = df.iloc[idx]
        current_timestamp = candle.name 
        
        # --- CORRECCIÓN CLAVE: Asegurar la misma zona horaria que el índice del DF ---
        if df.index.tz is not None:
            # Obtener el timezone del índice del DataFrame (Ej: Etc/GMT+3)
            df_tz = df.index.tz
            
            if pos.entry_time.tzinfo is None:
                # Si entry_time es naive, asumimos que está en el mismo TZ que el DF
                pos.entry_time = pos.entry_time.replace(tzinfo=df_tz)
            elif pos.entry_time.tzinfo != df_tz:
                # Si entry_time tiene un TZ diferente, lo convertimos
                try:
                    # Se usa .astimezone(df_tz) para convertir, luego se extrae el tzinfo para entry_time (que es un datetime.datetime nativo)
                    if not isinstance(pos.entry_time, pd.Timestamp):
                        # Convertir primero a Timestamp para usar .tz_convert
                        pos.entry_time = pd.Timestamp(pos.entry_time).tz_convert(df_tz).to_pydatetime()
                    else:
                        pos.entry_time = pos.entry_time.tz_convert(df_tz).to_pydatetime()
                    logger.debug(f"[{symbol}] Corregido TZ de entry_time a {df_tz}.")
                except Exception as e:
                    logger.error(f"[{symbol}] Error convirtiendo entry_time a TZ del DF: {e}")
                    return
        # --- FIN CORRECCIÓN CLAVE ---

        exit_reason, exit_price = None, 0
        new_sl_price = pos.stop_loss 

        if enable_structural_stop and pos.stop_loss > 0:
            try:
                if pos.entry_time <= current_timestamp:
                    df_since_entry = df.loc[pos.entry_time : current_timestamp].iloc[:-1]
                else:
                    logger.warning(f"[{symbol}] entry_time ({pos.entry_time}) posterior a current_timestamp ({current_timestamp}). No se puede calcular trailing.")
                    df_since_entry = pd.DataFrame() 
                
                if not df_since_entry.empty:
                    if pos.direction == 'LONG':
                        recent_structure_lows = df_since_entry['min'].dropna()
                        if not recent_structure_lows.empty:
                            new_protective_stop = recent_structure_lows.iloc[-1]
                            if new_protective_stop > pos.stop_loss: # and new_protective_stop >= pos.entry_price: (Quitado para BE más rápido)
                                new_sl_price = new_protective_stop
                    else: # SHORT
                        recent_structure_highs = df_since_entry['max'].dropna()
                        if not recent_structure_highs.empty:
                            new_protective_stop = recent_structure_highs.iloc[-1]
                            if new_protective_stop < pos.stop_loss: # and new_protective_stop <= pos.entry_price: (Quitado para BE más rápido)
                                new_sl_price = new_protective_stop
            except KeyError as ke:
                logger.warning(f"[{symbol}] Error de índice de tiempo al buscar trailing stop ({ke}).")
            except Exception as e:
                logger.error(f"[{symbol}] Error inesperado calculando trailing stop: {e}", exc_info=True)

            if new_sl_price > 0 and new_sl_price != pos.stop_loss:
                old_sl = pos.stop_loss
                pos.stop_loss = new_sl_price 
                logger.info(f"[{symbol}] 🛡️ TRAILING STOP ESTRUCTURAL (SMC): SL movido de ${old_sl:,.4f} a ${new_sl_price:,.4f}")
                
                # --- Simulación ---
                logger.warning(f"[{symbol}] SIMULACIÓN: Orden SL NO modificada en Bitunix.")
                msg = (
                        f"🛡️ <b>TRAILING STOP ACTUALIZADO ({symbol} - SMC)</b>\n\n"
                        f"Posición: <b>{pos.direction}</b>\n"
                        f"El Stop Loss se ha movido de ${old_sl:,.4f} a <b>${new_sl_price:,.4f}</b>"
                    )
                asyncio.create_task(self.send_telegram_message(msg))
                await self._save_persistent_positions()
                # --- Fin Simulación ---

        if pos.direction == 'LONG':
            if not exit_reason and pos.take_profit > 0 and candle['high'] >= pos.take_profit: 
                exit_reason, exit_price = 'Take Profit', pos.take_profit
                logger.info(f"[{symbol}] ✅ TP ALCANZADO - Vela: {current_timestamp} | High: ${candle['high']:.4f} >= TP: ${pos.take_profit:.4f} | Entry: ${pos.entry_price:.4f}")
            elif not exit_reason and pos.stop_loss > 0 and candle['low'] <= pos.stop_loss: 
                exit_reason = 'Stop Loss' if abs(pos.stop_loss - pos.original_stop_loss) < 1e-9 else 'Stop Estructural' 
                exit_price = pos.stop_loss
                logger.info(f"[{symbol}] ❌ SL ALCANZADO - Vela: {current_timestamp} | Low: ${candle['low']:.4f} <= SL: ${pos.stop_loss:.4f} | Entry: ${pos.entry_price:.4f}")
        else: # SHORT
            if not exit_reason and pos.take_profit > 0 and candle['low'] <= pos.take_profit: 
                exit_reason, exit_price = 'Take Profit', pos.take_profit
                logger.info(f"[{symbol}] ✅ TP ALCANZADO - Vela: {current_timestamp} | Low: ${candle['low']:.4f} <= TP: ${pos.take_profit:.4f} | Entry: ${pos.entry_price:.4f}")
            elif not exit_reason and pos.stop_loss > 0 and candle['high'] >= pos.stop_loss: 
                exit_reason = 'Stop Loss' if abs(pos.stop_loss - pos.original_stop_loss) < 1e-9 else 'Stop Estructural'
                exit_price = pos.stop_loss
                logger.info(f"[{symbol}] ❌ SL ALCANZADO - Vela: {current_timestamp} | High: ${candle['high']:.4f} >= SL: ${pos.stop_loss:.4f} | Entry: ${pos.entry_price:.4f}")
        
        if not exit_reason and self.max_candles_in_trade > 0:
            try:
                if pos.entry_time <= current_timestamp:
                    candles_in_trade = len(df.loc[pos.entry_time : current_timestamp]) 
                    if candles_in_trade >= self.max_candles_in_trade + 1: 
                        exit_reason, exit_price = 'Time Limit', candle['close']
                        logger.info(f"[{symbol}] Límite de tiempo alcanzado ({candles_in_trade-1} velas completas >= {self.max_candles_in_trade}).")
                else:
                    logger.warning(f"[{symbol}] entry_time posterior a current_timestamp, no se puede comprobar límite de tiempo.")
            except KeyError as ke:
                logger.warning(f"[{symbol}] Error de índice de tiempo al comprobar límite de velas ({ke}).")
            except Exception as e:
                logger.error(f"[{symbol}] Error inesperado comprobando límite de tiempo: {e}", exc_info=True)

        if exit_reason:
            logger.info(f"[{symbol}] Condición de cierre SMC detectada: {exit_reason} @ ${exit_price:.4f}")
            # --- Simulación ---
            logger.warning(f"[{symbol}] SIMULACIÓN: Orden de cierre SMC ({exit_reason}) NO enviada a Bitunix.")
            await self.close_position(symbol, exit_price, reason=f"SMC {exit_reason}")
            # --- Fin Simulación ---
    
    async def run(self):
        await self.send_telegram_message(
            f"🚀 <b>Bot SMC (MTF 1H) Iniciado</b>\n\n"
            f"📉 Operando en: {', '.join(self.symbols)}\n"
            f"💼 Balance Inicial: ${self.balance:,.2f}\n\n"
            f"💬 Comandos disponibles:\n"
            f"    • /wallet - Estado de la wallet rastreada\n"
            f"    • /fvg - Ver FVGs no mitigados"
        )
        self.is_running = True
        
        asyncio.create_task(self.listen_telegram_commands())

        while self.is_running:
            try:
                # Actualizar datos de alta frecuencia (1H)
                for symbol in self.symbols:
                    try:
                        if not await self.update_market_data_1h_4h(symbol):
                                logger.warning(f"[{symbol}] No se pudieron actualizar datos MTF, saltando ciclo.")
                                continue
                    except Exception as e:
                        logger.error(f"Error actualizando datos MTF para {symbol}: {e}", exc_info=True)

                # Bucle principal de 15m
                for symbol in self.symbols:
                    try:
                        # 1. Actualizar datos de 15m (que ahora fusiona 1H)
                        if not await self.update_market_data(symbol):
                            logger.warning(f"[{symbol}] No se pudieron actualizar datos (15m), saltando ciclo.")
                            continue 

                        df = self.dfs[symbol]
                        if df is None: continue
                        
                        idx = len(df) - 1 
                        
                        if idx < self.structure_lookback + 2:
                            logger.warning(f"[{symbol}] Esperando más datos históricos (15m)...")
                            continue

                        current_candle_time = df.index[idx]
                        current_price = df['close'].iloc[idx] 
                        
                        logger.info(f"🕵️ [{symbol}] Analizando vela {current_candle_time.strftime('%H:%M:%S')} (Precio actual: ${current_price:,.4f})...")

                        # 2. Gestionar posiciones
                        smc_position = self.positions[symbol]
                        copy_position = self.copy_positions[symbol]
                        
                        if smc_position:
                            await self.manage_position(symbol, idx)
                        if copy_position:
                            await self.manage_position(symbol, idx, is_copy=True)
                        
                        # 3. Buscar nuevos setups SMC
                        if smc_position:
                            logger.debug(f"    [{symbol}] Posición SMC activa, no se buscan más setups SMC.")
                            continue
                        
                        smc_count = sum(1 for p in self.positions.values() if p is not None)
                        copy_count = sum(1 for p in self.copy_positions.values() if p is not None)
                        total_open = smc_count + copy_count
                        
                        if total_open >= self.max_concurrent_open:
                            logger.info(f"↩️ Límite de {self.max_concurrent_open} posiciones alcanzado ({smc_count} SMC + {copy_count} COPY), saltando nuevas entradas.")
                            continue
                        
                        last_time = self.last_signal_times[symbol]
                        if last_time and last_time == current_candle_time:
                            logger.debug(f"    [{symbol}] Ya se abrió posición SMC en vela {last_time}, esperando siguiente vela...")
                            continue

                        # --- LÓGICA DE PRIORIDAD (MTF) ---
                        
                        # Prioridad 1: FVG con Memoria
                        if self.check_fvg_memory_long(symbol, idx): pass
                        elif self.check_fvg_memory_short(symbol, idx): pass
                        
                        # Prioridad 2: Barrido (Sweep) + FVG Inmediato
                        elif self.check_long_setup(symbol, idx): pass
                        elif self.check_short_setup(symbol, idx): pass
                        
                        else:
                            logger.debug(f"    [{symbol}] No se encontraron setups válidos (ni memoria ni sweep) en la vela actual.")
                    
                    except Exception as e:
                        logger.error(f"Error procesando el símbolo {symbol}: {e}", exc_info=True)
                        await self.send_telegram_message(f"🚨 <b>Error Crítico en {symbol}</b>\n{e}")
                
                logger.debug(f"Ciclo completado. Esperando {self.refresh_seconds} segundos...")
                await asyncio.sleep(self.refresh_seconds)

            except Exception as e:
                logger.error(f"Error en el bucle principal (externo): {e}", exc_info=True)
                await self.send_telegram_message(f"🚨 <b>Error Crítico en el Bot (Bucle Principal)</b>\n\n{e}\n\nReintentando en 60 segundos.")
                await asyncio.sleep(60)

    def stop(self):
        self.is_running = False
        if self.wallet_tracker:
            asyncio.create_task(self.wallet_tracker.stop_monitoring())
    
    async def start_wallet_tracking(self, wallet_address: str, etherscan_api_key: Optional[str] = None, moralis_api_key: Optional[str] = None, debank_api_key: Optional[str] = None, enable_copy_trading: bool = True):
        """Inicia el rastreo de una wallet específica."""
        try:
            api_keys = self.load_advanced_wallet_config()
            final_etherscan_key = api_keys.get('etherscan_api_key') or etherscan_api_key
            final_moralis_key = api_keys.get('moralis_api_key') or moralis_api_key
            final_debank_key = api_keys.get('debank_api_key') or debank_api_key
            
            logger.info(f"🔑 Usando API keys:")
            logger.info(f"    Etherscan: {'✅ Configurada' if final_etherscan_key else '❌ No disponible'}")
            logger.info(f"    Moralis: {'✅ Configurada' if final_moralis_key else '❌ No disponible'}")
            logger.info(f"    DeBank: {'✅ Configurada' if final_debank_key else '❌ No disponible'}")
            
            self.wallet_tracker = WalletTracker(
                wallet_address=wallet_address,
                telegram_bot=self.telegram_bot,
                telegram_chat_id=self.telegram_chat_id,
                etherscan_api_key=final_etherscan_key,
                moralis_api_key=final_moralis_key,
                debank_api_key=final_debank_key,
                check_interval=0.5 
            )
            
            self.copy_trading_enabled = enable_copy_trading  
            if enable_copy_trading:
                self.wallet_tracker.enable_copy_trading(
                    open_callback=self.copy_open_position,
                    close_callback=self.copy_close_position
                )
            
            asyncio.create_task(self.wallet_tracker.start_monitoring())
            
            logger.info(f"🔍 Rastreo de wallet iniciado: {wallet_address}")
            logger.info("📊 ESTADO ACTUAL DEL BOT SMC:")
            
            bot_open_positions = []
            for symbol, position in self.positions.items():
                if position is not None:
                    bot_open_positions.append({
                        'symbol': symbol, 'direction': position.direction, 'entry_price': position.entry_price,
                        'size': position.size_base, 'leverage': self.leverage_per_symbol.get(symbol, 15)
                    })
            
            logger.info(f"    💼 Balance bot: ${self.balance:.2f}")
            logger.info(f"    📈 Posiciones bot abiertas: {len(bot_open_positions)}")
            
            if bot_open_positions:
                for i, pos in enumerate(bot_open_positions, 1):
                    logger.info(f"    📊 Bot Posición {i}: {pos['symbol']} | {pos['direction']} | "
                                f"${pos['entry_price']:.2f} | {pos['leverage']}x | Tamaño: {pos['size']:.4f}")
            else:
                logger.info("    ✅ Bot sin posiciones activas")
            
            logger.info(f"    🎯 Símbolos monitoreados: {', '.join(self.symbols)}")
            logger.info(f"    🔄 Copy trading: {'HABILITADO' if enable_copy_trading else 'DESHABILITADO'}")
            
            logger.info("=" * 60)
            logger.info("👁️ CONSULTANDO POSICIONES DE LA WALLET RASTREADA:")
            
            try:
                wallet_positions = await self.get_wallet_current_positions(wallet_address)
                if wallet_positions:
                    logger.info(f"    📊 Posiciones wallet activas: {len(wallet_positions)}")
                    for i, pos in enumerate(wallet_positions, 1):
                        logger.info(f"    🎯 Wallet Posición {i}: {pos['symbol']} | {pos['direction']} | "
                                    f"${pos['entry_price']:.2f} | {pos['leverage']}x | Tamaño: {pos['size']:.4f}")
                else:
                    logger.info("    ✅ Wallet sin posiciones activas detectadas")
            except Exception as e:
                logger.warning(f"    ⚠️ No se pudieron obtener posiciones de la wallet: {e}")
            
            logger.info("=" * 60)
            
            copy_status = "✅ HABILITADO" if enable_copy_trading else "❌ DESHABILITADO"
            message = (
                f"🔍 <b>RASTREO DE WALLET ACTIVADO</b>\n\n"
                f"📍 Wallet: <code>{wallet_address}</code>\n"
                f"🔄 Copy Trading: {copy_status}\n"
                f"⏱️ Intervalo: 500ms (ultra-rápido)\n\n"
            )
            
            try:
                wallet_positions = await self.get_wallet_current_positions(wallet_address)
                message += f"👁️ <b>POSICIONES DE LA WALLET RASTREADA:</b>\n"
                if wallet_positions:
                    message += f"Cantidad posiciones wallet: {len(wallet_positions)}\n\n"
                    for i, pos in enumerate(wallet_positions, 1):
                        message += (
                            f"<b>Wallet Posición {i}:</b>\n"
                            f"Símbolo: {pos['symbol']}\n"
                            f"Dirección: {pos['direction']}\n"
                            f"Precio entrada: ${pos['entry_price']:.2f}\n"
                            f"Tamaño: {pos['size']:.4f}\n"
                            f"Apalancamiento: {pos['leverage']}x\n"
                            f"Plataforma: {pos['platform']}\n\n"
                        )
                else:
                    message += "Cantidad posiciones wallet: 0\n"
                    message += "Wallet sin posiciones activas\n\n"
            except:
                message += "⚠️ No se pudieron consultar posiciones de la wallet\n\n"
            
            message += f"🤖 <b>POSICIONES DEL BOT SMC:</b>\n"
            if bot_open_positions:
                message += f"Cantidad posiciones bot: {len(bot_open_positions)}\n\n"
                for i, pos in enumerate(bot_open_positions, 1):
                    message += (
                        f"<b>Bot Posición {i}:</b>\n"
                        f"Símbolo: {pos['symbol']}\n"
                        f"Dirección: {pos['direction']}\n"
                        f"Precio entrada: ${pos['entry_price']:.2f}\n"
                        f"Tamaño: {pos['size']:.4f}\n"
                        f"Apalancamiento: {pos['leverage']}x\n\n"
                    )
            else:
                message += "Cantidad posiciones bot: 0\n"
                message += "Bot sin posiciones activas\n\n"
            message += "🎯 Monitoreando wallet para copy trading..."
            await self.send_telegram_message(message)
        except Exception as e:
            logger.error(f"Error iniciando rastreo de wallet: {e}")
            await self.send_telegram_message(f"🚨 Error iniciando rastreo de wallet: {e}")
    
    async def copy_open_position(self, tracked_position):
        """Callback para *alertar* sobre trades de wallet. LA EJECUCIÓN ESTÁ DESHABILITADA."""
        if not self.copy_trading_enabled:
            logger.info(f"⏸️ Alertas de copy deshabilitadas. Ignorando señal de apertura para {tracked_position.symbol}")
            return
        try:
            original_symbol = tracked_position.symbol.upper()
            symbol = None
            current_monitored_symbols = list(self.symbols) 
            for possible in [f"{original_symbol}USDT", f"{original_symbol}/USDT", original_symbol, f"{original_symbol}USD"]:
                normalized_possible = possible.replace('/', '')
                normalized_monitored = [s.replace('/', '') for s in current_monitored_symbols]
                if normalized_possible in normalized_monitored:
                    idx = normalized_monitored.index(normalized_possible)
                    symbol = current_monitored_symbols[idx]
                    break
            if not symbol:
                logger.warning(f"Símbolo {original_symbol} de wallet rastreada no está en la lista de símbolos monitoreados. Ignorando alerta.")
                return
            
            if not await self.update_market_data(symbol):
                logger.error(f"No se pudieron obtener datos de mercado para {symbol}. No se puede alertar.")
                return
            df = self.dfs[symbol]
            
            if df is None or len(df) < 2:
                logger.error(f"Datos de mercado insuficientes para {symbol} después de actualizar. No se puede alertar.")
                return
            
            current_price = df['close'].iloc[-1]
            direction = tracked_position.position_type.upper()
            
            wallet_leverage = tracked_position.leverage
            final_leverage = self.leverage_per_symbol.get(symbol, self.leverage) 
            if wallet_leverage is not None and wallet_leverage > 0:
                final_leverage = int(wallet_leverage)
            
            logger.info(f"🔔 DETECTADO COPY TRADE (SOLO ALERTA): {direction} en {symbol} @ ${current_price:.4f}")
            await self.send_telegram_message(
                f"🔔 <b>ALERTA DE WALLET (NO EJECUTADO)</b>\n\n"
                f"Se detectó una <b>APERTURA</b> en la wallet rastreada:\n"
                f"📊 Par: {symbol}\n"
                f"📈 Dirección: <b>{direction}</b>\n"
                f"💰 Precio Aprox.: ${current_price:,.4f}\n"
                f"⚖️ Apalancamiento: {final_leverage}x"
            )
        except Exception as e:
            symbol_name = symbol if 'symbol' in locals() else tracked_position.symbol.upper()
            logger.error(f"Error CRÍTICO en callback copy_open_position (alerta) para {symbol_name}: {e}", exc_info=True)
            try: await self.send_telegram_message(f"🚨 Error crítico procesando alerta de apertura para {symbol_name}: {e}")
            except Exception as tg_err: logger.error(f"Fallo al notificar error de copy_open_position por Telegram: {tg_err}")

    async def copy_close_position(self, tracked_position):
        """Callback para *alertar* sobre cierre de posición. LA EJECUCIÓN ESTÁ DESHABILITADA."""
        if not self.copy_trading_enabled:
            logger.info(f"⏸️ Alertas de copy deshabilitadas. Ignorando señal de cierre para {tracked_position.symbol}")
            return
        try:
            original_symbol = tracked_position.symbol.upper()
            symbol = None
            current_monitored_symbols = list(self.symbols) 
            for active_symbol in current_monitored_symbols:
                if (active_symbol.replace('USDT', '').replace('/USDT', '') == original_symbol or
                    active_symbol == original_symbol or
                    active_symbol == f"{original_symbol}USDT"):
                    symbol = active_symbol
                    break
            if not symbol:
                logger.warning(f"Señal de cierre de wallet para {original_symbol} ignorada (no está en la lista de monitoreo).")
                return
            
            if not await self.update_market_data(symbol):
                logger.error(f"No se pudieron obtener datos de {symbol} para alerta de copy.")
                return
            df = self.dfs[symbol]

            if df is None or len(df) < 1:
                logger.error(f"Datos insuficientes para {symbol} para alerta de copy.")
                return
            
            current_price = df['close'].iloc[-1]
            logger.info(f"🔔 DETECTADO CIERRE DE COPY (SOLO ALERTA) en {symbol} @ ${current_price:.4f}")
            await self.send_telegram_message(
                f"🔔 <b>ALERTA DE WALLET (NO EJECUTADO)</b>\n\n"
                f"Se detectó un <b>CIERRE</b> en la wallet rastreada:\n"
                f"📊 Par: {symbol}\n"
                f"💰 Precio Aprox.: ${current_price:,.4f}"
            )
        except Exception as e:
            symbol_name = symbol if 'symbol' in locals() else tracked_position.symbol.upper()
            logger.error(f"Error en copy trading (alerta de cierre): {e}", exc_info=True)
            await self.send_telegram_message(f"🚨 Error en alerta de cierre de copy para {symbol_name}: {e}")

    async def handle_wallet_command(self, message_id=None):
        """Maneja el comando /wallet para mostrar estado actual de la wallet rastreada."""
        if not self.wallet_tracker:
            await self.send_telegram_message("⚠️ <b>Rastreo de Wallet No Activo</b>\n\nNo hay ninguna wallet siendo rastreada.")
            return
        try:
            positions = await self.wallet_tracker.get_current_positions()
            if not positions:
                await self.send_telegram_message(f"📊 <b>ESTADO DE LA WALLET</b>\n\n📍 Wallet: <code>{self.wallet_tracker.wallet_address}</code>\n💼 Posiciones Activas: 0\n\n✅ Wallet sin posiciones abiertas")
                return
            
            message = (f"📊 <b>ESTADO DE LA WALLET</b>\n\n📍 Wallet: <code>{self.wallet_tracker.wallet_address}</code>\n"
                                f"💼 Posiciones Activas: {len(positions)}\n⏰ Última actualización: {datetime.now().strftime('%H:%M:%S')}\n\n")
            
            import aiohttp
            url = "https://api.hyperliquid.xyz/info"
            headers = {"Content-Type": "application/json"}
            user_payload = {"type": "clearinghouseState", "user": self.wallet_tracker.wallet_address}
            
            total_pnl = 0
            total_margin = 0
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=user_payload) as response:
                    logger.info(f"📡 Status code API Hyperliquid: {response.status}")
                    if response.status == 200:
                        data = await response.json()
                        margin_summary = data.get("marginSummary", {})
                        if not margin_summary: logger.warning(f"⚠️ marginSummary vacío o None.")
                        
                        total_margin = float(margin_summary.get("totalMargin") or 0)
                        total_pnl = float(margin_summary.get("totalUnrealizedPnl") or 0)
                        
                        message += (f"💰 <b>Resumen de Cuenta:</b>\n"
                                    f"    • Margen Total: ${total_margin:,.2f}\n"
                                    f"    • PnL No Realizado: ${total_pnl:,.2f}\n\n")
                        
                        positions_data = data.get("assetPositions", [])
                        if not positions_data: logger.warning(f"⚠️ No se encontraron posiciones en assetPositions.")
                        
                        for idx, item in enumerate(positions_data, 1):
                            position_data = item.get("position", {})
                            symbol = position_data.get("coin", "UNKNOWN")
                            size = float(position_data.get("szi") or 0)
                            if size == 0: continue
                            
                            direction = "🟢 LONG" if size > 0 else "🔴 SHORT"
                            entry_price = float(position_data.get("entryPx") or 0)
                            position_value = float(position_data.get("positionValue") or 0)
                            unrealized_pnl = float(position_data.get("unrealizedPnl") or 0)
                            liq_price = float(position_data.get("liquidationPx") or 0)
                            leverage_info = position_data.get("leverage", {})
                            leverage_type = leverage_info.get("type") or "cross"
                            leverage_value = leverage_info.get("value") or 1
                            leverage_str = f"Cross ({leverage_value}x)" if leverage_type == "cross" else f"{leverage_value}x"
                            pnl_emoji = "🟢" if unrealized_pnl >= 0 else "🔴"
                            
                            message += (
                                f"<b>#{idx} {symbol}</b> {direction}\n"
                                f"    • Tamaño: {abs(size):.4f}\n"
                                f"    • Precio Entrada: ${entry_price:,.2f}\n"
                                f"    • Valor Posición: ${position_value:,.2f}\n"
                                f"    • Apalancamiento: {leverage_str}\n"
                                f"    • {pnl_emoji} PnL: ${unrealized_pnl:,.2f}\n"
                                f"    • Precio Liq.: ${liq_price:,.2f}\n\n"
                            )
                    else:
                        logger.error(f"❌ Error en API Hyperliquid: Status {response.status}")
                        message += f"\n⚠️ Error consultando API de Hyperliquid (Status: {response.status})\n"
            
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="refresh_wallet")]])
            
            if message_id:
                try:
                    await self.telegram_bot.edit_message_text(
                        chat_id=self.telegram_chat_id, message_id=message_id, text=message,
                        parse_mode='HTML', reply_markup=keyboard
                    )
                except Exception as e:
                    logger.error(f"Error editando mensaje: {e}")
            else:
                await self.telegram_bot.send_message(
                    chat_id=self.telegram_chat_id, text=message,
                    parse_mode='HTML', reply_markup=keyboard
                )
        except Exception as e:
            logger.error(f"Error obteniendo estado de wallet: {e}")
            await self.send_telegram_message(f"🚨 <b>Error al obtener estado de wallet</b>\n\nError: {str(e)}")
    
    async def get_wallet_status(self) -> Dict:
        if not self.wallet_tracker:
            return {'status': 'inactive', 'message': 'Rastreo de wallet no iniciado'}
        try:
            summary = await self.wallet_tracker.get_wallet_summary()
            return {'status': 'active', 'summary': summary}
        except Exception as e:
            logger.error(f"Error obteniendo estado de wallet: {e}")
            return {'status': 'error', 'message': str(e)}
    
    async def handle_copy_on_command(self):
        self.copy_trading_enabled = True
        logger.info("✅ Copy trading HABILITADO")
        await self.send_telegram_message("✅ <b>Copy Trading HABILITADO</b>\n\nEl bot ahora copiará las operaciones de la wallet rastreada.")
    
    async def handle_copy_off_command(self):
        self.copy_trading_enabled = False
        logger.info("❌ Copy trading DESHABILITADO")
        await self.send_telegram_message("❌ <b>Copy Trading DESHABILITADO</b>\n\nEl bot ya NO copiará las operaciones.")
    
    async def handle_copy_status_command(self):
        status_emoji = "✅" if self.copy_trading_enabled else "❌"
        status_text = "HABILITADO" if self.copy_trading_enabled else "DESHABILITADO"
        copy_positions_count = sum(1 for pos in self.copy_positions.values() if pos is not None)
        wallets_info = [f"    • {name}" for name in self.clients.keys()]
        message = (
            f"{status_emoji} <b>Estado de Copy Trading</b>\n\n"
            f"📊 Estado: <b>{status_text}</b>\n"
            f"💼 Posiciones de copy activas: {copy_positions_count}\n"
            f"🏦 Wallets conectadas: {len(self.clients)}\n"
        )
        if wallets_info:
            message += "\n<b>Wallets:</b>\n" + "\n".join(wallets_info)
        message += (
            f"\n\n<b>Comandos disponibles:</b>\n"
            f"/copy_on - Habilitar copy trading\n"
            f"/copy_off - Deshabilitar copy trading\n"
            f"/copy_status - Ver este estado"
        )
        await self.send_telegram_message(message)

    # --- ¡INICIO DE CÓDIGO CORREGIDO (/fvg)! ---
    # Muestra TODOS los FVGs, sin filtro de antigüedad.
    
    async def handle_fvg_command(self, args: List[str]):
        """Muestra un resumen de TODOS los FVGs no mitigados (sin filtro de antigüedad)."""
        logger.info(f"ℹ️ Recibido comando /fvg (args: {args}), generando reporte completo...")
        message = "🔍 <b>TODOS los FVGs No Mitigados (Mapa Completo)</b>\n\n"
        found_any = False
        
        # Determinar qué símbolos mostrar
        symbols_to_show = self.symbols
        if args: # Si el usuario especificó un símbolo (ej: /fvg SUIUSDT)
            symbol_arg = args[0].upper().replace('/', '')
            symbols_to_show = [s for s in self.symbols if s.replace('/', '').upper() == symbol_arg]
            if not symbols_to_show:
                await self.send_telegram_message(f"⚠️ Símbolo '{args[0]}' no encontrado. Mostrando todos.")
                symbols_to_show = self.symbols

        try:
            for symbol in symbols_to_show:
                message += f"<b>--- {symbol} ---</b>\n"
                df = self.dfs.get(symbol)
                
                if df is None or df.empty or 'is_mitigated' not in df.columns:
                    message += "  (Datos de 15m aún no disponibles)\n\n"
                    continue

                # --- ¡REVERTIDO! YA NO SE APLICA FILTRO DE ANTIGÜEDAD AQUÍ ---
                # El reporte mostrará TODOS los FVGs del historial cargado (300 velas)
                # La lógica de trading (check_fvg_memory...) SÍ mantendrá el filtro.
                # --- FIN REVERSIÓN ---

                # Bullish FVGs (usa 'df' completo)
                bull_fvgs = df[
                    (df['is_fvg_bullish'] == True) & (df['is_mitigated'] == False)
                ]
                if not bull_fvgs.empty:
                    found_any = True
                    message += "🟢 <b>FVGs Alcistas (Long):</b>\n"
                    # Ordenar por índice descendente para mostrar los más recientes primero
                    for _, row in bull_fvgs.sort_index(ascending=False).head(5).iterrows():
                        message += f"  - ${row['fvg_bull_low']:,.4f} - ${row['fvg_bull_high']:,.4f} (Mid: ${row['fvg_bull_mid']:,.4f})\n"
                else:
                    message += "🟢 <b>FVGs Alcistas (Long):</b> (Ninguno)\n"

                # Bearish FVGs (usa 'df' completo)
                bear_fvgs = df[
                    (df['is_fvg_bearish'] == True) & (df['is_mitigated'] == False)
                ]
                if not bear_fvgs.empty:
                    found_any = True
                    message += "🔴 <b>FVGs Bajistas (Short):</b>\n"
                    # Ordenar por índice descendente para mostrar los más recientes primero
                    for _, row in bear_fvgs.sort_index(ascending=False).head(5).iterrows():
                        message += f"  - ${row['fvg_bear_low']:,.4f} - ${row['fvg_bear_high']:,.4f} (Mid: ${row['fvg_bear_mid']:,.4f})\n"
                else:
                    message += "🔴 <b>FVGs Bajistas (Short):</b> (Ninguno)\n"
                
                # Añadir estado MTF
                df_1h = self.dfs_1h.get(symbol)
                if df_1h is not None and not df_1h.empty and 'macd' in df_1h.columns:
                    macd_1h_val = df_1h['macd'].iloc[-1]
                    trend_1h = "⬆️ ALCISTA" if macd_1h_val > 0 else "⬇️ BAJISTA"
                    message += f"⏱️ <b>Tendencia 1H (MACD):</b> {trend_1h} ({macd_1h_val:,.2f})\n"
                else:
                    message += "⏱️ <b>Tendencia 1H (MACD):</b> (Calculando...)\n"

                message += "\n"  

            if not found_any and len(symbols_to_show) == 1:
                message += "ℹ️ No se encontraron FVGs no mitigados para este símbolo."
            elif not found_any:
                message = "ℹ️ No se encontraron FVGs no mitigados en ningún símbolo."

        except Exception as e:
            logger.error(f"Error generando reporte de FVG: {e}", exc_info=True)
            message = f"🚨 Error al generar el reporte de FVGs: {e}"
        
        await self.send_telegram_message(message)
    
    # --- ¡FIN DE CÓDIGO CORREGIDO (/fvg)! ---

    # --- COMANDO /add_symbol ---
    
    async def handle_add_symbol_command(self, args: List[str]):
        """Inicia el flujo conversacional para agregar un nuevo símbolo."""
        if not args:
            await self.send_telegram_message(
                "❌ <b>Error</b>\n\n"
                "Uso: /add_symbol SIMBOLO\n"
                "Ejemplo: /add_symbol BNBUSDT"
            )
            return
        
        symbol = args[0].upper()
        if not symbol.endswith('USDT'):
            symbol += 'USDT'
        
        # Verificar si ya existe
        if symbol in self.symbols:
            await self.send_telegram_message(
                f"⚠️ <b>El símbolo {symbol} ya está en la lista</b>\n\n"
                f"Símbolos actuales: {', '.join(self.symbols)}"
            )
            return
        
        # Verificar disponibilidad en Binance
        logger.info(f"🔍 Verificando disponibilidad de {symbol} en Binance...")
        try:
            markets = await asyncio.to_thread(self.data_exchange.load_markets)
            ccxt_symbol = symbol.replace('USDT', '/USDT')
            
            if ccxt_symbol not in markets:
                await self.send_telegram_message(
                    f"❌ <b>Símbolo no disponible</b>\n\n"
                    f"El símbolo {symbol} no está disponible en Binance Futures.\n"
                    f"Verifica que el símbolo sea correcto."
                )
                return
            
            # Verificar que sea un futuro perpetuo
            market_info = markets[ccxt_symbol]
            if not market_info.get('swap') and not market_info.get('future'):
                await self.send_telegram_message(
                    f"❌ <b>Tipo de mercado no soportado</b>\n\n"
                    f"El símbolo {symbol} no es un contrato perpetuo (swap)."
                )
                return
            
            logger.info(f"✅ {symbol} está disponible en Binance Futures")
            
            # Iniciar flujo conversacional
            self.pending_symbol_add = {
                'active': True,
                'symbol': symbol,
                'step': 'structural_stop',
                'data': {}
            }
            
            await self.send_telegram_message(
                f"✅ <b>Símbolo {symbol} disponible en Binance</b>\n\n"
                f"Vamos a configurarlo. Responde las siguientes preguntas:\n\n"
                f"<b>1. ¿Stop estructural activado?</b>\n"
                f"Responde: <code>si</code> o <code>no</code>"
            )
            
        except Exception as e:
            logger.error(f"Error verificando símbolo en Binance: {e}", exc_info=True)
            await self.send_telegram_message(
                f"🚨 <b>Error al verificar símbolo</b>\n\n"
                f"Error: {str(e)}"
            )
    
    async def process_symbol_add_response(self, text: str):
        """Procesa las respuestas del flujo conversacional de /add_symbol."""
        if not self.pending_symbol_add.get('active'):
            return
        
        step = self.pending_symbol_add.get('step')
        symbol = self.pending_symbol_add.get('symbol')
        data = self.pending_symbol_add.get('data', {})
        
        try:
            if step == 'structural_stop':
                # Procesar respuesta de stop estructural
                response = text.lower().strip()
                if response in ['si', 'sí', 's', 'yes', 'y']:
                    data['enable_structural_stop'] = True
                elif response in ['no', 'n']:
                    data['enable_structural_stop'] = False
                else:
                    await self.send_telegram_message(
                        "⚠️ Respuesta no válida. Responde: <code>si</code> o <code>no</code>"
                    )
                    return
                
                # Pasar al siguiente paso
                self.pending_symbol_add['step'] = 'tp_percentage'
                self.pending_symbol_add['data'] = data
                
                await self.send_telegram_message(
                    f"✅ Stop estructural: <b>{'Activado' if data['enable_structural_stop'] else 'Desactivado'}</b>\n\n"
                    f"<b>2. Porcentaje de Take Profit (%)</b>\n"
                    f"Ejemplo: <code>2.0</code> (para 2%)"
                )
            
            elif step == 'tp_percentage':
                # Procesar porcentaje de TP
                try:
                    tp_pct = float(text.strip())
                    if tp_pct <= 0 or tp_pct > 100:
                        await self.send_telegram_message(
                            "⚠️ El porcentaje debe estar entre 0 y 100.\n"
                            "Ejemplo: <code>2.0</code>"
                        )
                        return
                    
                    data['tp_percentage'] = tp_pct
                    
                    # Pasar al siguiente paso
                    self.pending_symbol_add['step'] = 'sl_percentage'
                    self.pending_symbol_add['data'] = data
                    
                    await self.send_telegram_message(
                        f"✅ Take Profit: <b>{tp_pct}%</b>\n\n"
                        f"<b>3. Porcentaje de Stop Loss (%)</b>\n"
                        f"Ejemplo: <code>1.0</code> (para 1%)"
                    )
                
                except ValueError:
                    await self.send_telegram_message(
                        "⚠️ Valor no válido. Ingresa un número.\n"
                        "Ejemplo: <code>2.0</code>"
                    )
                    return
            
            elif step == 'sl_percentage':
                # Procesar porcentaje de SL
                try:
                    sl_pct = float(text.strip())
                    if sl_pct <= 0 or sl_pct > 100:
                        await self.send_telegram_message(
                            "⚠️ El porcentaje debe estar entre 0 y 100.\n"
                            "Ejemplo: <code>1.0</code>"
                        )
                        return
                    
                    data['sl_percentage'] = sl_pct
                    data['risk_reward_ratio'] = 2.0  # Fijo como solicitaste
                    
                    # Finalizar y guardar
                    await self.finalize_symbol_add(symbol, data)
                
                except ValueError:
                    await self.send_telegram_message(
                        "⚠️ Valor no válido. Ingresa un número.\n"
                        "Ejemplo: <code>1.0</code>"
                    )
                    return
        
        except Exception as e:
            logger.error(f"Error procesando respuesta de add_symbol: {e}", exc_info=True)
            await self.send_telegram_message(
                f"🚨 Error procesando respuesta: {str(e)}"
            )
            self.pending_symbol_add = {}
    
    async def finalize_symbol_add(self, symbol: str, config_data: Dict):
        """Finaliza el proceso agregando el símbolo al config y al bot."""
        try:
            # Cargar configuración actual
            script_dir = os.path.dirname(os.path.realpath(__file__))
            config_path = os.path.join(script_dir, 'cofigETHBTC.json')
            
            with open(config_path, 'r') as f:
                config = json.load(f)
            
            # Agregar símbolo a la lista
            if symbol not in config['symbols']:
                config['symbols'].append(symbol)
            
            # Agregar configuración del símbolo
            config['symbol_configs'][symbol] = {
                'enable_structural_stop': config_data['enable_structural_stop'],
                'tp_percentage': config_data['tp_percentage'],
                'sl_percentage': config_data['sl_percentage'],
                'risk_reward_ratio': config_data['risk_reward_ratio']
            }
            
            # Agregar leverage por defecto (15x)
            if 'leverage_per_symbol' not in config:
                config['leverage_per_symbol'] = {}
            config['leverage_per_symbol'][symbol] = 15
            
            # Guardar configuración
            with open(config_path, 'w') as f:
                json.dump(config, f, indent=2)
            
            logger.info(f"✅ Símbolo {symbol} agregado al archivo de configuración")
            
            # Actualizar el bot en tiempo real
            self.symbols.append(symbol)
            self.ccxt_symbols[symbol] = symbol.replace('USDT', '/USDT')
            self.dfs[symbol] = None
            self.dfs_1h[symbol] = None
            self.dfs_4h[symbol] = None
            self.positions[symbol] = None
            self.copy_positions[symbol] = None
            self.last_signal_times[symbol] = None
            self.position_close_times[symbol] = None
            self.failed_entry_prices[symbol] = []
            self.leverage_per_symbol[symbol] = 15
            self.symbol_configs[symbol] = config['symbol_configs'][symbol]
            
            # Crear archivo Excel para el nuevo símbolo
            filename = f"live_report_SMC_{symbol.replace('/', '_')}.xlsx"
            self.excel_filenames[symbol] = filename
            self._initialize_excel(filename)
            
            # Limpiar estado conversacional
            self.pending_symbol_add = {}
            
            # Enviar confirmación
            await self.send_telegram_message(
                f"🎉 <b>¡Símbolo agregado exitosamente!</b>\n\n"
                f"📊 <b>Símbolo:</b> {symbol}\n"
                f"🛑 <b>Stop Estructural:</b> {'✅ Activado' if config_data['enable_structural_stop'] else '❌ Desactivado'}\n"
                f"🎯 <b>Take Profit:</b> {config_data['tp_percentage']}%\n"
                f"🔻 <b>Stop Loss:</b> {config_data['sl_percentage']}%\n"
                f"📈 <b>Risk/Reward:</b> {config_data['risk_reward_ratio']}\n"
                f"⚡ <b>Leverage:</b> 15x\n\n"
                f"El bot comenzará a monitorear este símbolo en el próximo ciclo.\n\n"
                f"<b>Símbolos activos:</b> {', '.join(self.symbols)}"
            )
            
        except Exception as e:
            logger.error(f"Error finalizando agregado de símbolo: {e}", exc_info=True)
            await self.send_telegram_message(
                f"🚨 <b>Error al guardar configuración</b>\n\n"
                f"Error: {str(e)}\n\n"
                f"El símbolo NO fue agregado."
            )
            self.pending_symbol_add = {}
    
    # --- FIN COMANDO /add_symbol ---

    
    # ... (El resto de funciones: get_wallet_current_positions, parse_moralis, etc.) ...
    async def get_wallet_current_positions(self, wallet_address: str):
        """Obtiene las posiciones actuales de la wallet rastreada consultando directamente las APIs."""
        try:
            wallet_positions = []
            
            logger.info("🔍 Método 1: Consultando Moralis API con API key real...")
            if hasattr(self, 'wallet_tracker') and self.wallet_tracker and self.wallet_tracker.moralis_api_key:
                try:
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        headers = {'X-API-Key': self.wallet_tracker.moralis_api_key}
                        url = f"https://deep-index.moralis.io/api/v2.2/{wallet_address}/erc20"
                        params = {'chain': 'eth'}
                        
                        async with session.get(url, headers=headers, params=params) as response:
                            if response.status == 200:
                                data = await response.json()
                                positions = self.parse_moralis_balances(data)
                                wallet_positions.extend(positions)
                                logger.info(f"✅ Moralis API encontró {len(positions)} posiciones REALES")
                            else:
                                logger.info(f"❌ Moralis API error: {response.status}")
                except Exception as e:
                    logger.info(f"❌ Error consultando Moralis: {e}")
            else:
                logger.info("❌ Moralis API key no disponible")
            
            if not wallet_positions:
                logger.info("🔍 Método 2: Consultando HyperDash API...")
                try:
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        url = f"https://hyperdash.info/api/trader/{wallet_address}"
                        async with session.get(url) as response:
                            if response.status == 200:
                                data = await response.json()
                                if 'positions' in data:
                                    for pos_data in data['positions']:
                                        if pos_data.get('size', 0) > 0: 
                                            wallet_positions.append({
                                                'symbol': pos_data.get('symbol', 'UNKNOWN'),
                                                'direction': pos_data.get('side', 'LONG').upper(),
                                                'entry_price': float(pos_data.get('entry_price', 0)),
                                                'size': float(pos_data.get('size', 0)),
                                                'leverage': float(pos_data.get('leverage', 1)),
                                                'platform': 'HyperDash'
                                            })
                                    logger.info(f"✅ HyperDash encontró {len(wallet_positions)} posiciones")
                                else:
                                    logger.info("❌ HyperDash no devolvió posiciones")
                            else:
                                logger.info(f"❌ HyperDash API error: {response.status}")
                except Exception as e:
                    logger.info(f"❌ Error consultando HyperDash: {e}")
            
            if not wallet_positions:
                logger.info("Intentando APIs alternativas más confiables...")
                wallet_positions = await self.detect_positions_alternative_apis(wallet_address)
            
            if not wallet_positions:
                logger.info("Intentando detectar posiciones mediante análisis de transacciones...")
                wallet_positions = await self.detect_positions_from_transactions(wallet_address)
            
            if not wallet_positions and hasattr(self, 'wallet_tracker') and self.wallet_tracker:
                try:
                    tracker_summary = await self.wallet_tracker.get_wallet_summary()
                    if tracker_summary.get('positions'):
                        for pos in tracker_summary['positions']:
                            wallet_positions.append({
                                'symbol': pos.symbol, 'direction': pos.position_type, 'entry_price': pos.entry_price,
                                'size': pos.size, 'leverage': pos.leverage, 'platform': pos.platform
                            })
                        logger.info(f"Posiciones obtenidas del wallet tracker: {len(wallet_positions)}")
                except Exception as e:
                    logger.debug(f"Error obteniendo posiciones del tracker: {e}")
            
            return wallet_positions
        except Exception as e:
            logger.error(f"Error obteniendo posiciones de wallet: {e}")
            return []
    
    def parse_moralis_balances(self, moralis_data):
        try:
            positions = []
            for token in moralis_data:
                balance = token.get('balance', '0')
                decimals = int(token.get('decimals', 18))
                symbol = token.get('symbol', 'UNKNOWN')
                if balance and balance != '0':
                    balance_decimal = float(balance) / (10 ** decimals)
                    if balance_decimal > 0.001: 
                        positions.append({
                            'symbol': symbol, 'direction': 'LONG', 'entry_price': 0.0,
                            'size': balance_decimal, 'leverage': 1.0, 'platform': 'Moralis_API'
                        })
            return positions
        except Exception as e:
            logger.error(f"Error parseando datos Moralis: {e}")
            return []
    
    def load_advanced_wallet_config(self):
        try:
            script_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
            config_path = os.path.join(script_dir, 'advanced_wallet_config.json')
            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    config = json.load(f)
                    api_keys = config.get('wallet_tracking', {}).get('api_keys', {})
                    logger.info(f"✅ Configuración avanzada cargada desde: {config_path}")
                    return api_keys
            else:
                logger.debug(f"Archivo de configuración avanzada no encontrado: {config_path}")
                return {}
        except Exception as e:
            logger.error(f"Error cargando configuración avanzada: {e}")
            return {}
    
    async def detect_positions_alternative_apis(self, wallet_address: str):
        try:
            wallet_positions = []
            logger.info("🔍 Método 1: Consultando Alchemy API...")
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    alchemy_url = f"https://eth-mainnet.g.alchemy.com/v2/demo/getTokenBalances"
                    params = {'address': wallet_address, 'type': 'erc20'}
                    async with session.get(alchemy_url, params=params) as response:
                        if response.status == 200:
                            data = await response.json()
                            positions = self.parse_alchemy_balances(data)
                            wallet_positions.extend(positions)
                            logger.info(f"✅ Alchemy API encontró {len(positions)} posiciones REALES")
                        else:
                            logger.info(f"❌ Alchemy API error: {response.status}")
            except Exception as e:
                logger.info(f"❌ Error consultando Alchemy: {e}")
            
            if not wallet_positions:
                logger.info("🔍 Método 2: Consultando CoinGecko API...")
                try:
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        url = f"https://api.coingecko.com/api/v3/simple/price"
                        params = {'ids': 'ethereum,bitcoin,tether,usd-coin', 'vs_currencies': 'usd'}
                        async with session.get(url, params=params) as response:
                            if response.status == 200:
                                logger.info("✅ CoinGecko API disponible, pero no detecta posiciones específicas de wallet")
                            else:
                                logger.info(f"❌ CoinGecko API error: {response.status}")
                except Exception as e:
                    logger.info(f"❌ Error consultando CoinGecko: {e}")
            
            if not wallet_positions:
                logger.info("🔍 Método 3: Consultando Zapper API...")
                try:
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        zapper_url = f"https://api.zapper.fi/v1/protocols/tokens/balances"
                        params = {'addresses[]': wallet_address, 'network': 'ethereum'}
                        async with session.get(zapper_url, params=params) as response:
                            if response.status == 200:
                                data = await response.json()
                                positions = self.parse_zapper_balances(data)
                                wallet_positions.extend(positions)
                                logger.info(f"✅ Zapper API encontró {len(positions)} posiciones REALES")
                            else:
                                logger.info(f"❌ Zapper API error: {response.status}")
                except Exception as e:
                    logger.info(f"❌ Error consultando Zapper: {e}")
            
            if wallet_positions:
                logger.info(f"🎯 RESULTADO: {len(wallet_positions)} posiciones detectadas de APIs REALES")
            else:
                logger.info("🎯 RESULTADO: 0 posiciones detectadas - TODAS las APIs fallaron")
            return wallet_positions
        except Exception as e:
            logger.error(f"Error en APIs alternativas: {e}")
            return []
    
    def parse_alchemy_balances(self, alchemy_data):
        try:
            positions = []
            if 'result' in alchemy_data and 'tokenBalances' in alchemy_data['result']:
                for token in alchemy_data['result']['tokenBalances']:
                    balance = token.get('tokenBalance', '0x0')
                    if balance and balance != '0x0':
                        balance_decimal = int(balance, 16) / 1e18
                        if balance_decimal > 0.001: 
                            contract_address = token.get('contractAddress', '').lower()
                            symbol = self.map_contract_to_symbol(contract_address)
                            positions.append({
                                'symbol': symbol, 'direction': 'LONG', 'entry_price': 0.0,
                                'size': balance_decimal, 'leverage': 1.0, 'platform': 'Alchemy_Detected'
                            })
            return positions
        except Exception as e:
            logger.error(f"Error parseando datos Alchemy: {e}")
            return []
    
    def parse_zapper_balances(self, zapper_data):
        try:
            positions = []
            for address_data in zapper_data.values():
                for protocol_data in address_data.values():
                    for position in protocol_data.get('products', []):
                        for asset in position.get('assets', []):
                            balance = float(asset.get('balance', 0))
                            if balance > 0.001:
                                positions.append({
                                    'symbol': asset.get('symbol', 'UNKNOWN'), 'direction': 'LONG', 'entry_price': 0.0,
                                    'size': balance, 'leverage': 1.0, 'platform': 'Zapper_DeFi'
                                })
            return positions
        except Exception as e:
            logger.error(f"Error parseando datos Zapper: {e}")
            return []
    
    def map_contract_to_symbol(self, contract_address):
        contract_mapping = {
            '0xdac17f958d2ee523a2206206994597c13d831ec7': 'USDT',
            '0xa0b86a33e6417c1c6c7c4b4c3d3c3c3c3c3c3c3c': 'USDC',
            '0x2260fac5e5542a773aa44fbcfedf7c193bc2c599': 'BTC',
            '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2': 'ETH',
            '0x0000000000000000000000000000000000000000': 'ETH',
        }
        return contract_mapping.get(contract_address, 'UNKNOWN')
    
    async def detect_positions_from_transactions(self, wallet_address: str):
        try:
            wallet_positions = []
            if hasattr(self, 'wallet_tracker') and self.wallet_tracker and self.wallet_tracker.etherscan_api_key:
                try:
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        params = {
                            'module': 'account', 'action': 'txlist', 'address': wallet_address,
                            'startblock': 0, 'endblock': 99999999, 'page': 1, 'offset': 10,
                            'sort': 'desc', 'apikey': self.wallet_tracker.etherscan_api_key
                        }
                        url = "https://api.etherscan.io/api"
                        async with session.get(url, params=params) as response:
                            if response.status == 200:
                                data = await response.json()
                                if data.get('status') == '1':
                                    positions = self.analyze_trading_transactions(data.get('result', []))
                                    wallet_positions.extend(positions)
                except Exception as e:
                    logger.debug(f"Error analizando transacciones Etherscan: {e}")
            
            if not wallet_positions:
                try:
                    if hasattr(self, 'wallet_tracker') and self.wallet_tracker and hasattr(self.wallet_tracker, 'debank_api_key'):
                        import aiohttp
                        async with aiohttp.ClientSession() as session:
                            headers = {'AccessKey': self.wallet_tracker.debank_api_key} if self.wallet_tracker.debank_api_key else {}
                            url = f"https://pro-openapi.debank.com/v1/user/complex_protocol_list"
                            params = {'id': wallet_address}
                            async with session.get(url, headers=headers, params=params) as response:
                                if response.status == 200:
                                    data = await response.json()
                                    positions = self.parse_debank_positions(data)
                                    wallet_positions.extend(positions)
                except Exception as e:
                    logger.debug(f"Error consultando DeBank para detección: {e}")
            
            logger.info(f"Posiciones detectadas mediante análisis: {len(wallet_positions)}")
            return wallet_positions
        except Exception as e:
            logger.error(f"Error en detección de posiciones por transacciones: {e}")
            return []
    
    def analyze_trading_transactions(self, transactions):
        try:
            positions = []
            trading_contracts = {
                '0x65c7c7c4f3d6f5e4b4e4f4e4f4e4f4img': 'dYdX',
                '0xe592427a0aece92de3edee1f18e0157c05861564': 'Uniswap_V3',
                '0x1111111254eeb25477b68fb85ed929f73a960582': '1inch',
                '0x3f5ce5fbfe3e9af3971dd833d26ba9b5c936f0be': 'Binance',
            }
            from datetime import datetime, timedelta
            cutoff_time = datetime.now() - timedelta(hours=24)
            
            for tx in transactions[:20]:
                try:
                    tx_time = datetime.fromtimestamp(int(tx.get('timeStamp', 0)))
                    if tx_time < cutoff_time: continue
                    to_address = tx.get('to', '').lower()
                    value = float(tx.get('value', 0)) / 1e18
                    platform = trading_contracts.get(to_address)
                    if platform and value > 0.01: 
                        symbol = self.infer_symbol_from_transaction(tx)
                        if symbol:
                            positions.append({
                                'symbol': symbol, 'direction': 'LONG', 'entry_price': 0.0, 
                                'size': value, 'leverage': 1.0, 'platform': platform
                            })
                except Exception as e:
                    logger.debug(f"Error analizando transacción: {e}")
                    continue
            return positions
        except Exception as e:
            logger.error(f"Error analizando transacciones de trading: {e}")
            return []
    
    def infer_symbol_from_transaction(self, tx):
        try:
            contract_to_symbol = {'eth': 'ETH'}
            value = float(tx.get('value', 0))
            if value > 0: return 'ETH'
            return 'ETH' 
        except Exception as e:
            logger.debug(f"Error infiriendo símbolo: {e}")
            return 'UNKNOWN'
    
    def parse_debank_positions(self, debank_data):
        try:
            positions = []
            for protocol in debank_data.get('data', []):
                protocol_name = protocol.get('name', 'Unknown')
                for portfolio in protocol.get('portfolio_item_list', []):
                    for asset in portfolio.get('detail', {}).get('supply_token_list', []):
                        symbol = asset.get('symbol', 'UNKNOWN')
                        amount = float(asset.get('amount', 0))
                        if amount > 0.001: 
                            positions.append({
                                'symbol': symbol, 'direction': 'LONG', 'entry_price': 0.0,
                                'size': amount, 'leverage': 1.0, 'platform': protocol_name
                            })
            return positions
        except Exception as e:
            logger.error(f"Error parseando posiciones DeBank: {e}")
            return []

# --- Función Main (AHORA AÑADIDA Y CORREGIDA) ---
async def main():
    script_dir = os.path.dirname(os.path.realpath(__file__))
    config_filename = 'cofigETHBTC.json' # <-- CORREGIDO EL TYPO
    config_path = os.path.join(script_dir, config_filename)
    print(config_path)

    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
    except FileNotFoundError:
        logger.error(f"No se encontró '{config_filename}' en la ruta: {config_path}. Por favor, crea uno.")
        return
    except json.JSONDecodeError:
        logger.error(f"Error al decodificar JSON desde '{config_filename}'. Verifica el formato.")
        return

    wallets = config.get('wallets')
    if not wallets:
        if 'api_key' in config and 'api_secret' in config:
            wallets = [{
                'name': 'Wallet Principal',
                'api_key': config['api_key'],
                'api_secret': config['api_secret'],
                'enabled': True
            }]
        else:
            logger.error("No se encontraron credenciales de wallet en la configuración")
            return
    
    telegram_token = config.get('telegram_token')
    telegram_chat_id = config.get('telegram_chat_id')
    
    if not telegram_token or not telegram_chat_id:
        logger.error("Faltan 'telegram_token' o 'telegram_chat_id' en el archivo de configuración.")
        return
        
    bot = SmartMoneyLiveBot(
        wallets=wallets,
        telegram_token=telegram_token,
        telegram_chat_id=telegram_chat_id,
        symbols=config.get('symbols', ['BTCUSDT']),
        initial_balance=float(config.get('initial_balance', 1000.0))
    )
    
    try:
        if 'enable_macd_filter' in config:
            bot.enable_macd_filter = bool(config['enable_macd_filter'])
        if 'macd_fast' in config:
            bot.macd_fast = int(config['macd_fast'])
        if 'macd_slow' in config:
            bot.macd_slow = int(config['macd_slow'])
        if 'macd_signal' in config:
            bot.macd_signal = int(config['macd_signal'])
        if 'max_concurrent_open' in config:
            bot.max_concurrent_open = int(config['max_concurrent_open'])
        if 'leverage_per_symbol' in config and isinstance(config['leverage_per_symbol'], dict):
            bot.leverage_per_symbol.update({str(k): int(v) for k, v in config['leverage_per_symbol'].items()})
        
        if 'symbol_configs' in config and isinstance(config['symbol_configs'], dict):
            bot.symbol_configs = config['symbol_configs']
            logger.info(f"✅ Configuraciones por símbolo cargadas para: {', '.join(bot.symbol_configs.keys())}")
            for symbol, cfg in bot.symbol_configs.items():
                logger.info(f"    {symbol}: TP={cfg.get('tp_percentage')}%, "
                            f"Structural Stop={'✅' if cfg.get('enable_structural_stop') else '❌'}, "
                            f"R:R={cfg.get('risk_reward_ratio')}")
    except Exception as e:
        logger.error(f"Error aplicando configuración opcional desde JSON: {e}")
    
    wallet_config = config.get('wallet_tracking', {})
    if wallet_config.get('enabled', False):
        wallet_address = wallet_config.get('wallet_address')
        if wallet_address:
            etherscan_key = wallet_config.get('etherscan_api_key') or None
            moralis_key = wallet_config.get('moralis_api_key') or None
            debank_key = wallet_config.get('debank_api_key') or None
            copy_trading = wallet_config.get('copy_trading_enabled', wallet_config.get('copy_trading', True))
            
            logger.info(f"🔍 Configurando rastreo de wallet: {wallet_address}")
            logger.info(f"🔄 Copy trading: {'✅ HABILITADO' if copy_trading else '❌ DESHABILITADO'}")
            
            logger.info("=" * 60)
            logger.info("🚀 INICIANDO WALLET TRACKER CON COPY TRADING")
            logger.info("=" * 60)
            
            asyncio.create_task(bot.start_wallet_tracking(wallet_address, etherscan_key, moralis_key, debank_key, copy_trading))
        else:
            logger.warning("Wallet tracking habilitado pero no se especificó dirección")
    
    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("Deteniendo bot...")
        await bot.send_telegram_message("🛑 <b>Bot Detenido Manualmente</b>")
        bot.stop()

if __name__ == "__main__":
    asyncio.run(main())