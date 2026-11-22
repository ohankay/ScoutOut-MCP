"""Scan one table: sequential sampling until every column is decided or n_max is reached."""

from __future__ import annotations

from dataclasses import dataclass

from scoutout.connector.base import TableInfo
from scoutout.core.config import ScanConfig
from scoutout.detectors import Detector, generic_detectors
from scoutout.metadata_detector import column_scores
from scoutout.sampler import Sampler, SampleRecord

from .engine import ColumnDecision, ColumnEvidence, Thresholds, decide_column, protect_sensitive_pks


@dataclass
class TableResult:
    table: TableInfo
    decisions: list[ColumnDecision]
    record: SampleRecord
    skipped_columns: list[str]


def _merge(total: SampleRecord | None, r: SampleRecord) -> SampleRecord:
    if total is None:
        return r
    total.n_target += r.n_target
    total.n_actual += r.n_actual
    for k, v in r.strata.items():
        total.strata[k] = total.strata.get(k, 0) + v if k != "tablesample_percent" else v
    total.bias_flags = sorted(set(total.bias_flags) | set(r.bias_flags))
    total.duration_ms += r.duration_ms
    total.queries += r.queries
    total.rounds = r.rounds
    if r.strategy != total.strategy and r.strategy == "tablesample":
        total.strategy = r.strategy
    return total


def scan_table(adapter, table: TableInfo, cfg: ScanConfig, thresholds: Thresholds | None = None,
               detectors: list[Detector] | None = None, learned_names: dict | None = None,
               overrides: dict | None = None) -> TableResult:
    th = thresholds or Thresholds()
    detectors = detectors if detectors is not None else generic_detectors()
    sampler = Sampler(adapter, cfg)
    cols = sampler.readable_columns(table)
    skipped = [c.name for c in table.columns if c not in cols]
    evidence = {c.name: ColumnEvidence(c) for c in cols}
    meta = {c.name: column_scores(c.name, c.data_type, c.max_length, learned_names) for c in cols}
    seen: set = set()
    record = None
    looks = len(cfg.looks)
    rows: list[tuple] = []
    exhausted = False
    used = 0
    active = list(cols)  # columns not yet fully decided: only these keep consuming rows (CPU)
    decided: dict[str, ColumnDecision] = {}

    for i, target in enumerate(cfg.looks):
        # fetch until the look's size is reached (small tables arrive in one query, already in random order)
        while len(rows) < target and not exhausted:
            need = target - len(rows)
            s = sampler.sample(table, need, i, seen)
            record = _merge(record, s.record)
            rows.extend(s.rows)
            # a small table that returned fewer rows than asked has no more; seek strategies may simply overlap
            exhausted = s.record.n_actual == 0 or (s.record.strategy == "small_table" and s.record.n_actual < need)
        batch = rows[used:target]
        used += len(batch)
        idx = {c.name: k for k, c in enumerate(cols)}
        for pk, values in batch:
            for c in active:
                evidence[c.name].add(pk, values[idx[c.name]], detectors)
        for c in active:
            decided[c.name] = decide_column(evidence[c.name], meta[c.name], th, looks, detectors, overrides)
        # keep reading for a column while its zone OR any of its data types is still undecided
        active = [c for c in active if decided[c.name].zone == "UNDECIDED"
                  or any(e.zone == "UNDECIDED" for e in decided[c.name].evidence)]
        if not active or (exhausted and used >= len(rows)):
            break

    decisions = [decided[c.name] for c in cols if c.name in decided]
    protect_sensitive_pks(decisions, table.primary_key)
    return TableResult(table, decisions, record or SampleRecord("none", cfg.seed, 0), skipped)
