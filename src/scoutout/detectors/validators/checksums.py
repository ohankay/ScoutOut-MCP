"""Checksum algorithms for structured identifiers.

Each algorithm is implemented from its published definition and cross-checked in the
test-suite against an independent reference implementation (python-stdnum) plus known
valid/invalid vectors. Both a ``*_valid`` predicate and a ``*_check_digits`` helper (used by
the synthetic generator) are provided so generator and validator cannot silently diverge.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# TCKN (T.C. Kimlik No) / YKN (Yabancı Kimlik No)
# ---------------------------------------------------------------------------


def tckn_check_digits(first9: str) -> str:
    """Return the 10th and 11th digits for the given first 9 digits.

    d10 = ((d1+d3+d5+d7+d9)*7 - (d2+d4+d6+d8)) mod 10
    d11 = (d1 + ... + d10) mod 10
    """
    if len(first9) != 9 or not first9.isdigit():
        raise ValueError("first9 must be 9 digits")
    d = [int(c) for c in first9]
    odd = d[0] + d[2] + d[4] + d[6] + d[8]
    even = d[1] + d[3] + d[5] + d[7]
    d10 = (odd * 7 - even) % 10  # Python's % is non-negative for positive modulus
    d11 = (sum(d) + d10) % 10
    return f"{d10}{d11}"


def tckn_valid(value: str) -> bool:
    """Validate an 11-digit Turkish national identity number (first digit != 0)."""
    if len(value) != 11 or not value.isascii() or not value.isdigit() or value[0] == "0":
        return False
    return tckn_check_digits(value[:9]) == value[9:]


def ykn_valid(value: str) -> bool:
    """Foreign resident identity number: starts with 99 and follows the TCKN checksum."""
    return value.startswith("99") and tckn_valid(value)


# ---------------------------------------------------------------------------
# VKN (Vergi Kimlik No) — Turkish tax identification number (10 digits)
# Algorithm as published by GİB: for i = 1..9 (from the left)
#   t = (d_i + 10 - i) mod 10
#   v = 9 if t == 9 else (t * 2^(10 - i)) mod 9
#   check = (10 - (sum v) mod 10) mod 10
# ---------------------------------------------------------------------------


def vkn_check_digit(first9: str) -> str:
    if len(first9) != 9 or not first9.isdigit():
        raise ValueError("first9 must be 9 digits")
    total = 0
    for i, ch in enumerate(first9, start=1):
        t = (int(ch) + 10 - i) % 10
        total += 9 if t == 9 else (t * (2 ** (10 - i))) % 9
    return str((10 - total % 10) % 10)


def vkn_valid(value: str) -> bool:
    if len(value) != 10 or not value.isascii() or not value.isdigit():
        return False
    return vkn_check_digit(value[:9]) == value[9]


# ---------------------------------------------------------------------------
# IBAN — ISO 13616, mod 97-10 (ISO 7064)
# ---------------------------------------------------------------------------

# Total IBAN length per country (SWIFT IBAN registry). Subset relevant for banking data;
# unknown countries are validated by checksum only with a generic 15..34 length bound.
IBAN_LENGTHS: dict[str, int] = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BG": 22,
    "BH": 22, "BR": 29, "CH": 21, "CY": 28, "CZ": 24, "DE": 22, "DK": 18, "EE": 20,
    "EG": 29, "ES": 24, "FI": 18, "FO": 18, "FR": 27, "GB": 22, "GE": 22, "GI": 23,
    "GL": 18, "GR": 27, "HR": 21, "HU": 28, "IE": 22, "IL": 23, "IQ": 23, "IS": 26,
    "IT": 27, "JO": 30, "KW": 30, "KZ": 20, "LB": 28, "LI": 21, "LT": 20, "LU": 20,
    "LV": 21, "MC": 27, "MD": 24, "ME": 22, "MK": 19, "MT": 31, "MU": 30, "NL": 18,
    "NO": 15, "PK": 24, "PL": 28, "PS": 29, "PT": 25, "QA": 29, "RO": 24, "RS": 22,
    "SA": 24, "SE": 24, "SI": 19, "SK": 24, "SM": 27, "TN": 24, "TR": 26, "UA": 29,
    "XK": 20,
}


def _iban_to_int_string(rearranged: str) -> str:
    out = []
    for ch in rearranged:
        if ch.isdigit():
            out.append(ch)
        elif "A" <= ch <= "Z":
            out.append(str(ord(ch) - 55))  # A=10 ... Z=35
        else:
            raise ValueError(f"invalid IBAN character {ch!r}")
    return "".join(out)


def _mod97(numeric: str) -> int:
    # Piecewise to stay fast and avoid huge ints (also mirrors the ISO 7064 description).
    rem = 0
    for i in range(0, len(numeric), 9):
        rem = int(str(rem) + numeric[i : i + 9]) % 97
    return rem


def iban_check_digits(country: str, bban: str) -> str:
    country = country.upper()
    rearranged = bban.upper() + country + "00"
    return f"{98 - _mod97(_iban_to_int_string(rearranged)):02d}"


def iban_valid(value: str) -> bool:
    """Validate an electronic-format IBAN (no spaces, uppercase)."""
    if not value.isascii() or not value.isalnum():
        return False
    v = value.upper()
    if len(v) < 15 or len(v) > 34 or not v[:2].isalpha() or not v[2:4].isdigit():
        return False
    expected_len = IBAN_LENGTHS.get(v[:2])
    if expected_len is not None and len(v) != expected_len:
        return False
    if v[:2] == "TR" and not v[4:].isdigit():
        return False
    try:
        return _mod97(_iban_to_int_string(v[4:] + v[:4])) == 1
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Luhn (ISO/IEC 7812-1) — payment card numbers
# ---------------------------------------------------------------------------


def luhn_check_digit(payload: str) -> str:
    """Return the Luhn check digit to append to ``payload``."""
    if not payload.isdigit():
        raise ValueError("payload must be digits")
    total = 0
    # Rightmost payload digit is doubled (it will be 2nd from right after appending).
    for i, ch in enumerate(reversed(payload)):
        n = int(ch)
        if i % 2 == 0:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return str((10 - total % 10) % 10)


def luhn_valid(value: str) -> bool:
    if len(value) < 2 or not value.isascii() or not value.isdigit():
        return False
    return luhn_check_digit(value[:-1]) == value[-1]
