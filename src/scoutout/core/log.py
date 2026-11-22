"""Logging with a redaction safety net: messages are never meant to carry values, and this masks any that slip."""

from __future__ import annotations

import logging
import re
from pathlib import Path

_LONG_DIGITS = re.compile(r"\d{6,}")
_QUOTED = re.compile(r"N?'(?:[^']|'')*'")


def scrub(message: str) -> str:
    """Mask quoted SQL literals and long digit runs (e.g. inside a pyodbc error message)."""
    return _LONG_DIGITS.sub(lambda m: "*" * len(m.group()), _QUOTED.sub("'***'", message))


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub(record.getMessage())
        record.args = ()
        return True


def setup_logging(log_dir: Path, level: int = logging.INFO) -> logging.Logger:
    log = logging.getLogger("scoutout")
    if log.handlers:
        return log
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_dir / "scoutout.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
    handler.addFilter(RedactingFilter())
    log.addHandler(handler)
    log.setLevel(level)
    return log
