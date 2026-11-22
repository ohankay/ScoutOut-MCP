"""MS SQL Server adapter (pyodbc). Catalog discovery uses metadata/DMVs only — no table data is read."""

from __future__ import annotations

from collections import defaultdict

import pyodbc

from scoutout.core.config import ConnectionConfig

from .base import Catalog, ColumnInfo, PartitionInfo, ReadOnlyReport, StatsInfo, TableInfo

DRIVERS = ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server")
CHAR_BYTES = {"nchar": 2, "nvarchar": 2}
SIZED_TYPES = {"char", "varchar", "nchar", "nvarchar", "binary", "varbinary"}


def pick_driver(requested: str = "auto") -> str:
    if requested and requested != "auto":
        return requested
    installed = pyodbc.drivers()
    for name in DRIVERS:
        if name in installed:
            return name
    raise RuntimeError("'ODBC Driver 18/17 for SQL Server' kurulu değil")


def connection_string(c: ConnectionConfig, database: str | None = None) -> str:
    parts = [
        f"DRIVER={{{pick_driver(c.driver)}}}",
        f"SERVER={c.server}",
        f"DATABASE={database or c.database}",
        "ApplicationIntent=ReadOnly",
        f"Encrypt={'yes' if c.encrypt else 'no'}",
        f"TrustServerCertificate={'yes' if c.trust_server_certificate else 'no'}",
        "APP=ScoutOut",
    ]
    if c.auth == "windows":
        parts.append("Trusted_Connection=yes")
    else:
        parts += [f"UID={c.user}", "PWD={" + c.password.replace("}", "}}") + "}"]
    return ";".join(parts)


class MsSqlAdapter:
    def __init__(self, cfg: ConnectionConfig):
        self.cfg = cfg
        self.conn = pyodbc.connect(connection_string(cfg), autocommit=True, timeout=cfg.login_timeout_s)

    def close(self) -> None:
        self.conn.close()

    # -- generic access used by the sampler -----------------------------------------
    queries_executed = 0

    def set_query_timeout(self, seconds: int) -> None:
        self.conn.timeout = seconds

    def query(self, sql: str, *params) -> list:
        self.queries_executed += 1
        if self.before_query:
            self.before_query()
        return self.conn.execute(sql, *params).fetchall()

    before_query = None  # optional hook (rate limiting, maintenance window)

    def histogram(self, schema: str, table: str, stats_id: int, cast_to: str) -> list[tuple]:
        """(high_key, range_rows, eq_rows, distinct_range_rows) steps of a statistics object — metadata only."""
        return self.query(
            f"SELECT CAST(h.range_high_key AS {cast_to}), h.range_rows, h.equal_rows, h.distinct_range_rows "
            "FROM sys.dm_db_stats_histogram(OBJECT_ID(?), ?) h ORDER BY h.step_number",
            f"[{schema}].[{table}]", stats_id,
        )

    def estimated_plan(self, sql: str) -> str:
        """Estimated XML plan (needs SHOWPLAN permission). The statement is not executed."""
        self.conn.execute("SET SHOWPLAN_XML ON")
        try:
            return self.conn.execute(sql).fetchone()[0]
        finally:
            self.conn.execute("SET SHOWPLAN_XML OFF")

    def _rows(self, sql: str, *params):
        return self.conn.execute(sql, *params).fetchall()

    def _scalar(self, sql: str, *params):
        return self.conn.execute(sql, *params).fetchone()[0]

    # -- read-only check -----------------------------------------------------------
    def check_read_only(self) -> ReadOnlyReport:
        r = self.conn.execute(
            """SELECT CAST(DATABASEPROPERTYEX(DB_NAME(), 'Updateability') AS nvarchar(20)),
                      IS_SRVROLEMEMBER('sysadmin'), IS_ROLEMEMBER('db_owner'),
                      IS_ROLEMEMBER('db_datawriter'), IS_ROLEMEMBER('db_ddladmin'),
                      HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'INSERT'),
                      HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'UPDATE'),
                      HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'DELETE'),
                      HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'ALTER'),
                      HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CREATE TABLE'),
                      HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'VIEW DATABASE STATE')"""
        ).fetchone()
        updateability, sysadmin, owner, writer, ddl, ins, upd, dele, alt, create, view_state = r
        warnings = []
        if not view_state:
            warnings.append(
                "VIEW DATABASE STATE yetkisi yok: satır sayıları yaklaşık (sys.partitions) alınacak. "
                "Önerilen: GRANT VIEW DATABASE STATE TO <kullanıcı>"
            )
        if updateability == "READ_ONLY":
            return ReadOnlyReport(True, [], warnings)

        reasons = []
        for flag, text in (
            (sysadmin, "sysadmin rolü"),
            (owner, "db_owner rolü"),
            (writer, "db_datawriter rolü"),
            (ddl, "db_ddladmin rolü"),
            (ins, "INSERT yetkisi"),
            (upd, "UPDATE yetkisi"),
            (dele, "DELETE yetkisi"),
            (alt, "ALTER yetkisi"),
            (create, "CREATE TABLE yetkisi"),
        ):
            if flag == 1:
                reasons.append(text)
        if not reasons:
            objs = self._rows(
                """SELECT TOP 5 s.name + '.' + t.name FROM sys.tables t
                   JOIN sys.schemas s ON s.schema_id = t.schema_id
                   CROSS APPLY (SELECT QUOTENAME(s.name) + '.' + QUOTENAME(t.name) AS q) x
                   WHERE HAS_PERMS_BY_NAME(x.q, 'OBJECT', 'INSERT') = 1
                      OR HAS_PERMS_BY_NAME(x.q, 'OBJECT', 'UPDATE') = 1
                      OR HAS_PERMS_BY_NAME(x.q, 'OBJECT', 'DELETE') = 1"""
            )
            if objs:
                reasons.append("tablo düzeyinde yazma yetkisi: " + ", ".join(o[0] for o in objs))
        return ReadOnlyReport(not reasons, reasons, warnings)

    # -- catalog ---------------------------------------------------------------------
    def discover_catalog(self) -> Catalog:
        warnings: list[str] = []
        version = self._scalar("SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(50))")

        tables = {
            oid: TableInfo(schema, name, 0, [])
            for oid, schema, name in self._rows(
                """SELECT t.object_id, s.name, t.name FROM sys.tables t
                   JOIN sys.schemas s ON s.schema_id = t.schema_id
                   WHERE t.is_ms_shipped = 0 ORDER BY s.name, t.name"""
            )
        }

        part_rows: dict[int, list[int]] = defaultdict(list)
        try:
            rows = self._rows(
                """SELECT object_id, partition_number, SUM(row_count) FROM sys.dm_db_partition_stats
                   WHERE index_id IN (0, 1) GROUP BY object_id, partition_number ORDER BY object_id, partition_number"""
            )
        except pyodbc.Error:
            warnings.append("sys.dm_db_partition_stats okunamadı; satır sayıları sys.partitions'tan (yaklaşık).")
            rows = self._rows(
                """SELECT object_id, partition_number, rows FROM sys.partitions
                   WHERE index_id IN (0, 1) ORDER BY object_id, partition_number"""
            )
        for oid, _pn, cnt in rows:
            if oid in tables:
                part_rows[oid].append(int(cnt))
        for oid, t in tables.items():
            t.row_count = sum(part_rows.get(oid, []))

        for oid, name, type_name, max_len, prec, scale, nullable in self._rows(
            """SELECT c.object_id, c.name, ty.name, c.max_length, c.precision, c.scale, c.is_nullable
               FROM sys.columns c JOIN sys.types ty ON ty.user_type_id = c.user_type_id
               JOIN sys.tables t ON t.object_id = c.object_id
               WHERE t.is_ms_shipped = 0 ORDER BY c.object_id, c.column_id"""
        ):
            if oid not in tables:
                continue
            if type_name in SIZED_TYPES:
                length = -1 if max_len == -1 else max_len // CHAR_BYTES.get(type_name, 1)
            else:
                length = None
            tables[oid].columns.append(ColumnInfo(name, type_name, length, prec, scale, bool(nullable)))

        for oid, is_pk, is_clustered, col in self._rows(
            """SELECT i.object_id, i.is_primary_key, CAST(CASE WHEN i.index_id = 1 THEN 1 ELSE 0 END AS bit), c.name
               FROM sys.indexes i
               JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id AND ic.key_ordinal > 0
               JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
               WHERE i.index_id = 1 OR i.is_primary_key = 1
               ORDER BY i.object_id, i.index_id, ic.key_ordinal"""
        ):
            if oid not in tables:
                continue
            if is_clustered:
                tables[oid].clustered_key.append(col)
            if is_pk:
                tables[oid].primary_key.append(col)

        for oid, func, col in self._rows(
            """SELECT i.object_id, pf.name, c.name FROM sys.indexes i
               JOIN sys.partition_schemes ps ON ps.data_space_id = i.data_space_id
               JOIN sys.partition_functions pf ON pf.function_id = ps.function_id
               JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id AND ic.partition_ordinal = 1
               JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
               WHERE i.index_id IN (0, 1)"""
        ):
            if oid in tables:
                tables[oid].partition = PartitionInfo(func, col, part_rows.get(oid, []))

        for oid, name, lead, rows_, sampled, mods, updated in self._rows(
            """SELECT s.object_id, s.name, c.name, sp.rows, sp.rows_sampled, sp.modification_counter,
                      CONVERT(varchar(19), sp.last_updated, 126)
               FROM sys.stats s
               JOIN sys.tables t ON t.object_id = s.object_id AND t.is_ms_shipped = 0
               JOIN sys.stats_columns sc ON sc.object_id = s.object_id AND sc.stats_id = s.stats_id AND sc.stats_column_id = 1
               JOIN sys.columns c ON c.object_id = sc.object_id AND c.column_id = sc.column_id
               OUTER APPLY sys.dm_db_stats_properties(s.object_id, s.stats_id) sp
               ORDER BY s.object_id, s.stats_id"""
        ):
            if oid in tables:
                tables[oid].stats.append(StatsInfo(name, lead, rows_, sampled, mods, updated))

        server_start = None
        try:
            server_start = self._scalar("SELECT CONVERT(varchar(23), sqlserver_start_time, 126) FROM sys.dm_os_sys_info")
            for oid, last in self._rows(
                """SELECT object_id, CONVERT(varchar(23), MAX(last_user_update), 126) FROM sys.dm_db_index_usage_stats
                   WHERE database_id = DB_ID() GROUP BY object_id"""
            ):
                if oid in tables:
                    tables[oid].last_user_update = last
        except pyodbc.Error:
            warnings.append("VIEW SERVER STATE yetkisi yok: artımlı taramada sadece istatistik sayaçları kullanılabilir; "
                            "değişiklik tespit edilemeyen tablolar yeniden taranır.")
        created, state = self.conn.execute(
            "SELECT CONVERT(varchar(23), create_date, 126), state_desc FROM sys.databases WHERE name = DB_NAME()").fetchone()
        return Catalog(self.cfg.server, self.cfg.database, version, list(tables.values()), warnings, server_start, created, state)

    def list_databases(self) -> list[dict]:
        """User databases on this server that the login can open (for adding several databases at once)."""
        return [{"name": n, "state": s, "created": c} for n, s, c in self._rows(
            """SELECT name, state_desc, CONVERT(varchar(19), create_date, 126) FROM sys.databases
               WHERE database_id > 4 AND name NOT IN ('distribution', 'SSISDB', 'ReportServer', 'ReportServerTempDB')
                 AND HAS_DBACCESS(name) = 1 ORDER BY name""")]
