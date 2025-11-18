import logging, os, sys
from logging.handlers import RotatingFileHandler
def setup_logger(name: str = "ntsbot", log_dir: str = None) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    if log_dir is None:
        log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "logs")
    os.makedirs(log_dir, exist_ok=True)
    fh = RotatingFileHandler(os.path.join(log_dir, f"{name}.log"), maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stdout); ch.setFormatter(fmt)
    logger.addHandler(fh); logger.addHandler(ch)
    return logger
