"""
Módulo de Comandos de Telegram
Gestiona los comandos del bot (/fvg, /status, /positions)
"""

import logging
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


class TelegramCommands:
    """Gestiona los comandos de Telegram del bot"""
    
    def __init__(self, bot_instance):
        """
        Args:
            bot_instance: Instancia del SmartMoneyLiveBot
        """
        self.bot = bot_instance
    
    async def cmd_fvg(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /fvg - Muestra FVGs operables"""
        logger.info(f"📱 Comando /fvg recibido de {update.effective_user.username}")
        try:
            await update.message.reply_text("🔍 Generando reporte de FVGs...")
            
            # Obtener datos de todos los símbolos
            symbols_data = {}
            current_prices = {}
            
            for symbol in self.bot.symbols:
                df = self.bot.market_data.get_dataframe(symbol, '15m')
                df_1h = self.bot.market_data.get_dataframe(symbol, '1h')
                
                if df is not None and not df.empty:
                    current_prices[symbol] = float(df['close'].iloc[-1])
                    
                    macd_1h = None
                    if df_1h is not None and not df_1h.empty and 'macd' in df_1h.columns:
                        macd_1h = float(df_1h['macd'].iloc[-1])
                    
                    symbols_data[symbol] = {
                        'df': df,
                        'macd_1h': macd_1h
                    }
            
            # Enviar reporte
            await self.bot.telegram.send_fvg_report(symbols_data, current_prices)
            logger.info("✅ Reporte FVG enviado")
            
        except Exception as e:
            logger.error(f"❌ Error en comando /fvg: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Error generando reporte: {str(e)}")
    
    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /status - Muestra estado del bot"""
        try:
            await update.message.reply_text("📊 Generando reporte de estado...")
            
            await self.bot.telegram.send_status_report(
                balance=self.bot.balance,
                open_positions=self.bot.execution.open_positions,
                total_pnl=self.bot.total_pnl
            )
            
        except Exception as e:
            logger.error(f"Error en comando /status: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Error: {str(e)}")
    
    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /positions - Muestra posiciones abiertas"""
        try:
            open_positions = self.bot.execution.open_positions
            
            if not open_positions:
                await update.message.reply_text("ℹ️ No hay posiciones abiertas actualmente.")
                return
            
            message = f"💼 <b>POSICIONES ABIERTAS ({len(open_positions)})</b>\n\n"
            
            for symbol, pos in open_positions.items():
                # Calcular PnL actual
                df = self.bot.market_data.get_dataframe(symbol, '15m')
                if df is not None and not df.empty:
                    current_price = float(df['close'].iloc[-1])
                    
                    if pos.direction == 'LONG':
                        unrealized_pnl = (current_price - pos.entry_price) * pos.size_base
                    else:
                        unrealized_pnl = (pos.entry_price - current_price) * pos.size_base
                    
                    pnl_emoji = "🟢" if unrealized_pnl >= 0 else "🔴"
                    
                    message += (
                        f"<b>{symbol}</b> ({pos.direction})\n"
                        f"  • Entrada: ${pos.entry_price:,.4f}\n"
                        f"  • Actual: ${current_price:,.4f}\n"
                        f"  • SL: ${pos.stop_loss:,.4f}\n"
                        f"  • TP: ${pos.take_profit:,.4f}\n"
                        f"  • {pnl_emoji} PnL: ${unrealized_pnl:,.2f}\n"
                        f"  • Setup: {pos.setup_type}\n\n"
                    )
            
            await update.message.reply_text(message, parse_mode='HTML')
            
        except Exception as e:
            logger.error(f"Error en comando /positions: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Error: {str(e)}")
    
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /help - Muestra ayuda"""
        message = (
            "📚 <b>COMANDOS DISPONIBLES</b>\n\n"
            "/fvg - Ver FVGs operables\n"
            "/status - Estado del bot y balance\n"
            "/positions - Posiciones abiertas\n"
            "/levels - Niveles históricos (soportes/resistencias)\n"
            "/help - Este mensaje de ayuda\n\n"
            "⏰ <b>Actualización:</b> Cada 10 segundos\n"
            "🔄 <b>FVGs:</b> Recalculados en cada ciclo\n"
            "📊 <b>Niveles:</b> ATH, ATL, zonas probadas"
        )
        await update.message.reply_text(message, parse_mode='HTML')
    
    async def cmd_levels(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /levels - Muestra niveles históricos"""
        logger.info(f"📱 Comando /levels recibido de {update.effective_user.username}")
        try:
            await update.message.reply_text("📊 Generando reporte de niveles históricos...")
            
            message = "📊 <b>NIVELES HISTÓRICOS</b>\n\n"
            
            for symbol in self.bot.symbols:
                df = self.bot.market_data.get_dataframe(symbol, '15m')
                if df is None or df.empty:
                    continue
                
                current_price = float(df['close'].iloc[-1])
                
                levels_data = self.bot.levels_cache.get(symbol, {})
                if not levels_data:
                    # Si no hay en cache, calcular al momento
                    try:
                        levels = self.bot.level_detector.detect_all_levels(symbol, df, current_price)
                        supports = [l for l in levels if l.price < current_price]
                        resistances = [l for l in levels if l.price > current_price]
                    except Exception as e:
                        logger.error(f"Error detectando niveles para {symbol}: {e}")
                        continue
                else:
                    supports = levels_data.get('support', [])
                    resistances = levels_data.get('resistance', [])
                
                message += f"<b>--- {symbol} ---</b>\n"
                message += f"💰 Precio Actual: ${current_price:,.2f}\n\n"
                
                # Resistencias
                if resistances:
                    message += "🔴 <b>RESISTENCIAS:</b>\n"
                    for level in resistances[:5]:  # Limitar a 5 más cercanas
                        distance_pct = ((level.price - current_price) / current_price) * 100
                        strength_emoji = "🔥" * min(getattr(level, 'strength', 3) // 3, 5)
                        
                        message += (
                            f"  {strength_emoji} ${level.price:,.2f} "
                            f"(+{distance_pct:.2f}%)\n"
                            f"      {getattr(level, 'description', 'Nivel histórico')}\n"
                            f"      Testeado: {getattr(level, 'strength', 3)}x\n"
                        )
                else:
                    message += "🔴 <b>RESISTENCIAS:</b> (Ninguna cercana)\n"
                
                message += "\n"
                
                # Soportes
                if supports:
                    message += "🟢 <b>SOPORTES:</b>\n"
                    for level in supports[:5]:  # Limitar a 5 más cercanos
                        distance_pct = ((current_price - level.price) / current_price) * 100
                        strength_emoji = "🔥" * min(getattr(level, 'strength', 3) // 3, 5)
                        
                        message += (
                            f"  {strength_emoji} ${level.price:,.2f} "
                            f"(-{distance_pct:.2f}%)\n"
                            f"      {getattr(level, 'description', 'Nivel histórico')}\n"
                            f"      Testeado: {getattr(level, 'strength', 3)}x\n"
                        )
                else:
                    message += "🟢 <b>SOPORTES:</b> (Ninguno cercano)\n"
                
                message += "\n"
            
            # Dividir mensaje si es muy largo
            if len(message) > 4000:
                parts = [message[i:i+4000] for i in range(0, len(message), 4000)]
                for part in parts:
                    await update.message.reply_text(part, parse_mode='HTML')
            else:
                await update.message.reply_text(message, parse_mode='HTML')
            
            logger.info("✅ Reporte de niveles enviado")
            
        except Exception as e:
            logger.error(f"❌ Error en /levels: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Error: {str(e)}")
    
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /start - Mensaje de bienvenida"""
        logger.info(f"📱 Comando /start recibido de {update.effective_user.username}")
        message = (
            "✅ <b>SmartMoneyLiveBot</b>\n\n"
            "Bot de trading SMC con detección automática de FVGs.\n\n"
            "Usa /help para ver los comandos disponibles."
        )
        await update.message.reply_text(message, parse_mode='HTML')
        logger.info("✅ Mensaje de bienvenida enviado")