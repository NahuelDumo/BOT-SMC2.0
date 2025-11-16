"""
Módulo de Detección de Patrones SMC
Fair Value Gaps, Mitigación, y Swings Estructurales
"""

import logging
import pandas as pd
import numpy as np
from scipy.signal import argrelextrema
from typing import List, Dict

logger = logging.getLogger(__name__)


class PatternDetector:
    """Detecta patrones SMC (FVG, Mitigación, Swings)"""
    
    def __init__(self, structure_lookback: int = 20):
        self.structure_lookback = structure_lookback
    
    def detect_fvg_and_mitigation(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Detecta Fair Value Gaps y calcula mitigación cuando toca >50% del FVG.
        
        IMPORTANTE: Un FVG se considera mitigado cuando una vela:
        - Toca el 50% o más del gap (no solo el punto exacto)
        - Para FVG alcista: low <= 50% del gap
        - Para FVG bajista: high >= 50% del gap
        
        Args:
            df: DataFrame con datos OHLCV
            
        Returns:
            DataFrame con columnas de FVG y mitigación
        """
        if df.empty or len(df) < 3:
            return df
        
        df_copy = df.copy()
        
        # Inicializar columnas
        df_copy['is_fvg_bullish'] = False
        df_copy['is_fvg_bearish'] = False
        df_copy['is_mitigated'] = False
        df_copy['fvg_bull_high'] = np.nan
        df_copy['fvg_bull_low'] = np.nan
        df_copy['fvg_bull_mid'] = np.nan
        df_copy['fvg_bear_high'] = np.nan
        df_copy['fvg_bear_low'] = np.nan
        df_copy['fvg_bear_mid'] = np.nan
        
        # Arrays numpy para performance
        is_fvg_bullish_np = np.zeros(len(df_copy), dtype=bool)
        is_fvg_bearish_np = np.zeros(len(df_copy), dtype=bool)
        fvg_bull_low_np = np.full(len(df_copy), np.nan)
        fvg_bull_high_np = np.full(len(df_copy), np.nan)
        fvg_bull_mid_np = np.full(len(df_copy), np.nan)
        fvg_bear_low_np = np.full(len(df_copy), np.nan)
        fvg_bear_high_np = np.full(len(df_copy), np.nan)
        fvg_bear_mid_np = np.full(len(df_copy), np.nan)
        
        lows = df_copy['low'].values
        highs = df_copy['high'].values
        
        # Detectar FVGs
        for i in range(2, len(df_copy)):
            # FVG Alcista: Low[i] > High[i-2]
            if lows[i] > highs[i-2]:
                is_fvg_bullish_np[i-1] = True
                low_edge = highs[i-2]
                high_edge = lows[i]
                fvg_bull_low_np[i-1] = low_edge
                fvg_bull_high_np[i-1] = high_edge
                fvg_bull_mid_np[i-1] = low_edge + (high_edge - low_edge) * 0.5
            
            # FVG Bajista: High[i] < Low[i-2]
            if highs[i] < lows[i-2]:
                is_fvg_bearish_np[i-1] = True
                low_edge = highs[i]
                high_edge = lows[i-2]
                fvg_bear_low_np[i-1] = low_edge
                fvg_bear_high_np[i-1] = high_edge
                fvg_bear_mid_np[i-1] = low_edge + (high_edge - low_edge) * 0.5
        
        # Asignar arrays a DataFrame
        df_copy['is_fvg_bullish'] = is_fvg_bullish_np
        df_copy['is_fvg_bearish'] = is_fvg_bearish_np
        df_copy['fvg_bull_low'] = fvg_bull_low_np
        df_copy['fvg_bull_high'] = fvg_bull_high_np
        df_copy['fvg_bull_mid'] = fvg_bull_mid_np
        df_copy['fvg_bear_low'] = fvg_bear_low_np
        df_copy['fvg_bear_high'] = fvg_bear_high_np
        df_copy['fvg_bear_mid'] = fvg_bear_mid_np
        
        # Calcular mitigación (>50% del FVG)
        is_mitigated_np = np.zeros(len(df_copy), dtype=bool)
        
        bull_fvg_indices = df_copy.index[df_copy['is_fvg_bullish']]
        bear_fvg_indices = df_copy.index[df_copy['is_fvg_bearish']]
        
        # Mitigación de FVGs Alcistas (>50% del gap)
        for fvg_idx_time in bull_fvg_indices:
            fvg_iloc = df_copy.index.get_loc(fvg_idx_time)
            fvg_low = df_copy['fvg_bull_low'].iloc[fvg_iloc]
            fvg_high = df_copy['fvg_bull_high'].iloc[fvg_iloc]
            fvg_mid = df_copy['fvg_bull_mid'].iloc[fvg_iloc]
            
            if fvg_iloc + 1 < len(df_copy):
                # Verificar cada vela futura
                for j in range(fvg_iloc + 1, len(df_copy)):
                    candle_low = df_copy['low'].iloc[j]
                    candle_high = df_copy['high'].iloc[j]
                    
                    # FVG mitigado si la vela toca >=50% del gap
                    # Para FVG alcista: el low de la vela toca el 50% o más del gap
                    if candle_low <= fvg_mid:
                        # Calcular qué porcentaje del FVG fue tocado
                        touched_percentage = ((fvg_high - candle_low) / (fvg_high - fvg_low)) * 100
                        
                        if touched_percentage >= 50.0:  # Más del 50% tocado
                            is_mitigated_np[fvg_iloc] = True
                            logger.debug(
                                f"FVG alcista mitigado en índice {fvg_idx_time}: "
                                f"tocado {touched_percentage:.1f}% del gap "
                                f"(low: {candle_low:.4f} <= mid: {fvg_mid:.4f})"
                            )
                            break
        
        # Mitigación de FVGs Bajistas (>50% del gap)
        for fvg_idx_time in bear_fvg_indices:
            fvg_iloc = df_copy.index.get_loc(fvg_idx_time)
            fvg_low = df_copy['fvg_bear_low'].iloc[fvg_iloc]
            fvg_high = df_copy['fvg_bear_high'].iloc[fvg_iloc]
            fvg_mid = df_copy['fvg_bear_mid'].iloc[fvg_iloc]
            
            if fvg_iloc + 1 < len(df_copy):
                # Verificar cada vela futura
                for j in range(fvg_iloc + 1, len(df_copy)):
                    candle_low = df_copy['low'].iloc[j]
                    candle_high = df_copy['high'].iloc[j]
                    
                    # FVG mitigado si la vela toca >=50% del gap
                    # Para FVG bajista: el high de la vela toca el 50% o más del gap
                    if candle_high >= fvg_mid:
                        # Calcular qué porcentaje del FVG fue tocado
                        touched_percentage = ((candle_high - fvg_low) / (fvg_high - fvg_low)) * 100
                        
                        if touched_percentage >= 50.0:  # Más del 50% tocado
                            is_mitigated_np[fvg_iloc] = True
                            logger.debug(
                                f"FVG bajista mitigado en índice {fvg_idx_time}: "
                                f"tocado {touched_percentage:.1f}% del gap "
                                f"(high: {candle_high:.4f} >= mid: {fvg_mid:.4f})"
                            )
                            break
        
        df_copy['is_mitigated'] = is_mitigated_np
        
        return df_copy
    
    def detect_swings(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Detecta swings estructurales (highs y lows).
        
        Args:
            df: DataFrame con datos OHLCV
            
        Returns:
            DataFrame con columnas 'max' y 'min'
        """
        if df.empty or len(df) < self.structure_lookback * 2:
            return df
        
        df_copy = df.copy()
        n = self.structure_lookback
        
        # Detectar swing highs
        df_copy['max'] = df_copy.iloc[
            argrelextrema(
                df_copy['high'].values, 
                np.greater_equal, 
                order=n
            )[0]
        ]['high']
        
        # Detectar swing lows
        df_copy['min'] = df_copy.iloc[
            argrelextrema(
                df_copy['low'].values, 
                np.less_equal, 
                order=n
            )[0]
        ]['low']
        
        return df_copy
    
    def check_fvg_proximity(
        self,
        df: pd.DataFrame,
        direction: str,
        max_proximity_pct: float = 30.0
    ) -> List[Dict]:
        """
        Verifica si el precio actual está cerca de un FVG no mitigado.
        
        Args:
            df: DataFrame con FVGs detectados
            direction: 'LONG' o 'SHORT'
            max_proximity_pct: Máxima distancia porcentual para considerar "cerca"
            
        Returns:
            Lista de FVGs cercanos no mitigados
        """
        if df.empty or len(df) < 3:
            return []
        
        current_price = float(df['close'].iloc[-1])
        nearby_fvgs = []
        
        if direction == 'LONG':
            # Buscar FVGs alcistas no mitigados
            bull_fvgs = df[df['is_fvg_bullish'] & ~df['is_mitigated']]
            
            for idx, row in bull_fvgs.iterrows():
                fvg_low = row['fvg_bull_low']
                fvg_high = row['fvg_bull_high']
                fvg_mid = row['fvg_bull_mid']
                
                # Verificar si estamos cerca del FVG
                if current_price <= fvg_high and current_price >= fvg_low:
                    # Estamos dentro del FVG
                    proximity_pct = 0.0
                elif current_price > fvg_high:
                    # Estamos por encima, calcular distancia
                    proximity_pct = ((current_price - fvg_high) / fvg_high) * 100
                else:
                    # Estamos por debajo, calcular distancia
                    proximity_pct = ((fvg_low - current_price) / fvg_low) * 100
                
                if proximity_pct <= max_proximity_pct:
                    nearby_fvgs.append({
                        'index': idx,
                        'type': 'bullish',
                        'low': fvg_low,
                        'high': fvg_high,
                        'mid': fvg_mid,
                        'proximity_pct': proximity_pct,
                        'current_price': current_price
                    })
        
        else:  # SHORT
            # Buscar FVGs bajistas no mitigados
            bear_fvgs = df[df['is_fvg_bearish'] & ~df['is_mitigated']]
            
            for idx, row in bear_fvgs.iterrows():
                fvg_low = row['fvg_bear_low']
                fvg_high = row['fvg_bear_high']
                fvg_mid = row['fvg_bear_mid']
                
                # Verificar si estamos cerca del FVG
                if current_price <= fvg_high and current_price >= fvg_low:
                    # Estamos dentro del FVG
                    proximity_pct = 0.0
                elif current_price < fvg_low:
                    # Estamos por debajo, calcular distancia
                    proximity_pct = ((fvg_low - current_price) / fvg_low) * 100
                else:
                    # Estamos por encima, calcular distancia
                    proximity_pct = ((current_price - fvg_high) / fvg_high) * 100
                
                if proximity_pct <= max_proximity_pct:
                    nearby_fvgs.append({
                        'index': idx,
                        'type': 'bearish',
                        'low': fvg_low,
                        'high': fvg_high,
                        'mid': fvg_mid,
                        'proximity_pct': proximity_pct,
                        'current_price': current_price
                    })
        
        # Ordenar por proximidad (más cerca primero)
        nearby_fvgs.sort(key=lambda x: x['proximity_pct'])
        
        return nearby_fvgs
    
    def get_structural_stop_loss(
        self, 
        df: pd.DataFrame, 
        direction: str, 
        entry_price: float
    ) -> tuple[float, int]:
        """
        Encuentra el stop loss estructural más cercano.
        
        Args:
            df: DataFrame con swings detectados
            direction: 'LONG' o 'SHORT'
            entry_price: Precio de entrada
            
        Returns:
            (stop_loss_price, swing_index)
        """
        if df.empty or 'min' not in df.columns or 'max' not in df.columns:
            return 0.0, 0
        
        # Usar solo velas cerradas (excluir la última)
        df_closed = df.iloc[:-1].copy()
        
        if direction == 'LONG':
            # Buscar swing low más reciente por debajo del entry
            valid_lows = df_closed[df_closed['min'].notna() & (df_closed['min'] < entry_price)]
            
            if valid_lows.empty:
                return 0.0, 0
            
            # Tomar el swing low más reciente
            swing_low = valid_lows.iloc[-1]
            sl_price = float(swing_low['min']) * 0.9995  # Buffer de 0.05%
            sl_idx = df.index.get_loc(swing_low.name)
            
            return sl_price, sl_idx
        
        else:  # SHORT
            # Buscar swing high más reciente por encima del entry
            valid_highs = df_closed[df_closed['max'].notna() & (df_closed['max'] > entry_price)]
            
            if valid_highs.empty:
                return 0.0, 0
            
            # Tomar el swing high más reciente
            swing_high = valid_highs.iloc[-1]
            sl_price = float(swing_high['max']) * 1.0005  # Buffer de 0.05%
            sl_idx = df.index.get_loc(swing_high.name)
            
            return sl_price, sl_idx