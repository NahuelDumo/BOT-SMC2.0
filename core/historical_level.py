"""
Módulo de Detección de Niveles Históricos
Identifica soportes y resistencias clave: ATH, ATL, zonas probadas múltiples veces
"""

import logging
import pandas as pd
import numpy as np
from typing import List, Dict, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class HistoricalLevel:
    """Representa un nivel histórico significativo"""
    price: float
    level_type: str  # 'RESISTANCE', 'SUPPORT', 'ATH', 'ATL'
    strength: int    # Número de veces que fue testeado
    first_touch: pd.Timestamp
    last_touch: pd.Timestamp
    description: str
    is_broken: bool = False


class HistoricalLevelsDetector:
    """Detecta niveles históricos significativos en el precio"""
    
    def __init__(
        self, 
        lookback_days: int = 365,
        touch_tolerance_pct: float = 0.3,
        min_touches: int = 3
    ):
        """
        Args:
            lookback_days: Días hacia atrás para analizar
            touch_tolerance_pct: Tolerancia % para considerar un "toque" (0.3 = 0.3%)
            min_touches: Mínimo de toques para considerar un nivel significativo
        """
        self.lookback_days = lookback_days
        self.touch_tolerance_pct = touch_tolerance_pct / 100  # Convertir a decimal
        self.min_touches = min_touches
        self.levels: Dict[str, List[HistoricalLevel]] = {}
    
    def detect_all_levels(
        self, 
        symbol: str, 
        df: pd.DataFrame,
        current_price: float
    ) -> List[HistoricalLevel]:
        """
        Detecta todos los niveles históricos para un símbolo.
        
        Args:
            symbol: Par de trading
            df: DataFrame con datos históricos (debe tener suficiente historia)
            current_price: Precio actual
            
        Returns:
            Lista de niveles históricos ordenados por fuerza
        """
        if df.empty or len(df) < 100:
            logger.warning(f"Datos insuficientes para {symbol}")
            return []
        
        levels = []
        
        # 1. Detectar ATH y ATL
        ath_level = self._detect_ath(df)
        if ath_level:
            levels.append(ath_level)
        
        atl_level = self._detect_atl(df)
        if atl_level:
            levels.append(atl_level)
        
        # 2. Detectar niveles psicológicos (números redondos)
        psychological_levels = self._detect_psychological_levels(df, current_price)
        levels.extend(psychological_levels)
        
        # 3. Detectar zonas de consolidación
        consolidation_levels = self._detect_consolidation_zones(df)
        levels.extend(consolidation_levels)
        
        # 4. Detectar niveles probados múltiples veces
        tested_levels = self._detect_tested_levels(df)
        levels.extend(tested_levels)
        
        # 5. Filtrar niveles débiles y duplicados
        levels = self._filter_and_merge_levels(levels, current_price)
        
        # 6. Clasificar como soporte o resistencia según precio actual
        levels = self._classify_levels(levels, current_price)
        
        # 7. Ordenar por fuerza (más fuerte primero)
        levels.sort(key=lambda x: x.strength, reverse=True)
        
        # Guardar en caché
        self.levels[symbol] = levels
        
        logger.info(
            f"📊 {symbol}: Detectados {len(levels)} niveles históricos "
            f"(ATH/ATL: {sum(1 for l in levels if l.level_type in ['ATH', 'ATL'])}, "
            f"Fuertes: {sum(1 for l in levels if l.strength >= 5)})"
        )
        
        return levels
    
    def _detect_ath(self, df: pd.DataFrame) -> Optional[HistoricalLevel]:
        """Detecta el All-Time High"""
        max_price = df['high'].max()
        max_idx = df['high'].idxmax()
        
        # Contar cuántas veces se acercó al ATH
        touches = self._count_touches(df, max_price)
        
        return HistoricalLevel(
            price=float(max_price),
            level_type='ATH',
            strength=touches,
            first_touch=max_idx,
            last_touch=max_idx,
            description=f"All-Time High (${max_price:,.2f})"
        )
    
    def _detect_atl(self, df: pd.DataFrame) -> Optional[HistoricalLevel]:
        """Detecta el All-Time Low"""
        min_price = df['low'].min()
        min_idx = df['low'].idxmin()
        
        touches = self._count_touches(df, min_price)
        
        return HistoricalLevel(
            price=float(min_price),
            level_type='ATL',
            strength=touches,
            first_touch=min_idx,
            last_touch=min_idx,
            description=f"All-Time Low (${min_price:,.2f})"
        )
    
    def _detect_psychological_levels(
        self, 
        df: pd.DataFrame, 
        current_price: float
    ) -> List[HistoricalLevel]:
        """
        Detecta niveles psicológicos (números redondos).
        Ejemplo: 100, 150, 200 para precios < 1000
                 1000, 2000, 5000 para precios > 1000
        """
        levels = []
        
        # Determinar el step según el rango de precio
        if current_price < 10:
            steps = [1, 5]  # $1, $5
        elif current_price < 100:
            steps = [10, 25, 50]  # $10, $25, $50
        elif current_price < 1000:
            steps = [50, 100, 250]  # $50, $100, $250
        else:
            steps = [500, 1000, 5000]  # $500, $1000, $5000
        
        price_min = df['low'].min()
        price_max = df['high'].max()
        
        for step in steps:
            # Generar niveles redondos en el rango
            level_price = (price_min // step) * step
            
            while level_price <= price_max:
                if price_min <= level_price <= price_max:
                    # Contar toques
                    touches = self._count_touches(df, level_price)
                    
                    if touches >= 2:  # Al menos 2 toques
                        first_touch, last_touch = self._get_touch_times(df, level_price)
                        
                        levels.append(HistoricalLevel(
                            price=float(level_price),
                            level_type='PSYCHOLOGICAL',
                            strength=touches,
                            first_touch=first_touch,
                            last_touch=last_touch,
                            description=f"Nivel psicológico ${level_price:,.0f}"
                        ))
                
                level_price += step
        
        return levels
    
    def _detect_consolidation_zones(self, df: pd.DataFrame) -> List[HistoricalLevel]:
        """
        Detecta zonas de consolidación (precio se mantuvo en un rango por tiempo).
        """
        levels = []
        window = 50  # Ventana de análisis
        
        if len(df) < window:
            return levels
        
        for i in range(window, len(df), window // 2):
            window_data = df.iloc[i-window:i]
            
            # Calcular rango y volatilidad
            price_range = window_data['high'].max() - window_data['low'].min()
            avg_price = window_data['close'].mean()
            volatility = price_range / avg_price
            
            # Zona de consolidación: baja volatilidad (< 5%)
            if volatility < 0.05:
                mid_price = (window_data['high'].max() + window_data['low'].min()) / 2
                touches = self._count_touches(df, mid_price)
                
                if touches >= self.min_touches:
                    levels.append(HistoricalLevel(
                        price=float(mid_price),
                        level_type='CONSOLIDATION',
                        strength=touches,
                        first_touch=window_data.index[0],
                        last_touch=window_data.index[-1],
                        description=f"Zona consolidación ${mid_price:,.2f}"
                    ))
        
        return levels
    
    def _detect_tested_levels(self, df: pd.DataFrame) -> List[HistoricalLevel]:
        """
        Detecta niveles que fueron probados múltiples veces.
        Usa clustering de precios para encontrar zonas "magnéticas".
        """
        levels = []
        
        # Combinar highs y lows
        all_prices = pd.concat([df['high'], df['low']]).sort_values()
        
        # Agrupar precios similares (clustering simple)
        clusters = self._cluster_prices(all_prices.values)
        
        for cluster_price, cluster_indices in clusters.items():
            if len(cluster_indices) >= self.min_touches:
                # Obtener timestamps
                touches_times = []
                for idx in cluster_indices:
                    if idx < len(df):
                        touches_times.append(df.index[idx])
                
                if touches_times:
                    levels.append(HistoricalLevel(
                        price=float(cluster_price),
                        level_type='TESTED',
                        strength=len(cluster_indices),
                        first_touch=min(touches_times),
                        last_touch=max(touches_times),
                        description=f"Nivel probado {len(cluster_indices)} veces"
                    ))
        
        return levels
    
    def _cluster_prices(self, prices: np.ndarray) -> Dict[float, List[int]]:
        """Agrupa precios similares en clusters"""
        clusters = {}
        
        for i, price in enumerate(prices):
            # Buscar cluster existente cercano
            found_cluster = False
            
            for cluster_price in list(clusters.keys()):
                tolerance = cluster_price * self.touch_tolerance_pct
                
                if abs(price - cluster_price) <= tolerance:
                    clusters[cluster_price].append(i)
                    found_cluster = True
                    break
            
            if not found_cluster:
                clusters[price] = [i]
        
        return clusters
    
    def _count_touches(self, df: pd.DataFrame, price: float) -> int:
        """Cuenta cuántas veces el precio tocó un nivel"""
        tolerance = price * self.touch_tolerance_pct
        
        touches = 0
        touches += ((df['high'] >= price - tolerance) & 
                   (df['high'] <= price + tolerance)).sum()
        touches += ((df['low'] >= price - tolerance) & 
                   (df['low'] <= price + tolerance)).sum()
        
        return int(touches)
    
    def _get_touch_times(
        self, 
        df: pd.DataFrame, 
        price: float
    ) -> tuple[pd.Timestamp, pd.Timestamp]:
        """Obtiene el primer y último toque de un nivel"""
        tolerance = price * self.touch_tolerance_pct
        
        touches_mask = (
            ((df['high'] >= price - tolerance) & (df['high'] <= price + tolerance)) |
            ((df['low'] >= price - tolerance) & (df['low'] <= price + tolerance))
        )
        
        touch_times = df[touches_mask].index
        
        if len(touch_times) > 0:
            return touch_times[0], touch_times[-1]
        else:
            return df.index[0], df.index[-1]
    
    def _filter_and_merge_levels(
        self, 
        levels: List[HistoricalLevel],
        current_price: float
    ) -> List[HistoricalLevel]:
        """Filtra niveles débiles y fusiona niveles muy cercanos"""
        if not levels:
            return []
        
        # Ordenar por precio
        levels.sort(key=lambda x: x.price)
        
        filtered = []
        
        for level in levels:
            # Filtrar niveles muy débiles (excepto ATH/ATL)
            if level.level_type not in ['ATH', 'ATL'] and level.strength < 2:
                continue
            
            # Verificar si es muy cercano a un nivel existente
            is_duplicate = False
            tolerance = level.price * (self.touch_tolerance_pct * 2)
            
            for existing in filtered:
                if abs(level.price - existing.price) <= tolerance:
                    # Fusionar: mantener el más fuerte
                    if level.strength > existing.strength:
                        filtered.remove(existing)
                        filtered.append(level)
                    is_duplicate = True
                    break
            
            if not is_duplicate:
                filtered.append(level)
        
        return filtered
    
    def _classify_levels(
        self, 
        levels: List[HistoricalLevel],
        current_price: float
    ) -> List[HistoricalLevel]:
        """Clasifica niveles como soporte o resistencia según precio actual"""
        for level in levels:
            if level.level_type in ['ATH', 'ATL']:
                continue  # Ya están clasificados
            
            if level.price > current_price:
                level.level_type = 'RESISTANCE'
            else:
                level.level_type = 'SUPPORT'
        
        return levels
    
    def get_nearest_levels(
        self, 
        symbol: str, 
        current_price: float,
        n: int = 3
    ) -> Dict[str, List[HistoricalLevel]]:
        """
        Obtiene los N niveles más cercanos al precio actual.
        
        Returns:
            {'support': [...], 'resistance': [...]}
        """
        if symbol not in self.levels:
            return {'support': [], 'resistance': []}
        
        levels = self.levels[symbol]
        
        # Separar soportes y resistencias
        supports = [l for l in levels if l.price < current_price]
        resistances = [l for l in levels if l.price > current_price]
        
        # Ordenar por cercanía
        supports.sort(key=lambda x: abs(current_price - x.price))
        resistances.sort(key=lambda x: abs(current_price - x.price))
        
        return {
            'support': supports[:n],
            'resistance': resistances[:n]
        }
    
    def get_level_at_price(
        self, 
        symbol: str, 
        price: float,
        tolerance_pct: float = 0.5
    ) -> Optional[HistoricalLevel]:
        """Verifica si hay un nivel histórico en un precio dado"""
        if symbol not in self.levels:
            return None
        
        tolerance = price * (tolerance_pct / 100)
        
        for level in self.levels[symbol]:
            if abs(level.price - price) <= tolerance:
                return level
        
        return None
        