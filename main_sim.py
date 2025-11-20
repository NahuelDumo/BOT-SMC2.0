import os
import sys
import json
import logging
import pandas as pd
from datetime import datetime

# Agregar la ruta del proyecto para importar módulos
sys.path.insert(0, os.path.dirname(__file__))

# Importar la clase del backtest
from backtest import MultiSymbolBacktest

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config', 'cofigETHBTC.json')
DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')

# Leer configuración
with open(CONFIG_PATH, 'r') as f:
    cfg = json.load(f)
    symbols = cfg.get('symbols', ['ETHUSDT', 'SOLUSDT', 'HYPEUSDT', 'SUIUSDT'])
    initial_balance = float(cfg.get('initial_balance', 30.0))
    symbol_configs = cfg.get('symbol_configs', {})

logger.info(f"Iniciando simulación con datos desde CSV")
logger.info(f"Símbolos: {symbols}")
logger.info(f"Balance Inicial: ${initial_balance}")
logger.info(f"Directorio de datos: {DATA_DIR}")

# Verificar que existan los archivos CSV
missing_files = []
for symbol in symbols:
    csv_15m = os.path.join(DATA_DIR, f"{symbol}_15m_130d.csv")
    csv_1h = os.path.join(DATA_DIR, f"{symbol}_1h_130d.csv")
    if not os.path.exists(csv_15m) or not os.path.exists(csv_1h):
        missing_files.append(symbol)

if missing_files:
    logger.error(f"❌ Archivos CSV faltantes para: {missing_files}")
    sys.exit(1)

logger.info(f"✅ Todos los archivos CSV encontrados")

# Crear instancia del backtest
backtest = MultiSymbolBacktest(
    symbols=symbols,
    initial_balance=initial_balance,
    symbol_configs=symbol_configs,
    use_csv_data=True,  # Flag para usar CSV en lugar de descargar
    csv_dir=DATA_DIR    # Directorio donde están los CSV
)

# Ejecutar el backtest (que actúa como simulación)
logger.info("Ejecutando simulación...")
backtest.run(days=130)

logger.info("✅ Simulación completada exitosamente")
