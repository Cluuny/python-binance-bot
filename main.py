# main.py
from config import Config
from bot import Bot
from logger import setup_logging
import logging

if __name__ == "__main__":
    setup_logging()

    logger = logging.getLogger("Main")
    logger.info("Iniciando bot")
    config = Config()
    bot = Bot()
    bot.start()