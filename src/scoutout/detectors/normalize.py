"""Turkish-aware normalisation and digit-group candidate extraction (spec §5.4)."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

_FOLD = str.maketrans({"ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u", "â": "a", "î": "i", "û": "u"})


def tr_lower(s: str) -> str:
    """Turkish lower-case: I -> ı, İ -> i (Python's str.lower gets both wrong for Turkish)."""
    return s.replace("I", "ı").replace("İ", "i").lower()


def fold(s: str) -> str:
    """Lower-case + diacritic folding: 'IŞIK Çağrı' -> 'isik cagri'. Used for dictionary/keyword matching."""
    s = tr_lower(s).translate(_FOLD)
    if s.isascii():
        return s
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return tr_lower(s).translate(_FOLD)  # compatibility forms (e.g. '𝕊') may decompose to upper case


@dataclass(frozen=True)
class Candidate:
    digits: str  # separators removed
    start: int  # offsets in the original text
    end: int


_GROUP = re.compile(r"\d+")
SEPARATORS = " -./"
MAX_JOINED = 34


def digit_candidates(text: str) -> list[Candidate]:
    """All digit strings that could be one identifier.

    Digit groups are joined only across exactly ONE separator character, and only while the separator
    stays the same inside a window (``0532 123 45 67``, ``4111-1111-1111-1111``). Every window of
    consecutive groups is a candidate, so two separate numbers that happen to sit next to each other
    are still found individually, and a different separator, a comma, two spaces, a letter or a line
    break always ends a number — that is the boundary that stops two numbers merging into a fake one.
    """
    groups = [(m.group(), m.start(), m.end()) for m in _GROUP.finditer(text)]
    out: list[Candidate] = []
    i = 0
    while i < len(groups):
        # build the maximal chain starting at i with one consistent separator
        chain = [groups[i]]
        sep = None
        j = i + 1
        while j < len(groups):
            prev_end, start = chain[-1][2], groups[j][1]
            between = text[prev_end:start]
            if len(between) != 1 or between not in SEPARATORS or (sep is not None and between != sep):
                break
            sep = between
            chain.append(groups[j])
            j += 1
        for a in range(len(chain)):
            joined = ""
            for b in range(a, len(chain)):
                joined += chain[b][0]
                if len(joined) > MAX_JOINED:
                    break
                out.append(Candidate(joined, chain[a][1], chain[b][2]))
        i = j
    return out


def word_tokens(text: str) -> list[tuple[str, int, int]]:
    return [(m.group(), m.start(), m.end()) for m in re.finditer(r"[^\W\d_]+", text)]


# --- dictionaries -------------------------------------------------------------------

def _lines(name: str) -> list[str]:
    text = resources.files("scoutout.detectors.data").joinpath(name).read_text(encoding="utf-8")
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]


def _sections(name: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    current = None
    text = resources.files("scoutout.detectors.data").joinpath(name).read_text(encoding="utf-8")
    for ln in text.splitlines():
        ln = ln.strip()
        if ln.startswith("# section:"):
            current = ln.split(":", 1)[1]
            out[current] = []
        elif ln and not ln.startswith("#") and current:
            out[current].append(ln)
    return out


@lru_cache(maxsize=1)
def dictionaries() -> dict[str, frozenset[str]]:
    """Folded name and place dictionaries. Override with SCOUTOUT_NAMES_DIR (given.txt / surnames.txt)."""
    import os
    from pathlib import Path

    given, surnames = _lines("tr_given_names.txt"), _lines("tr_surnames.txt")
    custom = os.environ.get("SCOUTOUT_NAMES_DIR")
    if custom:
        d = Path(custom)
        if (d / "given.txt").is_file():
            given += [x.strip() for x in (d / "given.txt").read_text(encoding="utf-8").splitlines() if x.strip()]
        if (d / "surnames.txt").is_file():
            surnames += [x.strip() for x in (d / "surnames.txt").read_text(encoding="utf-8").splitlines() if x.strip()]
    places = _sections("tr_places.txt")
    return {
        "given": frozenset(fold(x) for x in given),
        "surname": frozenset(fold(x) for x in surnames),
        "place": frozenset(fold(x) for k in ("city", "district") for x in places.get(k, [])),
    }
