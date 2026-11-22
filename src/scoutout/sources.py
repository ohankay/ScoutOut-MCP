"""Database sources: the databases ScoutOut scans regularly. Kept in the store and managed from the UI or CLI.

A source is a named connection plus its own scan schedule. Passwords never enter the store: they come from
``SCOUTOUT_DB_PASSWORD_<NAME>`` or a file under ``data_dir/secrets``.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from scoutout.core.config import Config, ConfigError
from scoutout.store import Store, now

NAME_RX = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


@dataclass
class Source:
    name: str
    server: str
    database: str
    auth: str = "sql"
    user: str = ""
    password_env: str = ""
    password_file: str = ""
    encrypt: bool = True
    trust: bool = False
    include_schemas: list[str] = field(default_factory=list)
    schedule: str = ""  # "", "daily HH:MM" or "weekly <1-7> HH:MM" (1 = Monday)
    enabled: bool = True
    prod_name: str = ""  # name of the database in production (as the masking list knows it), if the copy is named differently

    @property
    def names(self) -> set[str]:
        """Every name this database may be referred to by (lower case)."""
        return {x.lower() for x in (self.name, self.database, self.prod_name) if x}

    @property
    def label(self) -> str:
        return f"{self.server} / {self.database}"

    @property
    def has_password(self) -> bool:
        return bool(os.environ.get(self.password_env or env_name(self.name))
                    or (self.password_file and Path(self.password_file).is_file()))


def env_name(name: str) -> str:
    return "SCOUTOUT_DB_PASSWORD_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()


def _row(r) -> Source:
    return Source(r["name"], r["server"], r["database"], r["auth"], r["username"] or "", r["password_env"] or "",
                  r["password_file"] or "", bool(r["encrypt"]), bool(r["trust"]), json.loads(r["include_schemas"] or "[]"),
                  r["schedule"] or "", bool(r["enabled"]), r["prod_name"] or "")


def list_sources(store: Store) -> list[Source]:
    return [_row(r) for r in store.q("SELECT * FROM sources ORDER BY name")]


def get_source(store: Store, name: str) -> Source | None:
    r = store.one("SELECT * FROM sources WHERE name=?", name)
    return _row(r) if r else None


def validate_schedule(spec: str) -> str:
    spec = " ".join(spec.split())
    if not spec:
        return ""
    parts = spec.split(" ")
    try:
        if parts[0] == "daily" and len(parts) == 2:
            hh, mm = map(int, parts[1].split(":"))
        elif parts[0] == "weekly" and len(parts) == 3 and 1 <= int(parts[1]) <= 7:
            hh, mm = map(int, parts[2].split(":"))
        else:
            raise ValueError
        if not (0 <= hh < 24 and 0 <= mm < 60):
            raise ValueError
    except ValueError:
        raise ConfigError("Zamanlama biçimi: 'daily 02:00' veya 'weekly 1 02:00' (1 = Pazartesi)") from None
    return spec


def save_source(store: Store, src: Source, user: str, data_dir: Path, password: str | None = None,
                previous_name: str | None = None) -> Source:
    if not NAME_RX.match(src.name):
        raise ConfigError("Ad sadece harf, rakam, '-', '_' ve '.' içerebilir (en fazla 40).")
    if not src.server.strip() or not src.database.strip():
        raise ConfigError("Sunucu ve veritabanı zorunlu.")
    if src.auth not in ("sql", "windows"):
        raise ConfigError("Kimlik doğrulama 'sql' veya 'windows' olmalı.")
    src.schedule = validate_schedule(src.schedule)
    old = get_source(store, previous_name or src.name)
    if old:  # editing keeps where the password comes from unless a new one is typed
        src.password_env = src.password_env or old.password_env
        src.password_file = src.password_file or old.password_file
    src.password_env = src.password_env or env_name(src.name)
    if password:
        sec = data_dir / "secrets"
        sec.mkdir(parents=True, exist_ok=True)
        f = sec / f"db_password_{re.sub(r'[^A-Za-z0-9_.-]', '_', src.name)}"
        f.write_text(password, encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(f, 0o600)
        src.password_file = str(f)
    with store.tx() as c:
        if previous_name and previous_name != src.name:
            c.execute("DELETE FROM sources WHERE name=?", (previous_name,))
        c.execute(
            "INSERT INTO sources (name, server, database, auth, username, password_env, password_file, encrypt, trust, "
            "include_schemas, schedule, enabled, created_at, updated_by, prod_name) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET server=excluded.server, database=excluded.database, auth=excluded.auth, "
            "username=excluded.username, password_env=excluded.password_env, password_file=excluded.password_file, "
            "encrypt=excluded.encrypt, trust=excluded.trust, include_schemas=excluded.include_schemas, "
            "schedule=excluded.schedule, enabled=excluded.enabled, updated_by=excluded.updated_by, prod_name=excluded.prod_name",
            (src.name, src.server.strip(), src.database.strip(), src.auth, src.user.strip(), src.password_env,
             src.password_file, int(src.encrypt), int(src.trust), json.dumps(src.include_schemas), src.schedule,
             int(src.enabled), now(), user, src.prod_name.strip()))
    store.audit(user, "source.save", src.name, server=src.server, database=src.database, schedule=src.schedule,
                enabled=src.enabled, password_changed=bool(password))
    return src


def delete_source(store: Store, name: str, user: str) -> None:
    src = get_source(store, name)
    store.x("DELETE FROM sources WHERE name=?", name)
    shared = src and src.password_file and store.one("SELECT 1 FROM sources WHERE password_file=?", src.password_file)
    if src and src.password_file and not shared:  # databases added from the same server share one password file
        with contextlib.suppress(OSError):
            Path(src.password_file).unlink()
    store.audit(user, "source.delete", name)


def config_for(base: Config, src: Source) -> Config:
    """The base configuration with this source's connection (and schema filter) applied."""
    cfg = copy.deepcopy(base)
    c = cfg.connection
    c.server, c.database, c.auth, c.user = src.server, src.database, src.auth, src.user
    c.password_env, c.password_file = src.password_env or env_name(src.name), src.password_file
    c.encrypt, c.trust_server_certificate = src.encrypt, src.trust
    if src.include_schemas:
        cfg.scan.include_schemas = list(src.include_schemas)
    return cfg


def ensure_default_source(store: Store, cfg: Config) -> None:
    """Upgrade path: a single connection from scoutout.yaml / config.yaml becomes the first source."""
    if store.one("SELECT 1 FROM sources LIMIT 1") or not cfg.connection.database:
        return
    c = cfg.connection
    src = Source(c.database, c.server, c.database, c.auth, c.user, c.password_env, c.password_file, c.encrypt,
                 c.trust_server_certificate, list(cfg.scan.include_schemas))
    save_source(store, src, "sistem", cfg.paths.root)


def discover(store: Store, base: Config, template: str) -> list[dict]:
    """Databases on the server of ``template`` (using its connection) that are not registered yet."""
    from scoutout.connector.mssql import MsSqlAdapter

    src = get_source(store, template)
    if src is None:
        raise ConfigError(f"Böyle bir veritabanı kaydı yok: {template}")
    db = MsSqlAdapter(config_for(base, src).connection)
    try:
        found = db.list_databases()
    finally:
        db.close()
    known = {(s.server.lower(), s.database.lower()) for s in list_sources(store)}
    return [d for d in found if (src.server.lower(), d["name"].lower()) not in known]


def add_from_server(store: Store, base: Config, template: str, databases: list[str], user: str,
                    schedule: str | None = None) -> list[str]:
    """Register several databases of the template's server with the same connection settings (and password)."""
    src = get_source(store, template)
    if src is None:
        raise ConfigError(f"Böyle bir veritabanı kaydı yok: {template}")
    added = []
    for name in databases:
        new_name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:40]
        if get_source(store, new_name):
            continue
        new = Source(new_name, src.server, name, src.auth, src.user, src.password_env, src.password_file, src.encrypt,
                     src.trust, [], src.schedule if schedule is None else schedule, True)
        save_source(store, new, user, base.paths.root)
        added.append(new_name)
    return added


DISCOVERY = "discovery.new"  # {server: [database names not registered]} — refreshed hourly by the scheduler


def discovery_if_due(store: Store, base: Config, interval_min: int = 60) -> None:
    from datetime import UTC, datetime, timedelta

    last = store.get_setting(DISCOVERY) or {}
    if last.get("at") and datetime.fromisoformat(last["at"]) > datetime.now(UTC) - timedelta(minutes=interval_min):
        return
    found: dict[str, dict] = {}
    for s in list_sources(store):
        if s.server.lower() in {k.lower() for k in found}:
            continue
        try:
            found[s.server] = {"template": s.name, "databases": [d["name"] for d in discover(store, base, s.name)]}
        except Exception:  # noqa: BLE001 - an unreachable server must not stop the others
            continue
    store.x("INSERT INTO settings VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
            "updated_at=excluded.updated_at", DISCOVERY, json.dumps({"at": now(), "servers": found}), now(), "sistem")


def resolve(store: Store, base: Config, name: str | None) -> tuple[Source, Config]:
    """Pick a source by name, or the only one there is."""
    ensure_default_source(store, base)
    if name:
        src = get_source(store, name)
        if src is None:
            raise ConfigError(f"Böyle bir veritabanı kaynağı yok: {name}")
        return src, config_for(base, src)
    all_ = list_sources(store)
    if not all_:
        raise ConfigError("Henüz veritabanı tanımlı değil. Arayüzden (Ayarlar → Veritabanları) veya "
                          "'scoutout sources add' ile ekleyin.")
    if len(all_) > 1:
        raise ConfigError("Birden fazla veritabanı var; --source ile seçin: " + ", ".join(s.name for s in all_))
    return all_[0], config_for(base, all_[0])
