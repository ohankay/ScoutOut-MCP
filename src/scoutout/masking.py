"""The current masking list (which columns the masking process masks today) and how it compares with ScoutOut.

The list lives in the main system and changes from time to time, so ScoutOut reads it from there regularly: a
read-only SELECT on one of the registered databases. Every change is stored as a new version with what was added and
removed; if reading fails the last good list stays in use and the UI says how old it is. A manual upload is the fallback.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

from scoutout.core.config import Config, ConfigError
from scoutout.store import Store, now

SETTING = "masking.source"  # {"source": name, "query": sql, "interval_min": 60}
LAST_CHECK = "masking.last_check"  # {"at", "ok", "error", "version"}
MAX_ROWS = 500_000
#: (database or None, schema, table, column, masking algorithm or None, business term or None)
Entry = tuple[str | None, str, str, str, str | None, str | None]

_FORBIDDEN = re.compile(r"\b(insert|update|delete|merge|exec|execute|drop|alter|create|truncate|grant|revoke|into|"
                        r"openrowset|opendatasource|xp_\w+|sp_\w+)\b", re.I)


# --- reading -----------------------------------------------------------------------------------------

def _norm_header(h: str) -> str:
    """'DatabaseName', 'database_name', 'Veritabanı Adı' -> 'databasename' / 'veritabaniadi' (letters and digits only)."""
    h = (h or "").strip().replace("İ", "i").replace("I", "ı").lower().translate(str.maketrans("şıöüçğ", "sioucg"))
    return re.sub(r"[^a-z0-9]", "", h)


HEADERS = {"source": ("veritabani", "veritabaniadi", "database", "databasename", "db", "dbname", "kaynak", "source"),
           "schema": ("sema", "semaadi", "schema", "schemaname", "owner"),
           "table": ("tablo", "tabloadi", "table", "tablename"),
           "column": ("kolon", "kolonadi", "column", "columnname", "alan", "field"),
           "algorithm": ("datamasktype", "masktype", "maskingtype", "maskingalgorithm", "algoritma", "algorithm",
                         "maskelemetipi", "maskelemeturu", "maskelemealgoritmasi", "yontem", "method"),
           "term": ("termname", "term", "terim", "isterimi", "businessterm")}


def entries_from_rows(header: list[str] | None, rows: list[list[str]]) -> list[Entry]:
    """Rows with named columns (database, schema, table, column) or single 'schema.table.column' values."""
    rows = [[("" if v is None else str(v)).strip() for v in r] for r in rows]
    rows = [r for r in rows if any(r)]
    out: list[Entry] = []
    if header:
        h = [_norm_header(x) for x in header]
        idx = {k: next((i for i, x in enumerate(h) if x in v), None) for k, v in HEADERS.items()}
        if idx["table"] is not None and idx["column"] is not None:
            def get(r, k):
                return r[idx[k]] if idx[k] is not None and idx[k] < len(r) else ""

            for r in rows:
                if get(r, "table") and get(r, "column"):
                    out.append((get(r, "source") or None, get(r, "schema") or "dbo", get(r, "table"), get(r, "column"),
                                get(r, "algorithm") or None, get(r, "term") or None))
            return out
        rows = [header] + rows if "." in (header[0] or "") else rows
    for r in rows:
        parts = [p.strip("[] ") for p in r[0].split(".")]
        if len(parts) == 3:
            out.append((None, *parts, None, None))
        elif len(parts) == 4:
            out.append((parts[0], parts[1], parts[2], parts[3], None, None))
    return out


def validate_query(sql: str) -> str:
    q = sql.strip().rstrip(";").strip()
    if not re.match(r"^(select|with)\b", q, re.I):
        raise ConfigError("Sorgu SELECT (veya WITH) ile başlamalı.")
    if ";" in q:
        raise ConfigError("Sorgu tek bir SELECT olmalı.")
    m = _FORBIDDEN.search(q)
    if m:
        raise ConfigError(f"Sorguda izin verilmeyen ifade: {m.group(1)}")
    return q


def read_from_database(store: Store, base: Config, source_name: str, query: str) -> list[Entry]:
    """Run the read-only query on a registered database (write-permission policy applies as for scans)."""
    from scoutout.connector import open_database
    from scoutout.sources import config_for, get_source

    src = get_source(store, source_name)
    if src is None:
        raise ConfigError(f"Maskeleme listesi için seçilen veritabanı yok: {source_name}")
    db, _ = open_database(config_for(base, src))
    try:
        db.set_query_timeout(base.scan.query_timeout_s)
        cur = db.conn.cursor()
        cur.execute(validate_query(query))
        header = [d[0] for d in cur.description]
        rows = cur.fetchmany(MAX_ROWS + 1)
        if len(rows) > MAX_ROWS:
            raise ConfigError(f"Sorgu {MAX_ROWS:,} satırdan fazla döndürdü; filtreleyin.")
    finally:
        db.close()
    return entries_from_rows(header, [list(r) for r in rows])


# --- versions --------------------------------------------------------------------------------------

def _key(e) -> tuple:
    return ((e[0] or "").lower(), e[1].lower(), e[2].lower(), e[3].lower())


def _label(e) -> str:
    return ".".join(x for x in e[:4] if x)


def _full(e) -> tuple:
    """Entries stored before algorithms were read have 4 fields."""
    e = tuple(e)
    return e + (None,) * (6 - len(e))


def save_version(store: Store, entries: list[Entry], origin: str, user: str) -> dict:
    """Store the list if it differs from the latest version. Returns {changed, version, added, removed, count}."""
    uniq = sorted({_key(e): _full(e) for e in entries}.values(), key=_key)
    digest = hashlib.sha256(json.dumps([[*_key(e), e[4] or "", e[5] or ""] for e in uniq]).encode()).hexdigest()
    last = store.one("SELECT * FROM masking_lists ORDER BY version DESC LIMIT 1")
    if last and last["content_hash"] == digest:
        return {"changed": False, "version": last["version"], "added": [], "removed": [], "algo_changed": [],
                "count": last["entry_count"]}
    before = {_key(e): _full(e) for e in json.loads(last["entries_json"])} if last else {}
    after = {_key(e): e for e in uniq}
    added = [_label(after[k]) for k in sorted(set(after) - set(before))]
    removed = [_label(before[k]) for k in sorted(set(before) - set(after))]
    algo_changed = [f"{_label(after[k])}: {before[k][4] or '-'} → {after[k][4] or '-'}"
                    for k in sorted(set(after) & set(before)) if (before[k][4] or "") != (after[k][4] or "")]
    version = store.x("INSERT INTO masking_lists (fetched_at, origin, entries_json, entry_count, content_hash, added_json, "
                      "removed_json, created_by) VALUES (?,?,?,?,?,?,?,?)", now(), origin, json.dumps(uniq, ensure_ascii=False),
                      len(uniq), digest, json.dumps(added, ensure_ascii=False), json.dumps(removed, ensure_ascii=False), user)
    store.audit(user, "masking_list.update", f"v{version}", origin=origin, count=len(uniq), added=len(added), removed=len(removed),
                algorithm_changed=len(algo_changed))
    return {"changed": True, "version": version, "added": added, "removed": removed, "algo_changed": algo_changed,
            "count": len(uniq)}


def current(store: Store):
    """(version row, entries) of the list in use, or (None, None)."""
    r = store.one("SELECT * FROM masking_lists ORDER BY version DESC LIMIT 1")
    if r is None:
        return None, None
    return r, [_full(e) for e in json.loads(r["entries_json"])]


def sync(store: Store, base: Config, user: str = "sistem") -> dict:
    """Read the list from its database now. Never raises: the outcome is recorded and returned."""
    conf = store.get_setting(SETTING)
    if not conf or not conf.get("source") or not conf.get("query"):
        return {"ok": False, "error": "Maskeleme listesi kaynağı ayarlanmamış."}
    try:
        entries = read_from_database(store, base, conf["source"], conf["query"])
        res = save_version(store, entries, f"sql:{conf['source']}", user) | {"ok": True}
        status = {"at": now(), "ok": True, "error": "", "version": res["version"], "count": res["count"]}
    except Exception as e:  # noqa: BLE001 - keep the last good list, report the failure
        from scoutout.core.log import scrub

        res = {"ok": False, "error": scrub(f"{type(e).__name__}: {e}")[:300]}
        prev = store.get_setting(LAST_CHECK) or {}
        status = prev | {"at": now(), "ok": False, "error": res["error"]}
    store.x("INSERT INTO settings VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by", LAST_CHECK, json.dumps(status), now(), user)
    return res


def sync_if_due(store: Store, base: Config) -> dict | None:
    conf = store.get_setting(SETTING)
    if not conf or not conf.get("source"):
        return None
    last = store.get_setting(LAST_CHECK) or {}
    interval = timedelta(minutes=int(conf.get("interval_min") or 60))
    if last.get("at") and datetime.fromisoformat(last["at"]) > datetime.now(UTC) - interval:
        return None
    return sync(store, base)


def staleness(store: Store) -> str | None:
    """A warning when the list in use may be outdated, otherwise None."""
    row, _ = current(store)
    conf = store.get_setting(SETTING)
    if row is None:
        return None
    if conf and conf.get("source"):
        last = store.get_setting(LAST_CHECK) or {}
        if not last.get("ok"):
            when = last.get("at", "")[:16].replace("T", " ")
            return (f"Maskeleme listesi kaynağından okunamadı ({when} UTC): {last.get('error', '')}. "
                    f"Son başarılı liste (v{row['version']}) kullanılıyor.")
        return None
    age = datetime.now(UTC) - datetime.fromisoformat(row["fetched_at"])
    if age > timedelta(days=30):
        return f"Maskeleme listesi {age.days} gün önce elle yüklendi; güncel olmayabilir."
    return None


# --- comparison ---------------------------------------------------------------------------------------

SENSITIVE = ("SENSITIVE", "SPARSE")
STATUS_TR = {"missing": "Eksik maskeleme", "unnecessary": "Gereksiz maskeleme", "correct": "Doğru maskeleniyor"}


class Index:
    """Lookup of list entries. An entry without a database matches that schema.table.column in every database; an entry
    with a database matches the ScoutOut source name or the real database name."""

    def __init__(self, entries: list[Entry], sources: dict[str, str]):
        self.full: dict[tuple, dict] = {}
        self.short: dict[tuple, dict] = {}
        for e in entries:
            e = _full(e)
            k = _key(e)
            info = {"algorithm": e[4], "term": e[5]}
            if e[0]:
                self.full[k] = info
            else:
                self.short[k[1:]] = info
        # source name -> every name it may appear under in the list (ScoutOut name, database name, production name)
        self.aliases = {name: ({names.lower()} if isinstance(names, str) else {x.lower() for x in names}) | {name.lower()}
                        for name, names in sources.items()}

    def lookup(self, source: str, schema: str, table: str, column: str) -> dict | None:
        """{'algorithm', 'term'} of the list entry for this column, or None when the column is not in the list."""
        t = (schema.lower(), table.lower(), column.lower())
        for a in self.aliases.get(source, {source.lower()}):
            if (a, *t) in self.full:
                return self.full[(a, *t)]
        return self.short.get(t)

    def listed(self, source: str, schema: str, table: str, column: str) -> bool:
        return self.lookup(source, schema, table, column) is not None


def status(listed: bool, zone: str | None) -> str | None:
    if zone is None:
        return None
    sensitive = zone in SENSITIVE
    if listed:
        return "correct" if sensitive else "unnecessary"
    return "missing" if sensitive else None


#: words in an algorithm code -> data types it is meant for (codes like S_DET_NAME, S_RND_PHONE, N_DET_NUMBER)
ALGO_KEYWORDS = [
    ("NAME", {"PERSON_NAME", "GIVEN_NAME", "SURNAME"}),
    ("PHONE", {"PHONE"}), ("GSM", {"PHONE"}), ("TEL", {"PHONE"}),
    ("MAIL", {"EMAIL"}),
    ("ADDR", {"ADDRESS"}), ("ADRES", {"ADDRESS"}),
    ("IBAN", {"IBAN"}),
    ("CARD", {"PAN", "MASKED_PAN"}), ("PAN", {"PAN", "MASKED_PAN"}), ("KART", {"PAN", "MASKED_PAN"}),
    ("TCKN", {"TCKN", "YKN"}), ("KIMLIK", {"TCKN", "YKN"}), ("IDENTITY", {"TCKN", "YKN"}),
    ("VKN", {"VKN"}), ("TAX", {"VKN"}),
    ("DATE", {"BIRTH_DATE"}), ("TARIH", {"BIRTH_DATE"}),
    ("NUMBER", {"TCKN", "YKN", "VKN", "PAN", "IBAN", "PHONE", "CUSTOMER_NO", "ACCOUNT_NO", "INTERNAL_ID"}),
    ("NUM", {"TCKN", "YKN", "VKN", "PAN", "IBAN", "PHONE", "CUSTOMER_NO", "ACCOUNT_NO", "INTERNAL_ID"}),
]
FIT_TR = {"mismatch": "Algoritma veriye uymuyor olabilir", "partial": "Daha hafif maskeleme yeterli olabilir"}


def algo_fit(algorithm: str | None, zone: str | None, data_type: str | None) -> tuple[str, str] | None:
    """Does the current masking algorithm suit what ScoutOut found? None = nothing to say (or unknown algorithm)."""
    if not algorithm or zone not in SENSITIVE:
        return None
    from scoutout.report import TYPE_TR

    code = algorithm.upper()
    if "NULL" in code:
        if zone == "SPARSE":
            return ("partial", f"Kolon tamamen boşaltılıyor ({algorithm}), ama hassas veri değerlerin küçük bir kısmında. "
                               "Sadece bulunan parçaları değiştiren metin içi maskeleme yeterli olabilir.")
        return None
    expected: set[str] = set()
    for word, types in ALGO_KEYWORDS:
        if word in code:
            expected |= types
    if expected and data_type and data_type not in expected:
        return ("mismatch", f"Algoritma {algorithm}, ama kolonda {TYPE_TR.get(data_type, data_type)} bulundu; "
                            "algoritma bu veri tipine uygun olmayabilir.")
    return None


def index_for(store: Store, entries: list[Entry] | None = None) -> Index | None:
    from scoutout.sources import list_sources

    if entries is None:
        _, entries = current(store)
        if entries is None:
            return None
    return Index(entries, {s.name: s.names for s in list_sources(store)})


def column_statuses(store: Store) -> dict[int, dict]:
    """Masking status of every latest decision: final zone if reviewed, else the system's (marked pending)."""
    idx = index_for(store)
    if idx is None:
        return {}
    out = {}
    for d in store.latest_decisions():
        reviewed = d["review_status"] in ("APPROVED", "OVERRIDDEN")
        if d["review_status"] == "REJECTED":
            continue
        zone = d["final_zone"] if reviewed else d["zone"]
        dtype = d["final_type"] if reviewed else d["sensitive_type"]
        info = idx.lookup(d["source"] or "", d["schema_name"], d["table_name"], d["column_name"])
        st = status(info is not None, zone)
        if st:
            fit = algo_fit(info["algorithm"], zone, dtype) if info else None
            out[d["id"]] = {"status": st, "listed": info is not None, "reviewed": reviewed, "source": d["source"],
                            "algorithm": info["algorithm"] if info else None, "term": info["term"] if info else None,
                            "fit": fit[0] if fit else None, "fit_text": fit[1] if fit else ""}
    return out
