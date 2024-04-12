import logging
import sys

from vllm.logger import (
    _DATE_FORMAT, _FORMAT, NewLineFormatter, init_logger
)


def test_vllm_root_logger_configuration():
    logger = logging.getLogger("vllm")
    assert logger.level == logging.DEBUG
    assert not logger.propagate

    handler = logger.handlers[0]
    assert handler.stream == sys.stdout
    assert handler.level == logging.INFO

    formatter = handler.formatter
    assert formatter is not None
    assert isinstance(formatter, NewLineFormatter)
    assert formatter._fmt == _FORMAT
    assert formatter.datefmt == _DATE_FORMAT


def test_init_logger_configures_the_logger_like_the_root_logger():
    root_logger = logging.getLogger("vllm")
    logger = init_logger(__name__)

    assert logger.name == __name__
    assert logger.level == logging.DEBUG
    assert logger.handlers == root_logger.handlers
    assert logger.propagate == root_logger.propagate
