"""
Módulo de Handlers de Telegram
Gestión de comandos y notificaciones
"""

import logging
from typing import List, Dict
from telegram import Bot
from telegram.error import TelegramError

logger = logging.getLogger(__name__)


class TelegramHandler:
    """Gestiona la comunicación con Telegram"""
    
    def __init__(self, token: str, chat_id: str):
        self.bot = Bot(token=token)
        self.chat_id = chat_id
    
    async def send_message(self, text: str, parse_mode: str = 'HTML'):
        """Envía un mensaje a Telegram"""
        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=parse_mode
            )
        except TelegramError as e:
            logger.error(f"Error enviando mensaje de Telegram: {e}")
    
    async def send_trade_opened(self, position):
        """Notifica apertura de posición"""
        try:
            leverage = int(position.size_usd / position.margin_used) if position.margin_used > 0 else 0
            
            message = (
                f"🟢 <b>POSICIÓN ABIERTA - {position.symbol}</b> 🟢\n\n"
                f"📈 Dirección: <b>{position.direction}</b>\n"
                f"💰 Precio Entrada: ${position.entry_price:,.4f}\n"
                f"📊 Tamaño: {position.size_base:.4f} ({position.size_usd:,.2f} USD)\n"
                f"🛑 Stop Loss: ${position.stop_loss:,.4f}\n"
                f"🎯 Take Profit: ${position.take_profit:,.4f}\n"
                f"🔧 Setup: {position.setup_type}\n"
                f"⚖️ Apalancamiento: {leverage}x"
            )
            await self.send_message(message)
        except Exception as e:
            logger.error(f"Error enviando notificación de apertura: {e}")
    
    async def send_trade_closed(self, trade_log: Dict):
        """Notifica cierre de posición"""
        try:
            pnl_emoji = "✅ WIN" if trade_log['pnl'] >= 0 else "❌ LOSS"
            
            message = (
                f"<b>POSICIÓN CERRADA - {trade_log['symbol']}</b> {pnl_emoji}\n\n"
                f"📉 Dirección: {trade_log['direction']}\n"
                f"💰 Precio Entrada: ${trade_log['entry_price']:,.4f}\n"
                f"💰 Precio Salida: ${trade_log['exit_price']:,.4f}\n"
                f"💵 PnL: <b>${trade_log['pnl']:,.2f}</b> ({trade_log['pnl_pct']:,.2f}%)\n"
                f"📜 Razón: {trade_log['exit_reason']}\n"
                f"🔧 Setup: {trade_log.get('setup_type', 'UNKNOWN')}"
            )
            await self.send_message(message)
        except Exception as e:
            logger.error(f"Error enviando notificación de cierre: {e}")
    
    async def send_sl_updated(self, symbol: str, old_sl: float, new_sl: float):
        """Notifica actualización de SL"""
        try:
            message = (
                f"🔔 <b>SL AJUSTADO: {symbol}</b>\n\n"
                f"Anterior: ${old_sl:,.4f}\n"
                f"Nuevo: ${new_sl:,.4f}"
            )
            await self.send_message(message)
        except Exception as e:
            logger.error(f"Error enviando actualización de SL: {e}")
    
    async def send_fvg_report(
        self, 
        symbols_data: Dict[str, Dict],
        current_prices: Dict[str, float]
    ):
        """Envía reporte de FVGs operables"""
        try:
            message = "🔍 <b>FVGs Operables (Soporte/Resistencia)</b>\n\n"
            found_any = False
            
            for symbol, data in symbols_data.items():
                message += f"<b>--- {symbol} ---</b>\n"
                
                df = data.get('df')
                if df is None or df.empty or 'is_mitigated' not in df.columns:
                    message += "  (Datos aún no disponibles)\n\n"
                    continue
                
                current_price = current_prices.get(symbol, 0)
                if current_price == 0:
                    message += "  (Precio actual no disponible)\n\n"
                    continue
                
                # Solo velas cerradas
                df_closed = df.iloc[:-1]
                
                # FVGs Alcistas (Soporte)
                bull_fvgs_raw = df_closed[
                    (df_closed['is_fvg_bullish'] == True) & 
                    (df_closed['is_mitigated'] == False)
                ]
                bull_fvgs = bull_fvgs_raw[bull_fvgs_raw['fvg_bull_high'] <= current_price]
                
                if not bull_fvgs.empty:
                    found_any = True
                    message += "🟢 <b>FVGs Alcistas (Long - SOPORTE):</b>\n"
                    for _, row in bull_fvgs.sort_values(by='fvg_bull_high', ascending=False).head(5).iterrows():
                        message += (
                            f"  - ${row['fvg_bull_low']:,.4f} - ${row['fvg_bull_high']:,.4f} "
                            f"(Mid: ${row['fvg_bull_mid']:,.4f})\n"
                        )
                else:
                    message += "🟢 <b>FVGs Alcistas (Long - SOPORTE):</b> (Ninguno)\n"
                
                # FVGs Bajistas (Resistencia)
                bear_fvgs_raw = df_closed[
                    (df_closed['is_fvg_bearish'] == True) & 
                    (df_closed['is_mitigated'] == False)
                ]
                bear_fvgs = bear_fvgs_raw[bear_fvgs_raw['fvg_bear_low'] >= current_price]
                
                if not bear_fvgs.empty:
                    found_any = True
                    message += "🔴 <b>FVGs Bajistas (Short - RESISTENCIA):</b>\n"
                    for _, row in bear_fvgs.sort_values(by='fvg_bear_low', ascending=True).head(5).iterrows():
                        message += (
                            f"  - ${row['fvg_bear_low']:,.4f} - ${row['fvg_bear_high']:,.4f} "
                            f"(Mid: ${row['fvg_bear_mid']:,.4f})\n"
                        )
                else:
                    message += "🔴 <b>FVGs Bajistas (Short - RESISTENCIA):</b> (Ninguno)\n"
                
                # Estado MTF
                macd_1h = data.get('macd_1h')
                if macd_1h is not None:
                    trend = "⬆️ ALCISTA" if macd_1h > 1e-6 else ("⬇️ BAJISTA" if macd_1h < -1e-6 else "Neutral")
                    message += f"⏱️ <b>Tendencia 1H (MACD):</b> {trend} ({macd_1h:,.2f})\n"
                
                message += "\n"
            
            if not found_any:
                message = "ℹ️ No se encontraron FVGs operables en ningún símbolo."
            
            await self.send_message(message)
        except Exception as e:
            logger.error(f"Error enviando reporte FVG: {e}")
    
    async def send_status_report(
        self,
        balance: float,
        open_positions: Dict,
        total_pnl: float
    ):
        """Envía reporte de estado del bot"""
        try:
            message = (
                f"📊 <b>ESTADO DEL BOT</b>\n\n"
                f"💰 Balance: ${balance:,.2f}\n"
                f"💼 Posiciones Abiertas: {len(open_positions)}\n"
                f"📈 PnL Total Sesión: ${total_pnl:,.2f}\n\n"
            )
            
            if open_positions:
                message += "<b>Posiciones Activas:</b>\n"
                for symbol, pos in open_positions.items():
                    message += (
                        f"  • {symbol} ({pos.direction}): "
                        f"${pos.entry_price:,.4f}\n"
                    )
            
            await self.send_message(message)
        except Exception as e:
            logger.error(f"Error enviando reporte de estado: {e}")
    
    async def send_startup_message(self, symbols: List[str]):
        """Envía mensaje de inicio del bot"""
        try:
            message = (
                "✅ <b>SmartMoneyLiveBot ha iniciado</b>\n\n"
                f"📊 Símbolos monitoreados: {', '.join(symbols)}\n"
                f"⏰ Actualización: Cada 10 segundos\n\n"
                "<b>Comandos disponibles:</b>\n"
                "/fvg - Ver FVGs operables\n"
                "/status - Estado del bot\n"
                "/positions - Posiciones abiertas\n"
                "/help - Ayuda"
            )
            await self.send_message(message)
        except Exception as e:
            logger.error(f"Error enviando mensaje de inicio: {e}")
    
    async def send_error(self, error_msg: str):
        """Envía notificación de error"""
        try:
            message = f"🚨 <b>ERROR</b>\n\n{error_msg}"
            await self.send_message(message)
        except Exception as e:
            logger.error(f"Error enviando notificación de error: {e}")