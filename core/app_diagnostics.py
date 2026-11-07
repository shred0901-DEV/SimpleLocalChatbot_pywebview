"""Keep application logs and native crash tracebacks after the console closes."""
from datetime import datetime
import faulthandler
import logging
import os
from pathlib import Path

_handler = None


def enable_diagnostics(directory):
    global _handler
    if _handler is not None:
        return Path(_handler.baseFilename)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'app_{datetime.now():%Y%m%d_%H%M%S}_{os.getpid()}.log'
    handler = logging.FileHandler(path, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] [%(threadName)s] %(name)s: %(message)s'))
    logging.getLogger().addHandler(handler)
    _handler = handler  # Keep the file descriptor alive for faulthandler.
    faulthandler.enable(file=handler.stream, all_threads=True)
    logging.getLogger(__name__).info('診断ログ: %s', path)
    return path
