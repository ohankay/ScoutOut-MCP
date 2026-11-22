"""The single exit for everything an MCP tool returns (SPEC §5).

Tools build their answers from metadata, counts and enums only. ``guard`` is the safety net on top:
* keys that could only carry values are refused outright (a programming error, caught by the leak test);
* every free string is scrubbed (quoted SQL literals, long digit runs) and capped;
* identifier-like keys (names, ids, timestamps, rule patterns) keep their text but are still capped.
"""

from __future__ import annotations

import functools
import logging
import re

from scoutout.core.log import scrub

log = logging.getLogger("scoutout")

#: keys whose text is metadata the agent may see verbatim
VERBATIM = {"database", "schema", "table", "column", "name", "job_id", "run_id", "at", "started_at", "finished_at",
            "last_scan", "restored_at", "pattern", "path", "data_type", "sql_type", "algorithm", "term", "status",
            "zone", "sensitive_type", "type", "rule", "detector", "source", "kind", "state", "column_names", "context",
            "checksum", "shape", "prefix", "files"}
#: keys that never leave the server
FORBIDDEN = {"redacted_examples", "examples", "values", "value", "sample", "samples", "rows_raw", "raw", "record_json",
             "prompt_text", "llm_text"}
MAX_STR = 400
_DIGITS = re.compile(r"\d{6,}")


class GuardError(Exception):
    pass


def guard(obj, key: str | None = None):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            k = str(k)
            if k in FORBIDDEN:
                raise GuardError(f"çıktıda yasak alan: {k}")
            out[k] = guard(v, k)
        return out
    if isinstance(obj, (list, tuple, set)):
        return [guard(v, key) for v in obj]
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    text = str(obj)
    if key in VERBATIM:
        return text[:MAX_STR]
    return scrub(text)[:MAX_STR]


def kcount(n: int, k: int):
    """A count the agent may see: exact when 0 or >= k, otherwise "<k"."""
    return n if n == 0 or n >= k else f"<{k}"


def tool_result(fn):
    """Decorator for tool implementations: guarded result, errors as short scrubbed messages."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return guard(fn(*args, **kwargs))
        except GuardError:
            log.error("guard refused output of %s", fn.__name__)
            return {"error": "İç hata: çıktı güvenlik kontrolünden geçemedi."}
        except Exception as e:  # noqa: BLE001 - the agent gets a message, never a traceback with values
            msg = scrub(str(e)) if isinstance(e, (ValueError, KeyError, PermissionError)) else scrub(f"{type(e).__name__}: {e}")
            log.warning("tool %s failed: %s", fn.__name__, msg)
            return {"error": _DIGITS.sub("***", msg)[:MAX_STR]}

    return wrapper
