"""Signals from table/column names, data type and length (spec §5.5). Never decides on its own."""

from __future__ import annotations

import re

from scoutout.detectors.normalize import fold

# Folded name tokens (TR + EN) per type. A keyword is either one token or a token sequence joined by '_'.
KEYWORDS: dict[str, list[str]] = {
    "TCKN": ["tckn", "tckimlik", "tcno", "tc_no", "tc_kimlik", "kimlik_no", "kimlikno", "identity_no", "identityno",
             "national_id", "nationalid", "tc", "tcid", "citizen_id"],
    "YKN": ["ykn", "yabanci_kimlik", "yabancikimlik", "foreign_id", "foreigner_id"],
    "VKN": ["vkn", "vergi_no", "vergino", "vergi_kimlik", "tax_no", "taxno", "tax_id", "taxid", "vergi"],
    "IBAN": ["iban"],
    "PAN": ["card_no", "cardno", "kart_no", "kartno", "pan", "card_number", "cardnumber", "kredi_kart", "credit_card"],
    "MASKED_PAN": ["masked_pan", "maskeli_kart", "masked_card"],
    "PHONE": ["tel", "telefon", "gsm", "phone", "cep", "mobile", "msisdn", "gsm_no", "tel_no"],
    "EMAIL": ["email", "e_mail", "eposta", "e_posta", "mail"],
    "ADDRESS": ["adres", "address", "addr", "adress"],
    "GIVEN_NAME": ["ad", "adi", "isim", "first_name", "firstname", "given_name"],
    "SURNAME": ["soyad", "soyadi", "last_name", "lastname", "surname"],
    "PERSON_NAME": ["ad_soyad", "adsoyad", "full_name", "fullname", "musteri_adi", "customer_name", "unvan"],
    "BIRTH_DATE": ["dogum", "dogum_tarihi", "birth", "birth_date", "birthdate", "dob"],
    "PASSPORT": ["pasaport", "passport"],
    "PLATE": ["plaka", "plate"],
    "SWIFT": ["swift", "bic"],
    "IP": ["ip", "ip_address", "ipaddress", "ip_adres"],
}

TYPE_FIT = {  # (compatible base types, optional exact/max length predicate)
    "TCKN": ({"char", "varchar", "nchar", "nvarchar", "bigint", "decimal", "numeric"}, lambda n: n in (None, 11)),
    "YKN": ({"char", "varchar", "nchar", "nvarchar", "bigint"}, lambda n: n in (None, 11)),
    "VKN": ({"char", "varchar", "nchar", "nvarchar", "bigint"}, lambda n: n in (None, 10)),
    "IBAN": ({"char", "varchar", "nchar", "nvarchar"}, lambda n: n is None or n >= 26),
    "PAN": ({"char", "varchar", "nchar", "nvarchar", "bigint", "decimal", "numeric"}, lambda n: n is None or 13 <= n <= 25),
    "BIRTH_DATE": ({"date", "datetime", "datetime2", "smalldatetime", "char", "varchar", "nvarchar"}, None),
}

_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def tokens(name: str) -> list[str]:
    parts = []
    for chunk in re.split(r"[\W_]+", name):
        parts += _CAMEL.findall(chunk)
    return [fold(p) for p in parts if p]


def column_scores(column: str, data_type: str = "", max_length: int | None = None,
                  learned: dict[str, dict[str, float]] | None = None) -> dict[str, float]:
    """Type -> score in [0, 1] from the column name; ``learned`` adds institution-specific token weights."""
    toks = tokens(column)
    joined = "_".join(toks)
    compact = "".join(toks)
    scores: dict[str, float] = {}
    for dtype, words in KEYWORDS.items():
        best = 0.0
        for w in words:
            seq = w.split("_")
            if any(toks[i : i + len(seq)] == seq for i in range(len(toks))):
                best = max(best, 0.85 if len(seq) > 1 or len(w) >= 4 else 0.6)
            elif len(w.replace("_", "")) >= 5 and w.replace("_", "") in compact:
                best = max(best, 0.6)
        if best:
            fit = TYPE_FIT.get(dtype)
            if fit:
                types, length_ok = fit
                if data_type and data_type not in types:
                    best *= 0.3
                elif length_ok and length_ok(max_length):
                    best = min(1.0, best + 0.1)
            scores[dtype] = round(best, 3)
    for dtype, weights in (learned or {}).items():
        s = max((weights.get(t, 0.0) for t in toks + [joined]), default=0.0)
        if s > scores.get(dtype, 0.0):
            scores[dtype] = round(s, 3)
    return scores
