"""Logging configuration for vLLM."""
import json
import logging
import os
from logging import Logger
from logging.config import dictConfig
from os import path
from typing import Dict

VLLM_CONFIGURE_LOGGING = int(os.getenv("VLLM_CONFIGURE_LOGGING", "1"))
VLLM_LOGGING_CONFIG_PATH = os.getenv("VLLM_LOGGING_CONFIG_PATH")

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
    if VLLM_CONFIGURE_LOGGING:
        logging_config: Dict = DEFAULT_LOGGING_CONFIG

    if VLLM_LOGGING_CONFIG_PATH:
        if not path.exists(VLLM_LOGGING_CONFIG_PATH):
            raise RuntimeError(
                "Could not load logging config. File does not exist:"
                f" {VLLM_LOGGING_CONFIG_PATH}")
        with open(VLLM_LOGGING_CONFIG_PATH, encoding="utf-8",
                  mode="r") as file:
            custom_config = json.loads(file.read())

        if not isinstance(custom_config, dict):
            raise ValueError("Invalid logging config. Expected Dict, got"
                             f" {type(custom_config).__name__}.")
        logging_config = custom_config

    if logging_config:
        dictConfig(logging_config)


def _configure_vllm_logger(logger: Logger) -> None:
    # Use the same settings as for root logger
    _root_logger = logging.getLogger("vllm")
    default_log_level = os.getenv("LOG_LEVEL", _root_logger.level)
    logger.setLevel(default_log_level)
    for handler in _root_logger.handlers:
        logger.addHandler(handler)
    logger.propagate = False


def init_logger(name: str) -> Logger:
    logger_is_new = name not in logging.Logger.manager.loggerDict
    logger = logging.getLogger(name)
    if VLLM_CONFIGURE_LOGGING and logger_is_new:
        _configure_vllm_logger(logger)
    return logger


# The logger is initialized when the module is imported.
# This is thread-safe as the module is only imported once,
# guaranteed by the Python GIL.
if VLLM_CONFIGURE_LOGGING or VLLM_LOGGING_CONFIG_PATH:
    _configure_vllm_root_logger()
