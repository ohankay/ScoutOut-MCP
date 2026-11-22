"""T-SQL expression builders for deterministic, set-based synthetic data generation.

Every row is identified by an integer ``n`` (alias ``t.n``). All values are pure functions of
``(seed, salt, n)``, so the same seed always yields the same database. Generated identifiers
carry correct check digits (TCKN, VKN, IBAN, Luhn); the test-suite reads them back and
validates them with the Python validators, so the two implementations cannot drift apart.

Expressions that need intermediate values are expressed as ``CROSS APPLY`` clauses; a builder
returns ``Expr(applies, sql)``.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from importlib import resources

P = 2147483647  # Mersenne prime 2^31-1; all products stay below 2^62 (fits bigint)


@dataclass
class Expr:
    sql: str
    applies: list[str] = field(default_factory=list)


def sql_str(s: str) -> str:
    return "N'" + s.replace("'", "''") + "'"


class Gen:
    """Builds deterministic expressions for one seed. ``salt`` separates independent streams."""

    def __init__(self, seed: int):
        self.seed = seed
        self._alias = 0

    def _alias_name(self, prefix: str) -> str:
        self._alias += 1
        return f"{prefix}{self._alias}"

    # -- randomness ------------------------------------------------------------
    def rnd(self, salt: str) -> str:
        """Integer in [0, P). Affine -> square -> affine mixing of n (non-linear across salts)."""
        r = random.Random(f"{self.seed}:{salt}")
        a1, c1, c2, a3, c3 = (r.randrange(1, P) for _ in range(5))
        h1 = f"((t.n % {P}) * {a1} + {c1}) % {P}"
        h2 = f"(({h1}) * ({h1}) + {c2}) % {P}"
        return f"((({h2}) * {a3} + {c3}) % {P})"

    def below(self, salt: str, k: int) -> str:
        return f"({self.rnd(salt)} % {k})"

    def flag(self, salt: str, rate: float) -> str:
        """Boolean SQL predicate true for ~``rate`` of rows."""
        return f"({self.rnd(salt)} % 10000 < {round(rate * 10000)})"

    def digits(self, salt: str, k: int) -> str:
        """k random decimal digits as varchar (leading zeros kept). k <= 18."""
        parts = []
        i = 0
        while k > 0:
            take = min(k, 9)
            parts.append(
                f"RIGHT(REPLICATE('0',{take}) + CAST({self.rnd(f'{salt}#{i}')} % {10**take} AS varchar(9)),"
                f" {take})"
            )
            k -= take
            i += 1
        return parts[0] if len(parts) == 1 else "CONCAT(" + ", ".join(parts) + ")"

    def choose(self, salt: str, options: list[str]) -> str:
        return f"CHOOSE(1 + {self.below(salt, len(options))}, {', '.join(sql_str(o) for o in options)})"

    def choose_idx(self, idx_sql: str, options: list[str]) -> str:
        return f"CHOOSE(1 + {idx_sql}, {', '.join(sql_str(o) for o in options)})"

    # -- identifiers -------------------------------------------------------------
    @staticmethod
    def _d(alias: str, col: str, i: int) -> str:
        return f"(ASCII(SUBSTRING({alias}.{col}, {i}, 1)) - 48)"

    def tckn(self, salt: str, prefix: str = "") -> Expr:
        """Valid TCKN. ``prefix`` (e.g. '99' for YKN) replaces the leading digits."""
        a, b = self._alias_name("tk"), self._alias_name("tc")
        rest = 9 - len(prefix)
        if prefix:
            base = f"CONCAT('{prefix}', {self.digits(salt, rest)})"
        else:
            base = f"CONCAT(CAST(1 + {self.below(salt + ':f', 9)} AS varchar(1)), {self.digits(salt, 8)})"
        d = lambda i: self._d(a, "s", i)  # noqa: E731
        odd = "+".join(d(i) for i in (1, 3, 5, 7, 9))
        even = "+".join(d(i) for i in (2, 4, 6, 8))
        total9 = "+".join(d(i) for i in range(1, 10))
        applies = [
            f"CROSS APPLY (SELECT CAST({base} AS varchar(9)) AS s) {a}",
            f"CROSS APPLY (SELECT ((((({odd})*7 - ({even})) % 10) + 10) % 10) AS d10) {b}",
        ]
        sql = f"CONCAT({a}.s, {b}.d10, ({total9} + {b}.d10) % 10)"
        return Expr(sql, applies)

    def vkn(self, salt: str) -> Expr:
        a = self._alias_name("vk")
        terms = []
        for i in range(1, 10):
            t = f"(({self._d(a, 's', i)} + {10 - i}) % 10)"
            terms.append(f"CASE WHEN {t} = 9 THEN 9 ELSE ({t} * {2 ** (10 - i)}) % 9 END")
        applies = [f"CROSS APPLY (SELECT CAST({self.digits(salt, 9)} AS varchar(9)) AS s) {a}"]
        sql = f"CONCAT({a}.s, (10 - ({' + '.join(terms)}) % 10) % 10)"
        return Expr(sql, applies)

    def iban_tr(self, salt: str, bank_codes: list[str]) -> Expr:
        a = self._alias_name("ib")
        bban = f"CONCAT({self.choose_idx(self.below(salt + ':b', len(bank_codes)), bank_codes)}, '0', " \
               f"{self.digits(salt, 16)})"
        # 'T'=29, 'R'=27 ; checksum = 98 - (BBAN || 2927 || 00) mod 97 ; 28 digits fits decimal(38,0)
        applies = [f"CROSS APPLY (SELECT CAST({bban} AS varchar(22)) AS s) {a}"]
        check = f"RIGHT('0' + CAST(98 - CAST(CONCAT({a}.s, '292700') AS decimal(38,0)) % 97 AS varchar(2)), 2)"
        return Expr(f"CONCAT('TR', {check}, {a}.s)", applies)

    def luhn_pan(self, salt: str, bins: list[str], length: int = 16) -> Expr:
        """Luhn-valid PAN starting with one of ``bins`` (all bins must have the same length)."""
        blen = len(bins[0])
        assert all(len(b) == blen for b in bins)
        plen = length - 1
        a = self._alias_name("pn")
        payload = f"CONCAT({self.choose_idx(self.below(salt + ':b', len(bins)), bins)}, " \
                  f"{self.digits(salt, plen - blen)})"
        terms = []
        for pos in range(1, plen + 1):
            x = self._d(a, "s", pos)
            # positions counted from the right of the payload: rightmost is doubled
            if (plen - pos) % 2 == 0:
                terms.append(f"(2*{x} - 9*({x}/5))")
            else:
                terms.append(x)
        applies = [f"CROSS APPLY (SELECT CAST({payload} AS varchar({plen})) AS s) {a}"]
        return Expr(f"CONCAT({a}.s, (10 - ({' + '.join(terms)}) % 10) % 10)", applies)


# -- word lists -------------------------------------------------------------------

def _read_list(name: str) -> list[str]:
    text = resources.files("scoutout.detectors.data").joinpath(name).read_text(encoding="utf-8")
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]


def _read_sections(name: str) -> dict[str, list[str]]:
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


_FOLD = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")


def ascii_fold(s: str) -> str:
    return s.translate(_FOLD)


GIVEN_NAMES = _read_list("tr_given_names.txt")
SURNAMES = _read_list("tr_surnames.txt")
PLACES = _read_sections("tr_places.txt")
