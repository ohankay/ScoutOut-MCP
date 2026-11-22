"""Candidate rules (spec §5.7): base-derived shape rules and LLM proposals, all through the same gate:
syntax + ReDoS -> leak check -> measurement -> threshold -> human approval. Run-time detection stays deterministic."""

from __future__ import annotations

import json
import logging
import re
import re._parser as sre_parse  # stdlib regex parser, used only to list a pattern's literals
from collections import Counter
from importlib import resources
from pathlib import Path

import numpy as np

from scoutout.core.config import Config
from scoutout.core.redact import redact
from scoutout.detectors import generic_detectors, scan_cell
from scoutout.detectors.normalize import fold, word_tokens
from scoutout.profiler import shape
from scoutout.store import Store, now

log = logging.getLogger("scoutout")

MIN_RECALL = 0.9
MIN_PRECISION = 0.9
MEASURE_SECONDS = 20.0  # a usable rule measures thousands of values in well under a second
ADVERSARIAL = ["a" * 4000 + "!", "1" * 4000 + "x", " " * 4000 + "x", "aA1-" * 1000, "\n" * 2000 + "a",
               "@" * 3000, "MN" + "0" * 4000 + "x"]
_NESTED_QUANT = re.compile(r"\((?:[^()\\]|\\.)*[+*}]\)\s*[+*{]")


# --- the gate -----------------------------------------------------------------------------------

def check_syntax_and_redos(pattern: str) -> str | None:
    import regex

    if not pattern or len(pattern) > 500:
        return "boş veya çok uzun desen"
    try:
        rx = regex.compile(pattern)
    except regex.error as e:
        return f"derlenemiyor: {e}"
    if _NESTED_QUANT.search(pattern):
        return "iç içe tekrar (felaket geri izleme riski)"
    for s in ADVERSARIAL:
        try:
            rx.search(s, timeout=0.2)
        except TimeoutError:
            return "zaman aşımı (felaket geri izleme)"
    if rx.search(""):
        return "boş metinle eşleşiyor"
    return None


def literals(pattern: str) -> list[str]:
    """All literal character runs of a regex, including every branch of an alternation."""
    out: list[str] = []

    def walk(items):
        buf = ""
        for op, av in items:
            name = str(op)
            if name == "LITERAL":
                buf += chr(av)
                continue
            if buf:
                out.append(buf)
                buf = ""
            if name == "BRANCH":
                for b in av[1]:
                    walk(b)
            elif name == "SUBPATTERN":
                walk(av[-1])
            elif name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
                walk(av[2])
            elif name in ("ASSERT", "ASSERT_NOT"):
                walk(av[1])
        if buf:
            out.append(buf)

    walk(sre_parse.parse(pattern))
    return out


def leak_reason(body: dict, key: bytes, ref_dir: Path | None) -> str | None:
    """Spec §4.5: reject a rule whose body contains a real baseline value (or anything that looks like one)."""
    from scoutout.knowledge import h64, normalize_value

    texts = literals(body["pattern"]) + list(body.get("context", [])) + list(body.get("words", []))
    for t in texts:
        if re.search(r"\d{4,}", t):
            return f"kural içinde {len(re.search(r'[0-9]{4,}', t).group())} haneli sayı var"
    if ref_dir is None or not ref_dir.exists():
        return None
    sets = {f.stem: np.load(f) for f in ref_dir.glob("*.npy")}
    for t in texts:
        candidates = {t, t.strip()} | {w for w, _, _ in word_tokens(t)}
        for dtype, arr in sets.items():
            if dtype == "NAME_PAIR":
                continue
            for c in candidates:
                for tok in normalize_value(dtype, c):
                    x = h64(key, dtype, tok)
                    i = int(np.searchsorted(arr, np.uint64(x)))
                    if i < len(arr) and int(arr[i]) == x:
                        return "kural tabandaki gerçek bir değeri içeriyor"
    return None


CHECKSUMS = {"tckn": "tckn_valid", "vkn": "vkn_valid", "luhn": "luhn_valid", "iban": "iban_valid"}
_SEPARATORS = re.compile(r"[\s\-./]")


def checksum_fn(name: str | None):
    """The validator a rule may require (only the verified built-in algorithms)."""
    if not name:
        return None
    from scoutout.detectors.validators import checksums

    if name not in CHECKSUMS:
        raise ValueError(f"Bilinmeyen doğrulama algoritması: {name}. Seçenekler: {', '.join(CHECKSUMS)}")
    return getattr(checksums, CHECKSUMS[name])


def rule_matches(rx, text: str, context: list[str], check=None) -> list:
    """Matches of a rule in one value: regex, then optional nearby context word, then optional checksum."""
    try:
        found = list(rx.finditer(text, timeout=0.05))
    except TimeoutError:
        return []
    out = []
    for m in found:
        if context and not any(w in fold(text[max(0, m.start() - 30) : m.end() + 30]) for w in context):
            continue
        if check is not None and not check(_SEPARATORS.sub("", m.group()).upper()):
            continue
        out.append(m)
    return out


def measure(pattern: str, positives: list[str], negatives: list[str], context: list[str] | None = None,
            checksum: str | None = None, ignore_case: bool = False) -> dict:
    import regex

    rx = regex.compile(pattern, regex.IGNORECASE if ignore_case else 0)
    ctx = [fold(w) for w in (context or [])]
    check = checksum_fn(checksum)

    import time

    deadline = time.monotonic() + MEASURE_SECONDS

    def hit(text: str) -> bool:
        if time.monotonic() > deadline:  # a slow (not catastrophic) pattern times out on every value: stop early
            raise TimeoutError
        return bool(rule_matches(rx, text, ctx, check))

    tp = sum(hit(v) for v in positives)
    fp = sum(hit(v) for v in negatives)
    sens = tp / len(positives) if positives else 0.0
    spec = 1 - fp / len(negatives) if negatives else 1.0
    prec = tp / (tp + fp) if tp + fp else 0.0
    return {"sensitivity": round(sens, 4), "specificity": round(spec, 4), "precision": round(prec, 4),
            "positives": len(positives), "negatives": len(negatives), "tp": tp, "fp": fp}


def signature(body: dict) -> tuple:
    """What makes two rules the same: the pattern AND its conditions (a context-gated variant is a different rule)."""
    return (body["pattern"], tuple(sorted(fold(w) for w in body.get("context") or [])), body.get("checksum") or None,
            bool(body.get("ignore_case")))


def find_rule(store: Store, dtype: str, body: dict):
    sig = signature(body)
    for r in store.q("SELECT * FROM rules WHERE data_type=? AND json_extract(body_json, '$.pattern')=? ORDER BY id DESC",
                     dtype, body["pattern"]):
        if signature(json.loads(r["body_json"])) == sig:
            return r
    return None


def submit(store: Store, user: str, dtype: str, body: dict, source: str, key: bytes, ref_dir: Path | None,
           positives: list[str], negatives: list[str], pre_reason: str | None = None, min_recall: float = MIN_RECALL,
           min_matches: int = 1) -> str:
    """Run a proposed rule through the gate and record it. Returns its status.

    ``pre_reason``: a rejection found by the caller's own checks (e.g. the MCP generality / shown-value checks)."""
    if find_rule(store, dtype, body) is not None:
        return "DUPLICATE"
    reason = pre_reason or check_syntax_and_redos(body["pattern"]) or leak_reason(body, key, ref_dir)
    metrics = {}
    if reason is None:
        try:
            metrics = measure(body["pattern"], positives, negatives, body.get("context"), body.get("checksum"),
                              bool(body.get("ignore_case")))
        except TimeoutError:
            metrics, reason = {}, "desen çok yavaş çalışıyor (ölçüm süresi aşıldı); daha basit bir desen yazın"
        if reason is None and metrics["tp"] < min_matches:
            reason = f"pozitif örneklerde yeterli eşleşme yok ({min_matches} gerekli)"
        if reason is None and (metrics["sensitivity"] < min_recall or metrics["precision"] < MIN_PRECISION):
            reason = f"eşik altında (duyarlılık {metrics['sensitivity']:.2f}, kesinlik {metrics['precision']:.2f})"
    status = "CANDIDATE" if reason is None else "REJECTED"
    store.x("INSERT INTO rules (data_type, kind, body_json, source, status, metrics_json, created_at, decided_by, decided_at, reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)", dtype, "regex", json.dumps(body, ensure_ascii=False), source, status,
            json.dumps(metrics), now(), None if status == "CANDIDATE" else "sistem", None if status == "CANDIDATE" else now(),
            reason)
    store.audit(user, f"rule.{'candidate' if status == 'CANDIDATE' else 'auto_rejected'}", dtype, source=source,
                pattern=body["pattern"], reason=reason)
    return status


# --- base-derived rules: generalise the dominant value shape of a baseline column ------------------------

def shape_pattern(values: list[str], min_share: float = 0.95) -> str | None:
    vals = [v.strip() for v in values if v and v.strip()]
    if len(vals) < 50:
        return None
    top, count = Counter(shape(v, max_len=64) for v in vals).most_common(1)[0]
    if count / len(vals) < min_share or len(top) > 40:
        return None
    same = [v for v in vals if shape(v, max_len=64) == top]
    parts: list[str] = []
    for i, cls in enumerate(top):
        chars = Counter(v[i] for v in same)
        ch, n = chars.most_common(1)[0]
        if cls in "dXx" and n / len(same) >= 0.99 and not ch.isdigit():
            parts.append(re.escape(ch))  # a constant letter (e.g. a prefix 'MN'); constant digits are never kept
        elif cls == "d":
            parts.append(r"\d")
        elif cls == "X":
            parts.append("[A-Z]")
        elif cls == "x":
            parts.append("[a-z]")
        else:
            parts.append(re.escape(cls))
    out, i = "", 0
    while i < len(parts):
        j = i
        while j < len(parts) and parts[j] == parts[i]:
            j += 1
        out += parts[i] + (f"{{{j - i}}}" if j - i > 1 else "")
        i = j
    return r"(?<![A-Za-z0-9])" + out + r"(?![A-Za-z0-9])"


def mine_base_rules(store: Store, user: str, samples: dict, neg_samples: dict, positives, key: bytes,
                    ref_dir: Path) -> int:
    from scoutout.knowledge import bkey

    covered = {d.meta.data_type for d in generic_detectors()} | {"GIVEN_NAME", "SURNAME"}
    by_type: dict[str, list[str]] = {}
    for b in positives:
        if b["data_type"] in covered:
            continue  # generic detectors already know this type
        by_type.setdefault(b["data_type"], []).extend(samples.get(bkey(b), []))
    negatives = [v for vals in neg_samples.values() for v in vals]
    for colkey, vals in samples.items():  # other baseline types are negatives for this one
        b = next(x for x in positives if bkey(x) == colkey)
        if b["data_type"] in covered:
            negatives.extend(vals[:500])
    created = 0
    for dtype, vals in by_type.items():
        pat = shape_pattern(vals)
        if not pat:
            continue
        body = {"pattern": pat, "description": f"Tabandaki {dtype} değerlerinin biçimi"}
        others = negatives + [v for t, vs in by_type.items() if t != dtype for v in vs[:500]]
        if submit(store, user, dtype, body, "base-derived", key, ref_dir, vals, others) == "CANDIDATE":
            created += 1
    return created


# --- LLM-assisted rule mining (offline; never used for detection at run time) --------------------------

PROMPT_VERSION = "rule_miner_v1"


def load_prompt(name: str = PROMPT_VERSION) -> str:
    return resources.files("scoutout.prompts").joinpath(f"{name}.md").read_text(encoding="utf-8")


def validate_llm_output(obj) -> list[dict]:
    """Minimal JSON-schema check of the model's answer."""
    if not isinstance(obj, dict) or not isinstance(obj.get("rules"), list):
        raise ValueError("Beklenen biçim: {\"rules\": [...]}")
    out = []
    for r in obj["rules"][:20]:
        if not isinstance(r, dict) or not isinstance(r.get("pattern"), str) or not isinstance(r.get("data_type"), str):
            continue
        ctx = r.get("context") or []
        if not isinstance(ctx, list) or not all(isinstance(w, str) for w in ctx):
            ctx = []
        out.append({"data_type": r["data_type"].upper().strip(), "pattern": r["pattern"], "context": ctx[:10],
                    "description": str(r.get("description", ""))[:200]})
    return out


def mine_rules(cfg: Config, store: Store, user: str, client=None, max_columns: int = 20) -> str:
    """Find exact baseline hits inside free-text columns, show the model only *redacted* context (D-011), and
    run every proposal through the gate."""
    from scoutout.connector import open_database
    from scoutout.knowledge import RefSetDetector, load_active_pack
    from scoutout.llm import LlmClient
    from scoutout.sampler import Sampler

    pack = load_active_pack(cfg, store)
    if pack is None or pack.ref_dir is None:
        raise ValueError("Önce tabandan bir bilgi paketi oluşturun.")
    if client is None:
        if not cfg.llm.endpoint:
            raise ValueError("Dil modeli adresi ayarlanmamış.")
        client = LlmClient.from_config(cfg.llm)
    refdet = next(d for d in pack.detectors() if isinstance(d, RefSetDetector))

    free_cols: dict[str, list[tuple[str, str, str]]] = {}  # per database source
    for d in store.latest_decisions():
        if json.loads(d["record_json"]).get("free_text"):
            free_cols.setdefault(d["source"] or "", []).append((d["schema_name"], d["table_name"], d["column_name"]))
    snippets: dict[str, list[str]] = {}
    hit_texts: dict[str, list[str]] = {}
    clean_texts: list[str] = []
    budget = max_columns
    for source, cols in free_cols.items():
        if budget <= 0:
            break
        from scoutout.sources import resolve

        _, scfg = resolve(store, cfg, source or None)
        db, _ = open_database(scfg)
        try:
            catalog = {t.full_name: t for t in db.discover_catalog().tables}
            sampler = Sampler(db, scfg.scan)
            for s, t, c in cols[:budget]:
                budget -= 1
                table = catalog.get(f"{s}.{t}")
                if table is None:
                    continue
                sample = sampler.sample(table, 1000)
                idx = [col.name for col in sample.columns].index(c)
                for _, values in sample.rows:
                    text = values[idx]
                    if not text:
                        continue
                    text = str(text)
                    f = scan_cell(text, [refdet])
                    if not f.spans:
                        if len(clean_texts) < 2000:
                            clean_texts.append(text)
                        continue
                    for sp in f.spans:
                        hit_texts.setdefault(sp.data_type, []).append(text)
                        if len(snippets.setdefault(sp.data_type, [])) < 40:
                            snippets[sp.data_type].append(redact(text, f.spans, max_len=140))
        finally:
            db.close()
    if not snippets:
        return "Serbest metinlerde tabanla eşleşen değer bulunmadı; önerilecek kural yok."

    fps = json.loads((pack.pack_dir / "fingerprints.json").read_text(encoding="utf-8"))
    shapes = {}
    for fp in fps.values():
        shapes.setdefault(fp["type"], []).extend(s for s, _ in fp["shapes"][:3])
    user_msg = json.dumps({"types": {t: {"value_shapes": shapes.get(t, [])[:5], "redacted_contexts": s}
                                     for t, s in snippets.items()}}, ensure_ascii=False)
    answer = client.chat_json(load_prompt(), user_msg)
    proposals = validate_llm_output(answer)
    counts: Counter = Counter()
    for p in proposals:
        body = {"pattern": p["pattern"], "context": p["context"], "description": p["description"],
                "prompt": PROMPT_VERSION}
        counts[submit(store, user, p["data_type"], body, "llm-derived", pack.key, pack.ref_dir,
                      hit_texts.get(p["data_type"], []), clean_texts)] += 1
    return f"{len(proposals)} öneri: {counts.get('CANDIDATE', 0)} aday, {counts.get('REJECTED', 0)} otomatik red"
