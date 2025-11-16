import traceback
from datetime import datetime

import numpy as np
from binance import Client, ThreadedWebsocketManager
import pandas as pd
from ta.trend import EMAIndicator
from ta.volatility import AverageTrueRange
import time

from config import Config


class Bot:
    def __init__(self):
        self.config = Config()
        self.client = Client(self.config.api_key, self.config.api_secret, testnet=True)
        self.ws = ThreadedWebsocketManager(self.config.api_key, self.config.api_secret)
        self.kline_data = None
        self.ws_connected = False
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 10

        # Configuración ATR para gestión de riesgo
        self.atr_period = 14
        self.atr_multiplier_sl = 1.5  # Para Stop Loss
        self.atr_multiplier_tp = 2.0  # Para Take Profit
        self.current_position = None  # 'BUY' o 'SELL'
        self.entry_price = None
        self.stop_loss = None
        self.take_profit = None

    def handle_kline_message(self, message):
        try:
            if 'e' in message and message['e'] == 'kline':
                kline = message['k']
                if kline['x']:  # x = is_closed
                    self.process_closed_kline(kline)
        except Exception as e:
            print(f"Error procesando mensaje: {e}")

    def handle_websocket_error(self, error):
        """Manejar errores del websocket"""
        print(f"WebSocket error: {error}")
        self.ws_connected = False
        self.reconnect_websocket()

    def reconnect_websocket(self):
        """Reconectar el websocket automáticamente"""
        if self.reconnect_attempts >= self.max_reconnect_attempts:
            print("Máximo de intentos de reconexión alcanzado")
            return

        self.reconnect_attempts += 1
        print(f"Intentando reconexión #{self.reconnect_attempts}...")

        try:
            # Detener conexión existente
            if hasattr(self.ws, '_running') and self.ws._running:
                self.ws.stop()

            time.sleep(5)  # Esperar antes de reconectar

            # Reiniciar conexión
            self.start_websocket()

        except Exception as e:
            print(f"Error en reconexión: {e}")
            # Reintentar después de un tiempo exponencial
            wait_time = min(60 * self.reconnect_attempts, 300)  # Máximo 5 minutos
            print(f"Esperando {wait_time} segundos antes del próximo intento...")
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
            print("WebSocket conectado exitosamente")

        except Exception as e:
            print(f"Error iniciando WebSocket: {e}")
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
                    limit=(100 + self.config.data['slow_ema'].iloc[0])
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
                print(f"Datos históricos obtenidos: {len(df)} velas")
                break

            except Exception as e:
                print(f"Error obteniendo datos históricos (intento {attempt + 1}/{max_retries}): {e}")
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
                    print("Conexión WebSocket perdida, intentando reconectar...")
                    self.reconnect_websocket()

                # Verificar condiciones de salida si hay posición abierta
                if self.current_position is not None:
                    self.check_exit_conditions()

                time.sleep(60)  # Verificar cada minuto

            except Exception as e:
                print(f"Error en get_live_data: {e}")
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

            print(f"Nueva vela: {new_data['close']:.2f} - {new_data['time']}")

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
                print(f"SEÑAL: {signal}")
                self.execute_trade(signal)

            # Verificar condiciones de salida
            self.check_exit_conditions()

            print(self.kline_data.tail(1).to_string())

        except Exception as e:
            print(f"Error en process_closed_kline: {e}")

    def calculate_indicators(self):
        """Calcular todos los indicadores técnicos"""
        try:
            # Calcular EMAs
            slow_ema = self.config.data['slow_ema'].iloc[0]
            fast_ema = self.config.data['fast_ema'].iloc[0]

            self.kline_data['slow_ema'] = EMAIndicator(
                self.kline_data['close'], window=slow_ema
            ).ema_indicator()

            self.kline_data['fast_ema'] = EMAIndicator(
                self.kline_data['close'], window=fast_ema
            ).ema_indicator()

            # Calcular ATR
            self.calculate_atr()

        except Exception as e:
            print(f"Error calculando indicadores: {e}")
            traceback.print_exc()

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
                print(f"ATR (ta): {atr_ta.iloc[-1]:.2f}")
            else:
                # Si es muy alto, usar cálculo manual
                print(f"ATR de ta muy alto ({atr_ta.iloc[-1]:.2f}), usando cálculo manual")
                self.calculate_atr_manual()

        except Exception as e:
            print(f"Error con ATR de ta, usando manual: {e}")
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

            print(f"ATR manual calculado: {self.kline_data['atr'].iloc[-1]:.2f}")

        except Exception as e:
            print(f"Error calculando ATR manual: {e}")

    def debug_atr_detailed(self):
        """Debug detallado del ATR"""
        print("DEBUG DETALLADO ATR:")

        # Verificar las primeras velas del histórico
        print("Primeras 5 velas del dataset:")
        for i in range(min(5, len(self.kline_data))):
            print(f"  Vela {i}: H={self.kline_data['high'].iloc[i]:.2f}, "
                  f"L={self.kline_data['low'].iloc[i]:.2f}, "
                  f"C={self.kline_data['close'].iloc[i]:.2f}")

        # Verificar si hay datos anómalos
        high_low_diff = self.kline_data['high'] - self.kline_data['low']
        max_diff = high_low_diff.max()
        print(f"Mayor diferencia High-Low en dataset: {max_diff:.2f}")

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
            print("No hay suficientes datos para verificar señal")
            return None

        # Verificar que las EMAs no sean NaN
        if (pd.isna(self.kline_data['fast_ema'].iloc[-1]) or
                pd.isna(self.kline_data['slow_ema'].iloc[-1]) or
                pd.isna(self.kline_data['fast_ema'].iloc[-2]) or
                pd.isna(self.kline_data['slow_ema'].iloc[-2])):
            print("EMAs contienen valores NaN")
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
            print(f"   SEÑAL DE COMPRA DETECTADA")
            print(f"   Anterior: Fast({prev['fast_ema']:.5f}) <= Slow({prev['slow_ema']:.5f})")
            print(f"   Actual: Fast({last['fast_ema']:.5f}) > Slow({last['slow_ema']:.5f})")
            return 'BUY'

        # CRUCE BAJISTA: EMA rápida cruza POR DEBAJO de la lenta → VENTA
        elif (prev['fast_ema'] >= prev['slow_ema'] and
              last['fast_ema'] < last['slow_ema']):
            print(f"   SEÑAL DE VENTA DETECTADA")
            print(f"   Anterior: Fast({prev['fast_ema']:.5f}) >= Slow({prev['slow_ema']:.5f})")
            print(f"   Actual: Fast({last['fast_ema']:.5f}) < Slow({last['slow_ema']:.5f})")
            return 'SELL'

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

            print(f"   ENTRADA COMPRA:")
            print(f"   Precio: {current_price:.2f}")
            print(f"   Stop Loss: {self.stop_loss:.2f}")
            print(f"   Take Profit: {self.take_profit:.2f}")
            print(f"   ATR: {current_atr:.2f}")
            print(f"   Risk: {current_price - self.stop_loss:.2f}")
            print(f"   Reward: {self.take_profit - current_price:.2f}")
            print(f"   Ratio R:R: {(self.take_profit - current_price) / (current_price - self.stop_loss):.1f}:1")

        elif signal == 'SELL' and self.current_position != 'SELL':
            self.current_position = 'SELL'
            self.entry_price = current_price
            self.stop_loss = current_price + (current_atr * self.atr_multiplier_sl)
            self.take_profit = current_price - (current_atr * self.atr_multiplier_tp)

            print(f"   ENTRADA VENTA:")
            print(f"   Precio: {current_price:.2f}")
            print(f"   Stop Loss: {self.stop_loss:.2f}")
            print(f"   Take Profit: {self.take_profit:.2f}")
            print(f"   ATR: {current_atr:.2f}")
            print(f"   Risk: {self.stop_loss - current_price:.2f}")
            print(f"   Reward: {current_price - self.take_profit:.2f}")
            print(f"   Ratio R:R: {(current_price - self.take_profit) / (self.stop_loss - current_price):.1f}:1")

    def check_exit_conditions(self):
        """Verificar si debemos cerrar la posición por SL o TP"""
        if self.current_position is None:
            return

        current_price = self.kline_data['close'].iloc[-1]

        if self.current_position == 'BUY':
            if current_price <= self.stop_loss:
                print(f"STOP LOSS COMPRA - Precio: {current_price:.2f}")
                self.current_position = None
                self.entry_price = None
                self.stop_loss = None
                self.take_profit = None
            elif current_price >= self.take_profit:
                print(f"TAKE PROFIT COMPRA - Precio: {current_price:.2f}")
                self.current_position = None
                self.entry_price = None
                self.stop_loss = None
                self.take_profit = None

        elif self.current_position == 'SELL':
            if current_price >= self.stop_loss:
                print(f"STOP LOSS VENTA - Precio: {current_price:.2f}")
                self.current_position = None
                self.entry_price = None
                self.stop_loss = None
                self.take_profit = None
            elif current_price <= self.take_profit:
                print(f"TAKE PROFIT VENTA - Precio: {current_price:.2f}")
                self.current_position = None
                self.entry_price = None
                self.stop_loss = None
                self.take_profit = None

    def check_testnet_balance(self):
        """Verificar saldo disponible en Testnet"""
        try:
            # Obtener información de la cuenta
            account_info = self.client.get_account()

            print("SALDO PRINCIPAL TESTNET:")
            for balance in account_info['balances']:
                if float(balance['free']) > 0 or float(balance['locked']) > 0:
                    if balance['asset'] == 'BTC' or balance['asset'] == 'USDT' or balance['asset'] == 'ETH' or balance['asset'] == 'BNB':
                        print(f"   {balance['asset']}: Libre: {balance['free']} | Bloqueado: {balance['locked']}")

        except Exception as e:
            print(f"Error consultando saldo: {e}")

    def start(self):
        """Iniciar el bot con manejo robusto de errores"""
        print("Iniciando bot...")

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

            print(self.kline_data.tail(1))

            # Iniciar datos en tiempo real
            self.get_live_data()

        except KeyboardInterrupt:
            print("Bot detenido por el usuario")
        except Exception as e:
            print(f"Error crítico en el bot: {e}")
            print("Reiniciando en 60 segundos...")
            time.sleep(60)
            self.start()  # Reiniciar automáticamente
