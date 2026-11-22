"""Generic detectors for the banking domain (spec §5.4).

A detector finds spans of one sensitive data type in a cell's text. Checksum-based detectors also report
how many *candidates* (strings of the right shape) they saw: random numbers pass a checksum with a known
probability (``chance``), which the decision engine uses to tell real identifiers from look-alikes
(DECISIONS D-028).
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field

from .normalize import Candidate, dictionaries, fold, word_tokens
from .validators.checksums import IBAN_LENGTHS, iban_valid, luhn_valid, tckn_valid, vkn_valid


@dataclass(frozen=True)
class Span:
    data_type: str
    start: int
    end: int
    detector_id: str


@dataclass
class DetectorMeta:
    id: str
    version: str
    data_type: str
    level: str  # "value" (whole cell) or "span" (inside free text)
    source: str = "generic"  # generic | base-derived | llm-derived
    se: float = 0.97
    sp: float = 0.998
    chance: float | None = None  # P(random shaped candidate passes the check)


@dataclass
class Finding:
    spans: list[Span] = field(default_factory=list)
    candidates: dict[str, int] = field(default_factory=dict)  # data_type -> shaped candidates seen


class Detector:
    meta: DetectorMeta

    def scan(self, text: str, cands: list[Candidate], finding: Finding, ctx: dict) -> None:  # pragma: no cover
        raise NotImplementedError


def _context_near(text: str, start: int, end: int, words: tuple[str, ...], window: int = 25) -> bool:
    around = fold(text[max(0, start - window) : end + window])
    return any(w in around for w in words)


# -- checksum identifiers -----------------------------------------------------------------

class TcknDetector(Detector):
    meta = DetectorMeta("tckn", "1", "TCKN", "span", chance=0.01)

    def scan(self, text, cands, finding, ctx):
        n = 0
        for c in cands:
            d = c.digits
            if len(d) == 11 and d[0] != "0" and not d.startswith("99"):
                n += 1
                if tckn_valid(d):
                    finding.spans.append(Span("TCKN", c.start, c.end, self.meta.id))
        finding.candidates["TCKN"] = finding.candidates.get("TCKN", 0) + n


class YknDetector(Detector):
    meta = DetectorMeta("ykn", "1", "YKN", "span", chance=0.01)

    def scan(self, text, cands, finding, ctx):
        n = 0
        for c in cands:
            if len(c.digits) == 11 and c.digits.startswith("99"):
                n += 1
                if tckn_valid(c.digits):
                    finding.spans.append(Span("YKN", c.start, c.end, self.meta.id))
        finding.candidates["YKN"] = finding.candidates.get("YKN", 0) + n


class VknDetector(Detector):
    meta = DetectorMeta("vkn", "1", "VKN", "span", chance=0.1)

    def scan(self, text, cands, finding, ctx):
        n = 0
        for c in cands:
            if len(c.digits) == 10:
                n += 1
                if vkn_valid(c.digits):
                    finding.spans.append(Span("VKN", c.start, c.end, self.meta.id))
        finding.candidates["VKN"] = finding.candidates.get("VKN", 0) + n


# IIN/BIN ranges of the major schemes (ISO/IEC 7812 issuer prefixes as published by the schemes).
def pan_brand(d: str) -> str | None:
    n = len(d)
    if d[0] == "4" and n in (13, 16, 19):
        return "VISA"
    p2, p4, p6 = int(d[:2]), int(d[:4]), int(d[:6])
    if n == 16 and (51 <= p2 <= 55 or 2221 <= p4 <= 2720):
        return "MASTERCARD"
    if n == 15 and p2 in (34, 37):
        return "AMEX"
    if n == 16 and d.startswith("9792"):
        return "TROY"
    if n in (16, 19) and (d.startswith("6011") or d.startswith("65") or 644 <= int(d[:3]) <= 649):
        return "DISCOVER"
    if 16 <= n <= 19 and 3528 <= p4 <= 3589:
        return "JCB"
    if 16 <= n <= 19 and 622126 <= p6 <= 622925 or (16 <= n <= 19 and 624 <= int(d[:3]) <= 628):
        return "UNIONPAY"
    if 12 <= n <= 19 and d[:2] in ("50", "56", "57", "58", "63", "67"):
        return "MAESTRO"
    return None


class PanDetector(Detector):
    meta = DetectorMeta("pan", "1", "PAN", "span", chance=0.1)

    def scan(self, text, cands, finding, ctx):
        n = 0
        for c in cands:
            if 13 <= len(c.digits) <= 19 and pan_brand(c.digits):
                n += 1
                if luhn_valid(c.digits):
                    finding.spans.append(Span("PAN", c.start, c.end, self.meta.id))
        finding.candidates["PAN"] = finding.candidates.get("PAN", 0) + n


_IBAN = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?)(?![A-Za-z0-9])")


class IbanDetector(Detector):
    meta = DetectorMeta("iban", "1", "IBAN", "span", chance=1 / 97)

    def scan(self, text, cands, finding, ctx):
        n = 0
        for m in _IBAN.finditer(text):
            compact = m.group(1).replace(" ", "")
            if compact[:2] in IBAN_LENGTHS and len(compact) == IBAN_LENGTHS[compact[:2]]:
                n += 1
                if iban_valid(compact):
                    finding.spans.append(Span("IBAN", m.start(1), m.end(1), self.meta.id))
        finding.candidates["IBAN"] = finding.candidates.get("IBAN", 0) + n


# -- formats -------------------------------------------------------------------------------

def _regex_detector(det_id: str, data_type: str, pattern: str, flags=0, context: tuple[str, ...] = (),
                    whole_cell_ok: bool = True, validate=None, se=0.97, sp=0.998):
    rx = re.compile(pattern, flags)

    class _D(Detector):
        meta = DetectorMeta(det_id, "1", data_type, "span", se=se, sp=sp)

        def scan(self, text, cands, finding, ctx):
            for m in rx.finditer(text):
                s, e = m.span()
                if validate and not validate(m.group()):
                    continue
                whole = whole_cell_ok and s == 0 and e == len(text.rstrip())
                if context and not whole and not _context_near(text, s, e, context):
                    continue
                finding.spans.append(Span(data_type, s, e, det_id))

    _D.__name__ = f"{data_type.title()}Detector"
    return _D()


_MOBILE = r"(?<![\d+])(?:\+90[ .-]?|0090[ .-]?|0[ .-]?)?\(?5\d{2}\)?[ .-]?\d{3}[ .-]?\d{2}[ .-]?\d{2}(?!\d)"
_LANDLINE = r"(?<![\d+])(?:\+90[ .-]?|0)\(?[2-4]\d{2}\)?[ .-]?\d{3}[ .-]?\d{2}[ .-]?\d{2}(?!\d)"
_E164 = r"(?<![\d+])\+(?!90)[1-9]\d{9,13}(?!\d)"


_EMAIL_RX = r"(?<![\w.%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])"
_IP_RX = r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])|(?<![:\w])(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}(?![:\w])"


def _valid_ip(s: str) -> bool:
    try:
        ip = ipaddress.ip_address(s)
    except ValueError:
        return False
    return not (ip.is_unspecified or ip.is_loopback)


def build_generic() -> list[Detector]:
    return [
        TcknDetector(), YknDetector(), VknDetector(), PanDetector(), IbanDetector(),
        _regex_detector("phone", "PHONE", f"{_MOBILE}|{_LANDLINE}|{_E164}"),
        _regex_detector("email", "EMAIL", _EMAIL_RX),
        _regex_detector("masked_pan", "MASKED_PAN",
                        r"(?<![\dX*])(?:\d{4}[ -]?[X*x•]{4}[ -]?[X*x•]{4}[ -]?\d{4}|\d{6}[X*x•]{6}\d{4})(?![\dX*])"),
        _regex_detector("passport", "PASSPORT", r"(?<![A-Za-z0-9])[UZS]\d{8}(?![A-Za-z0-9])",
                        context=("pasaport", "passport")),
        _regex_detector("plate", "PLATE",
                        r"(?<![A-Za-z0-9])(?:0[1-9]|[1-7]\d|8[01]) ?[A-Z]{1,3} ?\d{2,4}(?![A-Za-z0-9])",
                        context=("plaka", "plate", "arac")),
        _regex_detector("swift", "SWIFT", r"(?<![A-Za-z0-9])[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?(?![A-Za-z0-9])",
                        context=("swift", "bic"), validate=lambda s: s[4:6] in IBAN_LENGTHS or s[4:6] == "US"),
        _regex_detector("ip", "IP", _IP_RX, validate=_valid_ip),
        _regex_detector("mac", "MAC", r"(?<![0-9A-Fa-f:-])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:-])"),
        AddressDetector(), NameDetector(), BirthDateDetector(),
    ]


# -- semantic types --------------------------------------------------------------------------

_ADDR_PARTS = {
    "street": re.compile(r"\b(mah(alle(si)?)?|mh|cad(de(si)?)?|cd|sok(ak|agi)?|sk|bulvar(i)?|blv|meydan(i)?)\b\.?"),
    "number": re.compile(r"\b(no|numara)\s*[:.]?\s*\d+"),
    "unit": re.compile(r"\b(d|daire|kat|apt|apartman(i)?|blok)\s*[:.]?\s*\d+"),
    "postcode": re.compile(r"(?<!\d)\d{5}(?!\d)"),
}


_FOLDED_WORD = re.compile(r"[^\W\d_]+")


class AddressDetector(Detector):
    meta = DetectorMeta("address_tr", "1", "ADDRESS", "span", se=0.95, sp=0.998)

    def scan(self, text, cands, finding, ctx):
        f = fold(text)
        kinds = {k for k, rx in _ADDR_PARTS.items() if rx.search(f)}
        if not kinds:
            return  # a place name alone is not an address
        places = dictionaries()["place"]
        if any(w in places for w in _FOLDED_WORD.findall(f)):
            kinds.add("place")
        if len(kinds - {"postcode"}) >= 2 and ("street" in kinds or "place" in kinds):
            finding.spans.append(Span("ADDRESS", 0, len(text), self.meta.id))


class NameDetector(Detector):
    """Cell that is exactly one given name / surname, or a given name directly followed by a surname."""

    meta = DetectorMeta("name_tr", "1", "PERSON_NAME", "span", se=0.95, sp=0.998)

    def scan(self, text, cands, finding, ctx):
        d = dictionaries()
        toks = ctx.get("words") or word_tokens(text)
        if len(toks) == 1 and toks[0][1] <= 1 and toks[0][2] >= len(text.rstrip()) - 1:
            w = fold(toks[0][0])
            if w in d["given"]:
                finding.spans.append(Span("GIVEN_NAME", toks[0][1], toks[0][2], self.meta.id))
            if w in d["surname"]:
                finding.spans.append(Span("SURNAME", toks[0][1], toks[0][2], self.meta.id))
            return
        for (a, s1, _e1), (b, _s2, e2) in zip(toks, toks[1:], strict=False):
            if a[:1].isupper() and b[:1].isupper() and fold(a) in d["given"] and fold(b) in d["surname"]:
                finding.spans.append(Span("PERSON_NAME", s1, e2, self.meta.id))


class BirthDateDetector(Detector):
    """Dates are only personal data with context: a date-typed column whose name says 'birth'."""

    meta = DetectorMeta("birth_date", "1", "BIRTH_DATE", "value", se=0.99, sp=0.999)
    HINTS = ("dogum", "birth", "dob", "dgm")

    def scan(self, text, cands, finding, ctx):
        if ctx.get("is_date_column") and any(h in ctx.get("column_folded", "") for h in self.HINTS):
            finding.spans.append(Span("BIRTH_DATE", 0, len(text), self.meta.id))
