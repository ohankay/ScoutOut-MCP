"""Database-agnostic catalog model and adapter interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

LOB_TYPES = {"text", "ntext", "image", "xml"}
TEXT_TYPES = {"char", "varchar", "nchar", "nvarchar", "text", "ntext"}
NUMERIC_TYPES = {"tinyint", "smallint", "int", "bigint", "decimal", "numeric", "money", "smallmoney"}
DATE_TYPES = {"date", "datetime", "datetime2", "smalldatetime", "datetimeoffset"}


@dataclass
class ColumnInfo:
    name: str
    data_type: str
    max_length: int | None  # characters; None = not applicable, -1 = (max)
    precision: int
    scale: int
    nullable: bool

    @property
    def is_lob(self) -> bool:
        return self.data_type in LOB_TYPES or self.max_length == -1

    @property
    def is_text(self) -> bool:
        return self.data_type in TEXT_TYPES

    @property
    def type_label(self) -> str:
        if self.max_length is None:
            if self.data_type in ("decimal", "numeric"):
                return f"{self.data_type}({self.precision},{self.scale})"
            return self.data_type
        return f"{self.data_type}({'max' if self.max_length == -1 else self.max_length})"


@dataclass
class StatsInfo:
    name: str
    leading_column: str
    rows: int | None
    rows_sampled: int | None
    modification_counter: int | None
    last_updated: str | None


@dataclass
class PartitionInfo:
    function: str
    column: str
    rows_per_partition: list[int]  # index = partition_number - 1

    @property
    def count(self) -> int:
        return len(self.rows_per_partition)


@dataclass
class TableInfo:
    schema: str
    name: str
    row_count: int
    columns: list[ColumnInfo]
    primary_key: list[str] = field(default_factory=list)
    clustered_key: list[str] = field(default_factory=list)  # empty = heap
    partition: PartitionInfo | None = None
    stats: list[StatsInfo] = field(default_factory=list)
    last_user_update: str | None = None  # from sys.dm_db_index_usage_stats (needs VIEW SERVER STATE)

    @property
    def full_name(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def is_heap(self) -> bool:
        return not self.clustered_key

    def column(self, name: str) -> ColumnInfo:
        return next(c for c in self.columns if c.name == name)


@dataclass
class Catalog:
    server: str
    database: str
    server_version: str
    tables: list[TableInfo]
    warnings: list[str] = field(default_factory=list)
    server_start: str | None = None  # usage stats are only meaningful since this moment
    db_created: str | None = None  # sys.databases.create_date: the moment of the last RESTORE for a restored copy
    db_state: str = "ONLINE"

    def table(self, full_name: str) -> TableInfo:
        return next(t for t in self.tables if t.full_name == full_name)

    @property
    def column_count(self) -> int:
        return sum(len(t.columns) for t in self.tables)


@dataclass
class ReadOnlyReport:
    read_only: bool
    reasons: list[str] = field(default_factory=list)  # why it is NOT read-only
    warnings: list[str] = field(default_factory=list)  # e.g. missing optional permissions


class DatabaseAdapter(Protocol):
    def check_read_only(self) -> ReadOnlyReport: ...

    def discover_catalog(self) -> Catalog: ...

    def close(self) -> None: ...
