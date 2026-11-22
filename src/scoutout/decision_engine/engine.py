"""Column decisions (spec §5.8): evidence per type -> corrected prevalence -> confidence bounds -> zone.

Raw values only live inside ``ColumnEvidence.add`` while a sample batch is processed; the evidence keeps
counts, shapes (through the profile) and redacted snippets.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

from scoutout.connector.base import DATE_TYPES, ColumnInfo
from scoutout.core.redact import redact
from scoutout.detectors import Detector, DetectorMeta, fold, generic_detectors, scan_cell
from scoutout.profiler import ColumnProfile, is_free_text, profile

from .stats import rogan_gladen, sprt_llr, wilson_lower, wilson_upper

ZONE_ORDER = ["CONFLICT", "SENSITIVE", "SPARSE", "UNDECIDED", "CLEAN", "EMPTY"]
SEVERITY = {"SENSITIVE": 4, "SPARSE": 3, "UNDECIDED": 2, "CLEAN": 1, "EMPTY": 0}
#: tie-break when two types reach the same zone with the same prevalence: identifiers first
TYPE_PRIORITY = ["TCKN", "YKN", "VKN", "PAN", "IBAN", "PASSPORT", "MASKED_PAN", "PHONE", "EMAIL", "BIRTH_DATE",
                 "ADDRESS", "PERSON_NAME", "GIVEN_NAME", "SURNAME", "PLATE", "SWIFT", "IP", "MAC"]
MAX_EXAMPLES = 3
DEFAULT_CHECKSUM_SE = 0.99


@dataclass
class Thresholds:
    p_sensitive: float = 0.90
    conf_sensitive: float = 0.95
    p_clean: float = 0.05
    conf_clean: float = 0.99
    conflict_metadata: float = 0.6
    per_type: dict[str, dict[str, float]] = field(default_factory=dict)  # e.g. {"PERSON_NAME": {"p_sensitive": .8}}

    def for_type(self, t: str) -> Thresholds:
        o = self.per_type.get(t)
        if not o:
            return self
        d = asdict(self)
        d.update(o)
        d["per_type"] = {}
        return Thresholds(**d)


def _text(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    s = str(v)
    return s if s.strip() else None


@dataclass
class ColumnEvidence:
    column: ColumnInfo
    n_rows: int = 0  # all sampled rows
    n: int = 0  # non-NULL, non-empty rows (basis of every rate, D-021)
    hits: dict[str, int] = field(default_factory=dict)
    cands: dict[str, int] = field(default_factory=dict)  # rows that had a shaped candidate for the type
    examples: dict[str, list[dict]] = field(default_factory=dict)
    by_detector: dict[str, dict[str, int]] = field(default_factory=dict)  # type -> detector/rule id -> rows (transparency)
    values_for_profile: list = field(default_factory=list, repr=False)

    def add(self, pk: tuple, value, detectors: list[Detector]) -> None:
        self.n_rows += 1
        self.values_for_profile.append(value)
        text = _text(value)
        if text is None:
            return
        self.n += 1
        ctx = {"is_date_column": self.column.data_type in DATE_TYPES, "column_folded": fold(self.column.name)}
        f = scan_cell(text, detectors, ctx)
        for t, c in f.candidates.items():
            if c:
                self.cands[t] = self.cands.get(t, 0) + 1
        by_type: dict[str, list] = {}
        for s in f.spans:
            by_type.setdefault(s.data_type, []).append(s)
        for t, spans in by_type.items():
            self.hits[t] = self.hits.get(t, 0) + 1
            per = self.by_detector.setdefault(t, {})
            for det in {s.detector_id for s in spans}:
                per[det] = per.get(det, 0) + 1
            ex = self.examples.setdefault(t, [])
            if len(ex) < MAX_EXAMPLES:
                ex.append({"pk": [_pk_text(v) for v in pk], "offset": spans[0].start, "snippet": redact(text, f.spans)})

    def profile(self) -> ColumnProfile:
        return profile(self.values_for_profile)


def _pk_text(v) -> str:
    if isinstance(v, (bytes, bytearray)):
        return "0x" + bytes(v).hex()
    return str(v)


@dataclass
class TypeResult:
    data_type: str
    detector_id: str
    detector_version: str
    hits: int
    n: int
    p_obs: float
    se: float
    sp: float
    p_corrected: float
    ci_low: float
    ci_high: float
    zone: str
    sprt_llr: float
    by_detector: dict = field(default_factory=dict)  # which rule found how many rows
    chance_hits: float = 0.0  # rows expected to pass a checksum by accident (removed by the correction)


def _meta_by_type(detectors: list[Detector]) -> dict[str, DetectorMeta]:
    out: dict[str, DetectorMeta] = {}
    for d in detectors:
        out.setdefault(d.meta.data_type, d.meta)
    # the name detector also emits GIVEN_NAME / SURNAME
    if "PERSON_NAME" in out:
        for t in ("GIVEN_NAME", "SURNAME"):
            out.setdefault(t, out["PERSON_NAME"])
    return out


def decide_type(t: str, x: int, n: int, cands: int, meta: DetectorMeta, th: Thresholds, looks: int,
                se_sp_override: dict | None = None, by_detector: dict | None = None) -> TypeResult:
    """Zone for one data type in one column."""
    th = th.for_type(t)
    se, sp = meta.se, meta.sp
    chance_hits = 0.0
    if meta.chance is not None:
        se = max(se, DEFAULT_CHECKSUM_SE)
        # Shaped candidates that failed the check are certainly not this type; the pool of such "random"
        # candidates is failed / (1 - chance), and chance of them pass by accident (DECISIONS D-028).
        random_pool = (cands - x) / (1 - meta.chance) if cands > x else 0.0
        sp = 1 - min(1.0, random_pool * meta.chance / n) if n else 1.0
        chance_hits = random_pool * meta.chance
    if se_sp_override:
        se = se_sp_override.get("se", se)
        sp = min(sp, se_sp_override.get("sp", sp))
    se = max(se, 1 - sp + 0.05)  # keep the correction informative (Se + Sp > 1)

    k = max(1, looks)  # Bonferroni over planned looks of the sequential design (D-009)
    conf_s = 1 - (1 - th.conf_sensitive) / k
    conf_c = 1 - (1 - th.conf_clean) / k

    def corr(p):
        return rogan_gladen(p, se, sp)

    p_obs = x / n if n else 0.0
    lo_s, hi_s = corr(wilson_lower(x, n, conf_s)), corr(wilson_upper(x, n, conf_s))
    lo_c, hi_c = corr(wilson_lower(x, n, conf_c)), corr(wilson_upper(x, n, conf_c))

    if n == 0:
        zone = "EMPTY"
    elif lo_s >= th.p_sensitive:
        zone = "SENSITIVE"
    elif hi_c <= th.p_clean and lo_c <= 0.0:
        zone = "CLEAN"  # D-006: small upper bound AND no statistical evidence of presence
    elif lo_c > 0.0 and hi_s < th.p_sensitive:
        zone = "SPARSE"
    else:
        zone = "UNDECIDED"
    return TypeResult(t, meta.id, meta.version, x, n, round(p_obs, 6), round(se, 6), round(sp, 6),
                      round(corr(p_obs), 6), round(lo_s, 6), round(hi_s, 6), zone,
                      round(sprt_llr(x, n, th.p_clean, th.p_sensitive), 3), dict(by_detector or {}), round(chance_hits, 1))


@dataclass
class ColumnDecision:
    column: str
    data_type_sql: str
    zone: str
    sensitive_type: str | None
    conflict: bool
    conflict_reason: str
    p_corrected: float
    ci_low: float
    ci_high: float
    confidence_level: float
    n: int
    n_rows: int
    evidence: list[TypeResult]
    metadata_signal: dict[str, float]
    profile_summary: dict
    redacted_examples: list[dict]
    free_text: bool
    vector: object = None  # profile vector (in memory only; used for classifier/vector signals, never stored)

    @property
    def review_rank(self) -> int:
        return ZONE_ORDER.index("CONFLICT" if self.conflict else self.zone)


def decide_column(ev: ColumnEvidence, meta_scores: dict[str, float], th: Thresholds, looks: int,
                  detectors: list[Detector] | None = None, overrides: dict | None = None) -> ColumnDecision:
    detectors = detectors if detectors is not None else generic_detectors()
    metas = _meta_by_type(detectors)
    prof = ev.profile()
    results = []
    for t in sorted(set(ev.hits) | set(ev.cands)):
        if t in metas:
            results.append(decide_type(t, ev.hits.get(t, 0), ev.n, ev.cands.get(t, 0), metas[t], th, looks,
                                       (overrides or {}).get(t), ev.by_detector.get(t)))
    if ev.n == 0:
        zone, best = "EMPTY", None
    elif not results:
        zone, best = "CLEAN", None
    else:
        best = max(results, key=lambda r: (SEVERITY[r.zone], r.p_corrected,
                                           -TYPE_PRIORITY.index(r.data_type) if r.data_type in TYPE_PRIORITY else -99))
        zone = best.zone
        if zone == "CLEAN":
            best = None

    free = is_free_text(prof)
    conflict, reason = False, ""
    top_meta = max(meta_scores.items(), key=lambda kv: kv[1], default=(None, 0.0))
    if zone == "CLEAN" and top_meta[1] >= th.conflict_metadata:
        conflict, reason = True, f"İsim {top_meta[0]} gösteriyor, değerlerde bulunmadı"
    elif zone == "SENSITIVE" and best and top_meta[0] and top_meta[1] >= th.conflict_metadata \
            and top_meta[0] != best.data_type and best.data_type not in ("PERSON_NAME", "GIVEN_NAME", "SURNAME"):
        conflict, reason = True, f"İsim {top_meta[0]} gösteriyor, değerler {best.data_type}"
    elif zone == "SENSITIVE" and best and not free and top_meta[1] < 0.3:
        conflict, reason = True, "Kolon adından anlaşılmayan hassas veri"

    examples = []
    if best:
        examples = ev.examples.get(best.data_type, [])
    return ColumnDecision(
        column=ev.column.name, data_type_sql=ev.column.type_label, zone=zone,
        sensitive_type=best.data_type if best else None, conflict=conflict, conflict_reason=reason,
        p_corrected=best.p_corrected if best else 0.0, ci_low=best.ci_low if best else 0.0,
        ci_high=best.ci_high if best else (results and max(r.ci_high for r in results)) or 0.0,
        confidence_level=th.conf_sensitive, n=ev.n, n_rows=ev.n_rows, evidence=results,
        metadata_signal=meta_scores, profile_summary=prof.summary(), redacted_examples=examples, free_text=free,
        vector=prof.vector(),
    )


def hash_pk(values: list[str]) -> str:
    return "pk#" + hashlib.sha256("|".join(values).encode()).hexdigest()[:10]


def protect_sensitive_pks(decisions: list[ColumnDecision], pk_columns: list[str]) -> None:
    """D-014: if a primary-key column itself holds sensitive data, examples carry a hash instead of the PK."""
    if not any(d.column in pk_columns and d.zone in ("SENSITIVE", "SPARSE", "UNDECIDED") for d in decisions):
        return
    for d in decisions:
        for ex in d.redacted_examples:
            ex["pk"] = [hash_pk(ex["pk"])]
