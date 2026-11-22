"""Create the synthetic SQL Server database and its ground-truth manifest.

Development tool only: it connects as an administrator (sa) to a dev server and drops/recreates
its own ``ScoutOutSynth*`` databases.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import pyodbc

from scoutout.connector.mssql import pick_driver

from .dataset import Column, Table, expected_zone, tables
from .sqlexpr import Expr, Gen

SCALES = ("small", "large")
CHUNK_ROWS = 5_000_000
MONTHS = [date(2024 + (11 + i) // 12, (11 + i) % 12 + 1, 1) for i in range(32)]  # 2024-12 .. 2027-07


@dataclass
class ServerConfig:
    server: str
    user: str
    password: str = field(repr=False)
    driver: str = ""

    @classmethod
    def from_env(cls) -> ServerConfig:
        port = os.environ.get("MSSQL_PORT", "14333")
        return cls(
            server=os.environ.get("SCOUTOUT_SYNTH_SERVER", f"localhost,{port}"),
            user=os.environ.get("SCOUTOUT_SYNTH_USER", "sa"),
            password=os.environ["MSSQL_SA_PASSWORD"],
            driver=os.environ.get("SCOUTOUT_ODBC_DRIVER") or pick_driver(),
        )

    def connect(self, database: str = "master") -> pyodbc.Connection:
        cs = (
            f"DRIVER={{{self.driver}}};SERVER={self.server};DATABASE={database};"
            f"UID={self.user};PWD={self.password};TrustServerCertificate=yes;Encrypt=no"
        )
        return pyodbc.connect(cs, autocommit=True)


def database_name(scale: str) -> str:
    return f"ScoutOutSynth{scale.capitalize()}"


# --- SQL rendering -------------------------------------------------------------------

def _column_ddl(c: Column) -> str:
    return f"[{c.name}] {c.sql_type} {'NULL' if c.nullable else 'NOT NULL'}"


def create_table_sql(t: Table) -> list[str]:
    cols = ",\n  ".join(_column_ddl(c) for c in t.columns)
    on = f" ON ps_month([{t.partition_by_month}])" if t.partition_by_month else ""
    if t.clustered and t.primary_key:
        keys = ", ".join(f"[{k}]" for k in t.clustered)
        cols += f",\n  CONSTRAINT [PK_{t.name}] PRIMARY KEY CLUSTERED ({keys})"
    return [f"CREATE TABLE {t.full_name} (\n  {cols}\n){on}"]


def _numbers_cte(start: int, count: int, total: int) -> str:
    return (
        "WITH t AS (SELECT CAST(ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS bigint) + "
        f"{start - 1} AS n, CAST({total} AS bigint) AS total FROM (SELECT TOP ({count}) 1 AS x "
        "FROM sys.all_columns a CROSS JOIN sys.all_columns b CROSS JOIN sys.all_columns c) z)"
    )


def _column_value(g: Gen, c: Column) -> Expr:
    e = c.gen(g)
    if isinstance(e, str):
        e = Expr(e)
    if 0 < c.null_rate < 1:
        e = Expr(f"CASE WHEN {g.flag(f'{c.name}:null', c.null_rate)} THEN NULL ELSE {e.sql} END", e.applies)
    elif c.null_rate >= 1:
        e = Expr("NULL")
    return e


def insert_sql(g: Gen, t: Table, start: int, count: int, total: int) -> str:
    values, applies = [], []
    for c in t.columns:
        e = _column_value(g, c)
        values.append(e.sql)
        applies.extend(e.applies)
    col_list = ", ".join(f"[{c.name}]" for c in t.columns)
    order = " ORDER BY t.n" if t.clustered else ""
    return (
        f"{_numbers_cte(start, count, total)}\n"
        f"INSERT INTO {t.full_name} WITH (TABLOCK) ({col_list})\n"
        f"SELECT {', '.join(values)}\nFROM t\n" + "\n".join(applies) + order + "\nOPTION (MAXDOP 0)"
    )


def realised_rates_sql(g: Gen, t: Table, total: int) -> tuple[str, list[Column]] | None:
    """Exact realised contamination rate of columns with a ``dirty`` predicate (no table read)."""
    cols = [c for c in t.columns if c.dirty is not None]
    if not cols:
        return None
    sums = []
    for c in cols:
        notnull = f"NOT {g.flag(f'{c.name}:null', c.null_rate)}" if 0 < c.null_rate < 1 else "1=1"
        sums.append(f"SUM(CASE WHEN {notnull} THEN 1 ELSE 0 END)")
        sums.append(f"SUM(CASE WHEN {notnull} AND {c.dirty(g)} THEN 1 ELSE 0 END)")
    return f"{_numbers_cte(1, total, total)}\nSELECT {', '.join(sums)} FROM t", cols


# --- build ---------------------------------------------------------------------------

def build(
    scale: str, seed: int, out_dir: Path, cfg: ServerConfig | None = None, log=print, database: str | None = None,
    factor: float = 1.0,
) -> Path:
    """Build the database; ``factor`` scales every table's row count (e.g. for limited disk)."""
    if scale not in SCALES:
        raise ValueError(f"scale must be one of {SCALES}")
    cfg = cfg or ServerConfig.from_env()
    db = database or database_name(scale)
    g = Gen(seed)
    t0 = time.monotonic()

    master = cfg.connect()
    log(f"[{db}] recreating database on {cfg.server}")
    master.execute(
        f"IF DB_ID('{db}') IS NOT NULL BEGIN ALTER DATABASE [{db}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE;"
        f" DROP DATABASE [{db}]; END"
    )
    master.execute(f"CREATE DATABASE [{db}]")
    master.execute(f"ALTER DATABASE [{db}] SET RECOVERY SIMPLE")
    _ensure_logins(master)

    conn = cfg.connect(db)
    for schema in sorted({t.schema for t in tables()}):
        conn.execute(f"CREATE SCHEMA [{schema}]")
    bounds = ", ".join(f"'{m.isoformat()}'" for m in MONTHS)
    conn.execute(f"CREATE PARTITION FUNCTION pf_month (datetime2(0)) AS RANGE RIGHT FOR VALUES ({bounds})")
    conn.execute("CREATE PARTITION SCHEME ps_month AS PARTITION pf_month ALL TO ([PRIMARY])")

    manifest_tables = []
    for t in tables():
        total = max(1, int(t.rows[scale] * factor))
        for stmt in create_table_sql(t):
            conn.execute(stmt)
        for start in range(1, total + 1, CHUNK_ROWS):
            count = min(CHUNK_ROWS, total - start + 1)
            ts = time.monotonic()
            conn.execute(insert_sql(g, t, start, count, total))
            log(f"[{db}] {t.full_name}: rows {start:,}-{start + count - 1:,} ({time.monotonic() - ts:.1f}s)")
        conn.execute(f"UPDATE STATISTICS {t.full_name}")
        manifest_tables.append(_manifest_table(conn, g, t, total))

    conn.execute("CREATE USER scoutout_ro FOR LOGIN scoutout_ro")
    conn.execute("ALTER ROLE db_datareader ADD MEMBER scoutout_ro")
    conn.execute("GRANT VIEW DATABASE STATE TO scoutout_ro")  # recommended setup (exact row counts)
    master.execute("GRANT VIEW SERVER STATE TO scoutout_ro")  # recommended setup (incremental scans)
    conn.execute("CREATE USER scoutout_rw FOR LOGIN scoutout_rw")
    conn.execute("ALTER ROLE db_datareader ADD MEMBER scoutout_rw")
    conn.execute("ALTER ROLE db_datawriter ADD MEMBER scoutout_rw")

    manifest = {
        "database": db,
        "scale": scale,
        "seed": seed,
        "factor": factor,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "rate_basis": "non-NULL rows (DECISIONS D-021)",
        "tables": manifest_tables,
    }
    out = out_dir / db
    out.mkdir(parents=True, exist_ok=True)
    path = out / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"[{db}] done in {time.monotonic() - t0:.0f}s -> {path}")
    return path


def _ensure_logins(master: pyodbc.Connection) -> None:
    for login, env in (("scoutout_ro", "SCOUTOUT_RO_PASSWORD"), ("scoutout_rw", "SCOUTOUT_RW_PASSWORD")):
        pwd = os.environ[env].replace("'", "''")
        master.execute(
            f"IF SUSER_ID('{login}') IS NULL CREATE LOGIN [{login}] WITH PASSWORD = '{pwd}', CHECK_POLICY = OFF;"
            f" ELSE ALTER LOGIN [{login}] WITH PASSWORD = '{pwd}'"
        )


def _manifest_table(conn: pyodbc.Connection, g: Gen, t: Table, total: int) -> dict:
    realised: dict[str, float] = {}
    q = realised_rates_sql(g, t, total)
    if q:
        sql, cols = q
        row = conn.execute(sql).fetchone()
        for i, c in enumerate(cols):
            nonnull, dirty = row[2 * i], row[2 * i + 1]
            realised[c.name] = (dirty / nonnull) if nonnull else 0.0

    columns = []
    for c in t.columns:
        rate = realised.get(c.name, 1.0 if c.truth.data_type else 0.0)
        all_null = c.null_rate >= 1
        columns.append({
            "name": c.name,
            "sql_type": c.sql_type,
            "expected_type": c.truth.data_type if not all_null and rate > 0 else None,
            "expected_zone": expected_zone(c, rate, all_null),
            "true_rate": round(rate, 6),
            "nominal_rate": c.truth.rate,
            "null_rate": c.null_rate,
            "role": c.truth.role or None,
            "requires_baseline": c.truth.requires_baseline,
        })
    return {
        "schema": t.schema,
        "name": t.name,
        "shape": t.shape,
        "rows": total,
        "clustered_key": t.clustered or None,
        "partitioned_by": t.partition_by_month,
        "columns": columns,
    }
