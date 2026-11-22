"""Configuration: one small YAML file (``scoutout.yaml``) plus environment overrides.

Secrets never live in the YAML: the database password is read from the environment variable
named by ``connection.password_env`` (default ``SCOUTOUT_DB_PASSWORD``).
Any connection field can be overridden with ``SCOUTOUT_DB_<FIELD>`` (e.g. ``SCOUTOUT_DB_SERVER``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

DEFAULT_CONFIG_FILE = "scoutout.yaml"


def tz():
    from zoneinfo import ZoneInfo

    return ZoneInfo(os.environ.get("SCOUTOUT_TZ", "Europe/Istanbul"))


class ConfigError(Exception):
    pass


@dataclass
class ConnectionConfig:
    server: str = "localhost"
    database: str = ""
    auth: str = "sql"  # "sql" or "windows"
    user: str = ""
    password_env: str = "SCOUTOUT_DB_PASSWORD"
    password_file: str = ""  # written by the setup wizard (data_dir/secrets); the env variable wins
    driver: str = "auto"
    encrypt: bool = True
    trust_server_certificate: bool = False
    login_timeout_s: int = 15

    @property
    def password(self) -> str:
        value = os.environ.get(self.password_env, "")
        if not value and self.password_file and Path(self.password_file).is_file():
            value = Path(self.password_file).read_text(encoding="utf-8").strip()
        return value


@dataclass
class SecurityConfig:
    on_write_permission: str = "refuse"  # "refuse" or "warn"


@dataclass
class ScanConfig:
    seed: int = 42
    looks: list[int] = field(default_factory=lambda: [400, 1000, 2500, 5000])  # sequential sample sizes
    small_table_rows: int = 100_000  # below this, simple reads/hash ordering are allowed
    rows_per_seek: int = 10
    seeks_per_query: int = 50
    lob_chars: int = 4000
    read_uncommitted: bool = False
    maxdop: int = 1
    query_timeout_s: int = 60
    concurrency: int = 4
    queries_per_second: float = 0.0  # 0 = unlimited
    maintenance_window: str = ""  # e.g. "22:00-06:00"; empty = always
    recent_months: int = 6
    recent_share: float = 0.2
    max_tablesample_percent: float = 1.0
    incremental_threshold: float = 0.05  # modification_counter / rows below this -> reuse previous result
    max_carry_days: int = 7  # every table is really re-scanned at least this often, however unchanged it looks
    include_schemas: list[str] = field(default_factory=list)  # empty = all
    exclude_tables: list[str] = field(default_factory=list)  # "schema.table"

    @property
    def n_max(self) -> int:
        return self.looks[-1]


@dataclass
class LlmConfig:
    endpoint: str = ""  # OpenAI-compatible base URL, e.g. http://localhost:11434/v1 (Ollama); empty = no LLM
    model: str = "qwen2.5:7b-instruct"
    allowed_hosts: list[str] = field(default_factory=lambda: ["localhost", "127.0.0.1", "::1", "ollama"])
    timeout_s: int = 120


@dataclass
class PathsConfig:
    data_dir: str = "var"  # store, reports, logs, reference sets, secrets

    @property
    def root(self) -> Path:
        return Path(os.environ.get("SCOUTOUT_DATA_DIR", self.data_dir))

    @property
    def store(self) -> Path:
        return self.root / "scoutout.db"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def logs(self) -> Path:
        return self.root / "logs"


@dataclass
class Config:
    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    scan: ScanConfig = field(default_factory=ScanConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)


def _apply(section, values: dict, where: str) -> None:
    known = {f.name: f for f in fields(section)}
    for key, value in (values or {}).items():
        if key not in known:
            raise ConfigError(f"{where}: bilinmeyen ayar '{key}'")
        setattr(section, key, value)


def _coerce(current, raw: str):
    if isinstance(current, bool):
        return raw.strip().lower() in ("1", "true", "yes", "evet")
    if isinstance(current, int):
        return int(raw)
    return raw


def load_config(path: str | Path | None = None, require_database: bool = True, missing_ok: bool = False) -> Config:
    cfg = Config()
    p = Path(path or os.environ.get("SCOUTOUT_CONFIG", DEFAULT_CONFIG_FILE))
    if not path and not p.is_file():
        # the web setup wizard saves to <data_dir>/config.yaml: CLI and UI share one configuration
        wizard = Path(os.environ.get("SCOUTOUT_DATA_DIR", "var")) / "config.yaml"
        if wizard.is_file():
            p = wizard
    if p.is_file():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        _apply(cfg.connection, data.get("connection"), "connection")
        _apply(cfg.security, data.get("security"), "security")
        _apply(cfg.scan, data.get("scan"), "scan")
        _apply(cfg.paths, data.get("paths"), "paths")
        _apply(cfg.llm, data.get("llm"), "llm")
    elif path and not missing_ok:
        raise ConfigError(f"Yapılandırma dosyası bulunamadı: {p}")

    for f in fields(cfg.connection):
        raw = os.environ.get(f"SCOUTOUT_DB_{f.name.upper()}")
        if raw is not None and f.name != "password_env":
            setattr(cfg.connection, f.name, _coerce(getattr(cfg.connection, f.name), raw))

    c = cfg.connection
    if c.auth not in ("sql", "windows"):
        raise ConfigError("connection.auth 'sql' veya 'windows' olmalı")
    if cfg.security.on_write_permission not in ("refuse", "warn"):
        raise ConfigError("security.on_write_permission 'refuse' veya 'warn' olmalı")
    if not c.database and require_database:
        raise ConfigError("connection.database boş olamaz")
    return cfg


def save_config(cfg: Config, path: str | Path) -> None:
    """Write the config back as YAML (used by the setup wizard). Never contains a password."""
    from dataclasses import asdict

    data = asdict(cfg)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
