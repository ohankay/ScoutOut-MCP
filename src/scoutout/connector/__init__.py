"""Database access. ``open_database`` is the single entry point: it connects and enforces read-only policy."""

from __future__ import annotations

from scoutout.core.config import Config

from .base import Catalog, ColumnInfo, DatabaseAdapter, PartitionInfo, ReadOnlyReport, StatsInfo, TableInfo


class WriteAccessRefused(Exception):
    def __init__(self, report: ReadOnlyReport):
        self.report = report
        super().__init__("Bağlantının yazma yetkisi var: " + ", ".join(report.reasons))


def open_database(cfg: Config) -> tuple[DatabaseAdapter, ReadOnlyReport]:
    """Connect, check write access and apply ``security.on_write_permission``."""
    from .mssql import MsSqlAdapter

    adapter = MsSqlAdapter(cfg.connection)
    report = adapter.check_read_only()
    if not report.read_only and cfg.security.on_write_permission == "refuse":
        adapter.close()
        raise WriteAccessRefused(report)
    return adapter, report


__all__ = [
    "Catalog", "ColumnInfo", "DatabaseAdapter", "PartitionInfo", "ReadOnlyReport", "StatsInfo", "TableInfo",
    "WriteAccessRefused", "open_database",
]
