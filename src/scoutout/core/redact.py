"""The single redaction function (spec §4.2). Every path that could carry a raw value goes through here."""

from __future__ import annotations

import re

_DIGIT_RUN = re.compile(r"\d{3,}")
_EMAIL = re.compile(r"[\w.%+-]+@[\w.-]+")
_WORD = re.compile(r"[^\W\d_]+")
KEEP_PREFIX = {"PHONE": 2}  # '05** *** ** **' — the operator prefix alone identifies nobody


def _mask(chars: list[str], start: int, end: int, keep: int = 0) -> None:
    seen = 0
    for i in range(start, min(end, len(chars))):
        if chars[i].isalnum():
            if seen >= keep:
                chars[i] = "*"
            seen += 1


def redact(text: str | None, spans=(), max_len: int = 160) -> str:
    """Mask sensitive spans and anything that still looks identifying; keep context words.

    ``spans`` are detector findings (objects with ``start``, ``end``, ``data_type``). Without spans the whole
    value is masked to its shape. Outside spans, digit runs of 3+, e-mail-like tokens and dictionary names are
    masked too, so a missed detection still does not leak.
    """
    if text is None:
        return ""
    text = str(text)
    chars = list(text)
    if not spans:
        _mask(chars, 0, len(chars))
        return "".join(chars)[:max_len]

    from scoutout.detectors.normalize import dictionaries, fold

    for s in spans:
        _mask(chars, s.start, s.end, KEEP_PREFIX.get(s.data_type, 0))
    for m in _DIGIT_RUN.finditer(text):
        _mask(chars, m.start(), m.end())
    for m in _EMAIL.finditer(text):
        _mask(chars, m.start(), m.end())
    names = dictionaries()
    for m in _WORD.finditer(text):
        w = fold(m.group())
        if m.group()[:1].isupper() and (w in names["given"] or w in names["surname"]):
            _mask(chars, m.start(), m.end())

    # the kept prefix (e.g. phone operator '05') must survive the digit-run pass above
    for s in spans:
        keep = KEEP_PREFIX.get(s.data_type, 0)
        for i in range(s.start, min(s.end, len(text))):
            if keep <= 0:
                break
            if text[i].isalnum():
                chars[i] = text[i]
                keep -= 1

    out = "".join(chars)
    if len(out) <= max_len:
        return out
    first = min(s.start for s in spans)
    start = max(0, first - max_len // 3)
    return ("…" if start else "") + out[start : start + max_len] + "…"
