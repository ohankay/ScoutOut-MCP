"""Learning from the bank's own data: baseline packs, local-LLM rule mining, agent rule proposals, shape profiles.

Real values are read here and only here. They stay in memory: the local LLM may see them (SPEC §6.1), the agent
never does — it gets rule definitions, measurements and k-anonymous shape statistics.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from datetime import UTC, datetime
from importlib import resources

from scoutout.connector import open_database
from scoutout.decision_engine.engine import _text
from scoutout.profiler import is_free_text, profile, shape
from scoutout.rules import CHECKSUMS, checksum_fn, find_rule, leak_reason, submit
from scoutout.sampler import Sampler
from scoutout.store import now

from . import rules_dsl
from .guard import kcount
from .service import SENSITIVE, USER, Service

PROMPT_VERSION = "rule_miner_mcp_v1"
_SEP = str.maketrans("", "", " -./")


# --- reading ------------------------------------------------------------------------------------------

def _column(t, name: str):
    c = next((c for c in t.columns if c.name.lower() == (name or "").strip().strip("[]").lower()), None)
    if c is None:
        raise ValueError(f"Kolon bulunamadı: {t.full_name}.{name}")
    return c


def _resolve(svc: Service, e: dict, need_type: bool):
    if not isinstance(e, dict) or not e.get("database") or not e.get("table") or not e.get("column"):
        raise ValueError("Her kolon {database, table, column" + (", type" if need_type else "") + "} olarak verilmeli.")
    src, _ = svc.resolve(e["database"])
    t = svc.table(e["database"], e["table"])
    c = _column(t, e["column"])
    dtype = None
    if need_type:
        dtype = str(e.get("type", "")).strip().upper()
        if not rules_dsl.TYPE_RX.match(dtype):
            raise ValueError(f"{t.full_name}.{c.name}: type büyük harfli bir tür adı olmalı (ör. CUSTOMER_NO).")
    return src, t, c, dtype


def _sample(svc: Service, cols: list[tuple], n: int) -> dict:
    """{(source, table): {column name: [text values]}} for every readable column of each table (one sample per table)."""
    out: dict = {}
    by_src: dict = {}
    for src, t, *_ in cols:
        by_src.setdefault(src.name, (src, {}))[1][t.full_name] = t
    for src, tables in by_src.values():
        _, scfg = svc.resolve(src.database)
        db, _ = open_database(scfg)
        try:
            db.set_query_timeout(scfg.scan.query_timeout_s)
            sampler = Sampler(db, scfg.scan)
            for name, t in tables.items():
                s = sampler.sample(t, n)
                vals: dict[str, list[str]] = {c.name: [] for c in s.columns}
                for _, row in s.rows:
                    for c, v in zip(s.columns, row, strict=True):
                        txt = _text(v)
                        if txt and txt.strip():
                            vals[c.name].append(txt)
                out[(src.name, name)] = vals
        finally:
            db.close()
    return out


def _sensitive_columns(svc: Service, source: str, t) -> set[str]:
    return {d["column_name"] for d in svc._decisions(source, t.schema, t.name) if svc._effective(d)[0] in SENSITIVE}


def _budget(svc: Service, n: int = 1) -> None:
    start = datetime.now(UTC).strftime("%Y-%m-%dT00:00:00")
    used = svc.store.one("SELECT COUNT(*) n FROM audit WHERE action='rule.measure' AND at >= ?", start)["n"]
    if used + n > svc.mcp.daily_measure_budget:
        raise PermissionError(f"Günlük kural ölçüm bütçesi doldu ({svc.mcp.daily_measure_budget}). Yarın tekrar deneyin.")


def _gate(svc: Service, rule: dict, source: str, positives: list[str], negatives: list[str], shown: list[str],
          allowed_types: set[str] | None = None, extra: dict | None = None, free_text: bool = False) -> dict:
    """``free_text``: the positives are texts that only sometimes contain the value, so recall is not required —
    only precision and at least k matches."""
    # Normalise, run the MCP checks and the core gate. A rule rejected for carrying a value is never stored.
    from scoutout.knowledge import hmac_key, load_active_pack

    try:
        n = rules_dsl.normalize(rule)
    except ValueError as e:
        return {"status": "INVALID", "reason": str(e)}
    dtype, body = n["data_type"], n["body"]
    if allowed_types is not None and dtype not in allowed_types:
        return {"status": "INVALID", "reason": f"{dtype} istenen türlerden biri değil"}
    pack = load_active_pack(svc.cfg, svc.store)
    key = hmac_key(svc.cfg)
    ref_dir = pack.ref_dir if pack else None
    leak = rules_dsl.generality_reason(body["pattern"]) or rules_dsl.shown_value_reason(body, shown, svc.mcp.k) \
        or leak_reason(body, key, ref_dir)
    if leak:
        svc.store.audit(USER, "rule.leak_rejected", dtype, source=source, reason=leak)
        return {"status": "REJECTED", "data_type": dtype, "reason": leak, "pattern": None}
    _budget(svc)
    svc.store.audit(USER, "rule.measure", dtype, source=source)
    kw = {"min_recall": 0.0, "min_matches": svc.mcp.k} if free_text else {}
    status = submit(svc.store, USER, dtype, body | (extra or {}), source, key, ref_dir, positives, negatives, **kw)
    r = find_rule(svc.store, dtype, body)
    view = svc.rule_view(r) if r else {"data_type": dtype}
    if status == "DUPLICATE":
        view["note"] = "Bu desen zaten kayıtlı."
    return view


# --- tools ---------------------------------------------------------------------------------------------

def teach_baseline(svc: Service, entries: list[dict], negatives: list[dict]) -> dict:
    from scoutout.knowledge import build_pack

    if not entries:
        raise ValueError("En az bir taban kolonu verin: {database, table, column, type}.")
    rows = [_resolve(svc, e, True) for e in entries] + [(*_resolve(svc, e, False)[:3], "NEGATIVE") for e in negatives]
    for src, t, c, dtype in rows:
        svc.store.x("INSERT INTO baseline_columns (source, schema_name, table_name, column_name, data_type, added_by, added_at) "
                    "VALUES (?,?,?,?,?,?,?) ON CONFLICT(source, schema_name, table_name, column_name) DO UPDATE SET "
                    "data_type=excluded.data_type", src.name, t.schema, t.name, c.name, dtype, USER, now())
        svc.store.audit(USER, "baseline.add", f"{src.name}:{t.full_name}.{c.name}", data_type=dtype)
    started = now()
    res = build_pack(svc.cfg, svc.store, USER)
    new = svc.store.q("SELECT * FROM rules WHERE source='base-derived' AND created_at >= ? ORDER BY id", started)
    base = svc.store.q("SELECT data_type, COUNT(*) n FROM baseline_columns GROUP BY data_type")
    return {"pack_version": res.version, "summary": res.summary_text(),
            "baseline_columns": {r["data_type"]: r["n"] for r in base},
            "reference_values": res.stats["reference_values"], "new_candidate_rules": [svc.rule_view(r) for r in new],
            "message": "Bilgi paketi etkin: sonraki taramalar tabandaki değerleri ve öğrenilen kolon adlarını kullanır. "
                       "Aday kurallar decide_rule ile etkinleştirilene kadar kullanılmaz."}


def mine_rules(svc: Service, entries: list[dict], hint: str = "", max_rules: int = 8) -> dict:
    if not entries:
        raise ValueError("Kural çıkarılacak kolonları verin: {database, table, column, type}.")
    client = svc.llm()
    cols = [_resolve(svc, e, True) for e in entries]
    samples = _sample(svc, cols, svc.mcp.measure_values)
    rng = random.Random(svc.cfg.scan.seed)
    positives: dict[str, list[str]] = {}
    labels: dict[str, list[str]] = {}
    entry_cols = {(src.name, t.full_name, c.name) for src, t, c, _ in cols}
    for src, t, c, dtype in cols:
        positives.setdefault(dtype, []).extend(samples[(src.name, t.full_name)][c.name])
        labels.setdefault(dtype, []).append(f"{src.database}.{t.full_name}.{c.name}")
    negatives: list[str] = []
    neg_shown = []
    for (sname, tname), vals in samples.items():
        t = next(t for s, t, *_ in cols if s.name == sname and t.full_name == tname)
        skip = _sensitive_columns(svc, sname, t)
        for cname, vs in vals.items():
            if (sname, tname, cname) in entry_cols or cname in skip or not vs:
                continue
            negatives.extend(vs)
            if len(neg_shown) < 12:
                neg_shown.append({"column": f"{tname}.{cname}", "examples": rng.sample(vs, min(15, len(vs)))})
    shown: dict[str, list[str]] = {}
    for dtype, vs in positives.items():
        if not vs:
            raise ValueError(f"{dtype}: örneklemde dolu değer yok.")
        shown[dtype] = rng.sample(vs, min(svc.mcp.mine_values, len(vs)))
    free = {dtype: is_free_text(profile(vs)) for dtype, vs in positives.items()}
    existing = {dtype: [json.loads(r["body_json"])["pattern"] for r in svc.store.q(
        "SELECT body_json FROM rules WHERE data_type=? AND status IN ('ACTIVE','CANDIDATE')", dtype)] for dtype in positives}
    message = {"hint": (hint or "")[:1000],
               "types": {dtype: {"columns": labels[dtype], "free_text": free[dtype], "examples": shown[dtype],
                                 "existing_rules": existing[dtype]} for dtype in positives},
               "negative_examples": neg_shown}
    prompt = resources.files("scoutout_mcp.prompts").joinpath(f"{PROMPT_VERSION}.md").read_text(encoding="utf-8")
    try:
        answer = client.chat_json(prompt, json.dumps(message, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001 - connection refused, timeout, bad JSON
        raise ValueError(f"Yerel LLM'den yanıt alınamadı ({type(e).__name__}). LLM sunucusu çalışıyor mu?") from None
    proposals = answer.get("rules") if isinstance(answer, dict) else None
    if not isinstance(proposals, list):
        raise ValueError("Yerel LLM beklenen biçimde yanıt vermedi ({\"rules\": [...]}).")
    all_shown = [v for vs in shown.values() for v in vs] + [v for n in neg_shown for v in n["examples"]]
    results = []
    for p in proposals[: max(1, min(max_rules, 20))]:
        dtype = str((p or {}).get("data_type", "")).upper() if isinstance(p, dict) else ""
        others = [v for t, vs in positives.items() if t != dtype for v in vs]
        results.append(_gate(svc, p, "llm-derived", positives.get(dtype, []), negatives + others, all_shown,
                             set(positives), {"prompt": PROMPT_VERSION}, free.get(dtype, False)))
    counts = Counter(r.get("status") for r in results)
    return {"proposed": len(results), "candidates": counts.get("CANDIDATE", 0), "rejected": counts.get("REJECTED", 0),
            "invalid": counts.get("INVALID", 0), "rules": results,
            "values_sent_to_local_llm": len(all_shown),
            "message": "Aday kurallar kullanıcı onayıyla decide_rule (action=activate) çağrılana kadar taramada kullanılmaz."}


def propose_rule(svc: Service, rule: dict, positives: list[dict], negatives: list[dict]) -> dict:
    if not positives:
        raise ValueError("Kuralın bulması gereken kolonları positives olarak verin: {database, table, column}.")
    pos_cols = [_resolve(svc, e, False) for e in positives]
    neg_cols = [_resolve(svc, e, False) for e in negatives]
    samples = _sample(svc, pos_cols + neg_cols, svc.mcp.measure_values)
    pos = [v for src, t, c, _ in pos_cols for v in samples[(src.name, t.full_name)][c.name]]
    if neg_cols:
        neg = [v for src, t, c, _ in neg_cols for v in samples[(src.name, t.full_name)][c.name]]
    else:  # the other, non-sensitive columns of the same tables
        chosen = {(src.name, t.full_name, c.name) for src, t, c, _ in pos_cols}
        neg = []
        for src, t, _, _ in pos_cols:
            skip = _sensitive_columns(svc, src.name, t)
            for cname, vs in samples[(src.name, t.full_name)].items():
                if (src.name, t.full_name, cname) not in chosen and cname not in skip:
                    neg.extend(vs)
    if not pos:
        raise ValueError("Pozitif kolonlarda örneklemde dolu değer yok.")
    return _gate(svc, rule, "agent", pos, neg, shown=[], free_text=is_free_text(profile(pos)))


def profile_column(svc: Service, database: str, table: str, column: str) -> dict:
    src, t, c, _ = _resolve(svc, {"database": database, "table": table, "column": column}, False)
    vals = _sample(svc, [(src, t, c, None)], svc.mcp.measure_values)[(src.name, t.full_name)][c.name]
    k = svc.mcp.k
    prof = profile(vals)
    n = len(vals)
    lengths = Counter(len(v) for v in vals)
    shown_lengths = [{"length": L, "count": cnt} for L, cnt in sorted(lengths.items()) if cnt >= k]
    shapes = Counter(shape(v, 24) + ("…" if len(v) > 24 else "") for v in vals)
    prefixes = []
    for size in (2, 3, 4):
        cnt = Counter(v.strip()[:size] for v in vals if len(v.strip()) > size)
        prefixes += [{"prefix": p, "share": round(m / n, 3)} for p, m in cnt.most_common(5) if m >= k and m / n >= 0.05]
    # a shorter prefix that always continues the same way adds nothing: keep the longest of such a chain
    prefixes = [x for x in prefixes if not any(y["prefix"].startswith(x["prefix"]) and len(y["prefix"]) > len(x["prefix"])
                                               and y["share"] == x["share"] for y in prefixes)]
    checks = {}
    for name in CHECKSUMS:
        fn = checksum_fn(name)
        ok = sum(1 for v in vals if fn(v.translate(_SEP).upper()))
        checks[name] = {"passing": kcount(ok, k), "rate": round(ok / n, 3) if n and (ok == 0 or ok >= k) else None}
    return {"database": src.database, "table": t.full_name, "column": c.name, "sql_type": c.type_label,
            "non_empty_values": n, "distinct_rate": round(prof.distinct_rate, 3), "free_text": is_free_text(prof),
            "length": {"min": prof.len_min, "median": prof.len_p50, "p95": prof.len_p95, "max": prof.len_max,
                       "frequent": shown_lengths, "other": n - sum(x["count"] for x in shown_lengths)},
            "characters": {"digit": round(prof.digit_rate, 3), "letter": round(prof.alpha_rate, 3),
                           "upper": round(prof.upper_rate, 3), "turkish": round(prof.tr_char_rate, 3),
                           "space": round(prof.space_rate, 3), "punctuation": round(prof.punct_rate, 3)},
            "shapes": [{"shape": s, "share": round(m / n, 3)} for s, m in shapes.most_common(10) if m >= k],
            "frequent_prefixes": prefixes, "checksums": checks,
            "note": f"d=rakam, X=büyük harf, x=küçük harf. {k}'dan az değerde görülen biçimler gösterilmez."}
