"""
Bot de Trading SMC en Vivo - Orquestador Principal
Integra todos los módulos y ejecuta el loop principal
"""

import asyncio
import logging
import json
import os
from datetime import datetime
from typing import Dict, List
import ccxt
from telegram.ext import Application, CommandHandler

# Importar módulos
from core.market_data import MarketDataManager
from core.patterns import PatternDetector
from core.strategy import SMCStrategy
from core.risk_management import RiskManager, Position
from core.execution import ExecutionManager
from telegram_bot.handlers import TelegramHandler
from telegram_bot.commands import TelegramCommands

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('smc_bot_live.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class SmartMoneyLiveBot:
    """Bot principal de trading SMC en vivo"""
    
    def __init__(self, config: Dict):
        # Configuración
        self.symbols = [s.replace('USDT', '/USDT') if '/' not in s else s 
                       for s in config.get('symbols', [])]
        self.initial_balance = float(config.get('initial_balance', 1000.0))
        self.balance = self.initial_balance
        self.max_concurrent = int(config.get('max_concurrent_open', 4))
        self.update_interval = 10  # ACTUALIZAR CADA 10 SEGUNDOS
        
        # Apalancamiento por símbolo
        self.leverage_per_symbol = config.get('leverage_per_symbol', {})
        
        # Clientes de exchange
        self.public_client = self._setup_public_client()
        self.private_client = self._setup_private_client(config)
        
        # Módulos core
        self.market_data = MarketDataManager(self.public_client)
        self.pattern_detector = PatternDetector(structure_lookback=20)
        self.strategy = SMCStrategy()
        self.risk_manager = RiskManager(
            risk_per_trade_pct=1.0,
            min_risk_as_pct=0.1,
            risk_reward_ratio=2.0
        )
        self.execution = ExecutionManager(
            private_client=self.private_client,
            risk_manager=self.risk_manager,
            max_candles_in_trade=48
        )
        
        # Telegram
        self.telegram = TelegramHandler(
            token=config.get('telegram_token'),
            chat_id=config.get('telegram_chat_id')
        )
        self.telegram_commands = TelegramCommands(self)
        
        # Tracking
        self.candles_in_trade: Dict[str, int] = {}
        self.total_pnl = 0.0
    
    def _setup_public_client(self) -> ccxt.Exchange:
        """Configura cliente público para datos de mercado"""
        return ccxt.binanceusdm({
            'enableRateLimit': True,
            'options': {'defaultType': 'future'}
        })
    
    def _setup_private_client(self, config: Dict) -> ccxt.Exchange:
        """Configura cliente privado para trading"""
        api_key = config.get('api_key')
        api_secret = config.get('api_secret')
        
        if not api_key or not api_secret:
            logger.warning("Sin credenciales de API. Modo simulación.")
            return None
        
        return ccxt.binanceusdm({
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
            'options': {'defaultType': 'future'}
        })
    
    async def start(self):
        """Inicia el bot"""
        logger.info("🚀 Iniciando SmartMoneyLiveBot...")
        
        # Configurar aplicación de Telegram
        telegram_app = Application.builder().token(self.telegram.bot.token).build()
        
        # Registrar comandos
        telegram_app.add_handler(CommandHandler("fvg", self.telegram_commands.cmd_fvg))
        telegram_app.add_handler(CommandHandler("status", self.telegram_commands.cmd_status))
        telegram_app.add_handler(CommandHandler("positions", self.telegram_commands.cmd_positions))
        telegram_app.add_handler(CommandHandler("help", self.telegram_commands.cmd_help))
        telegram_app.add_handler(CommandHandler("start", self.telegram_commands.cmd_start))
        
        # Enviar mensaje de inicio
        await self.telegram.send_startup_message(self.symbols)
        
        # Inicializar datos
        await self._initialize_data()
        
        # Iniciar Telegram bot y loop principal simultáneamente
        async def run_telegram():
            await telegram_app.initialize()
            await telegram_app.start()
            await telegram_app.updater.start_polling(drop_pending_updates=True)
        
        logger.info("🤖 Iniciando bot de Telegram y loop principal...")
        
        # Ejecutar ambos loops en paralelo
        await asyncio.gather(
            run_telegram(),
            self._main_loop()
        )
    
    async def _initialize_data(self):
        """Inicializa datos de mercado para todos los símbolos"""
        logger.info("📊 Inicializando datos de mercado...")
        
        for symbol in self.symbols:
            success = await self.market_data.update_market_data(symbol)
            if not success:
                logger.error(f"Error inicializando datos para {symbol}")
                continue
            
            # Calcular indicadores
            self.market_data.compute_indicators(symbol)
            
            # Detectar patrones
            df = self.market_data.get_dataframe(symbol, '15m')
            if df is not None:
                df = self.pattern_detector.detect_fvg_and_mitigation(df)
                df = self.pattern_detector.detect_swings(df)
                self.market_data.dfs[symbol] = df
        
        logger.info("✅ Datos inicializados correctamente")
    
    async def _main_loop(self):
        """Loop principal del bot - SE EJECUTA CADA 10 SEGUNDOS"""
        logger.info("⚙️ Iniciando loop principal (actualización cada 10s)...")
        
        while True:
            try:
                loop_start = datetime.now()
                
                # 1. Actualizar datos de mercado para TODOS los símbolos
                await self._update_all_market_data()
                
                # 2. Gestionar posiciones abiertas
                await self._manage_open_positions()
                
                # 3. Buscar nuevas entradas (respetando concurrencia)
                await self._scan_for_entries()
                
                # 4. Log de estado
                loop_duration = (datetime.now() - loop_start).total_seconds()
                logger.info(
                    f"✅ Ciclo completado en {loop_duration:.2f}s | "
                    f"Posiciones: {len(self.execution.open_positions)}/{self.max_concurrent} | "
                    f"Balance: ${self.balance:,.2f}"
                )
                
                # 5. Esperar hasta siguiente ciclo (10 segundos)
                await asyncio.sleep(self.update_interval)
                
            except KeyboardInterrupt:
                logger.info("🛑 Deteniendo bot...")
                break
            except Exception as e:
                logger.error(f"Error en loop principal: {e}", exc_info=True)
                await self.telegram.send_error(str(e))
                await asyncio.sleep(10)
    
    async def _update_all_market_data(self):
        """Actualiza datos de mercado para todos los símbolos"""
        for symbol in self.symbols:
            try:
                # Actualizar OHLCV
                await self.market_data.update_market_data(symbol)
                
                # Recalcular indicadores
                self.market_data.compute_indicators(symbol)
                
                # RECALCULAR PATRONES (FVG, MITIGACIÓN, SWINGS)
                df = self.market_data.get_dataframe(symbol, '15m')
                if df is not None:
                    df = self.pattern_detector.detect_fvg_and_mitigation(df)
                    df = self.pattern_detector.detect_swings(df)
                    self.market_data.dfs[symbol] = df
                
            except Exception as e:
                logger.error(f"Error actualizando {symbol}: {e}")
    
    async def _manage_open_positions(self):
        """Gestiona todas las posiciones abiertas"""
        for symbol in list(self.execution.open_positions.keys()):
            try:
                position = self.execution.open_positions[symbol]
                df = self.market_data.get_dataframe(symbol, '15m')
                
                if df is None or df.empty:
                    continue
                
                current_candle = df.iloc[-1]
                current_price = float(current_candle['close'])
                
                # Incrementar contador de velas
                self.candles_in_trade[symbol] = self.candles_in_trade.get(symbol, 0) + 1
                
                # 1. Verificar condiciones de salida (SL, TP, Time Limit)
                exit_condition = self.execution.check_exit_conditions(
                    symbol=symbol,
                    current_candle=current_candle,
                    candles_in_trade=self.candles_in_trade[symbol]
                )
                
                if exit_condition:
                    reason, exit_price = exit_condition
                    trade_log = await self.execution.close_position(
                        symbol=symbol,
                        reason=reason,
                        current_price=exit_price
                    )
                    
                    if trade_log:
                        self.balance += trade_log['pnl']
                        self.total_pnl += trade_log['pnl']
                        self.candles_in_trade[symbol] = 0
                        await self.telegram.send_trade_closed(trade_log)
                    
                    continue
                
                # 2. Actualizar Breakeven
                breakeven_sl = self.risk_manager.check_breakeven(
                    position=position,
                    current_price=current_price
                )
                
                if breakeven_sl:
                    old_sl = position.stop_loss
                    await self.execution.update_stop_loss(position, breakeven_sl)
                    await self.telegram.send_sl_updated(symbol, old_sl, breakeven_sl)
                
                # 3. Actualizar Trailing Stop
                df_swings = df.iloc[position.entry_idx:] if hasattr(position, 'entry_idx') else df
                trailing_sl = self.risk_manager.update_trailing_stop(
                    position=position,
                    current_price=current_price,
                    df_swings=df_swings
                )
                
                if trailing_sl:
                    old_sl = position.stop_loss
                    await self.execution.update_stop_loss(position, trailing_sl)
                    await self.telegram.send_sl_updated(symbol, old_sl, trailing_sl)
                
            except Exception as e:
                logger.error(f"Error gestionando posición {symbol}: {e}", exc_info=True)
    
    async def _scan_for_entries(self):
        """Busca nuevas entradas respetando límite de concurrencia"""
        if len(self.execution.open_positions) >= self.max_concurrent:
            return
        
        for symbol in self.symbols:
            if len(self.execution.open_positions) >= self.max_concurrent:
                break
            
            if symbol in self.execution.open_positions:
                continue
            
            try:
                df = self.market_data.get_dataframe(symbol, '15m')
                
                if df is None or df.empty or len(df) < 50:
                    continue
                
                # PRIORIDAD 1: FVG Memory Long
                setup = self.strategy.check_fvg_memory_setup(df, 'LONG')
                setup = self.strategy.validate_setup(df, setup)
                
                if setup:
                    await self._execute_setup(symbol, setup)
                    continue
                
                # PRIORIDAD 2: FVG Memory Short
                setup = self.strategy.check_fvg_memory_setup(df, 'SHORT')
                setup = self.strategy.validate_setup(df, setup)
                
                if setup:
                    await self._execute_setup(symbol, setup)
                    continue
                
                # PRIORIDAD 3: Sweep Long
                setup = self.strategy.check_sweep_setup(df, 'LONG')
                setup = self.strategy.validate_setup(df, setup)
                
                if setup:
                    await self._execute_setup(symbol, setup)
                    continue
                
                # PRIORIDAD 4: Sweep Short
                setup = self.strategy.check_sweep_setup(df, 'SHORT')
                setup = self.strategy.validate_setup(df, setup)
                
                if setup:
                    await self._execute_setup(symbol, setup)
                    continue
                
            except Exception as e:
                logger.error(f"Error escaneando {symbol}: {e}", exc_info=True)
    
    async def _execute_setup(self, symbol: str, setup: Dict):
        """Ejecuta un setup validado"""
        leverage = self.leverage_per_symbol.get(
            symbol.replace('/', ''), 
            15
        )
        
        position = await self.execution.execute_trade(
            symbol=symbol,
            setup=setup,
            balance=self.balance,
            leverage=leverage,
            current_time=datetime.now()
        )
        
        if position:
            self.candles_in_trade[symbol] = 0
            await self.telegram.send_trade_opened(position)


def load_config() -> Dict:
    """Carga la configuración desde JSON"""
    script_dir = os.path.dirname(os.path.realpath(__file__))
    config_path = os.path.join(script_dir, 'cofigETHBTC.json')
    
    try:
        with open(config_path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error(f"Archivo de configuración no encontrado: {config_path}")
        return {}
    except json.JSONDecodeError:
        logger.error(f"Error decodificando JSON: {config_path}")
        return {}


async def main():
    """Punto de entrada principal"""
    config = load_config()
    
    if not config:
        logger.error("No se pudo cargar la configuración. Abortando.")
        return
    
    bot = SmartMoneyLiveBot(config)
    await bot.start()


if __name__ == '__main__':
    asyncio.run(main())