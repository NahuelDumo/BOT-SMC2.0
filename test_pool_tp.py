#!/usr/bin/env python3
"""
Test script para verificar el cálculo de TP con pools de liquidez
"""

import sys
import pandas as pd
import numpy as np
from datetime import datetime
from core.risk_management import RiskManager

def create_test_dataframe():
    """Crea un DataFrame de prueba con datos similares a los reales"""
    np.random.seed(42)
    
    # Generar 200 velas de 15m (simulando 50 horas)
    periods = 200
    timestamps = pd.date_range(start='2024-01-01', periods=periods, freq='15min')
    
    # Simular precio ETH con tendencia y volatilidad
    base_price = 3000
    price_changes = np.random.normal(0, 0.002, periods)  # 0.2% volatilidad
    prices = [base_price]
    
    for change in price_changes[1:]:
        new_price = prices[-1] * (1 + change)
        prices.append(new_price)
    
    # Crear OHLC
    df = pd.DataFrame({
        'timestamp': timestamps,
        'open': prices,
        'high': [p * (1 + abs(np.random.normal(0, 0.001))) for p in prices],
        'low': [p * (1 - abs(np.random.normal(0, 0.001))) for p in prices],
        'close': prices,
        'volume': np.random.uniform(1000, 5000, periods)
    })
    
    # Añadir algunos swings por encima del entry para LONG
    df.loc[180:185, 'high'] = 3040  # Swing high por encima del entry
    df.loc[170:175, 'high'] = 3035  # Otro swing high
    
    # Crear columnas de swings
    df['max'] = np.nan
    df['min'] = np.nan
    df.loc[52, 'max'] = 3050  # Swing high (lejos)
    df.loc[102, 'min'] = 2950  # Swing low
    df.loc[152, 'max'] = 3080  # Swing high (lejos)
    df.loc[182, 'max'] = 3040  # Swing high cerca del entry
    df.loc[172, 'max'] = 3035  # Swing high cerca del entry
    
    # Añadir ATR simplificado
    df['atr'] = df['high'] - df['low']
    df['atr'] = df['atr'].rolling(14).mean()
    
    # Añadir algunos FVGs
    df['fvg_bull_high'] = np.nan
    df['fvg_bear_low'] = np.nan
    df.loc[75, 'fvg_bull_high'] = 3020
    df.loc[125, 'fvg_bear_low'] = 2980
    
    return df

def test_pool_calculation():
    """Prueba el cálculo de TP con pools"""
    print("🧪 Test: Cálculo de TP con Pools de Liquidez")
    print("=" * 50)
    
    # Crear RiskManager con mismos parámetros que el backtest
    risk_manager = RiskManager(
        risk_per_trade_pct=1.0,
        min_risk_as_pct=0.1,
        risk_reward_ratio=2.0,
        pool_lookback_bars=192,
        equal_tol=0.0003
    )
    
    # Crear datos de prueba
    df = create_test_dataframe()
    
    # Configurar trade de prueba (LONG)
    entry_price = 3000.0
    stop_loss = 2985.0  # 15 puntos de riesgo
    direction = 'LONG'
    
    print(f"📊 Configuración del trade:")
    print(f"   - Entry Price: ${entry_price:.2f}")
    print(f"   - Stop Loss: ${stop_loss:.2f}")
    print(f"   - Direction: {direction}")
    print(f"   - Riesgo: ${entry_price - stop_loss:.2f}")
    print()
    
    # Método 1: TP tradicional (R:R)
    tp_traditional = risk_manager.calculate_take_profit(
        entry_price=entry_price,
        stop_loss=stop_loss,
        direction=direction
    )
    
    print(f"🎯 TP Tradicional (R:R 2:1): ${tp_traditional:.2f}")
    print(f"   - Ganancia potencial: ${tp_traditional - entry_price:.2f}")
    print()
    
    # Método 2: TP con pools de liquidez
    tp_with_pools = risk_manager.calculate_take_profit_with_pools(
        entry_price=entry_price,
        stop_loss=stop_loss,
        direction=direction,
        df_candles=df,
        atr=df['atr'].iloc[-1]
    )
    
    print(f"🎯 TP con Pools de Liquidez: ${tp_with_pools:.2f}")
    print(f"   - Ganancia potencial: ${tp_with_pools - entry_price:.2f}")
    print()
    
    # Analizar pools encontrados
    pools = risk_manager.build_liquidity_pools(df)
    print(f"🔍 Pools de liquidez detectados:")
    for i, pool in enumerate(pools[:5]):  # Top 5 pools
        print(f"   {i+1}. Precio: ${pool['price']:.2f} | Score: {pool['score']:.1f}")
    print()
    
    # Comparación
    if tp_with_pools != tp_traditional:
        diff = abs(tp_with_pools - tp_traditional)
        print(f"✅ Se encontró un pool de liquidez diferente al TP tradicional")
        print(f"   - Diferencia: ${diff:.2f}")
        print(f"   - Mejora: {((tp_with_pools - tp_traditional) / (tp_traditional - entry_price) * 100):+.1f}% en R:R")
    else:
        print("⚠️  No se encontraron pools mejores que el TP tradicional")
    
    print()
    print("🧪 Test: Configuración SHORT")
    print("=" * 30)
    
    # Probar configuración SHORT
    entry_price_short = 3000.0
    stop_loss_short = 3015.0  # 15 puntos de riesgo
    direction_short = 'SHORT'
    
    tp_short_traditional = risk_manager.calculate_take_profit(
        entry_price=entry_price_short,
        stop_loss=stop_loss_short,
        direction=direction_short
    )
    
    tp_short_pools = risk_manager.calculate_take_profit_with_pools(
        entry_price=entry_price_short,
        stop_loss=stop_loss_short,
        direction=direction_short,
        df_candles=df,
        atr=df['atr'].iloc[-1]
    )
    
    print(f"SHORT TP Tradicional: ${tp_short_traditional:.2f}")
    print(f"SHORT TP con Pools: ${tp_short_pools:.2f}")
    
    if tp_short_pools != tp_short_traditional:
        diff = abs(tp_short_pools - tp_short_traditional)
        print(f"✅ Diferencia en SHORT: ${diff:.2f}")
    else:
        print("⚠️  No se encontraron pools mejores para SHORT")

if __name__ == "__main__":
    try:
        test_pool_calculation()
        print("\n✅ Test completado exitosamente")
    except Exception as e:
        print(f"\n❌ Error en el test: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
