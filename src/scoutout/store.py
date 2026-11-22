"""ScoutOut's own metadata store (SQLite, stdlib). It never holds raw values (spec §5.12)."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, database TEXT, started_at TEXT, finished_at TEXT, status TEXT, seed INTEGER,
  pack_version TEXT, config_json TEXT, tables_total INTEGER DEFAULT 0, tables_done INTEGER DEFAULT 0,
  error TEXT, created_by TEXT);
CREATE TABLE IF NOT EXISTS run_tables (
  run_id TEXT, table_name TEXT, status TEXT, priority REAL, row_count INTEGER, schema_hash TEXT,
  stats_signature TEXT, sample_json TEXT, duration_ms INTEGER, error TEXT, finished_at TEXT,
  PRIMARY KEY (run_id, table_name));
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, schema_name TEXT, table_name TEXT, column_name TEXT,
  sql_type TEXT, zone TEXT, sensitive_type TEXT, conflict INTEGER, p_corrected REAL, record_json TEXT,
  carried_over INTEGER DEFAULT 0, created_at TEXT,
  UNIQUE (run_id, schema_name, table_name, column_name));
CREATE INDEX IF NOT EXISTS ix_decisions_col ON decisions (schema_name, table_name, column_name);
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id INTEGER, status TEXT, final_zone TEXT, final_type TEXT,
  reason TEXT, reviewer TEXT, reviewed_at TEXT, carried_from INTEGER);
CREATE INDEX IF NOT EXISTS ix_reviews_dec ON reviews (decision_id);
CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, pw_hash TEXT, role TEXT, created_at TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, username TEXT, action TEXT, target TEXT, detail_json TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value_json TEXT, updated_at TEXT, updated_by TEXT);
CREATE TABLE IF NOT EXISTS reports (version INTEGER PRIMARY KEY, created_at TEXT, created_by TEXT, files_json TEXT,
  summary_json TEXT, rows_json TEXT, content_hash TEXT);
CREATE TABLE IF NOT EXISTS rules (id INTEGER PRIMARY KEY AUTOINCREMENT, data_type TEXT, kind TEXT, body_json TEXT,
  source TEXT, status TEXT, metrics_json TEXT, created_at TEXT, decided_by TEXT, decided_at TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS packs (version TEXT PRIMARY KEY, created_at TEXT, created_by TEXT, summary_json TEXT, active INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS baseline_columns (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, schema_name TEXT, table_name TEXT,
  column_name TEXT, data_type TEXT, added_by TEXT, added_at TEXT, UNIQUE (source, schema_name, table_name, column_name));
CREATE TABLE IF NOT EXISTS masking_lists (
  version INTEGER PRIMARY KEY AUTOINCREMENT, fetched_at TEXT, origin TEXT, entries_json TEXT, entry_count INTEGER,
  content_hash TEXT, added_json TEXT, removed_json TEXT, created_by TEXT);
CREATE TABLE IF NOT EXISTS sources (
  name TEXT PRIMARY KEY, server TEXT, database TEXT, auth TEXT, username TEXT, password_env TEXT, password_file TEXT,
  encrypt INTEGER DEFAULT 1, trust INTEGER DEFAULT 0, include_schemas TEXT DEFAULT '[]', schedule TEXT DEFAULT '',
  enabled INTEGER DEFAULT 1, created_at TEXT, updated_by TEXT);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _columns(self, table: str) -> set[str]:
        return {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def _migrate(self) -> None:
        """Idempotent upgrades of an existing store (v1 -> v2: database sources)."""
        with self.tx() as c:
            if "source" not in self._columns("runs"):
                c.execute("ALTER TABLE runs ADD COLUMN source TEXT")
            c.execute("UPDATE runs SET source = database WHERE source IS NULL")
            if "source" not in self._columns("decisions"):
                c.execute("ALTER TABLE decisions ADD COLUMN source TEXT")
            c.execute("UPDATE decisions SET source = (SELECT r.source FROM runs r WHERE r.id = decisions.run_id) WHERE source IS NULL")
            c.execute("DROP INDEX IF EXISTS ix_decisions_col")
            c.execute("CREATE INDEX IF NOT EXISTS ix_decisions_src ON decisions (source, schema_name, table_name, column_name)")
            if "source" not in self._columns("baseline_columns"):  # v1 had a unique key without the source
                only = c.execute("SELECT DISTINCT source FROM runs").fetchall()
                default = only[0][0] if len(only) == 1 else ""
                c.execute("ALTER TABLE baseline_columns RENAME TO baseline_columns_v1")
                c.execute("""CREATE TABLE baseline_columns (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, schema_name TEXT,
                  table_name TEXT, column_name TEXT, data_type TEXT, added_by TEXT, added_at TEXT,
                  UNIQUE (source, schema_name, table_name, column_name))""")
                c.execute("INSERT INTO baseline_columns (source, schema_name, table_name, column_name, data_type, added_by, added_at) "
                          "SELECT ?, schema_name, table_name, column_name, data_type, added_by, added_at FROM baseline_columns_v1",
                          (default,))
                c.execute("DROP TABLE baseline_columns_v1")
            if "scanned_at" not in self._columns("run_tables"):  # when the table was really scanned (not carried)
                c.execute("ALTER TABLE run_tables ADD COLUMN scanned_at TEXT")
                c.execute("UPDATE run_tables SET scanned_at = finished_at WHERE status = 'done'")
            if "scope" not in self._columns("runs"):  # 'all' or 'tables' (manual scan of chosen tables)
                c.execute("ALTER TABLE runs ADD COLUMN scope TEXT DEFAULT 'all'")
            if "prod_name" not in self._columns("sources"):
                c.execute("ALTER TABLE sources ADD COLUMN prod_name TEXT DEFAULT ''")
            if "kind" not in self._columns("reports"):
                c.execute("ALTER TABLE reports ADD COLUMN kind TEXT DEFAULT 'inventory'")
            c.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self):
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def q(self, sql: str, *params) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, *params):
        rows = self.q(sql, *params)
        return rows[0] if rows else None

    def x(self, sql: str, *params) -> int:
        with self._lock:
            return self.conn.execute(sql, params).lastrowid

    # -- audit & settings ----------------------------------------------------------------
    def audit(self, user: str, action: str, target: str = "", **detail) -> None:
        self.x("INSERT INTO audit (at, username, action, target, detail_json) VALUES (?,?,?,?,?)",
               now(), user, action, target, json.dumps(detail, ensure_ascii=False, default=str))

    def get_setting(self, key: str, default=None):
        r = self.one("SELECT value_json FROM settings WHERE key = ?", key)
        return json.loads(r["value_json"]) if r else default

    def set_setting(self, key: str, value, user: str) -> None:
        before = self.get_setting(key)
        self.x("INSERT INTO settings VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
               "updated_at=excluded.updated_at, updated_by=excluded.updated_by", key, json.dumps(value), now(), user)
        self.audit(user, "setting.change", key, before=before, after=value)

    # -- decisions & reviews -----------------------------------------------------------
    def latest_decisions(self, where: str = "1=1", *params) -> list[sqlite3.Row]:
        """Latest decision per column with its latest review (if any)."""
        return self.q(
            f"""SELECT d.*, r.status AS review_status, r.final_zone, r.final_type, r.reason, r.reviewer,
                       r.reviewed_at, r.carried_from
                FROM decisions d
                JOIN (SELECT MAX(id) AS id FROM decisions GROUP BY source, schema_name, table_name, column_name) l ON l.id = d.id
                LEFT JOIN reviews r ON r.id = (SELECT MAX(id) FROM reviews WHERE decision_id = d.id)
                WHERE {where}
                ORDER BY d.source, d.schema_name, d.table_name, d.id""",
            *params,
        )

    def review(self, decision_id: int, status: str, final_zone: str, final_type: str | None, reason: str,
               reviewer: str, carried_from: int | None = None) -> None:
        if status not in ("APPROVED", "OVERRIDDEN", "REJECTED"):
            raise ValueError(status)
        if not reason or not reason.strip():
            raise ValueError("Gerekçe zorunludur")
        d = self.one("SELECT zone, sensitive_type, source, schema_name, table_name, column_name FROM decisions WHERE id = ?",
                     decision_id)
        if d is None:
            raise KeyError(decision_id)
        self.x("INSERT INTO reviews (decision_id, status, final_zone, final_type, reason, reviewer, reviewed_at, carried_from) "
               "VALUES (?,?,?,?,?,?,?,?)", decision_id, status, final_zone, final_type, reason.strip(), reviewer, now(),
               carried_from)
        if carried_from is None:
            self.audit(reviewer, f"review.{status.lower()}", f"{d['source']}:{d['schema_name']}.{d['table_name']}.{d['column_name']}",
                       system_zone=d["zone"], final_zone=final_zone, final_type=final_type, reason=reason.strip())
