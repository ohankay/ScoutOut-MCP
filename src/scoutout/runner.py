"""Scan orchestration (spec §3.3): priority order, concurrency, rate limit, maintenance window,
checkpoint/resume, incremental reuse, pause/stop and ETA."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
from datetime import time as dtime

from scoutout.connector import open_database
from scoutout.connector.base import TableInfo
from scoutout.connector.mssql import MsSqlAdapter
from scoutout.core.config import Config
from scoutout.core.log import scrub
from scoutout.decision_engine.engine import ColumnDecision, Thresholds
from scoutout.decision_engine.table_scan import TableResult, scan_table
from scoutout.metadata_detector import column_scores
from scoutout.store import Store, now

log = logging.getLogger("scoutout")


class RateLimiter:
    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second if per_second > 0 else 0.0
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if not self.interval:
            return
        with self._lock:
            t = time.monotonic()
            delay = self._next - t
            self._next = max(t, self._next) + self.interval
        if delay > 0:
            time.sleep(delay)


def in_window(window: str, at: datetime | None = None) -> bool:
    """'22:00-06:00' style maintenance window (may wrap midnight). Empty = always."""
    if not window.strip():
        return True
    start_s, end_s = (p.strip() for p in window.split("-"))
    start, end = dtime.fromisoformat(start_s), dtime.fromisoformat(end_s)
    t = (at or datetime.now()).time()
    return start <= t < end if start < end else (t >= start or t < end)


def schema_hash(t: TableInfo) -> str:
    sig = [(c.name, c.data_type, c.max_length, c.nullable) for c in t.columns]
    return hashlib.sha256(json.dumps(sig).encode()).hexdigest()[:16]


def stats_signature(t: TableInfo, server_start: str | None, db_created: str | None = None) -> tuple[dict, float]:
    """Change signature of a table (DMV metadata only) and the statistics modification ratio (D-030, D-043)."""
    last = max((s.last_updated or "" for s in t.stats), default="")
    mods = max((s.modification_counter or 0 for s in t.stats), default=0)
    sig = {"stats": last, "write": t.last_user_update, "server": server_start, "db": db_created,
           "rows": t.row_count, "mods": mods}
    return sig, mods / max(t.row_count, 1)


def can_carry(prev: dict | list, cur: dict, ratio: float, threshold: float, age_days: float, max_days: int) -> bool:
    """May the previous decisions of a table be reused instead of scanning it again?

    * proven unchanged: the last write time is known and equal, on the same server run and the same database
      (not restored since), statistics untouched and few modifications;
    * not provable (a restored copy or no VIEW SERVER STATE: the last write time is unknown): only when row count and
      statistics are exactly the same;
    * never when the real scan is older than ``max_days`` — a safety net for changes no metadata shows.
    """
    if not isinstance(prev, dict) or age_days >= max_days:
        return False  # older signature format or too old: scan again
    if cur["stats"] != prev.get("stats") or ratio >= threshold:
        return False
    if cur["write"] is not None and prev.get("write") is not None:
        return (cur["write"] == prev["write"] and cur["server"] == prev.get("server") and cur["db"] == prev.get("db")
                and abs(cur["rows"] - prev.get("rows", 0)) / max(prev.get("rows", 0), 1) < threshold)
    return cur["rows"] == prev.get("rows") and cur["mods"] == prev.get("mods")


def table_priority(t: TableInfo) -> float:
    """Spec §3.3: high metadata score first, then free-text types, then the rest."""
    meta = max((max(column_scores(c.name, c.data_type, c.max_length).values(), default=0) for c in t.columns), default=0)
    free_text = any(c.is_lob or (c.is_text and (c.max_length or 0) >= 100) for c in t.columns)
    return round(meta + (0.5 if free_text else 0.0), 3)


def decision_record(run_id: str, t: TableInfo, d: ColumnDecision, r: TableResult, pack_version: str | None,
                    extra: dict | None = None, source: str | None = None) -> dict:
    """Spec §5.9 decision record. Contains no raw values."""
    rec = {
        "column_id": f"{t.schema}.{t.name}.{d.column}",
        "source": source,
        "schema": t.schema, "table": t.name, "column": d.column, "data_type": d.data_type_sql,
        "row_count_estimate": t.row_count,
        "sampling": {
            "strategy": r.record.strategy, "seed": r.record.seed, "n_target": r.record.n_target,
            "n_actual": r.record.n_actual, "strata": r.record.strata, "bias_flags": r.record.bias_flags,
        },
        "profile_summary": d.profile_summary,
        "evidence": [asdict(e) for e in d.evidence],
        "metadata_signal": d.metadata_signal,
        "classifier_top_k": [], "vector_margin": None,
        "sensitive_type": d.sensitive_type,
        "p_corrected": d.p_corrected, "ci_low": d.ci_low, "ci_high": d.ci_high,
        "confidence_level": d.confidence_level, "n": d.n,
        "zone": d.zone, "conflict_flag": d.conflict, "conflict_reason": d.conflict_reason, "free_text": d.free_text,
        "redacted_examples": d.redacted_examples,
        "run_id": run_id, "knowledge_pack_version": pack_version, "created_at": now(),
    }
    if extra:
        rec.update(extra)
    return rec


class Runner:
    def __init__(self, cfg: Config, store: Store, user: str = "system", thresholds: Thresholds | None = None,
                 pack=None, adapter_factory=None, source: str | None = None, only_tables: list[str] | None = None,
                 force: bool = False):
        self.cfg = cfg
        self.source = source or cfg.connection.database  # name of the database source (one store, many databases)
        self.store = store
        self.user = user
        self.thresholds = thresholds or load_thresholds(store)
        self.pack = pack
        self.adapter_factory = adapter_factory or (lambda: MsSqlAdapter(cfg.connection))
        self.limiter = RateLimiter(cfg.scan.queries_per_second)
        self.fail_after: int | None = None  # test hook: simulate a crash after N tables
        self.only_tables = {t.lower() for t in only_tables} if only_tables else None  # manual scan of chosen tables
        self.force = force  # scan even tables that look unchanged

    # -- run lifecycle ----------------------------------------------------------------
    def start(self) -> str:
        db, report = open_database(self.cfg)  # enforces read-only policy
        try:
            catalog = db.discover_catalog()
        finally:
            db.close()
        if catalog.db_state != "ONLINE":  # e.g. RESTORING: the daily copy is being replaced; try again later
            raise RuntimeError(f"Veritabanı şu an kullanılamıyor ({catalog.db_state}); daha sonra yeniden denenecek.")
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        if self.only_tables is not None:
            tables = [t for t in catalog.tables if t.full_name.lower() in self.only_tables]
            if not tables:
                raise ValueError("Seçilen tablolar bu veritabanında bulunamadı.")
        else:
            tables = [t for t in catalog.tables if self._included(t)]
        prev = self._previous_table_states()
        with self.store.tx() as c:
            c.execute(
                "INSERT INTO runs (id, source, database, started_at, status, seed, pack_version, config_json, tables_total, created_by, "
                "scope) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, self.source, self.cfg.connection.database, now(), "running", self.cfg.scan.seed,
                 self.pack.version if self.pack else None, json.dumps(asdict(self.cfg.scan)), len(tables), self.user,
                 "tables" if self.only_tables is not None else "all"),
            )
            for t in tables:
                sig, _ = stats_signature(t, catalog.server_start, catalog.db_created)
                c.execute("INSERT INTO run_tables (run_id, table_name, status, priority, row_count, schema_hash, stats_signature) "
                          "VALUES (?,?,?,?,?,?,?)",
                          (run_id, t.full_name, "pending", table_priority(t), t.row_count, schema_hash(t),
                           json.dumps(sig)))
        self.store.audit(self.user, "run.start", run_id, source=self.source, tables=len(tables), read_only=report.read_only,
                         scope="seçili tablolar" if self.only_tables is not None else "tümü", force=self.force)
        if not self.force:
            self._carry_unchanged(run_id, tables, prev, catalog.server_start, catalog.db_created)
        self._catalog = {t.full_name: t for t in tables}
        return run_id

    def resume(self, run_id: str) -> None:
        r = self.store.one("SELECT status FROM runs WHERE id = ?", run_id)
        if r is None:
            raise KeyError(run_id)
        db, _ = open_database(self.cfg)
        try:
            catalog = db.discover_catalog()
        finally:
            db.close()
        self._catalog = {t.full_name: t for t in catalog.tables}
        self.store.x("UPDATE run_tables SET status='pending', error=NULL WHERE run_id=? AND status IN ('running','failed')", run_id)
        self.store.x("UPDATE runs SET status='running', finished_at=NULL WHERE id=?", run_id)
        self.store.audit(self.user, "run.resume", run_id)

    def execute(self, run_id: str, on_progress=None) -> str:
        """Process all pending tables of the run. Returns the final run status."""
        pending = self.store.q("SELECT table_name FROM run_tables WHERE run_id=? AND status='pending' "
                               "ORDER BY priority DESC, table_name", run_id)
        queue = [self._catalog[r["table_name"]] for r in pending if r["table_name"] in self._catalog]
        lock = threading.Lock()
        done_counter = {"n": 0}
        local = threading.local()

        crashed = threading.Event()

        def worker(t: TableInfo):
            if crashed.is_set() or self._should_stop(run_id):
                return
            if not hasattr(local, "db"):
                local.db = self.adapter_factory()
                local.db.set_query_timeout(self.cfg.scan.query_timeout_s)
                local.db.before_query = lambda: self._gate(run_id)
            self.store.x("UPDATE run_tables SET status='running' WHERE run_id=? AND table_name=?", run_id, t.full_name)
            t0 = time.monotonic()
            try:
                with lock:
                    if self.fail_after is not None and done_counter["n"] >= self.fail_after:
                        crashed.set()  # simulate the process dying mid-table: this table stays 'running'
                        return
                result = scan_table(local.db, t, self.cfg.scan, self.thresholds,
                                    self.pack.detectors() if self.pack else None,
                                    self.pack.learned_names if self.pack else None,
                                    self.pack.overrides if self.pack else None)
                self._save(run_id, t, result, int((time.monotonic() - t0) * 1000))
                with lock:
                    done_counter["n"] += 1
                if on_progress:
                    on_progress(t, result)
            except _Stop:
                self.store.x("UPDATE run_tables SET status='pending' WHERE run_id=? AND table_name=?", run_id, t.full_name)
            except Exception as e:  # noqa: BLE001 - one bad table must not stop the run
                msg = scrub(f"{type(e).__name__}: {e}")[:500]
                log.warning("table %s failed: %s", t.full_name, msg)
                self.store.x("UPDATE run_tables SET status='failed', error=?, finished_at=? WHERE run_id=? AND table_name=?",
                             msg, now(), run_id, t.full_name)

        with ThreadPoolExecutor(max_workers=max(1, self.cfg.scan.concurrency), thread_name_prefix="scan") as pool:
            list(pool.map(worker, queue))
        if crashed.is_set():
            self.store.x("UPDATE runs SET status='interrupted' WHERE id=?", run_id)
            return "interrupted"

        counts = {r["status"]: r["n"] for r in self.store.q(
            "SELECT status, COUNT(*) n FROM run_tables WHERE run_id=? GROUP BY status", run_id)}
        status = self.store.one("SELECT status FROM runs WHERE id=?", run_id)["status"]
        if status in ("stopping", "paused") or counts.get("pending"):
            final = "stopped"
        else:
            final = "done" if not counts.get("failed") else "done_with_errors"
        self.store.x("UPDATE runs SET status=?, finished_at=?, tables_done=? WHERE id=?", final, now(),
                     counts.get("done", 0) + counts.get("carried", 0), run_id)
        self.store.audit(self.user, "run.finish", run_id, status=final, **counts)
        return final

    def run(self, on_progress=None) -> str:
        run_id = self.start()
        self.execute(run_id, on_progress)
        return run_id

    # -- helpers --------------------------------------------------------------------------
    def _included(self, t: TableInfo) -> bool:
        s = self.cfg.scan
        if s.include_schemas and t.schema not in s.include_schemas:
            return False
        return t.full_name not in s.exclude_tables

    def _gate(self, run_id: str) -> None:
        """Called before every query: pause/stop, maintenance window, rate limit."""
        while True:
            status = self.store.one("SELECT status FROM runs WHERE id=?", run_id)["status"]
            if status == "stopping":
                raise _Stop()
            if status == "paused" or not in_window(self.cfg.scan.maintenance_window):
                time.sleep(1.0)
                continue
            break
        self.limiter.wait()

    def _should_stop(self, run_id: str) -> bool:
        return self.store.one("SELECT status FROM runs WHERE id=?", run_id)["status"] == "stopping"

    def _previous_table_states(self) -> dict:
        rows = self.store.q(
            """SELECT rt.* FROM run_tables rt JOIN runs r ON r.id = rt.run_id
               WHERE rt.status IN ('done','carried') AND r.source = ?
               ORDER BY rt.finished_at""", self.source)
        return {r["table_name"]: r for r in rows}  # last one wins

    def _carry_unchanged(self, run_id: str, tables: list[TableInfo], prev: dict, server_start: str | None,
                         db_created: str | None = None) -> None:
        """Incremental scan: reuse previous decisions of tables that did not change (see ``can_carry``)."""
        th = self.cfg.scan.incremental_threshold
        for t in tables:
            p = prev.get(t.full_name)
            if not p or p["schema_hash"] != schema_hash(t):
                continue
            sig, ratio = stats_signature(t, server_start, db_created)
            scanned = p["scanned_at"] or p["finished_at"]
            age = (datetime.now(UTC) - datetime.fromisoformat(scanned)).total_seconds() / 86400 if scanned else 1e9
            if not can_carry(json.loads(p["stats_signature"]), sig, ratio, th, age, self.cfg.scan.max_carry_days):
                continue
            with self.store.tx() as c:
                for d in c.execute("SELECT * FROM decisions WHERE run_id=? AND source=? AND schema_name=? AND table_name=?",
                                   (p["run_id"], self.source, t.schema, t.name)).fetchall():
                    rec = json.loads(d["record_json"])
                    rec["run_id"] = run_id
                    cur = c.execute(
                        "INSERT INTO decisions (run_id, source, schema_name, table_name, column_name, sql_type, zone, sensitive_type, "
                        "conflict, p_corrected, record_json, carried_over, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?)",
                        (run_id, self.source, d["schema_name"], d["table_name"], d["column_name"], d["sql_type"], d["zone"],
                         d["sensitive_type"], d["conflict"], d["p_corrected"], json.dumps(rec), now()))
                    self._carry_review(c, d["id"], cur.lastrowid, d["zone"], d["sensitive_type"])
                c.execute("UPDATE run_tables SET status='carried', sample_json=?, finished_at=?, scanned_at=? "
                          "WHERE run_id=? AND table_name=?", (p["sample_json"], now(), scanned, run_id, t.full_name))

    @staticmethod
    def _carry_review(c, old_id: int, new_id: int, zone: str, stype: str | None) -> None:
        """Keep a human review when the new system decision equals the one that was reviewed."""
        r = c.execute("SELECT * FROM reviews WHERE decision_id=? ORDER BY id DESC LIMIT 1", (old_id,)).fetchone()
        if r is None:
            return
        old = c.execute("SELECT zone, sensitive_type FROM decisions WHERE id=?", (old_id,)).fetchone()
        if old["zone"] == zone and old["sensitive_type"] == stype:
            c.execute("INSERT INTO reviews (decision_id, status, final_zone, final_type, reason, reviewer, reviewed_at, carried_from) "
                      "VALUES (?,?,?,?,?,?,?,?)", (new_id, r["status"], r["final_zone"], r["final_type"], r["reason"],
                                                   r["reviewer"], r["reviewed_at"], old_id))

    def _save(self, run_id: str, t: TableInfo, r: TableResult, duration_ms: int) -> None:
        version = self.pack.version if self.pack else None
        with self.store.tx() as c:
            for d in r.decisions:
                extra = self.pack.column_signals(t, d) if self.pack else None
                rec = decision_record(run_id, t, d, r, version, extra, self.source)
                conflict = d.conflict or bool(extra and extra.get("conflict_flag"))
                if extra and extra.get("conflict_flag") and not d.conflict:
                    rec["conflict_flag"], rec["conflict_reason"] = True, extra.get("conflict_reason", "")
                cur = c.execute(
                    "INSERT INTO decisions (run_id, source, schema_name, table_name, column_name, sql_type, zone, sensitive_type, "
                    "conflict, p_corrected, record_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, self.source, t.schema, t.name, d.column, d.data_type_sql, d.zone, d.sensitive_type, int(conflict),
                     d.p_corrected, json.dumps(rec, ensure_ascii=False), now()))
                prev = c.execute(
                    "SELECT id FROM decisions WHERE source=? AND schema_name=? AND table_name=? AND column_name=? AND id < ? "
                    "ORDER BY id DESC LIMIT 1", (self.source, t.schema, t.name, d.column, cur.lastrowid)).fetchone()
                if prev:
                    self._carry_review(c, prev["id"], cur.lastrowid, d.zone, d.sensitive_type)
            sample = asdict(r.record) | {"skipped_columns": r.skipped_columns}
            c.execute("UPDATE run_tables SET status='done', sample_json=?, duration_ms=?, finished_at=?, scanned_at=? "
                      "WHERE run_id=? AND table_name=?", (json.dumps(sample), duration_ms, now(), now(), run_id, t.full_name))


class _Stop(Exception):
    pass


def load_thresholds(store: Store) -> Thresholds:
    data = store.get_setting("thresholds")
    return Thresholds(**data) if data else Thresholds()


def progress(store: Store, run_id: str, concurrency: int) -> dict:
    """Progress and ETA (spec §12): remaining tables × mean table time ÷ concurrency."""
    rows = store.q("SELECT status, duration_ms FROM run_tables WHERE run_id=?", run_id)
    total = len(rows)
    done = [r for r in rows if r["status"] in ("done", "carried", "failed")]
    durations = [r["duration_ms"] for r in rows if r["status"] == "done" and r["duration_ms"]]
    remaining = total - len(done)
    avg = sum(durations) / len(durations) / 1000 if durations else None
    eta = remaining * avg / max(concurrency, 1) if avg is not None else None
    return {"total": total, "done": len(done), "remaining": remaining,
            "failed": sum(1 for r in rows if r["status"] == "failed"),
            "avg_table_s": round(avg, 2) if avg else None, "eta_s": round(eta) if eta is not None else None}
