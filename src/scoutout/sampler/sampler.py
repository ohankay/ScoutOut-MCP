"""Sampling strategies.

Rules (spec §3.1/§3.2): never scan a large table. Large tables are read with
``SELECT TOP (m) ... WHERE key >= <k> ORDER BY key`` seeks whose start keys are drawn from the
clustered index histogram (density-weighted), or with ``TABLESAMPLE ... REPEATABLE`` for heaps.
Everything is deterministic: start keys come from ``numpy`` seeded with (seed, round, table).
All SQL uses literals (no parameters) so the exact statement can be plan-checked.
"""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

import numpy as np

from scoutout.connector.base import DATE_TYPES, ColumnInfo, TableInfo
from scoutout.core.config import ScanConfig

INT_TYPES = {"tinyint", "smallint", "int", "bigint"}
DEC_TYPES = {"decimal", "numeric", "money", "smallmoney"}
SEEKABLE_DATE_TYPES = {"date", "datetime", "datetime2", "smalldatetime"}
SEEKABLE_TEXT_TYPES = {"char", "varchar", "nchar", "nvarchar"}
#: column types we read; anything else (binary, spatial, rowversion, ...) is not scanned
COARSE_ROWS_PER_KEY = 1000  # rows per distinct leading-key value above which seeks cannot spread
KEY_OVERSAMPLE = 1.25  # extra start keys to compensate for overlapping seeks
READABLE_TYPES = (
    SEEKABLE_TEXT_TYPES | {"text", "ntext", "xml", "uniqueidentifier", "bit", "float", "real", "time",
                           "datetimeoffset"} | INT_TYPES | DEC_TYPES | DATE_TYPES
)


@dataclass
class SampleRecord:
    strategy: str
    seed: int
    n_target: int
    n_actual: int = 0
    strata: dict[str, int] = field(default_factory=dict)
    bias_flags: list[str] = field(default_factory=list)
    duration_ms: int = 0
    queries: int = 0
    rounds: int = 0


@dataclass
class Sample:
    table: str
    columns: list[ColumnInfo]  # scanned columns, aligned with values in each row
    pk_columns: list[str]  # names of pk values (or ["%%physloc%%"])
    rows: list[tuple]  # (pk_tuple, values_tuple)
    record: SampleRecord


def q(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def choose_strategy(t: TableInfo, cfg: ScanConfig) -> str:
    if t.row_count <= cfg.small_table_rows:
        return "small_table"
    if t.clustered_key:
        lead = t.column(t.clustered_key[0])
        if lead.data_type in INT_TYPES | DEC_TYPES | SEEKABLE_DATE_TYPES | SEEKABLE_TEXT_TYPES:
            if t.partition and t.partition.column != lead.name:
                return "partitioned_seek"
            return "histogram_seek"
    return "tablesample"


def literal(value, col: ColumnInfo) -> str:
    if col.data_type in INT_TYPES:
        return str(int(value))
    if col.data_type in DEC_TYPES:
        if col.data_type in ("decimal", "numeric"):
            v = round(Decimal(str(value)), col.scale)
            return f"CAST({v} AS {col.data_type}({col.precision},{col.scale}))"
        return f"CAST({Decimal(str(value))} AS {col.data_type})"
    if col.data_type in SEEKABLE_DATE_TYPES:
        # Typed literal: an untyped string literal makes the optimizer scan partitioned indexes.
        sql_type = f"datetime2({col.scale})" if col.data_type == "datetime2" else col.data_type
        if col.data_type == "date":
            text = (value.date() if isinstance(value, datetime) else value).isoformat()
        elif isinstance(value, datetime):
            text = value.isoformat(timespec="milliseconds")
        else:
            text = value.isoformat()
        return f"CAST('{text}' AS {sql_type})"
    s = str(value).replace("'", "''")
    return f"N'{s}'" if col.data_type in ("nchar", "nvarchar") else f"'{s}'"


class Sampler:
    def __init__(self, adapter, cfg: ScanConfig):
        self.db = adapter
        self.cfg = cfg

    # -- SQL building blocks -------------------------------------------------------
    def readable_columns(self, t: TableInfo) -> list[ColumnInfo]:
        return [c for c in t.columns if c.data_type in READABLE_TYPES]

    def _select_list(self, t: TableInfo, cols: list[ColumnInfo], alias: str = "") -> tuple[str, list[str]]:
        a = f"{alias}." if alias else ""
        pk = t.primary_key or []
        pk_exprs = [f"{a}{q(c)} AS [k{i}]" for i, c in enumerate(pk)] if pk else [f"{a}%%physloc%% AS [k0]"]
        exprs = []
        for c in cols:
            col = a + q(c.name)
            if c.is_lob or c.data_type in ("text", "ntext", "xml"):
                exprs.append(f"LEFT(CAST({col} AS nvarchar(max)), {self.cfg.lob_chars})")
            elif c.data_type in ("datetimeoffset", "time", "uniqueidentifier"):
                exprs.append(f"CAST({col} AS nvarchar(64))")
            else:
                exprs.append(col)
        exprs = [f"{e} AS [c{i}]" for i, e in enumerate(exprs)]
        return ", ".join(pk_exprs + exprs), (pk or ["%%physloc%%"])

    def _from(self, t: TableInfo) -> str:
        hint = " WITH (NOLOCK)" if self.cfg.read_uncommitted else ""
        return f"{q(t.schema)}.{q(t.name)}{hint}"

    def _option(self) -> str:
        return f" OPTION (MAXDOP {self.cfg.maxdop})"

    def seek_sql(self, t: TableInfo, select: str, keys: list, where_extra: str = "") -> str:
        lead = t.column(t.clustered_key[0])
        m = self.cfg.rows_per_seek
        parts = []
        for k in keys:
            cond = f"{q(lead.name)} >= {literal(k, lead)}" + (f" AND {where_extra}" if where_extra else "")
            parts.append(
                f"SELECT * FROM (SELECT TOP ({m}) {select} FROM {self._from(t)} WHERE {cond} "
                f"ORDER BY {q(lead.name)}) s"
            )
        return " UNION ALL ".join(parts) + self._option()

    # -- main entry ----------------------------------------------------------------
    def sample(self, t: TableInfo, n_target: int, round_no: int = 0, seen: set | None = None) -> Sample:
        """Fetch about ``n_target`` new rows (not in ``seen``). Rounds are deterministic and additive."""
        seen = seen if seen is not None else set()
        cols = self.readable_columns(t)
        select, pk_names = self._select_list(t, cols)
        strategy = choose_strategy(t, self.cfg)
        rec = SampleRecord(strategy, self.cfg.seed, n_target, rounds=round_no + 1)
        start_q = getattr(self.db, "queries_executed", 0)
        t0 = time.monotonic()
        rng = np.random.default_rng([self.cfg.seed, round_no, _stable_hash(t.full_name)])
        npk = len(pk_names)

        def take(rows, stratum: str):
            got = 0
            for r in rows:
                pk = tuple(r[:npk])
                if pk in seen:
                    continue
                seen.add(pk)
                out.append((pk, tuple(r[npk:])))
                got += 1
            rec.strata[stratum] = rec.strata.get(stratum, 0) + got

        out: list[tuple] = []
        if strategy == "small_table":
            self._small(t, select, n_target, len(seen), take)
        elif strategy == "histogram_seek":
            self._histogram(t, select, n_target, rng, take, rec)
        elif strategy == "partitioned_seek":
            self._partitioned(t, select, n_target, rng, take, rec)
        else:
            self._tablesample(t, select, n_target, round_no, take, rec)

        rec.n_actual = len(out)
        rec.duration_ms = int((time.monotonic() - t0) * 1000)
        rec.queries = getattr(self.db, "queries_executed", 0) - start_q
        return Sample(t.full_name, cols, pk_names, out, rec)

    # -- strategies ------------------------------------------------------------------
    def _small(self, t, select, n_target, already, take):
        """Small tables: hash ordering is allowed (spec §3.1 exception). Each round re-reads the same deterministic
        order up to the new size; rows already seen are skipped, so rounds stay additive. With a primary key only
        the keys are hash-sorted and the chosen rows are fetched by key, so wide/LOB columns are not sorted."""
        n = already + n_target
        keys = t.primary_key or []
        if keys:
            cols = self.readable_columns(t)
            sel, _ = self._select_list(t, cols, alias="s")
            on = " AND ".join(f"s.{q(k)} = h.{q(k)}" for k in keys)
            key_list = ", ".join(q(k) for k in keys)
            sql = (
                f"SELECT {sel} FROM {self._from(t)} AS s INNER JOIN (SELECT TOP ({n}) {key_list} FROM {self._from(t)} "
                f"ORDER BY HASHBYTES('SHA2_256', CONCAT('{self.cfg.seed}|', {key_list}, ''))) AS h ON {on}"
                f"{self._option()}"
            )
        else:
            sql = (
                f"SELECT TOP ({n}) {select} FROM {self._from(t)} ORDER BY "
                f"HASHBYTES('SHA2_256', CONCAT('{self.cfg.seed}|', %%physloc%%, ''))"
                f"{self._option()}"
            )
        take(self.db.query(sql), "all")

    def _steps(self, t: TableInfo):
        lead = t.column(t.clustered_key[0])
        if lead.data_type in INT_TYPES:
            cast = "bigint"
        elif lead.data_type in DEC_TYPES:
            cast = "float"
        elif lead.data_type in SEEKABLE_DATE_TYPES:
            cast = "datetime2"
        else:
            cast = "nvarchar(4000)"
        self._last_hist = self.db.histogram(t.schema, t.name, 1, cast)
        steps = [s[:3] for s in self._last_hist if s[0] is not None]
        return lead, steps

    def _too_coarse(self, t, steps) -> bool:
        """Leading key with very few distinct values (e.g. branch code) cannot spread seeks -> TABLESAMPLE."""
        distinct = len(steps) + sum(float(r[3]) for r in self._last_hist if r[0] is not None)
        return t.row_count / max(distinct, 1) > COARSE_ROWS_PER_KEY

    def _histogram_keys(self, lead, steps, k: int, rng, lo_q: float = 0.0, hi_q: float = 1.0,
                        oversample: float = KEY_OVERSAMPLE) -> list:
        weights = np.array([float(r) + float(e) for _, r, e in steps])
        cum = np.cumsum(weights)
        total = cum[-1]
        u = np.sort(rng.uniform(lo_q * total, hi_q * total, size=math.ceil(k * oversample)))
        keys = []
        for x in u:
            i = int(np.searchsorted(cum, x, side="right"))
            i = min(i, len(steps) - 1)
            hi = steps[i][0]
            if i == 0:
                keys.append(hi)
                continue
            lo = steps[i - 1][0]
            f = (x - (cum[i - 1])) / weights[i] if weights[i] else 0.0
            keys.append(_interpolate(lo, hi, f, lead))
        return list(dict.fromkeys(keys))  # identical start keys would only return duplicate rows

    def _run_seeks(self, t, select, keys, take, stratum, where_extra=""):
        per = self.cfg.seeks_per_query
        for i in range(0, len(keys), per):
            take(self.db.query(self.seek_sql(t, select, keys[i : i + per], where_extra)), stratum)

    def _histogram(self, t, select, n_target, rng, take, rec):
        lead, steps = self._steps(t)
        if not steps:
            rec.bias_flags.append("no_histogram_fallback_tablesample")
            return self._tablesample(t, select, n_target, rec.rounds - 1, take, rec)
        if self._too_coarse(t, steps):
            rec.bias_flags.append("leading_key_coarse_fallback_tablesample")
            rec.strategy = "tablesample"
            return self._tablesample(t, select, n_target, rec.rounds - 1, take, rec)
        if lead.data_type in SEEKABLE_TEXT_TYPES:
            rec.bias_flags.append("text_key_step_boundaries")
        m = self.cfg.rows_per_seek
        recent = self._recent_keys(t, lead, steps, math.ceil(n_target * self.cfg.recent_share / m), rng)
        main = self._histogram_keys(lead, steps, math.ceil((n_target - len(recent) * m) / m), rng)
        self._run_seeks(t, select, main, take, "main")
        if recent:
            self._run_seeks(t, select, recent, take, "recent")

    def _recent_keys(self, t, lead, steps, k: int, rng) -> list:
        """Time stratum (spec §3.2.5): date key -> last N months; integer key -> top 5% of the key space."""
        if k <= 0 or self.cfg.recent_share <= 0:
            return []
        if lead.data_type in SEEKABLE_DATE_TYPES:
            hi = _as_datetime(steps[-1][0])  # histogram max = newest key at stats time (metadata only)
            lo = hi - timedelta(days=30 * self.cfg.recent_months)
            return [lo + (hi - lo) * float(f) for f in np.sort(rng.uniform(0, 1, k))]
        if lead.data_type in INT_TYPES:
            return self._histogram_keys(lead, steps, k, rng, lo_q=0.95)
        return []

    def _partitioned(self, t, select, n_target, rng, take, rec):
        """Partition column differs from the clustered key: proportional quota per non-empty partition."""
        lead, steps = self._steps(t)
        rows = t.partition.rows_per_partition
        total = sum(rows) or 1
        m = self.cfg.rows_per_seek
        func = q(t.partition.function)
        pcol = q(t.partition.column)
        if not steps:
            rec.bias_flags.append("no_histogram_fallback_tablesample")
            return self._tablesample(t, select, n_target, rec.rounds - 1, take, rec)
        # Largest-remainder allocation of seeks to non-empty partitions, proportional to their rows.
        total_keys = math.ceil(n_target / m)
        shares = [total_keys * cnt / total for cnt in rows]
        alloc = [int(x) for x in shares]
        for i in sorted(range(len(rows)), key=lambda i: shares[i] - alloc[i], reverse=True)[: total_keys - sum(alloc)]:
            alloc[i] += 1
        for p_no, (cnt, k) in enumerate(zip(rows, alloc, strict=True), start=1):
            if cnt == 0 or k == 0:
                continue
            keys = self._histogram_keys(lead, steps, k, rng, oversample=1.0)
            self._run_seeks(t, select, keys, take, f"p{p_no}", where_extra=f"$PARTITION.{func}({pcol}) = {p_no}")

    def _tablesample(self, t, select, n_target, round_no, take, rec):
        """Heaps / no usable index: page-based sampling. Clustering bias is flagged."""
        if "page_clustered" not in rec.bias_flags:
            rec.bias_flags.append("page_clustered")
        rows_total = max(t.row_count, 1)
        pct = min(self.cfg.max_tablesample_percent, max(0.0001, 300.0 * n_target / rows_total))
        got_before = sum(rec.strata.values())
        for attempt in range(6):
            seed = self.cfg.seed * 1000 + round_no * 10 + attempt
            sql = (
                f"SELECT TOP ({n_target}) {select} FROM {self._from(t)} "
                f"TABLESAMPLE SYSTEM ({pct:.6f} PERCENT) REPEATABLE ({seed}){self._option()}"
            )
            take(self.db.query(sql), "tablesample")
            if sum(rec.strata.values()) - got_before >= n_target or pct >= self.cfg.max_tablesample_percent:
                break
            pct = min(self.cfg.max_tablesample_percent, pct * 4)
        rec.strata["tablesample_percent"] = int(pct * 10000)  # basis points * 100, for the record


def _stable_hash(s: str) -> int:
    return int.from_bytes(hashlib.sha256(s.encode()).digest()[:4], "big")


def _as_datetime(v) -> datetime:
    return v if isinstance(v, datetime) else datetime(v.year, v.month, v.day)


def _interpolate(lo, hi, f: float, col: ColumnInfo):
    if lo is None:
        return hi
    if isinstance(lo, (int, Decimal)) and isinstance(hi, (int, Decimal)) and col.data_type in INT_TYPES:
        return int(lo) + int(f * (int(hi) - int(lo)))
    if isinstance(lo, float) or isinstance(hi, float):
        return float(lo) + f * (float(hi) - float(lo))
    if isinstance(lo, datetime) and isinstance(hi, datetime):
        return lo + (hi - lo) * f
    return lo  # text keys: start at the step's lower boundary
