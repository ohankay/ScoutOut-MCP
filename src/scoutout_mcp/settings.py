"""Configuration of the MCP server: one YAML file outside the workspace, the password only in the environment.

    connection:   server, user, auth, encrypt, trust_server_certificate (core ConnectionConfig fields)
    scan / llm:   core ScanConfig / LlmConfig fields (optional)
    mcp:          databases, exclude_databases, masking_database, masking_query, k, daily_measure_budget, mine_values
    data_dir:     store, reference sets, reports (default: next to the config file)

Config path: ``SCOUTOUT_CONFIG`` or ``%APPDATA%/ScoutOut-Mcp/config.yaml``. Password: ``SCOUTOUT_DB_PASSWORD``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

from scoutout.core.config import Config, ConfigError, load_config

DEFAULT_MASKING_QUERY = ("SELECT DatabaseName, SchemaName, TableName, ColumnName, DataMaskType, TermName "
                         "FROM Dtg.TableColumnDataMaskDetail")


@dataclass
class McpSettings:
    databases: list[str] = field(default_factory=list)  # empty = every online user database the login can open
    exclude_databases: list[str] = field(default_factory=list)
    masking_database: str = "BoaCatalog"
    masking_query: str = DEFAULT_MASKING_QUERY
    k: int = 20  # counts below k are shown as "<k" (k-anonymity of everything the agent sees)
    daily_measure_budget: int = 200  # rule measurements per day (limits probing with crafted rules)
    mine_values: int = 200  # real values per column shown to the local LLM
    measure_values: int = 1000  # values per column used to measure a rule


def default_config_path() -> Path:
    env = os.environ.get("SCOUTOUT_CONFIG")
    if env:
        return Path(env)
    base = Path(os.environ.get("APPDATA") or Path.home() / ".config")
    return base / "ScoutOut-Mcp" / "config.yaml"


def load(path: str | Path | None = None) -> tuple[Config, McpSettings]:
    p = (Path(path) if path else default_config_path()).resolve()
    if not p.is_file():
        raise ConfigError(f"Yapılandırma dosyası bulunamadı: {p}. Örnek: config.example.yaml")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg = load_config(p, require_database=False)
    cfg.connection.database = cfg.connection.database or "master"
    cfg.paths.data_dir = str(Path(data.get("data_dir") or p.parent / "data"))
    os.environ.setdefault("SCOUTOUT_DATA_DIR", cfg.paths.data_dir)
    mcp = McpSettings()
    known = {f.name for f in fields(mcp)}
    for key, value in (data.get("mcp") or {}).items():
        if key not in known:
            raise ConfigError(f"mcp: bilinmeyen ayar '{key}'")
        setattr(mcp, key, value)
    if mcp.k < 5:
        raise ConfigError("mcp.k en az 5 olmalı")
    return cfg, mcp
