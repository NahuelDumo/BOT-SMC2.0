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
from core.historical_level import HistoricalLevelsDetector as LevelDetector
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
        self.level_detector = LevelDetector()  # NUEVO: Detector de niveles
        self.strategy = SMCStrategy()
        self.risk_manager = RiskManager(
            risk_per_trade_pct=1.0,
            min_risk_as_pct=0.1,
            risk_reward_ratio=3.0,
            min_tp_pct=15.0,  # Mínimo 15% de ganancia en TP
            pool_distance_multiplier=1.5  # Aumentar TP si pool está lejos
        )
        self.execution = ExecutionManager(
            private_client=self.private_client,
            risk_manager=self.risk_manager,
            strategy=self.strategy,
            max_candles_in_trade=48
        )
        
        # Telegram
        self.telegram = TelegramHandler(
            token=config.get('telegram_token'),
            chat_id=config.get('telegram_chat_id')
        )
        self.telegram_commands = TelegramCommands(self)
        self.telegram_app = None  # Se inicializará en start()
        
        # Tracking
        self.candles_in_trade: Dict[str, int] = {}
        self.total_pnl = 0.0
        
        # NUEVO: Cache de niveles por símbolo
        self.levels_cache: Dict[str, Dict] = {}
        
        # NUEVO: Cache para evitar entradas duplicadas (cooldown por símbolo)
        self.last_close_time: Dict[str, datetime] = {}
        self.trade_cooldown_minutes = 10  # Esperar 10 minutos después de cerrar antes de otra entrada
    
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
        await self._setup_telegram_bot()
        
        # Inicializar datos ANTES de iniciar Telegram
        await self._initialize_data()
        
        # Iniciar bot de Telegram y loop principal en paralelo
        await asyncio.gather(
            self._run_telegram_bot(),
            self._main_loop(),
            self._send_startup_notification()
        )
    
    async def _setup_telegram_bot(self):
        """Configura el bot de Telegram con handlers de comandos"""
        config = load_config()
        token = config.get('telegram_token')
        
        if not token:
            logger.warning("No hay token de Telegram configurado")
            return
        
        # Crear aplicación
        self.telegram_app = Application.builder().token(token).build()
        
        # Registrar comandos
        self.telegram_app.add_handler(CommandHandler("start", self.telegram_commands.cmd_start))
        self.telegram_app.add_handler(CommandHandler("help", self.telegram_commands.cmd_help))
        self.telegram_app.add_handler(CommandHandler("fvg", self.telegram_commands.cmd_fvg))
        self.telegram_app.add_handler(CommandHandler("status", self.telegram_commands.cmd_status))
        self.telegram_app.add_handler(CommandHandler("positions", self.telegram_commands.cmd_positions))
        self.telegram_app.add_handler(CommandHandler("levels", self.telegram_commands.cmd_levels))  # NUEVO
        
        logger.info("✅ Comandos de Telegram configurados")
    
    async def _run_telegram_bot(self):
        """Ejecuta el bot de Telegram con manejo robusto de webhooks"""
        if self.telegram_app is None:
            logger.warning("⚠️ Bot de Telegram no configurado (falta token)")
            return
        
        max_retries = 3
        retry_count = 0
        
        while retry_count < max_retries:
            try:
                logger.info("📱 Inicializando bot de Telegram...")
                await self.telegram_app.initialize()
                
                logger.info("📱 Iniciando bot de Telegram...")
                await self.telegram_app.start()
                
                # PASO CRÍTICO: LIMPIAR WEBHOOKS MÚLTIPLES VECES CON REINTENTOS
                logger.info("🧹 Limpiando webhooks existentes...")
                webhook_found = False
                for attempt in range(3):
                    try:
                        logger.info(f"   Intento {attempt + 1}/3 de eliminar webhook...")
                        
                        # Verificar primero si hay webhook activo
                        try:
                            webhook_info = await self.telegram_app.bot.get_webhook_info()
                            if webhook_info and webhook_info.url:
                                logger.warning(f"   ⚠️ Webhook encontrado: {webhook_info.url}")
                                webhook_found = True
                        except Exception as e:
                            logger.debug(f"   No se pudo verificar webhook: {e}")
                        
                        # Eliminar webhook
                        await self.telegram_app.bot.delete_webhook(drop_pending_updates=True)
                        logger.info("   ✅ Webhook eliminado exitosamente")
                        await asyncio.sleep(0.5)  # Pequeña pausa entre intentos
                    except Exception as webhook_error:
                        logger.warning(f"   Intento {attempt + 1}: {webhook_error}")
                        await asyncio.sleep(0.5)
                
                # Verificación final
                try:
                    webhook_info = await self.telegram_app.bot.get_webhook_info()
                    if webhook_info and webhook_info.url:
                        logger.error(f"❌ Webhook aún activo después de eliminación: {webhook_info.url}")
                        logger.info("🧹 Intentando última eliminación...")
                        await self.telegram_app.bot.delete_webhook(drop_pending_updates=True)
                        await asyncio.sleep(2)
                    else:
                        logger.info("✅ No hay webhooks activos")
                except Exception as e:
                    logger.debug(f"No se pudo verificar estado final de webhook: {e}")
                
                logger.info("📱 Iniciando polling de Telegram...")
                await self.telegram_app.updater.start_polling(
                    drop_pending_updates=True,
                    allowed_updates=['message', 'callback_query'],
                    poll_interval=0.3
                )
                
                logger.info("✅ Bot de Telegram escuchando comandos")
                logger.info("📋 Comandos disponibles: /start, /help, /fvg, /status, /positions, /levels")
                
                # Mantener el bot corriendo con monitoreo de webhook
                webhook_check_counter = 0
                last_webhook_cleanup = None
                
                while True:
                    await asyncio.sleep(1)
                    
                    # Cada 30 segundos, verificar que no haya webhook activo
                    webhook_check_counter += 1
                    if webhook_check_counter >= 30:
                        webhook_check_counter = 0
                        try:
                            webhook_info = await self.telegram_app.bot.get_webhook_info()
                            if webhook_info and webhook_info.url:
                                logger.warning(f"⚠️ WEBHOOK ACTIVO DETECTADO: {webhook_info.url}")
                                
                                # Evitar spam de limpiezas - máximo una cada 5 minutos
                                now = datetime.now()
                                if last_webhook_cleanup is None or (now - last_webhook_cleanup).total_seconds() > 300:
                                    logger.info("🧹 LIMPIANDO WEBHOOK DETECTADO...")
                                    
                                    # Eliminar webhook
                                    for attempt in range(3):
                                        try:
                                            await self.telegram_app.bot.delete_webhook(drop_pending_updates=True)
                                            logger.info(f"   ✅ Webhook eliminado (intento {attempt + 1})")
                                            await asyncio.sleep(1)
                                            break
                                        except Exception as e:
                                            logger.warning(f"   Intento {attempt + 1} falló: {e}")
                                            await asyncio.sleep(0.5)
                                    
                                    # Notificar al usuario
                                    await self.telegram.send_webhook_cleanup()
                                    
                                    # Actualizar timestamp
                                    last_webhook_cleanup = now
                                    
                                    # Escanear por nuevas posiciones después de limpiar webhook
                                    logger.info("📍 Escaneando entradas después de limpiar webhook...")
                                    await self._scan_for_entries()
                                else:
                                    time_since_last_cleanup = (now - last_webhook_cleanup).total_seconds()
                                    logger.debug(f"Limpieza de webhook en cooldown ({time_since_last_cleanup:.0f}s de 300s)")
                        except Exception as e:
                            logger.debug(f"Error verificando webhook periódicamente: {e}")
                
            except Exception as e:
                retry_count += 1
                logger.error(f"❌ Error en bot de Telegram (intento {retry_count}/{max_retries}): {e}", exc_info=True)
                
                if retry_count < max_retries:
                    wait_time = 5 * retry_count  # 5s, 10s, 15s
                    logger.info(f"⏳ Reintentando en {wait_time} segundos...")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error("❌ No se pudo inicializar Telegram después de múltiples intentos")
                    break
    
    async def _send_startup_notification(self):
        """Envía notificación de inicio después de que todo esté listo"""
        await asyncio.sleep(3)  # Esperar 3 segundos para que Telegram esté listo
        await self.telegram.send_startup_message(self.symbols)
    
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
                
                # NUEVO: Detectar niveles iniciales
                self._update_levels(symbol, df)
        
        logger.info("✅ Datos inicializados correctamente")
    
    def _update_levels(self, symbol: str, df):
        """Actualiza los niveles de soporte/resistencia para un símbolo"""
        try:
            # Obtener precio actual
            current_price = float(df['close'].iloc[-1])
            
            # Detectar niveles
            levels = self.level_detector.detect_all_levels(symbol, df, current_price)
            
            # Separar soportes y resistencias
            supports = [l for l in levels if l.price < current_price]
            resistances = [l for l in levels if l.price > current_price]
            
            # Guardar en cache
            self.levels_cache[symbol] = {
                'support': supports,
                'resistance': resistances,
                'all_levels': levels,
                'timestamp': datetime.now()
            }
            
            logger.info(
                f"📊 {symbol} - Niveles: "
                f"{len(supports)} soportes, "
                f"{len(resistances)} resistencias"
            )
            
        except Exception as e:
            logger.error(f"Error actualizando niveles para {symbol}: {e}")
    
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
                    
                    # NUEVO: Actualizar niveles
                    self._update_levels(symbol, df)
                
            except Exception as e:
                logger.error(f"Error actualizando {symbol}: {e}")
    
    async def _manage_open_positions(self):
        """Gestiona todas las posiciones abiertas usando precio actual del ticker"""
        config = load_config()
        
        for symbol in list(self.execution.open_positions.keys()):
            try:
                position = self.execution.open_positions[symbol]
                df = self.market_data.get_dataframe(symbol, '15m')
                
                if df is None or df.empty:
                    continue
                
                current_candle = df.iloc[-1]
                
                # CRÍTICO: Obtener precio actual del ticker (tiempo real)
                try:
                    ticker = await self.public_client.fetch_ticker(symbol)
                    current_price = float(ticker['last'])
                    logger.debug(f"💰 {symbol}: Precio actual = ${current_price:.4f}")
                except Exception as e:
                    logger.warning(f"Error obteniendo ticker de {symbol}: {e}")
                    # Fallback: usar close de la vela actual
                    current_price = float(current_candle['close'])
                
                # Guardar el timestamp de la última vela
                if not hasattr(self, 'last_candle_time'):
                    self.last_candle_time = {}

                # Solo incrementar si es una nueva vela
                current_candle_time = df.index[-1]
                if self.last_candle_time.get(symbol) != current_candle_time:
                    self.candles_in_trade[symbol] = self.candles_in_trade.get(symbol, 0) + 1
                    self.last_candle_time[symbol] = current_candle_time
                
                # 1. Verificar condiciones de salida (SL, TP, Time Limit)
                # NOTA: check_exit_conditions debe usar current_price para verificar SL/TP
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
                        # REGISTRAR TIEMPO DE CIERRE para cooldown
                        self.last_close_time[symbol] = datetime.now()
                        await self.telegram.send_trade_closed(trade_log)
                    
                    continue
                
                # 2. Actualizar Breakeven (usa precio actual del ticker)
                breakeven_sl = self.risk_manager.check_breakeven(
                    position=position,
                    current_price=current_price
                )
                
                if breakeven_sl:
                    old_sl = position.stop_loss
                    await self.execution.update_stop_loss(position, breakeven_sl)
                    await self.telegram.send_sl_updated(symbol, old_sl, breakeven_sl)
                
                # 3. Actualizar Trailing Stop SOLO si está habilitado para este símbolo
                symbol_key = symbol.replace('/', '')  # ETH/USDT -> ETHUSDT
                symbol_config = config.get('symbol_configs', {}).get(symbol_key, {})
                enable_structural_stop = symbol_config.get('enable_structural_stop', True)
                
                if enable_structural_stop:
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
                        logger.debug(f"🔄 {symbol}: Trailing stop estructural activado")
                else:
                    logger.debug(f"⏸️ {symbol}: Trailing stop estructural deshabilitado en config")
                
            except Exception as e:
                logger.error(f"Error gestionando posición {symbol}: {e}", exc_info=True)
    
    async def _scan_for_entries(self):
        """Busca nuevas entradas respetando límite de concurrencia"""
        if len(self.execution.open_positions) >= self.max_concurrent:
            return
        
        for symbol in self.symbols:
            if len(self.execution.open_positions) >= self.max_concurrent:
                break
            
            # EVITAR DUPLICADOS: No abrir si ya hay posición en este símbolo
            if symbol in self.execution.open_positions:
                continue
            
            # EVITAR DUPLICADOS: Verificar cooldown desde último cierre
            last_close = self.last_close_time.get(symbol)
            if last_close:
                time_since_close = datetime.now() - last_close
                if time_since_close.total_seconds() < (self.trade_cooldown_minutes * 60):
                    logger.debug(f"⏳ {symbol} en cooldown desde cierre ({time_since_close.total_seconds():.0f}s)")
                    continue
            
            try:
                df = self.market_data.get_dataframe(symbol, '15m')
                
                if df is None or df.empty or len(df) < 50:
                    continue
                
                # CRÍTICO: Obtener precio actual del ticker (tiempo real)
                try:
                    ticker = self.public_client.fetch_ticker(symbol)
                    current_price = float(ticker['last'])
                    logger.debug(f"💰 {symbol}: Precio actual del ticker = ${current_price:.2f}")
                except Exception as e:
                    logger.warning(f"Error obteniendo ticker de {symbol}: {e}")
                    # Fallback: usar close de la vela actual
                    current_price = float(df['close'].iloc[-1])
                    logger.debug(f"💰 {symbol}: Usando close de vela = ${current_price:.2f}")
                
                # NUEVO: Obtener niveles actuales
                levels = self.levels_cache.get(symbol, {})
                
                # PRIORIDAD 1: FVG Memory Long
                setup = self.strategy.check_fvg_memory_setup(df, 'LONG', current_price=current_price)
                setup = self.strategy.validate_setup(df, setup, levels=levels)
                
                if setup:
                    await self._execute_setup(symbol, setup)
                    continue
                
                # PRIORIDAD 2: FVG Memory Short
                setup = self.strategy.check_fvg_memory_setup(df, 'SHORT', current_price=current_price)
                setup = self.strategy.validate_setup(df, setup, levels=levels)
                
                if setup:
                    await self._execute_setup(symbol, setup)
                    continue
                
                # PRIORIDAD 3: Sweep Long
                setup = self.strategy.check_sweep_setup(df, 'LONG', current_price=current_price)
                setup = self.strategy.validate_setup(df, setup, levels=levels)
                
                if setup:
                    await self._execute_setup(symbol, setup)
                    continue
                
                # PRIORIDAD 4: Sweep Short
                setup = self.strategy.check_sweep_setup(df, 'SHORT', current_price=current_price)
                setup = self.strategy.validate_setup(df, setup, levels=levels)
                
                if setup:
                    await self._execute_setup(symbol, setup)
                    continue
                
            except Exception as e:
                logger.error(f"Error escaneando {symbol}: {e}", exc_info=True)
                
    async def _execute_setup(self, symbol: str, setup: Dict):
        """Ejecuta un setup validado con TP dinámico (mínimo 15% ganancia sobre margen)"""
        leverage = self.leverage_per_symbol.get(
            symbol.replace('/', ''), 
            15
        )
        
        # Obtener DataFrame para análisis de pools
        df = self.market_data.get_dataframe(symbol, '15m')
        
        # Calcular tamaño estándar de posición
        size_result = self.risk_manager.calculate_position_size(
            balance=self.balance,
            leverage=leverage,
            entry_price=setup['entry_price'],
            stop_loss=setup['stop_loss'],
            direction=setup['direction']
        )
        
        if not size_result.get('valid', False):
            logger.warning(f"Tamaño de posición inválido para {symbol}")
            return
        
        # Calcular TP dinámico (mínimo 15% ganancia sobre margen + ajuste por pools)
        tp_result = self.risk_manager.calculate_dynamic_take_profit(
            entry_price=setup['entry_price'],
            stop_loss=setup['stop_loss'],
            direction=setup['direction'],
            df=df,
            position_size=size_result['size_base']  # Pasar tamaño real para cálculo de margen
        )
        
        # Actualizar setup con valores calculados
        setup['size_base'] = size_result['size_base']
        setup['size_usd'] = size_result['size_usd']
        setup['take_profit'] = tp_result['take_profit']
        
        # Logging de decisión dinámica
        pool_info = tp_result.get('pool_info', {})
        gain_pct = tp_result.get('gain_pct', 0)
        
        if pool_info.get('pool_distance_pct', 0) > 2.0:
            logger.info(
                f" {symbol}: Pool lejano ({pool_info['pool_distance_pct']:.2f}%). "
                f"TP aumentado: ${tp_result['take_profit']:.4f} "
                f"(ganancia: {gain_pct:.1f}% sobre margen, multiplicador: {tp_result['dynamic_multiplier']}x)"
            )
        else:
            logger.info(
                f" {symbol}: Pool cercano ({pool_info.get('pool_distance_pct', 0):.2f}%). "
                f"TP estándar: ${tp_result['take_profit']:.4f} "
                f"(ganancia: {gain_pct:.1f}% sobre margen)"
            )
        
        # Verificar mínimo 15% de ganancia sobre margen
        if gain_pct >= 15.0:
            logger.info(f" {symbol}: TP cumple mínimo 15% ganancia sobre margen ({gain_pct:.1f}%)")
        else:
            logger.info(f" {symbol}: TP ajustado al mínimo 15% ganancia sobre margen")
        
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
    config_path = os.path.join(script_dir, "config", 'cofigETHBTC.json')
    
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