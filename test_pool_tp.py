"""
Script de prueba para verificar la conexión con Telegram
"""

import asyncio
import json
import logging
from telegram import Bot
from telegram.ext import Application, CommandHandler

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def cmd_test(update, context):
    """Comando de prueba"""
    logger.info(f"✅ Comando recibido de {update.effective_user.username}")
    await update.message.reply_text("✅ Bot funcionando correctamente!")


async def test_telegram():
    """Prueba la configuración de Telegram"""
    
    # Cargar configuración
    try:
        with open('cofigETHBTC.json', 'r') as f:
            config = json.load(f)
    except Exception as e:
        logger.error(f"Error cargando config: {e}")
        return
    
    token = config.get('telegram_token')
    chat_id = config.get('telegram_chat_id')
    
    if not token:
        logger.error("❌ No hay token de Telegram en la configuración")
        return
    
    logger.info(f"📱 Token encontrado: {token[:10]}...")
    logger.info(f"📱 Chat ID: {chat_id}")
    
    # Probar envío de mensaje
    try:
        logger.info("📤 Enviando mensaje de prueba...")
        bot = Bot(token=token)
        await bot.send_message(
            chat_id=chat_id,
            text="✅ <b>TEST</b>\n\nConexión con Telegram funcionando correctamente.",
            parse_mode='HTML'
        )
        logger.info("✅ Mensaje enviado correctamente")
    except Exception as e:
        logger.error(f"❌ Error enviando mensaje: {e}")
        return
    
    # Probar recepción de comandos
    try:
        logger.info("📱 Iniciando bot para recibir comandos...")
        app = Application.builder().token(token).build()
        
        # Registrar comando de prueba
        app.add_handler(CommandHandler("test", cmd_test))
        
        logger.info("✅ Comando /test registrado")
        
        # Iniciar bot
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        
        logger.info("✅ Bot escuchando comandos")
        logger.info("📋 Envía /test al bot en Telegram")
        logger.info("⏸️  Presiona Ctrl+C para detener")
        
        # Mantener el bot corriendo
        while True:
            await asyncio.sleep(1)
            
    except KeyboardInterrupt:
        logger.info("🛑 Deteniendo bot de prueba...")
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)


if __name__ == '__main__':
    asyncio.run(test_telegram())