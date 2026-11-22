"""Knowledge pack (spec §5.6): what the system learns from baseline columns the user marked.

Stored per version under ``data_dir``:
  packs/<v>/        exportable: manifest, fingerprints (summaries), naming patterns, classifier weights, metrics
  refsets/<v>/      NOT exportable: HMAC'd reference sets and profile vectors (sensitive assets, spec §4.6)
Raw values are only held in memory while the pack is built.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from scoutout.connector import open_database
from scoutout.connector.base import TableInfo
from scoutout.core.config import Config
from scoutout.detectors import Detector, DetectorMeta, Finding, Span, generic_detectors, scan_cell
from scoutout.detectors.normalize import Candidate, fold, word_tokens
from scoutout.detectors.validators.checksums import iban_valid, luhn_valid, tckn_valid, vkn_valid
from scoutout.metadata_detector import tokens as name_tokens
from scoutout.profiler import profile, shape
from scoutout.store import Store, now

log = logging.getLogger("scoutout")

NAME_TYPES = {"GIVEN_NAME", "SURNAME", "PERSON_NAME"}
NO_REFSET = {"ADDRESS", "BIRTH_DATE", "MASKED_PAN"}  # not identifying on exact match / not useful
CHECKSUM = {"TCKN": tckn_valid, "YKN": tckn_valid, "VKN": vkn_valid, "PAN": luhn_valid, "IBAN": iban_valid}
GENERIC_NAME_TOKENS = {"no", "id", "kod", "code", "nr", "num", "numara", "number", "value", "deger", "bilgi", "info"}
SAMPLE_PER_COLUMN = 2000
K_ANON = 50


# --- keyed hashing -----------------------------------------------------------------------------

def hmac_key(cfg: Config) -> bytes:
    """HMAC key from SCOUTOUT_HMAC_KEY or data_dir/secrets/hmac.key (created once). Never in repo or store."""
    env = os.environ.get("SCOUTOUT_HMAC_KEY")
    if env:
        return env.encode()
    f = cfg.paths.root / "secrets" / "hmac.key"
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(secrets.token_hex(32), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(f, 0o600)
    return f.read_text(encoding="utf-8").strip().encode()


def h64(key: bytes, dtype: str, token: str) -> int:
    return int.from_bytes(hmac.new(key, f"{dtype}\x00{token}".encode(), hashlib.sha256).digest()[:8], "big")


_SEP = re.compile(r"[\s\-./()]")


def normalize_value(dtype: str, value: str) -> list[str]:
    """Tokens that represent a value in a reference set."""
    v = value.strip()
    if not v:
        return []
    if dtype in NAME_TYPES:
        return [fold(w) for w, _, _ in word_tokens(v)]
    if dtype == "EMAIL":
        return [v.lower()]
    if dtype == "PHONE":
        digits = re.sub(r"\D", "", v)
        return [digits[-10:]] if len(digits) >= 10 else []
    return [_SEP.sub("", v).upper()]


def clean_reason(dtype: str, token: str) -> str | None:
    """Why a baseline value must not be learned (spec §5.6.7): placeholders, test data, invalid checksum."""
    if len(set(token)) <= 1:
        return "yer tutucu"
    if token in ("0123456789", "1234567890", "12345678901", "TEST", "DENEME", "XXX", "NULL", "YOK", "-"):
        return "yer tutucu"
    if re.search(r"test|deneme|dummy|sample", token, re.I):
        return "test kaydı"
    check = CHECKSUM.get(dtype)
    if check and not check(token):
        return "geçersiz kontrol hanesi"
    return None


# --- runtime detectors from the pack -------------------------------------------------------------

class RefSetDetector(Detector):
    """Exact value overlap with the baseline (strongest evidence, spec §5.8.1). One instance covers all types."""

    meta = DetectorMeta("refset", "1", "*", "span", source="base-derived", se=0.97, sp=0.9999)

    def __init__(self, key: bytes, sets: dict[str, np.ndarray], pairs: np.ndarray | None, shapes: dict | None = None):
        self.key, self.sets, self.pairs = key, sets, pairs
        # only tokens with a length/character class seen in the baseline are hashed (keeps scans fast)
        shapes = shapes or {}
        self.lengths = {t: set(v["lengths"]) for t, v in shapes.items()}
        self.alpha = {t: v["alpha"] for t, v in shapes.items()}

    @staticmethod
    def _member(arr: np.ndarray, x: int) -> bool:
        i = int(np.searchsorted(arr, np.uint64(x)))
        return i < len(arr) and int(arr[i]) == x

    def scan(self, text: str, cands: list[Candidate], finding: Finding, ctx: dict) -> None:
        words = ctx.get("words") or word_tokens(text)
        alnum = [(m.group().upper(), m.start(), m.end()) for m in re.finditer(r"[A-Za-z0-9]{4,}", text)]
        for dtype, arr in self.sets.items():
            if dtype in NAME_TYPES:
                if len(words) == 1 and self._member(arr, h64(self.key, dtype, fold(words[0][0]))):
                    finding.spans.append(Span(dtype, words[0][1], words[0][2], "refset"))
                continue
            seen: set[tuple[int, int]] = set()
            lengths = self.lengths.get(dtype)
            pool = [(c.digits, c.start, c.end) for c in cands]
            if self.alpha.get(dtype, True):
                pool += alnum
            if dtype == "EMAIL":
                pool = [(m.group().lower(), m.start(), m.end()) for m in re.finditer(r"[\w.%+-]+@[\w.-]+", text)]
            for tok, s, e in pool:
                if dtype == "PHONE":
                    if not 10 <= len(tok) <= 12:
                        continue
                    tok = tok[-10:]
                elif lengths is not None and len(tok) not in lengths:
                    continue
                if (s, e) in seen:
                    continue
                if self._member(arr, h64(self.key, dtype, tok)):
                    seen.add((s, e))
                    finding.spans.append(Span(dtype, s, e, "refset"))
        if self.pairs is not None and len(words) >= 2:
            for (a, s1, _), (b, _, e2) in zip(words, words[1:], strict=False):
                if not (a[:1].isupper() and b[:1].isupper()):
                    continue
                if self._member(self.pairs, h64(self.key, "NAME_PAIR", f"{fold(a)}|{fold(b)}")):
                    finding.spans.append(Span("PERSON_NAME", s1, e2, "refset"))


class RuleDetector(Detector):
    """An approved rule (base- or LLM-derived). Runs with a per-call regex timeout."""

    def __init__(self, rule_id: int, dtype: str, body: dict, source: str, metrics: dict):
        import regex

        from scoutout.rules import checksum_fn

        self.rx = regex.compile(body["pattern"], regex.IGNORECASE if body.get("ignore_case") else 0)
        self.context = [fold(w) for w in body.get("context", [])]
        self.check = checksum_fn(body.get("checksum"))
        self.meta = DetectorMeta(f"rule{rule_id}", "1", dtype, "span", source=source,
                                 se=max(0.5, metrics.get("sensitivity", 0.9)),
                                 sp=min(0.9999, metrics.get("specificity", 0.998)))

    def scan(self, text, cands, finding, ctx):
        from scoutout.rules import rule_matches

        for m in rule_matches(self.rx, text, self.context, self.check):
            finding.spans.append(Span(self.meta.data_type, m.start(), m.end(), self.meta.id))


# --- the pack ---------------------------------------------------------------------------------

@dataclass
class KnowledgePack:
    version: str
    pack_dir: Path
    ref_dir: Path | None
    key: bytes
    rules: list = field(default_factory=list)
    learned_names: dict = field(default_factory=dict)
    overrides: dict = field(default_factory=dict)
    classifier: dict | None = None
    _detectors: list | None = None
    _vectors: dict | None = None

    @classmethod
    def load(cls, cfg: Config, store: Store, version: str) -> KnowledgePack:
        pdir = cfg.paths.root / "packs" / version
        rdir = cfg.paths.root / "refsets" / version
        def read(name, default):
            f = pdir / name
            return json.loads(f.read_text(encoding="utf-8")) if f.exists() else default
        rules = store.q("SELECT * FROM rules WHERE status='ACTIVE'")
        naming = read("naming.json", {})
        for r in rules:  # column-name tokens of approved rules become naming signals
            for tok in json.loads(r["body_json"]).get("column_names", []):
                naming.setdefault(r["data_type"], {})[tok] = max(naming.get(r["data_type"], {}).get(tok, 0.0), 0.7)
        return cls(version, pdir, rdir if rdir.exists() else None, hmac_key(cfg), rules,
                   naming, read("overrides.json", {}), read("classifier.json", None))

    def detectors(self) -> list[Detector]:
        if self._detectors is None:
            dets = list(generic_detectors())
            if self.ref_dir is not None:
                sets = {f.stem: np.load(f) for f in self.ref_dir.glob("*.npy") if f.stem != "NAME_PAIR"}
                pairs_f = self.ref_dir / "NAME_PAIR.npy"
                shapes_f = self.ref_dir / "token_shapes.json"
                shapes = json.loads(shapes_f.read_text(encoding="utf-8")) if shapes_f.exists() else None
                dets.append(RefSetDetector(self.key, sets, np.load(pairs_f) if pairs_f.exists() else None, shapes))
                # custom types (no generic detector) need a meta entry for the decision engine
                for t in sets:
                    if not any(d.meta.data_type == t for d in dets):
                        dets.append(_MetaOnly(t))
            for r in self.rules:
                try:
                    dets.append(RuleDetector(r["id"], r["data_type"], json.loads(r["body_json"]), r["source"],
                                             json.loads(r["metrics_json"] or "{}")))
                except Exception as e:  # noqa: BLE001 - a broken rule must not stop the scan
                    log.warning("rule %s skipped: %s", r["id"], type(e).__name__)
            self._detectors = dets
        return self._detectors

    def vectors(self) -> dict[str, np.ndarray]:
        if self._vectors is None:
            f = self.ref_dir / "vectors.npz" if self.ref_dir else None
            self._vectors = dict(np.load(f)) if f and f.exists() else {}
        return self._vectors

    def column_signals(self, t: TableInfo, d) -> dict:
        """Classifier top-k and vector margin for a decided column (pre-signals; never decide the zone)."""
        out: dict = {}
        vec = getattr(d, "vector", None)
        if vec is None:
            return out
        if self.classifier:
            out["classifier_top_k"] = predict(self.classifier, vec)[:3]
        vecs = self.vectors()
        if vecs:
            dists = sorted((float(np.min(1 - (m @ vec) / (np.linalg.norm(m, axis=1) * np.linalg.norm(vec) + 1e-9))), k)
                           for k, m in vecs.items())
            if len(dists) >= 2 and dists[1][0] > 0:
                out["vector_nearest"], out["vector_margin"] = dists[0][1], round((dists[1][0] - dists[0][0]) / dists[1][0], 3)
        top = out.get("classifier_top_k")
        if top and top[0][1] >= 0.8 and top[0][0] != "NEGATIVE" and d.zone == "CLEAN" and d.n > 0:
            out["conflict_flag"], out["conflict_reason"] = True, f"Taban modeli {top[0][0]} öngörüyor, değerlerde bulunmadı"
        return out


class _MetaOnly(Detector):
    def __init__(self, dtype: str):
        self.meta = DetectorMeta(f"refset_{dtype.lower()}", "1", dtype, "span", source="base-derived", se=0.97, sp=0.9999)

    def scan(self, text, cands, finding, ctx):
        return None


RULES_ONLY = "kurallar"  # no baseline pack yet, but approved rules exist


def load_active_pack(cfg: Config, store: Store) -> KnowledgePack | None:
    r = store.one("SELECT version FROM packs WHERE active=1")
    if r:
        return KnowledgePack.load(cfg, store, r["version"])
    if store.one("SELECT 1 FROM rules WHERE status='ACTIVE'"):
        return KnowledgePack.load(cfg, store, RULES_ONLY)
    return None


# --- classifier (logistic regression; weights as JSON so the pack stays transparent and pickle-free) ----

def predict(model: dict, vec: np.ndarray) -> list[tuple[str, float]]:
    x = (np.asarray(vec, dtype=float) - np.array(model["mean"])) / np.array(model["std"])
    z = np.array(model["coef"]) @ x + np.array(model["intercept"])
    if len(model["classes"]) == 2 and z.shape[0] == 1:
        p1 = 1 / (1 + np.exp(-z[0]))
        probs = np.array([1 - p1, p1])
    else:
        e = np.exp(z - z.max())
        probs = e / e.sum()
    return sorted(((c, round(float(p), 4)) for c, p in zip(model["classes"], probs, strict=True)), key=lambda kv: -kv[1])


def train_classifier(rows: list[tuple[np.ndarray, str]]) -> dict | None:
    labels = {lab for _, lab in rows}
    if len(labels) < 2 or len(rows) < 6:
        return None
    from sklearn.linear_model import LogisticRegression

    X = np.vstack([v for v, _ in rows]).astype(float)
    y = [lab for _, lab in rows]
    mean, std = X.mean(axis=0), X.std(axis=0) + 1e-6
    clf = LogisticRegression(max_iter=3000, C=1.0)
    clf.fit((X - mean) / std, y)
    return {"classes": list(clf.classes_), "coef": clf.coef_.round(6).tolist(), "intercept": clf.intercept_.round(6).tolist(),
            "mean": mean.round(6).tolist(), "std": std.round(6).tolist()}


# --- building ------------------------------------------------------------------------------------

@dataclass
class BuildResult:
    version: str
    stats: dict

    def summary_text(self) -> str:
        s = self.stats
        return (f"{s['positive_columns']} hassas + {s['negative_columns']} örnek kolon, "
                f"{s['reference_values']:,} referans değer, {s['candidate_rules']} aday kural")


def _read_column(db, schema: str, table: str, column: str, max_values: int, lob_chars: int):
    """Stream a baseline column (spec §3.4: fetchmany). Reading the whole column is an explicit user choice."""
    col = f"[{column.replace(']', ']]')}]"
    sql = (f"SELECT TOP ({max_values}) LEFT(CAST({col} AS nvarchar(max)), {lob_chars}) "
           f"FROM [{schema}].[{table}] WHERE {col} IS NOT NULL")
    cur = db.conn.cursor()
    cur.execute(sql)
    while True:
        rows = cur.fetchmany(20000)
        if not rows:
            break
        for (v,) in rows:
            yield v


def bkey(b) -> tuple[str, str, str, str]:
    """(source, schema, table, column) of a baseline row."""
    return (b["source"] or "", b["schema_name"], b["table_name"], b["column_name"])


def label(colkey: tuple) -> str:
    src, schema, table, column = colkey
    return f"{src + ':' if src else ''}{schema}.{table}.{column}"


def build_pack(cfg: Config, store: Store, user: str, max_values: int = 20_000_000) -> BuildResult:
    from scoutout.rules import mine_base_rules

    base = store.q("SELECT * FROM baseline_columns")
    positives = [b for b in base if b["data_type"] != "NEGATIVE"]
    negatives = [b for b in base if b["data_type"] == "NEGATIVE"]
    if not positives:
        raise ValueError("Taban listesinde hassas kolon yok.")
    key = hmac_key(cfg)
    version = datetime.now().strftime("%Y%m%d-%H%M%S")
    pdir, rdir = cfg.paths.root / "packs" / version, cfg.paths.root / "refsets" / version
    pdir.mkdir(parents=True, exist_ok=True)
    rdir.mkdir(parents=True, exist_ok=True)

    dbs: dict[str, object] = {}

    def db_for(source: str):
        """One read-only connection per database source (baseline columns may come from several databases)."""
        if source not in dbs:
            from scoutout.sources import resolve

            _, scfg = resolve(store, cfg, source or None)
            dbs[source] = open_database(scfg)[0]
        return dbs[source]

    rng = np.random.default_rng(cfg.scan.seed)
    hashes: dict[str, list[np.ndarray]] = {}
    token_shapes: dict[str, dict] = {}
    samples: dict[tuple, list[str]] = {}
    fingerprints, cleaning = {}, {}
    try:
        for b in positives:
            dtype, colkey = b["data_type"], bkey(b)
            removed: Counter = Counter()
            buf: list[int] = []
            sample: list[str] = []
            lengths: Counter = Counter()
            shapes: Counter = Counter()
            prefixes: Counter = Counter()
            n = 0
            for raw in _read_column(db_for(colkey[0]), *colkey[1:], max_values, cfg.scan.lob_chars):
                text = str(raw)
                n += 1
                toks = normalize_value(dtype, text)
                if not toks:
                    removed["boş"] += 1
                    continue
                reason = None if dtype in NAME_TYPES else clean_reason(dtype, toks[0])
                if reason:
                    removed[reason] += 1
                    continue
                if dtype not in NO_REFSET:
                    buf.extend(h64(key, dtype, t) for t in toks)
                    ts = token_shapes.setdefault(dtype, {"lengths": set(), "alpha": False})
                    for t in toks:
                        ts["lengths"].add(len(t))
                        ts["alpha"] = ts["alpha"] or any(ch.isalpha() for ch in t)
                lengths[len(text)] += 1
                shapes[shape(text)] += 1
                if dtype not in NAME_TYPES and len(toks[0]) >= 4:
                    prefixes[toks[0][: min(6, len(toks[0]) - 2)]] += 1
                if len(sample) < SAMPLE_PER_COLUMN:  # reservoir sample, memory only
                    sample.append(text)
                else:
                    j = int(rng.integers(0, n))
                    if j < SAMPLE_PER_COLUMN:
                        sample[j] = text
                if len(buf) >= 1_000_000:
                    hashes.setdefault(dtype, []).append(np.array(buf, dtype=np.uint64))
                    buf = []
            if buf:
                hashes.setdefault(dtype, []).append(np.array(buf, dtype=np.uint64))
            samples[colkey] = sample
            kept = sum(lengths.values())
            fingerprints[label(colkey)] = {
                "type": dtype, "values": kept,
                "length": {str(k): round(v / max(kept, 1), 4) for k, v in sorted(lengths.items())[:60]},
                "shapes": [(s, round(c / max(kept, 1), 4)) for s, c in shapes.most_common(5)],
                "prefixes": [(p, round(c / max(kept, 1), 4)) for p, c in prefixes.most_common(20) if c >= K_ANON],
            }
            cleaning[label(colkey)] = dict(removed)

        # same-record first+last name pairs (spec §5.6.1)
        by_table: dict[tuple, dict] = {}
        for b in positives:
            by_table.setdefault(bkey(b)[:3], {})[b["data_type"]] = b["column_name"]
        pair_hashes: list[int] = []
        for (src, s, t), cols in by_table.items():
            if "GIVEN_NAME" in cols and "SURNAME" in cols:
                g, sn = (f"[{cols[k].replace(']', ']]')}]" for k in ("GIVEN_NAME", "SURNAME"))
                cur = db_for(src).conn.cursor()
                cur.execute(f"SELECT TOP ({max_values}) {g}, {sn} FROM [{s}].[{t}] WHERE {g} IS NOT NULL AND {sn} IS NOT NULL")
                while rows := cur.fetchmany(20000):
                    pair_hashes.extend(h64(key, "NAME_PAIR", f"{fold(a).strip()}|{fold(c).strip()}") for a, c in rows)

        neg_samples: dict[tuple, list[str]] = {}
        for b in negatives:
            colkey = bkey(b)
            vals = []
            for i, raw in enumerate(_read_column(db_for(colkey[0]), *colkey[1:], SAMPLE_PER_COLUMN * 5, cfg.scan.lob_chars)):
                if i % 5 == 0:
                    vals.append(str(raw))
            neg_samples[colkey] = vals
    finally:
        for db in dbs.values():
            db.close()

    total_refs = 0
    for dtype, parts in hashes.items():
        arr = np.unique(np.concatenate(parts))
        np.save(rdir / f"{dtype}.npy", arr)
        total_refs += len(arr)
    if pair_hashes:
        np.save(rdir / "NAME_PAIR.npy", np.unique(np.array(pair_hashes, dtype=np.uint64)))
    (rdir / "token_shapes.json").write_text(json.dumps(
        {t: {"lengths": sorted(v["lengths"]), "alpha": v["alpha"]} for t, v in token_shapes.items()}), encoding="utf-8")

    # naming patterns learned from the institution's own column names
    neg_tokens = {t for b in negatives for t in name_tokens(b["column_name"])}
    naming: dict[str, dict[str, float]] = {}
    for b in positives:
        for tok in name_tokens(b["column_name"]) + ["_".join(name_tokens(b["column_name"]))]:
            if tok and tok not in GENERIC_NAME_TOKENS and tok not in neg_tokens and len(tok) >= 2:
                naming.setdefault(b["data_type"], {})[tok] = 0.7

    # measured detector quality: generic sensitivity on positives, specificity on negatives
    metrics: dict[str, dict] = {}
    for colkey, vals in samples.items():
        dtype = next(b["data_type"] for b in positives if bkey(b) == colkey)
        if vals:
            hits = sum(1 for v in vals if dtype in {s.data_type for s in scan_cell(v).spans})
            m = metrics.setdefault(dtype, {"generic_hits": 0, "n": 0})
            m["generic_hits"] += hits
            m["n"] += len(vals)
    overrides = {}
    for dtype, m in metrics.items():
        m["generic_sensitivity"] = round(m["generic_hits"] / max(m["n"], 1), 4)
        if dtype in NO_REFSET and 0.5 <= m["generic_sensitivity"] < 0.97:
            overrides[dtype] = {"se": m["generic_sensitivity"]}
    neg_values = [v for vals in neg_samples.values() for v in vals]
    if neg_values:
        fp: Counter = Counter()
        for v in neg_values:
            for t in {s.data_type for s in scan_cell(v).spans}:
                fp[t] += 1
        for t, c in fp.items():
            metrics.setdefault(t, {})["negative_hit_rate"] = round(c / len(neg_values), 4)

    # classifier + vector regions from column profiles (chunks give the model more than one row per column)
    rows, vectors = [], {}
    for colkey, vals in list(samples.items()) + list(neg_samples.items()):
        lab = next((b["data_type"] for b in base if bkey(b) == colkey), "NEGATIVE")
        for i in range(4):
            chunk = vals[i::4]
            if len(chunk) >= 25:
                v = profile(chunk).vector()
                rows.append((v, lab))
                if lab != "NEGATIVE":
                    vectors.setdefault(lab, []).append(v)
    classifier = train_classifier(rows)
    if vectors:
        np.savez(rdir / "vectors.npz", **{k: np.vstack(v) for k, v in vectors.items()})

    def write(name, obj):
        (pdir / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")

    write("fingerprints.json", fingerprints)
    write("naming.json", naming)
    write("overrides.json", overrides)
    write("metrics.json", metrics)
    write("cleaning.json", cleaning)
    if classifier:
        write("classifier.json", classifier)

    candidates = mine_base_rules(store, user, samples, neg_samples, positives, key, rdir)
    stats = {"positive_columns": len(positives), "negative_columns": len(negatives), "reference_values": total_refs,
             "candidate_rules": candidates, "removed": {k: sum(v.values()) for k, v in cleaning.items()}}
    write("manifest.json", {"version": version, "created_at": now(), "created_by": user, "stats": stats,
                            "types": sorted({b["data_type"] for b in positives})})
    res = BuildResult(version, stats)
    with store.tx() as c:
        c.execute("UPDATE packs SET active=0")
        c.execute("INSERT INTO packs VALUES (?,?,?,?,1)", (version, now(), user, json.dumps(stats | {"text": res.summary_text()})))
    store.audit(user, "pack.build", version, **stats)
    return res


# --- export / import (no reference sets, no vectors: spec §5.6) ---------------------------------

def export_pack(cfg: Config, store: Store, version: str, out: Path, user: str) -> Path:
    pdir = cfg.paths.root / "packs" / version
    if not pdir.exists():
        raise ValueError(f"Paket bulunamadı: {version}")
    rules = [{"data_type": r["data_type"], "kind": r["kind"], "body": json.loads(r["body_json"]), "source": r["source"],
              "metrics": json.loads(r["metrics_json"] or "{}")} for r in store.q("SELECT * FROM rules WHERE status='ACTIVE'")]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(pdir.glob("*.json")):
            z.write(f, f"{version}/{f.name}")
        z.writestr(f"{version}/rules.json", json.dumps(rules, ensure_ascii=False, indent=1))
    store.audit(user, "pack.export", version, file=str(out))
    return out


def import_pack(cfg: Config, store: Store, archive: Path, user: str) -> str:
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        version = names[0].split("/")[0]
        if any(not n.startswith(version + "/") or ".." in n or not n.endswith(".json") for n in names):
            raise ValueError("Geçersiz paket dosyası")
        pdir = cfg.paths.root / "packs" / version
        if pdir.exists():
            shutil.rmtree(pdir)
        pdir.mkdir(parents=True)
        for n in names:
            (pdir / Path(n).name).write_bytes(z.read(n))
    rules = json.loads((pdir / "rules.json").read_text(encoding="utf-8")) if (pdir / "rules.json").exists() else []
    for r in rules:
        store.x("INSERT INTO rules (data_type, kind, body_json, source, status, metrics_json, created_at, decided_by, decided_at, reason) "
                "VALUES (?,?,?,?, 'ACTIVE', ?,?,?,?, 'içe aktarılan paket')", r["data_type"], r["kind"], json.dumps(r["body"]),
                r["source"], json.dumps(r["metrics"]), now(), user, now())
    stats = json.loads((pdir / "manifest.json").read_text(encoding="utf-8")).get("stats", {}) if (pdir / "manifest.json").exists() else {}
    with store.tx() as c:
        c.execute("UPDATE packs SET active=0")
        c.execute("INSERT OR REPLACE INTO packs VALUES (?,?,?,?,1)", (version, now(), user,
                  json.dumps(stats | {"text": "içe aktarıldı (referans kümesi yok)"})))
    store.audit(user, "pack.import", version, rules=len(rules))
    return version
