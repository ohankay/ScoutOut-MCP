"""M1 acceptance: catalog of the synthetic DB is extracted correctly; write-access policy works."""

import os

import pytest

from scoutout.connector import WriteAccessRefused, open_database
from scoutout.connector.mssql import MsSqlAdapter
from scoutout.core.config import Config, ConnectionConfig, SecurityConfig

pytestmark = pytest.mark.mssql


def make_cfg(server_cfg, user, password_env, policy="refuse"):
    return Config(
        ConnectionConfig(
            server=server_cfg.server, database="ScoutOutSynthSmall", user=user, password_env=password_env,
            trust_server_certificate=True,
        ),
        SecurityConfig(policy),
    )


@pytest.fixture(scope="module")
def ro_cfg(server_cfg, small_db):
    return make_cfg(server_cfg, "scoutout_ro", "SCOUTOUT_RO_PASSWORD")


@pytest.fixture(scope="module")
def catalog(ro_cfg):
    db, report = open_database(ro_cfg)
    try:
        yield db.discover_catalog(), report
    finally:
        db.close()


def _base_type(sql_type):
    base = sql_type.split("(")[0]
    length = None
    if base in ("char", "varchar", "nchar", "nvarchar") and "(" in sql_type:
        inner = sql_type.split("(")[1].rstrip(")")
        length = -1 if inner == "max" else int(inner)
    return base, length


def test_catalog_matches_manifest(catalog, small_db):
    cat, _ = catalog
    _, manifest = small_db
    assert {t.full_name for t in cat.tables} == {f"{t['schema']}.{t['name']}" for t in manifest["tables"]}
    for mt in manifest["tables"]:
        t = cat.table(f"{mt['schema']}.{mt['name']}")
        assert t.row_count == mt["rows"], t.full_name  # exact: VIEW DATABASE STATE granted
        assert [c.name for c in t.columns] == [c["name"] for c in mt["columns"]]
        for mc in mt["columns"]:
            base, length = _base_type(mc["sql_type"])
            col = t.column(mc["name"])
            assert col.data_type == base, (t.full_name, col.name)
            assert col.max_length == length, (t.full_name, col.name)
        assert t.clustered_key == (mt["clustered_key"] or [])
        assert t.is_heap == (mt["shape"] == "heap")
        assert t.primary_key == (mt["clustered_key"] or [])
        if mt["partitioned_by"]:
            assert t.partition and t.partition.column == mt["partitioned_by"]
            assert t.partition.count == 33 and sum(t.partition.rows_per_partition) == mt["rows"]
        else:
            assert t.partition is None


def test_catalog_nullability_lob_and_stats(catalog):
    cat, _ = catalog
    note = cat.table("ops.CustomerNote")
    assert not note.column("NoteId").nullable and note.column("Note03").nullable
    assert note.column("LongNote").is_lob and not note.column("Note03").is_lob
    acc = cat.table("bank.Account")
    pk_stats = [s for s in acc.stats if s.leading_column == "BranchCode"]
    assert pk_stats and pk_stats[0].rows == 30_000 and pk_stats[0].modification_counter is not None


def test_read_only_user_passes(catalog):
    _, report = catalog
    assert report.read_only and report.reasons == [] and report.warnings == []


def test_writer_is_refused_by_default(server_cfg, small_db):
    with pytest.raises(WriteAccessRefused) as exc:
        open_database(make_cfg(server_cfg, "scoutout_rw", "SCOUTOUT_RW_PASSWORD"))
    assert "db_datawriter rolü" in exc.value.report.reasons


def test_writer_warn_mode_continues_and_falls_back_without_view_state(server_cfg, small_db):
    db, report = open_database(make_cfg(server_cfg, "scoutout_rw", "SCOUTOUT_RW_PASSWORD", policy="warn"))
    try:
        assert not report.read_only
        assert any("VIEW DATABASE STATE" in w for w in report.warnings)
        cat = db.discover_catalog()
        assert cat.warnings  # approximate row counts from sys.partitions
        assert cat.table("bank.Account").row_count == 30_000
    finally:
        db.close()


def test_sysadmin_is_detected(server_cfg, small_db):
    os.environ.setdefault("SCOUTOUT_SA_PWD", os.environ["MSSQL_SA_PASSWORD"])
    db = MsSqlAdapter(make_cfg(server_cfg, "sa", "SCOUTOUT_SA_PWD").connection)
    try:
        assert "sysadmin rolü" in db.check_read_only().reasons
    finally:
        db.close()

