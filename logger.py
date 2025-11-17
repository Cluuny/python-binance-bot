# logger.py
import logging
import sys


class ColoredFormatter(logging.Formatter):
    """Formateador simple con colores"""

    COLORS = {
        'DEBUG': '\033[36m',  # Cyan
        'INFO': '\033[32m',  # Verde
        'WARNING': '\033[33m',  # Amarillo
        'ERROR': '\033[31m',  # Rojo
        'CRITICAL': '\033[41m',  # Fondo rojo
        'RESET': '\033[0m'  # Reset
    }

    def format(self, record):
        color = self.COLORS.get(record.levelname, self.COLORS['RESET'])
        reset = self.COLORS['RESET']

        log_format = f'{color}%(asctime)s - %(name)s - %(levelname)s - %(message)s{reset}'
        formatter = logging.Formatter(log_format, datefmt='%Y-%m-%d %H:%M:%S')
        return formatter.format(record)


def setup_logging():
    """Configuración que silencia librerías pero muestra errores importantes"""

    # Silenciar librerías específicas completamente
    libraries_to_silence = [
        'urllib3', 'asyncio', 'websockets',
        'binance.ws', 'binance.ws.reconnecting_websocket'
    ]

    for lib in libraries_to_silence:
        logging.getLogger(lib).setLevel(logging.CRITICAL)
        logging.getLogger(lib).propagate = False

    # Binance principal solo mostrar WARNING y superior
    logging.getLogger('binance').setLevel(logging.WARNING)

    # Nuestros loggers en INFO
    our_loggers = ['Main', 'Bot', 'Config']
    for logger_name in our_loggers:
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.INFO)
        # No propagar para evitar duplicados
        logger.propagate = False

        # Solo si no tiene handlers ya
        if not logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setLevel(logging.INFO)
            handler.setFormatter(ColoredFormatter())
            logger.addHandler(handler)