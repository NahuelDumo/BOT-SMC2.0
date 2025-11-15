"""
Script de prueba para verificar la detección de FVG
Usa la lógica ORIGINAL que funcionaba correctamente
"""

import asyncio
import logging
import ccxt
import pandas as pd
import numpy as np
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class FVGTester:
    def __init__(self, symbol='SUIUSDT', timeframe='15m', candle_limit=300):
        self.symbol = symbol
        self.timeframe = timeframe
        self.candle_limit = candle_limit
        
        # Configurar ccxt para Binance Futures
        self.exchange = ccxt.binanceusdm({
            'enableRateLimit': True,
            'options': {'defaultType': 'future'}
        })
        
    async def fetch_data(self):
        """Descarga datos de mercado HASTA LA FECHA ACTUAL"""
        logger.info(f"📥 Descargando {self.candle_limit} velas de {self.symbol} ({self.timeframe})...")
        
        ccxt_symbol = self.symbol.replace('USDT', '/USDT')
        
        # Descargar datos SIN especificar 'since' para obtener las velas más recientes
        # Esto descargará las últimas N velas hasta el momento actual
        ohlcv = await asyncio.to_thread(
            self.exchange.fetch_ohlcv,
            ccxt_symbol, self.timeframe, limit=self.candle_limit
        )
        
        if not ohlcv:
            logger.error("No se pudieron descargar datos")
            return None
        
        # Crear DataFrame
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df = df.drop_duplicates(subset=['timestamp'], keep='last')
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert('Etc/GMT+3')
        df.set_index('timestamp', inplace=True)
        df = df.sort_index()
        df = df.astype(float)
        
        logger.info(f"✅ Descargadas {len(df)} velas")
        logger.info(f"📊 Rango: {df.index[0]} → {df.index[-1]}")
        logger.info(f"💰 Precio actual: ${df['close'].iloc[-1]:,.4f}")
        
        return df
    
    def detect_fvg_original(self, df):
        """
        Detección de FVG con la lógica ORIGINAL que funcionaba
        """
        logger.info("\n" + "="*60)
        logger.info("🔍 DETECTANDO FVG CON LÓGICA ORIGINAL")
        logger.info("="*60)
        
        # Separar velas cerradas de la vela actual
        df_closed = df.iloc[:-1].copy()
        df_current = df.iloc[[-1]].copy()
        
        logger.info(f"📊 Velas cerradas: {len(df_closed)}")
        logger.info(f"🕐 Vela actual: {df_current.index[0]}")
        
        # Inicializar columnas
        df_closed['is_fvg_bullish'] = False
        df_closed['is_fvg_bearish'] = False
        df_closed['fvg_bull_high'], df_closed['fvg_bull_low'] = np.nan, np.nan
        df_closed['fvg_bear_high'], df_closed['fvg_bear_low'] = np.nan, np.nan
        df_closed['fvg_bull_mid'] = np.nan
        df_closed['fvg_bear_mid'] = np.nan
        
        # Detectar FVG en velas cerradas
        # Patrón: [i-2], [i-1], [i]
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
        
        # Calcular mitigación (50% FVG)
        df_closed['is_mitigated'] = False
        
        bull_fvg_indices = df_closed[df_closed['is_fvg_bullish']].index
        bear_fvg_indices = df_closed[df_closed['is_fvg_bearish']].index
        
        for fvg_idx_time in bull_fvg_indices:
            fvg_row = df_closed.loc[fvg_idx_time]
            fvg_mid_price = fvg_row['fvg_bull_mid']
            fvg_iloc = df_closed.index.get_loc(fvg_idx_time)
            
            if fvg_iloc + 1 < len(df_closed):
                future_candles = df_closed.iloc[fvg_iloc + 1:]
                if (future_candles['low'] <= fvg_mid_price).any():
                    df_closed.loc[fvg_idx_time, 'is_mitigated'] = True
        
        for fvg_idx_time in bear_fvg_indices:
            fvg_row = df_closed.loc[fvg_idx_time]
            fvg_mid_price = fvg_row['fvg_bear_mid']
            fvg_iloc = df_closed.index.get_loc(fvg_idx_time)
            
            if fvg_iloc + 1 < len(df_closed):
                future_candles = df_closed.iloc[fvg_iloc + 1:]
                if (future_candles['high'] >= fvg_mid_price).any():
                    df_closed.loc[fvg_idx_time, 'is_mitigated'] = True
        
        return df_closed
    
    def print_fvg_report(self, df_closed):
        """Imprime reporte de FVG detectados"""
        logger.info("\n" + "="*60)
        logger.info("📋 REPORTE DE FVG")
        logger.info("="*60)
        
        # FVG Alcistas
        bull_fvgs = df_closed[
            (df_closed['is_fvg_bullish'] == True) & 
            (df_closed['is_mitigated'] == False)
        ]
        
        logger.info(f"\n🟢 FVG ALCISTAS NO MITIGADOS: {len(bull_fvgs)}")
        if not bull_fvgs.empty:
            logger.info("\nÚltimos 10 FVG alcistas (más recientes primero):")
            for idx, row in bull_fvgs.sort_index(ascending=False).head(10).iterrows():
                logger.info(
                    f"  📅 {idx.strftime('%Y-%m-%d %H:%M')} | "
                    f"${row['fvg_bull_low']:,.4f} - ${row['fvg_bull_high']:,.4f} | "
                    f"Mid: ${row['fvg_bull_mid']:,.4f}"
                )
        
        # FVG Bajistas
        bear_fvgs = df_closed[
            (df_closed['is_fvg_bearish'] == True) & 
            (df_closed['is_mitigated'] == False)
        ]
        
        logger.info(f"\n🔴 FVG BAJISTAS NO MITIGADOS: {len(bear_fvgs)}")
        if not bear_fvgs.empty:
            logger.info("\nÚltimos 10 FVG bajistas (más recientes primero):")
            for idx, row in bear_fvgs.sort_index(ascending=False).head(10).iterrows():
                logger.info(
                    f"  📅 {idx.strftime('%Y-%m-%d %H:%M')} | "
                    f"${row['fvg_bear_low']:,.4f} - ${row['fvg_bear_high']:,.4f} | "
                    f"Mid: ${row['fvg_bear_mid']:,.4f}"
                )
        
        # Estadísticas
        total_bull = len(df_closed[df_closed['is_fvg_bullish'] == True])
        total_bear = len(df_closed[df_closed['is_fvg_bearish'] == True])
        mitigated_bull = len(df_closed[(df_closed['is_fvg_bullish'] == True) & (df_closed['is_mitigated'] == True)])
        mitigated_bear = len(df_closed[(df_closed['is_fvg_bearish'] == True) & (df_closed['is_mitigated'] == True)])
        
        logger.info("\n" + "="*60)
        logger.info("📊 ESTADÍSTICAS")
        logger.info("="*60)
        logger.info(f"Total FVG Alcistas: {total_bull} (Mitigados: {mitigated_bull}, Activos: {total_bull - mitigated_bull})")
        logger.info(f"Total FVG Bajistas: {total_bear} (Mitigados: {mitigated_bear}, Activos: {total_bear - mitigated_bear})")
        logger.info("="*60 + "\n")


async def main():
    """Función principal"""
    tester = FVGTester(symbol='SUIUSDT', timeframe='15m', candle_limit=300)
    
    # Descargar datos
    df = await tester.fetch_data()
    if df is None:
        return
    
    # Detectar FVG con lógica original
    df_closed = tester.detect_fvg_original(df)
    
    # Imprimir reporte
    tester.print_fvg_report(df_closed)


if __name__ == "__main__":
    asyncio.run(main())
