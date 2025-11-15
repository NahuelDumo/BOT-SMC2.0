import os
import json
import logging
import argparse
import time
import hashlib
from typing import Any, Dict, Optional
import requests

# Datos de mercado (precio) con ccxt en Binance Futuros USDT-M
import ccxt

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("test-operation")


class BitunixFuturesClient:
    """Cliente para operar en Bitunix Perpetual Futures (USDT-M)"""
    
    def __init__(self, api_key: str, api_secret: str, base_url: str = "https://fapi.bitunix.com"):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url
        
    def _generate_signature(self, nonce: str, timestamp: str, body: str = "") -> str:
        """
        Genera la firma según documentación de Bitunix:
        1. digest_input = nonce + timestamp + api_key + "" + body
        2. first_hash = SHA256(digest_input)
        3. signature = SHA256(first_hash + secret_key)
        """
        # Paso 1: Concatenar nonce + timestamp + api_key + "" + body
        digest_input = nonce + timestamp + self.api_key + "" + body
        
        # Paso 2: Primer hash SHA256
        first_hash = hashlib.sha256(digest_input.encode()).hexdigest()
        
        # Paso 3: Segundo hash SHA256 (first_hash + secret_key)
        signature = hashlib.sha256((first_hash + self.api_secret).encode()).hexdigest()
        
        return signature
    
    def _request(self, method: str, endpoint: str, payload: Optional[Dict] = None) -> Dict:
        """Hace request firmado a la API"""
        url = f"{self.base_url}{endpoint}"
        
        # Generar nonce aleatorio (32 caracteres hexadecimales)
        nonce = os.urandom(16).hex()
        
        # Timestamp en milisegundos
        timestamp = str(int(time.time() * 1000))
        
        # Body JSON (si hay payload)
        body = ""
        if payload:
            body = json.dumps(payload, separators=(',', ':'))
        
        # Generar firma
        signature = self._generate_signature(nonce, timestamp, body)
        
        # Headers
        headers = {
            'api-key': self.api_key,
            'nonce': nonce,
            'timestamp': timestamp,
            'sign': signature,
            'language': 'en-US',
            'Content-Type': 'application/json'
        }
        
        logger.debug(f"Request: {method} {url}")
        logger.debug(f"Body: {body}")
        logger.debug(f"Headers: {headers}")
        
        if method.upper() == 'POST':
            response = requests.post(url, headers=headers, data=body)
        else:
            response = requests.get(url, headers=headers)
            
        logger.debug(f"Response status: {response.status_code}")
        logger.debug(f"Response body: {response.text}")
        
        # Manejar errores HTTP
        if response.status_code != 200:
            logger.error(f"Error HTTP {response.status_code}: {response.text}")
            response.raise_for_status()
        
        result = response.json()
        
        # Verificar errores de la API
        if result.get('code') != 0:
            error_msg = result.get('msg', 'Unknown error')
            logger.error(f"API Error: {error_msg} (code: {result.get('code')})")
            raise RuntimeError(f"API Error: {error_msg}")
        
        return result
    
    def place_order(
        self,
        symbol: str,
        side: str,  # 'BUY' o 'SELL'
        order_type: str,  # 'MARKET' o 'LIMIT'
        qty: float,
        trade_side: str = 'OPEN',  # 'OPEN' o 'CLOSE'
        price: Optional[float] = None,
        reduce_only: bool = False,
        position_id: Optional[str] = None
    ) -> Dict:
        """
        Coloca una orden en futuros perpetuos.
        
        Parámetros según: https://openapidoc.bitunix.com/doc/trade/place_order.html
        - symbol: ej. 'ETHUSDT'
        - side: 'BUY' o 'SELL'
        - order_type: 'MARKET' o 'LIMIT' (como string 'orderType')
        - qty: cantidad de contratos
        - trade_side: 'OPEN' para abrir posición, 'CLOSE' para cerrar
        - price: precio (solo para LIMIT)
        - reduce_only: solo reducir posición
        - position_id: ID de posición (opcional)
        """
        endpoint = "/api/v1/futures/trade/place_order"
        
        payload = {
            'symbol': symbol.upper(),
            'side': side.upper(),
            'orderType': order_type.upper(),
            'qty': str(qty),
            'tradeSide': trade_side.upper(),
            'reduceOnly': reduce_only
        }
        
        # Agregar precio si es orden LIMIT
        if order_type.upper() == 'LIMIT' and price is not None:
            payload['price'] = str(price)
        
        # Agregar position_id si se proporciona
        if position_id:
            payload['positionId'] = position_id
            
        return self._request('POST', endpoint, payload)
    
    def get_position(self, symbol: Optional[str] = None) -> Dict:
        """Obtiene posiciones abiertas"""
        endpoint = "/api/v1/futures/trade/get_position"
        
        # Para GET, agregar parámetros a la URL
        if symbol:
            endpoint += f"?symbol={symbol.upper()}"
            
        return self._request('GET', endpoint)


def load_config() -> Dict[str, Any]:
    script_dir = os.path.dirname(os.path.realpath(__file__))
    cfg_path = os.path.join(script_dir, 'cofigETHBTC.json')
    if not os.path.exists(cfg_path):
        raise FileNotFoundError(f"No se encontró el archivo de configuración: {cfg_path}")
    with open(cfg_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def fetch_futures_price(symbol: str) -> float:
    """
    Obtiene precio actual del símbolo en Binance Futuros USDT-M para cálculos de tamaño.
    """
    ex = ccxt.binance({
        'enableRateLimit': True,
        'options': {'defaultType': 'future'}
    })
    ccxt_symbol = symbol.upper().replace('USDT', '/USDT')
    ticker = ex.fetch_ticker(ccxt_symbol)
    price = ticker.get('last') or ticker.get('close') or ticker.get('info', {}).get('lastPrice')
    if price is None:
        raise RuntimeError(f"No se pudo obtener precio para {symbol}")
    return float(price)


def calc_qty_from_margin(price: float, margin_usdt: float, leverage: Optional[int] = None, round_decimals: int = 3) -> float:
    if price <= 0:
        raise ValueError("Precio inválido para calcular tamaño")
    lev = 1 if not leverage or leverage <= 1 else leverage
    qty = (margin_usdt * lev) / price
    return round(qty, round_decimals)


def open_and_close_market(
    client: BitunixFuturesClient,
    symbol: str,
    side: str,
    qty: float, 
    live: bool,
    wait_seconds: int = 3
) -> Dict[str, Any]:
    """
    Abre y cierra una posición a mercado en Bitunix Futures.
    """
    side_str = side.upper()
    if side_str not in ("BUY", "SELL"):
        raise ValueError("side debe ser 'BUY' o 'SELL'")

    if not live:
        logger.warning("DRY-RUN: Se simularía abrir %s %s %s y cerrar con orden opuesta.", symbol, side_str, qty)
        return {"open": {"dry_run": True}, "close": {"dry_run": True}}

    # 1. ABRIR POSICIÓN
    logger.info("Enviando ORDEN DE APERTURA a mercado: %s %s qty=%s", side_str, symbol, qty)
    
    open_res = client.place_order(
        symbol=symbol,
        side=side_str,
        order_type='MARKET',
        qty=qty,
        trade_side='OPEN'
    )
    
    logger.info("Orden de apertura enviada exitosamente")
    logger.info("Respuesta: %s", json.dumps(open_res, indent=2))

    # Espera configurable
    logger.info("Esperando %s segundos antes de cerrar posición...", wait_seconds)
    time.sleep(wait_seconds)

    # 2. CERRAR POSICIÓN (lado opuesto)
    opposite_side = 'SELL' if side_str == 'BUY' else 'BUY'
    logger.info("Enviando ORDEN DE CIERRE a mercado: %s %s qty=%s", opposite_side, symbol, qty)
    
    close_res = client.place_order(
        symbol=symbol,
        side=opposite_side,
        order_type='MARKET',
        qty=qty,
        trade_side='CLOSE'
    )
    
    logger.info("Orden de cierre enviada exitosamente")
    logger.info("Respuesta: %s", json.dumps(close_res, indent=2))

    return {"open": open_res, "close": close_res}


def main(args: argparse.Namespace) -> None:
    cfg = load_config()

    # Wallet (primera habilitada)
    wallets = cfg.get('wallets', [])
    wallet = next((w for w in wallets if w.get('enabled', True)), None)
    if not wallet:
        raise RuntimeError("No hay wallets habilitadas en cofigETHBTC.json")

    # Crear cliente de futuros
    client = BitunixFuturesClient(
        api_key=wallet['api_key'],
        api_secret=wallet['api_secret']
    )

    # Símbolo por defecto: primero de la lista en config
    default_symbol = (cfg.get('symbols') or ['ETHUSDT'])[0]
    symbol = (args.symbol or default_symbol).upper()

    # Cantidad
    qty: Optional[float] = args.qty
    if qty is None:
        leverage = int(args.leverage or 10)
        price = fetch_futures_price(symbol)
        qty = calc_qty_from_margin(price, args.margin, leverage if leverage > 1 else 1)
        logger.info(
            "Cantidad calculada: qty=%s (precio=%s, margen=%s USDT, lev=%sx)",
            qty, price, args.margin, leverage
        )

    # Ejecutar apertura y cierre
    result = open_and_close_market(client, symbol, args.side, float(qty), args.live, args.wait)
    
    print("\n" + "="*60)
    print("RESULTADO FINAL:")
    print(json.dumps(result, indent=2))
    print("="*60)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Prueba de apertura y cierre a mercado en Bitunix Futuros USDT-M"
    )
    parser.add_argument('--symbol', type=str, help="Símbolo, ej: ETHUSDT. Por defecto toma el primero del config.")
    parser.add_argument('--side', type=str, default='buy', choices=['buy', 'sell'], help="Lado de la orden de APERTURA")
    parser.add_argument('--qty', type=float, help="Cantidad (contratos). Si se omite, se calcula por margen y apalancamiento")
    parser.add_argument('--margin', type=float, default=5.0, help="Margen en USDT para calcular tamaño si no pasas --qty")
    parser.add_argument('--leverage', type=int, default=10, help="Apalancamiento para el cálculo")
    parser.add_argument('--wait', type=int, default=3, help="Segundos a esperar entre apertura y cierre de posición")
    parser.add_argument('--live', action='store_true', help="Si se pasa, ENVÍA órdenes reales. Por defecto es DRY-RUN")
    
    args = parser.parse_args()
    main(args)