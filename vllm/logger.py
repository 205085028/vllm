"""Logging configuration for vLLM."""
import logging
import os
from logging import Logger
from logging import config as logging_config

VLLM_CONFIGURE_LOGGING = int(os.getenv("VLLM_CONFIGURE_LOGGING", "1"))

_FORMAT = "%(levelname)s %(asctime)s %(filename)s:%(lineno)d] %(message)s"
_DATE_FORMAT = "%m-%d %H:%M:%S"

DEFAULT_LOGGING_CONFIG = {
    "formatters": {
        "vllm": {
            "class": "vllm.logging.NewLineFormatter",
            "datefmt": _DATE_FORMAT,
            "format": _FORMAT,
        },
    },
    "handlers": {
        "vllm": {
            "class": "logging.StreamHandler",
            "formatter": "vllm",
            "level": "INFO",
            "stream": "ext://sys.stdout",
        },
    },
    "loggers": {
        "vllm": {
            "handlers": ["vllm"],
            "level": "DEBUG",
            "propagate": False,
        },
    },
    "version": 1,
}


def _configure_vllm_root_logger() -> None:
    logging_config.dictConfig(DEFAULT_LOGGING_CONFIG)


# The logger is initialized when the module is imported.
# This is thread-safe as the module is only imported once,
# guaranteed by the Python GIL.
if VLLM_CONFIGURE_LOGGING:
    _configure_vllm_root_logger()


def _configure_vllm_logger(logger: Logger) -> None:
    # Use the same settings as for root logger
    _root_logger = logging.getLogger("vllm")
    default_log_level = os.getenv("LOG_LEVEL", _root_logger.level)
    logger.setLevel(default_log_level)
    for handler in _root_logger.handlers:
        logger.addHandler(handler)
    logger.propagate = _root_logger.propagate


def init_logger(name: str) -> Logger:
    logger = logging.getLogger(name)
    if VLLM_CONFIGURE_LOGGING:
        _configure_vllm_logger(logger)
    return logger
