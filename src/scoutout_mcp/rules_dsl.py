"""The rule language the agent and the local LLM may write (SPEC §6) and the extra MCP-side gate checks.

A rule is declarative JSON, never code:
    {"data_type": "CUSTOMER_NO", "pattern": "(?<![A-Z0-9])MN\\d{8}(?![0-9])", "context": ["müşteri"],
     "checksum": null, "column_names": ["musteri_no"], "ignore_case": false}
The core gate (syntax, ReDoS, digit-run and reference-set leak checks, measurement) then runs as in ScoutOut.
"""

from __future__ import annotations

import re

from scoutout.detectors.normalize import dictionaries, fold, word_tokens
from scoutout.metadata_detector import tokens as name_tokens
from scoutout.rules import CHECKSUMS, literals

TYPE_RX = re.compile(r"^[A-Z][A-Z0-9_]{1,29}$")
CONTEXT_RX = re.compile(r"^[^\W\d_]+(?: [^\W\d_]+){0,3}$")
MAX_LITERAL = 4  # longest run of constant characters a pattern may contain (a rule must generalise, not list values)


def normalize(rule: dict) -> dict:
    """Validate the fields and return the rule body as stored. Raises ValueError with a message for the agent."""
    if not isinstance(rule, dict):
        raise ValueError("Kural bir JSON nesnesi olmalı.")
    unknown = set(rule) - {"data_type", "pattern", "context", "checksum", "column_names", "ignore_case", "description"}
    if unknown:
        raise ValueError(f"Bilinmeyen kural alanı: {', '.join(sorted(unknown))}")
    dtype = str(rule.get("data_type", "")).strip().upper()
    if not TYPE_RX.match(dtype):
        raise ValueError("data_type büyük harf, rakam ve '_' içermeli (ör. CUSTOMER_NO).")
    pattern = rule.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip() or len(pattern) > 300:
        raise ValueError("pattern 1–300 karakterlik bir düzenli ifade olmalı.")
    context = rule.get("context") or []
    if not isinstance(context, list) or len(context) > 10:
        raise ValueError("context en fazla 10 kelimelik bir liste olmalı.")
    ctx = []
    for w in context:
        w = " ".join(str(w).split())
        if not CONTEXT_RX.match(w) or len(w) > 25:
            raise ValueError(f"context kelimesi sadece harf içermeli (en fazla 4 kelime, 25 karakter): {w[:25]!r}")
        ctx.append(w)
    checksum = (str(rule.get("checksum") or "").strip().lower()) or None
    if checksum is not None and checksum not in CHECKSUMS:
        raise ValueError(f"checksum şunlardan biri olmalı: {', '.join(CHECKSUMS)} (veya boş).")
    cols = rule.get("column_names") or []
    if not isinstance(cols, list) or len(cols) > 10:
        raise ValueError("column_names en fazla 10 kolon adı parçası olmalı.")
    col_tokens = []
    for c in cols:
        toks = name_tokens(str(c))
        if not toks:
            continue
        col_tokens.append("_".join(toks)[:40])
    body = {"pattern": pattern, "context": ctx, "checksum": checksum, "column_names": sorted(set(col_tokens)),
            "ignore_case": bool(rule.get("ignore_case"))}
    return {"data_type": dtype, "body": body}


def generality_reason(pattern: str) -> str | None:
    try:
        lits = literals(pattern)
    except Exception:  # noqa: BLE001 - the core gate reports the syntax error
        return None
    long = [x for x in lits if len(x.strip()) > MAX_LITERAL]
    if long:
        return f"desen en fazla {MAX_LITERAL} ardışık sabit karakter içerebilir; kural değer değil biçim tarif etmeli"
    return None


def shown_value_reason(body: dict, shown: list[str], k: int) -> str | None:
    """The local LLM saw real values: its rule must not carry any of them (SPEC §6.1)."""
    names = dictionaries()
    whole = {fold(v.strip()) for v in shown if v and v.strip()}
    for lit in literals(body["pattern"]) if generality_reason(body["pattern"]) is None else []:
        if len(lit) >= 3 and fold(lit.strip()) in whole:
            return "kural gösterilen gerçek bir değeri içeriyor"
    token_sets = [{fold(w) for w, _, _ in word_tokens(v)} for v in shown if v]
    for w in body.get("context", []):
        f = fold(w)
        if f in whole:
            return "bağlam kelimesi gösterilen gerçek bir değerle aynı"
        parts = f.split()
        if any(p in names["given"] or p in names["surname"] for p in parts):
            return "bağlam kelimesi bir kişi adı veya soyadı"
        seen = sum(1 for ts in token_sets if all(p in ts for p in parts))
        if 0 < seen < k:
            return "bağlam kelimesi örneklerde çok seyrek geçiyor (kişiye özgü olabilir)"
    return None
