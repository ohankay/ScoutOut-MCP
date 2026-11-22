"""Regular use: store migration, sources, scheduler, test-data masking report."""

import io
import json
import sqlite3
from zoneinfo import ZoneInfo

import pytest

from scoutout.core.config import Config, ConfigError
from scoutout.report import compare, masking_report, masking_rows, parse_masking_list
from scoutout.sources import Source, config_for, get_source, list_sources, resolve, save_source
from scoutout.store import Store

TZ = ZoneInfo("Europe/Istanbul")


# --- store migration -------------------------------------------------------------------------------

def test_v1_store_is_migrated(tmp_path):
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES ('schema_version', '1');
        CREATE TABLE runs (id TEXT PRIMARY KEY, database TEXT, started_at TEXT, finished_at TEXT, status TEXT, seed INTEGER,
          pack_version TEXT, config_json TEXT, tables_total INTEGER DEFAULT 0, tables_done INTEGER DEFAULT 0, error TEXT, created_by TEXT);
        CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, schema_name TEXT, table_name TEXT, column_name TEXT,
          sql_type TEXT, zone TEXT, sensitive_type TEXT, conflict INTEGER, p_corrected REAL, record_json TEXT,
          carried_over INTEGER DEFAULT 0, created_at TEXT, UNIQUE (run_id, schema_name, table_name, column_name));
        CREATE TABLE baseline_columns (id INTEGER PRIMARY KEY AUTOINCREMENT, schema_name TEXT, table_name TEXT,
          column_name TEXT, data_type TEXT, added_by TEXT, added_at TEXT, UNIQUE (schema_name, table_name, column_name));
        CREATE TABLE reports (version INTEGER PRIMARY KEY, created_at TEXT, created_by TEXT, files_json TEXT,
          summary_json TEXT, rows_json TEXT, content_hash TEXT);
        INSERT INTO runs (id, database, status) VALUES ('r1', 'BankDB', 'done');
        INSERT INTO decisions (run_id, schema_name, table_name, column_name, zone) VALUES ('r1', 'dbo', 'T', 'C', 'CLEAN');
        INSERT INTO baseline_columns (schema_name, table_name, column_name, data_type) VALUES ('dbo', 'T', 'C', 'TCKN');
    """)
    c.close()
    s = Store(db)
    assert s.one("SELECT source FROM decisions")["source"] == "BankDB"
    assert s.one("SELECT source FROM baseline_columns")["source"] == "BankDB"
    assert s.one("SELECT value FROM meta WHERE key='schema_version'")["value"] == "2"
    Store(db)  # idempotent


# --- sources ---------------------------------------------------------------------------------------

def test_sources_crud_and_passwords_stay_out_of_the_store(tmp_path):
    store = Store(tmp_path / "s.db")
    save_source(store, Source("CRM", "db1,1433", "Crm", user="r", schedule="daily 02:00"), "ali", tmp_path, password="Gizli-123")
    src = get_source(store, "CRM")
    assert src.password_env == "SCOUTOUT_DB_PASSWORD_CRM" and src.password_file.endswith("db_password_CRM")
    assert "Gizli-123" not in json.dumps([dict(r) for r in store.q("SELECT * FROM sources")])
    cfg = config_for(Config(), src)
    assert cfg.connection.database == "Crm" and cfg.connection.password == "Gizli-123"
    save_source(store, Source("CRM2", "db1", "Crm"), "ali", tmp_path, previous_name="CRM")  # rename keeps password file
    assert [s.name for s in list_sources(store)] == ["CRM2"] and get_source(store, "CRM2").password_file
    with pytest.raises(ConfigError):
        save_source(store, Source("kötü ad!", "x", "y"), "ali", tmp_path)


def test_editing_keeps_the_password_source(tmp_path, monkeypatch):
    """A source migrated from scoutout.yaml reads SCOUTOUT_DB_PASSWORD; editing it in the UI must keep that."""
    monkeypatch.setenv("SCOUTOUT_DB_PASSWORD", "env-parola")
    store = Store(tmp_path / "s.db")
    save_source(store, Source("Eski", "s", "d", password_env="SCOUTOUT_DB_PASSWORD"), "sistem", tmp_path)
    save_source(store, Source("Eski", "s", "d", schedule="daily 01:00"), "ali", tmp_path, previous_name="Eski")
    assert config_for(Config(), get_source(store, "Eski")).connection.password == "env-parola"


def test_resolve_needs_a_choice_with_several_sources(tmp_path):
    store = Store(tmp_path / "s.db")
    with pytest.raises(ConfigError, match="tanımlı değil"):
        resolve(store, Config(), None)
    save_source(store, Source("A", "s", "a"), "u", tmp_path)
    assert resolve(store, Config(), None)[0].name == "A"
    save_source(store, Source("B", "s", "b"), "u", tmp_path)
    with pytest.raises(ConfigError, match="--source"):
        resolve(store, Config(), None)
    assert resolve(store, Config(), "B")[1].connection.database == "b"


def make_store(tmp_path):
    store = Store(tmp_path / "s.db")
    cols = [  # (source, schema, table, column, zone, type, free_text, rows)
        ("CRM", "dbo", "Musteri", "TCKN", "SENSITIVE", "TCKN", False, 1_000_000),
        ("CRM", "dbo", "Musteri", "Aciklama", "SPARSE", "PHONE", True, 1_000_000),
        ("CRM", "dbo", "Musteri", "Segment", "CLEAN", None, False, 1_000_000),
        ("CRM", "dbo", "Islem", "RefNo", "CLEAN", None, False, 50_000_000),
        ("KART", "dbo", "Kart", "KartNo", "SENSITIVE", "PAN", False, 2_000_000),
        ("KART", "dbo", "Kart", "Eski", "EMPTY", None, False, 2_000_000),
    ]
    for i, (src, sc, tb, co, zone, typ, free, rows) in enumerate(cols, start=1):
        rec = {"p_corrected": 0.9, "ci_low": 0.8, "ci_high": 0.95, "n": 400, "row_count_estimate": rows, "free_text": free}
        store.x("INSERT INTO decisions (run_id, source, schema_name, table_name, column_name, sql_type, zone, sensitive_type, "
                "conflict, p_corrected, record_json, created_at) VALUES ('r',?,?,?,?, 'varchar', ?, ?, 0, 0.9, ?, 'x')",
                src, sc, tb, co, zone, typ, json.dumps(rec))
        store.review(i, "APPROVED", zone, typ, "kontrol", "ali")
    store.x("INSERT INTO decisions (run_id, source, schema_name, table_name, column_name, zone, record_json) "
            "VALUES ('r','CRM','dbo','Musteri','Bekleyen','CLEAN','{}')")  # not reviewed -> not in the report
    return store


def test_masking_plan_methods(tmp_path):
    rows = {r["column"]: r for r in masking_rows(make_store(tmp_path))}
    assert rows["TCKN"]["mask"] and "kontrol haneleri" in rows["TCKN"]["method"].lower()
    assert rows["Aciklama"]["mask"] and "Metin içi" in rows["Aciklama"]["method"]
    assert rows["KartNo"]["method"].startswith("BIN")
    assert not rows["Segment"]["mask"] and not rows["Eski"]["mask"] and "Bekleyen" not in rows


def test_masking_comparison(tmp_path):
    rows = masking_rows(make_store(tmp_path))
    current = [(None, "dbo", "Musteri", "TCKN"), (None, "dbo", "Musteri", "Segment"), ("CRM", "dbo", "Islem", "RefNo"),
               (None, "dbo", "Kart", "Eski"), (None, "dbo", "Yok", "Kolon")]
    c = compare(rows, current)
    assert {r["column"] for r in c["unnecessary"]} == {"Segment", "RefNo", "Eski"}
    assert {r["column"] for r in c["missing"]} == {"Aciklama", "KartNo"}
    assert [r["column"] for r in c["correct"]] == ["TCKN"]
    assert c["not_reviewed"] == ["dbo.Yok.Kolon"]


def test_masking_report_files_and_wasted_rows(tmp_path):
    store = make_store(tmp_path)
    res = masking_report(store, tmp_path / "out", "ali", [(None, "dbo", "Islem", "RefNo"), (None, "dbo", "Musteri", "TCKN")])
    s = res["summary"]
    assert (s["mask"], s["no_mask"], s["pending_review"]) == (3, 3, 1)
    assert s["unnecessary"] == 1 and s["unnecessary_rows"] == 50_000_000 and s["missing"] == 2
    from openpyxl import load_workbook

    wb = load_workbook(res["files"]["xlsx"])
    assert {"Özet", "Maskeleme planı", "Gereksiz maskeleme", "Eksik maskeleme"} <= set(wb.sheetnames)
    assert store.one("SELECT kind FROM reports WHERE version=?", res["version"])["kind"] == "masking"
    plain = masking_report(store, tmp_path / "out", "ali")  # without a list: plan only
    assert "unnecessary" not in plain["summary"] and plain["version"] == res["version"] + 1


def test_parse_masking_lists():
    csv_header = "Veritabanı;Şema;Tablo;Kolon\nCRM;dbo;Musteri;TCKN\n;dbo;Kart;KartNo\n".encode()
    four = lambda es: [e[:4] for e in es]  # noqa: E731
    assert four(parse_masking_list(csv_header, "x.csv")) == [("CRM", "dbo", "Musteri", "TCKN"), (None, "dbo", "Kart", "KartNo")]
    dotted = b"dbo.Musteri.TCKN\nCRM.dbo.Islem.RefNo\n[dbo].[Kart].[KartNo]\n"
    assert four(parse_masking_list(dotted, "x.txt")) == [(None, "dbo", "Musteri", "TCKN"), ("CRM", "dbo", "Islem", "RefNo"),
                                                   (None, "dbo", "Kart", "KartNo")]
    english = b"table,column\nMusteri,TCKN\n"
    assert four(parse_masking_list(english, "x.csv")) == [(None, "dbo", "Musteri", "TCKN")]
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(["schema", "table", "column"])
    wb.active.append(["dbo", "Musteri", "TCKN"])
    buf = io.BytesIO()
    wb.save(buf)
    assert four(parse_masking_list(buf.getvalue(), "liste.xlsx")) == [(None, "dbo", "Musteri", "TCKN")]


# --- masking list sync (no database needed) ---------------------------------------------------------

def test_masking_query_must_be_a_single_select():
    from scoutout.masking import validate_query

    assert validate_query("SELECT a FROM t;") == "SELECT a FROM t"
    assert validate_query("WITH x AS (SELECT 1 AS a) SELECT a FROM x")
    for bad in ("DELETE FROM t", "SELECT 1; DROP TABLE t", "SELECT * INTO yeni FROM t", "EXEC sp_who", "select 1 from openrowset(x)"):
        with pytest.raises(ConfigError):
            validate_query(bad)


def test_masking_versions_and_alias_matching(tmp_path):
    from scoutout.masking import Index, current, save_version

    store = Store(tmp_path / "s.db")
    v1 = save_version(store, [("BankDB", "dbo", "Musteri", "TCKN"), (None, "dbo", "Kart", "No")], "dosya:a.csv", "u")
    assert v1["changed"] and v1["count"] == 2
    assert not save_version(store, [(None, "dbo", "Kart", "No"), ("bankdb", "DBO", "musteri", "tckn")], "x", "u")["changed"]
    v2 = save_version(store, [(None, "dbo", "Kart", "No")], "x", "u")
    assert v2["removed"] == ["BankDB.dbo.Musteri.TCKN"] and current(store)[0]["version"] == v2["version"]
    idx = Index([("BankDB", "dbo", "Musteri", "TCKN")], {"CRM_PROD": "BankDB"})
    assert idx.listed("CRM_PROD", "dbo", "Musteri", "TCKN")  # real database name matches the ScoutOut alias
    assert not idx.listed("OTHER", "dbo", "Musteri", "TCKN")


def test_masking_sync_is_due_by_interval(tmp_path, monkeypatch):
    from scoutout import masking

    store = Store(tmp_path / "s.db")
    calls = []
    monkeypatch.setattr(masking, "sync", lambda s, b, user="sistem": calls.append(1) or {"ok": True})
    assert masking.sync_if_due(store, Config()) is None  # not configured
    store.set_setting(masking.SETTING, {"source": "R", "query": "SELECT 1", "interval_min": 60}, "u")
    masking.sync_if_due(store, Config())
    store.set_setting(masking.LAST_CHECK, {"at": masking.now(), "ok": True}, "u")
    masking.sync_if_due(store, Config())
    assert calls == [1]


def test_every_builtin_rule_is_described():
    from scoutout.catalog import GENERIC
    from scoutout.detectors import generic_detectors

    assert {d.meta.id for d in generic_detectors()} == set(GENERIC)


def test_rule_catalog_export(tmp_path):
    from openpyxl import load_workbook

    from scoutout.catalog import export_catalog, rule_label

    store = Store(tmp_path / "s.db")
    store.x("INSERT INTO rules (data_type, kind, body_json, source, status, metrics_json) VALUES "
            "('CUSTOMER_NO','regex','{\"pattern\": \"MN\\\\d{8}\"}','base-derived','ACTIVE','{\"sensitivity\": 1.0, \"precision\": 1.0}')")
    wb = load_workbook(export_catalog(store, Config(), tmp_path / "k.xlsx"))
    assert wb.sheetnames == ["Karar mantığı", "Kurulumla gelen kurallar", "Öğrenilen kurallar", "Kolon adı sinyalleri"]
    assert any("Luhn" in str(c.value) for row in wb["Kurulumla gelen kurallar"].iter_rows() for c in row)
    assert any(c.value == "Tabandan türetilen" for row in wb["Öğrenilen kurallar"].iter_rows() for c in row)
    assert rule_label("tckn") == "Kontrol hanesi (T.C. kimlik no)" and rule_label("refset") == "Taban değer eşleşmesi"


def test_evidence_breaks_down_hits_by_rule():
    from scoutout.connector.base import ColumnInfo
    from scoutout.decision_engine.engine import ColumnEvidence, Thresholds, decide_column

    ev = ColumnEvidence(ColumnInfo("Ref", "varchar", 20, 0, 0, True))
    for v in ["10000000146", "12345678950", "86507882991", "Müşteri 0532 123 45 67 aradı"] * 25:
        ev.add(("1",), v, __import__("scoutout.detectors", fromlist=["generic_detectors"]).generic_detectors())
    d = decide_column(ev, {}, Thresholds(), 4)
    tckn = next(e for e in d.evidence if e.data_type == "TCKN")
    assert tckn.by_detector == {"tckn": 50}
    assert tckn.chance_hits > 0  # the failed 11-digit candidates imply some accidental passes


def test_masking_repository_headers_need_no_aliases():
    """The bank's masking table: DatabaseName / SchemaName / TableName / ColumnName plus extra columns."""
    from scoutout.masking import entries_from_rows

    header = ["TableColumnDataMaskDetailId", "TermName", "DatabaseName", "SchemaName", "TableName", "ColumnName", "DataType",
              "DataMaskTypeId", "DataMaskType"]
    rows = [[136, "Hesap", "BOA", "COR", "Account", "AccountNumber", "INT", 5, "N_DET_NUMBER"],
            [1132, "Açıklama", "AnalyticDB", "SGM", "RetailCustomerIncomingMoneyTransferDescription", "Description",
             "VARCHAR(255)", 24, "S_NULLIFICATION"]]
    assert entries_from_rows(header, rows) == [
        ("BOA", "COR", "Account", "AccountNumber", "N_DET_NUMBER", "Hesap"),
        ("AnalyticDB", "SGM", "RetailCustomerIncomingMoneyTransferDescription", "Description", "S_NULLIFICATION", "Açıklama")]


@pytest.mark.parametrize("algo, zone, dtype, fit", [
    ("S_NULLIFICATION", "SPARSE", "TCKN", "partial"),   # whole free text emptied for a few TCKNs
    ("S_NULLIFICATION", "SENSITIVE", "TCKN", None),     # mostly sensitive: emptying is fine
    ("S_RND_PHONE", "SENSITIVE", "TCKN", "mismatch"),   # phone algorithm on a TCKN column
    ("S_RND_PHONE", "SENSITIVE", "PHONE", None),
    ("S_DET_NAME", "SENSITIVE", "GIVEN_NAME", None),
    ("N_DET_NUMBER", "SENSITIVE", "IBAN", None),
    ("N_DET_NUMBER", "SENSITIVE", "PERSON_NAME", "mismatch"),
    ("X_CUSTOM_123", "SENSITIVE", "TCKN", None),        # unknown code: no judgement
    ("S_RND_PHONE", "CLEAN", None, None),
])
def test_algorithm_fit(algo, zone, dtype, fit):
    from scoutout.masking import algo_fit

    res = algo_fit(algo, zone, dtype)
    assert (res[0] if res else None) == fit


def test_algorithm_changes_create_a_new_version(tmp_path):
    from scoutout.masking import Index, current, save_version

    store = Store(tmp_path / "s.db")
    save_version(store, [("BOA", "ACC", "T", "Description", "S_NULLIFICATION", "Açıklama")], "x", "u")
    res = save_version(store, [("BOA", "ACC", "T", "Description", "S_PARTIAL_TEXT", "Açıklama")], "x", "u")
    assert res["changed"] and res["algo_changed"] == ["BOA.ACC.T.Description: S_NULLIFICATION → S_PARTIAL_TEXT"]
    _, entries = current(store)
    assert Index(entries, {}).lookup("BOA", "ACC", "T", "Description") == {"algorithm": "S_PARTIAL_TEXT", "term": "Açıklama"}
