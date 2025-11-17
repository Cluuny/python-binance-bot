import traceback
from datetime import datetime

import numpy as np
from binance import Client, ThreadedWebsocketManager
from binance.enums import *
import pandas as pd
from ta.trend import EMAIndicator
from ta.volatility import AverageTrueRange
import time

from config import Config

from logger import setup_logging
import logging


class Bot:
    def __init__(self):
        setup_logging()
        self.logger = logging.getLogger("Bot")


        self.config = Config()
        self.client = Client(self.config.api_key, self.config.api_secret, testnet=True)
        self.ws = ThreadedWebsocketManager(self.config.api_key, self.config.api_secret)
        self.kline_data = None
        self.ws_connected = False
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 10

        self.symbol = self.config.data['symbol'].iloc[0]
        self.atr_period = self.config.data['atr_period'].iloc[0]
        self.atr_multiplier_sl = self.config.data['atr_multiplier_sl'].iloc[0]
        self.atr_multiplier_tp = self.config.data['atr_multiplier_tp'].iloc[0]
        self.slow_ema = self.config.data['slow_ema'].iloc[0]
        self.fast_ema = self.config.data['fast_ema'].iloc[0]
        self.current_position = None  # 'BUY' o 'SELL'
        self.max_concurrent_trades = self.config.data['max_concurrent_trades'].iloc[0]
        self.cocurrent_trades_count = 0

    def handle_kline_message(self, message):
        try:
            if 'e' in message and message['e'] == 'kline':
                kline = message['k']
                if kline['x']:  # x = is_closed
                    self.process_closed_kline(kline)
        except Exception as e:
            self.logger.error(f"Error procesando mensaje: {e}")

    def handle_websocket_error(self, error):
        """Manejar errores del websocket"""
        self.logger.error(f"WebSocket error: {error}")
        self.ws_connected = False
        self.reconnect_websocket()

    def reconnect_websocket(self):
        """Reconectar el websocket automáticamente"""
        if self.reconnect_attempts >= self.max_reconnect_attempts:
            self.logger.warning("Máximo de intentos de reconexión alcanzado")
            return

        self.reconnect_attempts += 1
        self.logger.info(f"Intentando reconexión #{self.reconnect_attempts}...")

        try:
            # Detener conexión existente
            if hasattr(self.ws, '_running') and self.ws._running:
                self.ws.stop()

            time.sleep(5)  # Esperar antes de reconectar

            # Reiniciar conexión
            self.start_websocket()

        except Exception as e:
            self.logger.error(f"Error en reconexión: {e}")
            # Reintentar después de un tiempo exponencial
            wait_time = min(60 * self.reconnect_attempts, 300)  # Máximo 5 minutos
            self.logger.warning(f"Esperando {wait_time} segundos antes del próximo intento...")
            time.sleep(wait_time)
            self.reconnect_websocket()

    def start_websocket(self):
        """Iniciar conexión websocket con manejo de errores"""
        try:
            self.ws.start()
            self.ws.start_kline_socket(
                callback=self.handle_kline_message,
                symbol=self.config.data['symbol'].iloc[0],
                interval=self.config.data['timeframe'].iloc[0]
            )
            self.ws_connected = True
            self.reconnect_attempts = 0
            self.logger.info("WebSocket conectado exitosamente")

        except Exception as e:
            self.logger.error(f"Error iniciando WebSocket: {e}")
            self.ws_connected = False
            self.reconnect_websocket()

    def get_historical_data(self, symbol: str, interval: str):
        """Obtener datos históricos con reintentos"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                klines = self.client.get_klines(
                    symbol=symbol,
                    interval=interval,
                    limit=(100 + self.slow_ema)
                )
                df = pd.DataFrame(klines,
                                  columns=['time', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'qav',
                                           'num_trades',
                                           'taker_base_vol', 'taker_quote_vol', 'ignore'])

                # Convert to numeric
                df['open'] = pd.to_numeric(df['open'])
                df['high'] = pd.to_numeric(df['high'])
                df['low'] = pd.to_numeric(df['low'])
                df['close'] = pd.to_numeric(df['close'])
                self.kline_data = df
                self.logger.info(f"Datos históricos obtenidos: {len(df)} velas")
                break

            except Exception as e:
                self.logger.error(f"Error obteniendo datos históricos (intento {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(5)
                else:
                    raise e

    def get_live_data(self):
        """Obtener datos en tiempo real con reconexión automática"""
        self.start_websocket()

        # Monitorear conexión periódicamente
        while True:
            try:
                if not self.ws_connected:
                    self.logger.error("Conexión WebSocket perdida, intentando reconectar...")
                    self.reconnect_websocket()

                # Verificar condiciones de salida si hay posición abierta
                if self.current_position is not None:
                    self.check_exit_conditions()

                time.sleep(60)  # Verificar cada minuto

            except Exception as e:
                self.logger.error(f"Error en get_live_data: {e}")
                time.sleep(60)

    def process_closed_kline(self, kline):
        """Procesar vela cerrada con manejo de errores"""
        try:
            # Crear nuevo registro
            new_data = {
                'time': datetime.fromtimestamp(kline['t'] / 1000),
                'open': float(kline['o']),
                'high': float(kline['h']),
                'low': float(kline['l']),
                'close': float(kline['c']),
                'volume': float(kline['v']),
                'close_time': datetime.fromtimestamp(kline['T'] / 1000),
                'num_trades': kline['n']
            }

            self.logger.info(f"Nueva vela: {self.symbol} - {new_data['close']:.2f} - {new_data['time']}")

            # Actualizar DataFrame
            new_row = pd.DataFrame([new_data])
            self.kline_data = pd.concat([self.kline_data, new_row], ignore_index=True)

            # Mantener solo los últimos 100 registros
            if len(self.kline_data) > 100:
                self.kline_data = self.kline_data.iloc[-100:]

            # Recalcular indicadores
            self.calculate_indicators()

            # Verificar señal
            signal = self.check_signal()
            if signal:
                self.logger.info(f"SEÑAL: {signal}")
                self.execute_trade(signal)

            # Verificar condiciones de salida
            self.check_exit_conditions()
        except Exception as e:
            self.logger.error(f"Error en process_closed_kline: {e}")

    def calculate_indicators(self):
        """Calcular todos los indicadores técnicos"""
        try:

            self.kline_data['slow_ema'] = EMAIndicator(
                self.kline_data['close'], window=self.slow_ema
            ).ema_indicator()

            self.kline_data['fast_ema'] = EMAIndicator(
                self.kline_data['close'], window=self.fast_ema
            ).ema_indicator()

            # Calcular ATR
            self.calculate_atr()

        except Exception as e:
            self.logger.error(f"Error calculando indicadores: {e}")

    def calculate_atr(self):
        """Calcular ATR - versión mejorada"""
        try:
            # Primero intenta con la librería ta
            atr_indicator = AverageTrueRange(
                high=self.kline_data['high'],
                low=self.kline_data['low'],
                close=self.kline_data['close'],
                window=self.atr_period
            )
            atr_ta = atr_indicator.average_true_range()

            # Verificar si el ATR de ta es razonable
            current_price = self.kline_data['close'].iloc[-1]
            atr_ratio = atr_ta.iloc[-1] / current_price

            if atr_ratio < 0.01:  # Menos del 1% - probablemente correcto
                self.kline_data['atr'] = atr_ta
            else:
                # Si es muy alto, usar cálculo manual
                self.logger.warning(f"ATR de ta muy alto ({atr_ta.iloc[-1]:.2f}), usando cálculo manual")
                self.calculate_atr_manual()

        except Exception as e:
            self.logger.error(f"Error con ATR de ta, usando manual: {e}")
            self.calculate_atr_manual()

    def calculate_atr_manual(self):
        """Calcular ATR manualmente para mayor control"""
        try:
            if len(self.kline_data) < self.atr_period + 1:
                return

            # Calcular True Range para cada vela
            true_ranges = []
            for i in range(1, len(self.kline_data)):
                high = self.kline_data['high'].iloc[i]
                low = self.kline_data['low'].iloc[i]
                prev_close = self.kline_data['close'].iloc[i - 1]

                tr1 = high - low
                tr2 = abs(high - prev_close)
                tr3 = abs(low - prev_close)
                true_range = max(tr1, tr2, tr3)
                true_ranges.append(true_range)

            # Calcular ATR como SMA de los True Ranges
            atr_values = []
            for i in range(len(true_ranges)):
                if i < self.atr_period - 1:
                    atr_values.append(np.nan)
                else:
                    # SMA de los últimos 'atr_period' True Ranges
                    period_ranges = true_ranges[i - self.atr_period + 1: i + 1]
                    atr = sum(period_ranges) / len(period_ranges)
                    atr_values.append(atr)

            # Ajustar índices y asignar al DataFrame
            # Los primeros atr_period-1 valores serán NaN
            self.kline_data['atr'] = pd.Series([np.nan] * (self.atr_period) + atr_values)

            self.logger.info(f"ATR manual calculado: {self.kline_data['atr'].iloc[-1]:.2f}")

        except Exception as e:
            self.logger.error(f"Error calculando ATR manual: {e}")

    def debug_atr_detailed(self):
        """Debug detallado del ATR"""
        self.logger.info("DEBUG DETALLADO ATR:")

        # Verificar las primeras velas del histórico
        self.logger.info("Primeras 5 velas del dataset:")
        for i in range(min(5, len(self.kline_data))):
            print(f"  Vela {i}: H={self.kline_data['high'].iloc[i]:.2f}, "
                  f"L={self.kline_data['low'].iloc[i]:.2f}, "
                  f"C={self.kline_data['close'].iloc[i]:.2f}")

        # Verificar si hay datos anómalos
        high_low_diff = self.kline_data['high'] - self.kline_data['low']
        max_diff = high_low_diff.max()
        self.logger.info(f"Mayor diferencia High-Low en dataset: {max_diff:.2f}")

        if max_diff > 1000:  # Si hay diferencias > $1000, es anómalo
            outlier_index = high_low_diff.idxmax()
            print(f"   POSIBLE OUTLIER en vela {outlier_index}:")
            print(f"   High: {self.kline_data['high'].iloc[outlier_index]:.2f}")
            print(f"   Low: {self.kline_data['low'].iloc[outlier_index]:.2f}")
            print(f"   Close: {self.kline_data['close'].iloc[outlier_index]:.2f}")


    def check_signal(self):
        """Verificar señales de trading con debugging mejorado"""
        # Verificar que hay suficientes datos para comparar
        if len(self.kline_data) < 2:
            self.logger.warning("No hay suficientes datos para verificar señal")
            return None

        # Verificar que las EMAs no sean NaN
        if (pd.isna(self.kline_data['fast_ema'].iloc[-1]) or
                pd.isna(self.kline_data['slow_ema'].iloc[-1]) or
                pd.isna(self.kline_data['fast_ema'].iloc[-2]) or
                pd.isna(self.kline_data['slow_ema'].iloc[-2])):
            self.logger.warning("EMAs contienen valores NaN")
            return None

        last = self.kline_data.iloc[-1]
        prev = self.kline_data.iloc[-2]

        # DEBUG: Mostrar estado anterior y actual
        # print(f"   COMPARACIÓN DE CRUCE:")
        # print(f"   Anterior - Fast: {prev['fast_ema']:.5f}, Slow: {prev['slow_ema']:.5f}")
        # print(f"   Actual   - Fast: {last['fast_ema']:.5f}, Slow: {last['slow_ema']:.5f}")

        # CRUCE ALCISTA: EMA rápida cruza POR ENCIMA de la lenta → COMPRA
        if (prev['fast_ema'] <= prev['slow_ema'] and
                last['fast_ema'] > last['slow_ema']):
            self.logger.info(f"   SEÑAL DE COMPRA DETECTADA")
            self.logger.info(f"   Anterior: Fast({prev['fast_ema']:.5f}) <= Slow({prev['slow_ema']:.5f})")
            self.logger.info(f"   Actual: Fast({last['fast_ema']:.5f}) > Slow({last['slow_ema']:.5f})")
            return 'BUY'

        # CRUCE BAJISTA: EMA rápida cruza POR DEBAJO de la lenta → VENTA
        elif (prev['fast_ema'] >= prev['slow_ema'] and
              last['fast_ema'] < last['slow_ema']):
            self.logger.info(f"   SEÑAL DE VENTA DETECTADA")
            self.logger.info(f"   Anterior: Fast({prev['fast_ema']:.5f}) >= Slow({prev['slow_ema']:.5f})")
            self.logger.info(f"   Actual: Fast({last['fast_ema']:.5f}) < Slow({last['slow_ema']:.5f})")
            return 'SELL'

        return None

    def convert(self, symbol_from, symbol_to, percentage):
        """
        Convertir un porcentaje del balance de symbol_from a symbol_to

        Args:
            symbol_from: Moneda origen
            symbol_to: Moneda destino
            percentage: Porcentaje a usar del balance de origen
        """
        try:
            # Validar el porcentaje
            if not 0 <= percentage <= 1:
                raise ValueError("El porcentaje debe estar entre 0 y 1")

            # Obtener balance real de la cuenta
            balance_info = self.client.get_asset_balance(asset=symbol_from)
            if not balance_info:
                raise Exception(f"No se encontró balance para {symbol_from}")

            available_balance = float(balance_info['free'])
            quantity = available_balance * percentage

            self.logger.info(f"Balance disponible de {symbol_from}: {available_balance}")
            self.logger.info(f"Porcentaje a usar: {percentage * 100}% = {quantity} {symbol_from}")

            # Si la cantidad es 0 o muy pequeña
            if quantity <= 0:
                return 0

            # Si son la misma moneda
            if symbol_from == symbol_to:
                return quantity

            # Obtener precio de mercado actual y convertir
            symbol_to_price = self.client.get_symbol_ticker(symbol=symbol_to)['price']
            return quantity / float(symbol_to_price)

        except Exception as e:
            self.logger.error(f"Error en conversión: {traceback.format_exc()}")
            return None

    def execute_trade(self, signal):
        """Ejecutar trade con stops basados en ATR"""
        current_price = self.kline_data['close'].iloc[-1]
        current_atr = self.kline_data['atr'].iloc[-1]

        if signal == 'BUY' and self.current_position != 'BUY':
            self.current_position = 'BUY'
            self.entry_price = current_price
            self.stop_loss = current_price - (current_atr * self.atr_multiplier_sl)
            self.take_profit = current_price + (current_atr * self.atr_multiplier_tp)

            order_qty = self.convert(symbol_from='USDT', symbol_to=self.config.data['symbol'].iloc[0], percentage=self.config.data['risk_per_trade'].iloc[0])

            market_order = self.client.create_order(
                symbol=self.config.data['symbol'].iloc[0],
                side=SIDE_BUY,
                type=ORDER_TYPE_MARKET,
                quantity=100,
                timeInForce=TIME_IN_FORCE_GTC,
            )

            order = self.client.create_oco_order(
                symbol=self.config.data['symbol'].iloc[0],
                side=SIDE_SELL,  # Vendes para tomar ganancias o limitar pérdidas
                quantity=100,
                price='0.000025',  # TAKE PROFIT - Precio objetivo de ganancia
                stopPrice='0.000015',  # STOP LOSS - Precio donde activar el stop
                stopLimitPrice='0.000014',  # Precio de ejecución una vez activado el stop
                stopLimitTimeInForce=TIME_IN_FORCE_GTC
            )

            self.logger.info(f"   ENTRADA COMPRA:")
            self.logger.info(f"   Precio: {current_price:.2f}")
            self.logger.info(f"   Stop Loss: {self.stop_loss:.2f}")
            self.logger.info(f"   Take Profit: {self.take_profit:.2f}")
            self.logger.info(f"   ATR: {current_atr:.2f}")
            self.logger.info(f"   Risk: {current_price - self.stop_loss:.2f}")
            self.logger.info(f"   Reward: {self.take_profit - current_price:.2f}")
            self.logger.info(f"   Ratio R:R: {(self.take_profit - current_price) / (current_price - self.stop_loss):.1f}:1")

        elif signal == 'SELL' and self.current_position != 'SELL':
            self.current_position = 'SELL'
            self.entry_price = current_price
            self.stop_loss = current_price + (current_atr * self.atr_multiplier_sl)
            self.take_profit = current_price - (current_atr * self.atr_multiplier_tp)

            self.logger.info(f"   ENTRADA VENTA:")
            self.logger.info(f"   Precio: {current_price:.2f}")
            self.logger.info(f"   Stop Loss: {self.stop_loss:.2f}")
            self.logger.info(f"   Take Profit: {self.take_profit:.2f}")
            self.logger.info(f"   ATR: {current_atr:.2f}")
            self.logger.info(f"   Risk: {self.stop_loss - current_price:.2f}")
            self.logger.info(f"   Reward: {current_price - self.take_profit:.2f}")
            self.logger.info(f"   Ratio R:R: {(current_price - self.take_profit) / (self.stop_loss - current_price):.1f}:1")

    def check_exit_conditions(self):
        """Verificar si debemos cerrar la posición por SL o TP"""
        if self.current_position is None:
            return

        current_price = self.kline_data['close'].iloc[-1]

        if self.current_position == 'BUY':
            if current_price <= self.stop_loss:
                self.logger.info(f"STOP LOSS COMPRA - Precio: {current_price:.2f}")
                self.current_position = None
                self.entry_price = None
                self.stop_loss = None
                self.take_profit = None
            elif current_price >= self.take_profit:
                self.logger.info(f"TAKE PROFIT COMPRA - Precio: {current_price:.2f}")
                self.current_position = None
                self.entry_price = None
                self.stop_loss = None
                self.take_profit = None

        elif self.current_position == 'SELL':
            if current_price >= self.stop_loss:
                self.logger.info(f"STOP LOSS VENTA - Precio: {current_price:.2f}")
                self.current_position = None
                self.entry_price = None
                self.stop_loss = None
                self.take_profit = None
            elif current_price <= self.take_profit:
                self.logger.info(f"TAKE PROFIT VENTA - Precio: {current_price:.2f}")
                self.current_position = None
                self.entry_price = None
                self.stop_loss = None
                self.take_profit = None

    def check_testnet_balance(self):
        """Verificar saldo disponible en Testnet"""
        try:
            # Obtener información de la cuenta
            account_info = self.client.get_account()

            self.logger.info("SALDO PRINCIPAL TESTNET:")
            for balance in account_info['balances']:
                if float(balance['free']) > 0 or float(balance['locked']) > 0:
                    if balance['asset'] == 'BTC' or balance['asset'] == 'USDT' or balance['asset'] == 'ETH' or balance['asset'] == 'BNB':
                        self.logger.info(f"   {balance['asset']}: Libre: {balance['free']} | Bloqueado: {balance['locked']}")

        except Exception as e:
            self.logger.error(f"Error consultando saldo: {e}")

    def start(self):
        """Iniciar el bot con manejo robusto de errores"""
        self.logger.info("Iniciando bot...")

        try:
            # Verificar saldo en testnet
            self.check_testnet_balance()

            # Obtener datos históricos
            self.get_historical_data(
                self.config.data['symbol'].iloc[0],
                self.config.data['timeframe'].iloc[0]
            )

            # Calcular indicadores iniciales
            self.calculate_indicators()

            # Iniciar datos en tiempo real
            self.get_live_data()

        except KeyboardInterrupt:
            self.logger.error("Bot detenido por el usuario")
        except Exception as e:
            self.logger.error(f"Error crítico en el bot: {e}")
            self.logger.error("Reiniciando en 60 segundos...")
            time.sleep(5)
            self.start()
