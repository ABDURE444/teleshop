"""Central logging: one rotating file + coloured console for the whole app.

v2 change: no more one-log-file-per-shop-process. Every log line from a shop
bot is tagged with its shop_id via the 'teleshop.shop.<shop_id>' logger name,
so `grep shop.<id> logs/teleshop.log` isolates a single shop when debugging.
"""
import logging
import os
from logging.handlers import RotatingFileHandler


class ColoredFormatter(logging.Formatter):
    COLORS = {
        'DEBUG': '\033[36m', 'INFO': '\033[32m', 'WARNING': '\033[33m',
        'ERROR': '\033[31m', 'CRITICAL': '\033[35m', 'RESET': '\033[0m',
    }

    def format(self, record):
        levelname = record.levelname
        if levelname in self.COLORS:
            record.levelname = f"{self.COLORS[levelname]}{levelname}{self.COLORS['RESET']}"
        return super().format(record)


def setup_logging(log_dir: str = "logs", level: str = "INFO") -> None:
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "teleshop.log")

    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    root.handlers.clear()

    file_handler = RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding='utf-8')
    file_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    root.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(ColoredFormatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    root.addHandler(console)

    logging.getLogger("aiogram").setLevel(logging.INFO)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def shop_logger(shop_id: str) -> logging.Logger:
    return logging.getLogger(f"teleshop.shop.{shop_id}")
