"""The masking list lives in the main system and changes; ScoutOut reads it regularly and compares."""

import os

import pytest

from scoutout.core.config import Config, PathsConfig, ScanConfig
from scoutout.masking import SETTING, column_statuses, current, staleness, sync
from scoutout.runner import Runner
from scoutout.sources import Source, config_for, get_source, save_source
from scoutout.store import Store

pytestmark = pytest.mark.mssql
REPO = "ScoutOutMaskRepo"
# the bank's own column names work without aliases (DataMaskType = algorithm, TermName = business term)
QUERY = "SELECT DatabaseName, SchemaName, TableName, ColumnName, DataMaskType, TermName FROM dbo.MaskingRule WHERE IsActive = 1"


@pytest.fixture(scope="module")
def repo(server_cfg, small_db):
    """A stand-in for the masking tool's repository database."""
    master = server_cfg.connect()
    master.execute(f"IF DB_ID('{REPO}') IS NOT NULL BEGIN ALTER DATABASE [{REPO}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
                   f"DROP DATABASE [{REPO}]; END")
    master.execute(f"CREATE DATABASE [{REPO}]")
    db = server_cfg.connect(REPO)
    db.execute("CREATE TABLE dbo.MaskingRule (DatabaseName sysname, SchemaName sysname, TableName sysname, "
               "ColumnName sysname, IsActive bit, DataMaskType varchar(50), TermName nvarchar(100))")
    db.execute("INSERT INTO dbo.MaskingRule VALUES "
               "('ScoutOutSynthSmall','bank','Customer','TCKN',1,'S_RND_PHONE',N'Kimlik'), "      # sensitive, wrong algorithm
               "('ScoutOutSynthSmall','bank','TxnLog','RefNo',1,'N_DET_NUMBER',N'Referans'), "    # look-alikes: unnecessary
               "('ScoutOutSynthSmall','ops','LegacyImport','TCKN',1,'N_DET_NUMBER',N'Kimlik'), "  # empty column: unnecessary
               "('ScoutOutSynthSmall','ops','CustomerNote','LongNote',1,'S_NULLIFICATION',N'Açıklama')")  # sparse: lighter
    db.execute("CREATE USER scoutout_ro FOR LOGIN scoutout_ro")
    db.execute("ALTER ROLE db_datareader ADD MEMBER scoutout_ro")
    yield db
    db.close()
    master.execute(f"ALTER DATABASE [{REPO}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{REPO}]")


@pytest.fixture(scope="module")
def world(server_cfg, repo, tmp_path_factory):
    root = tmp_path_factory.mktemp("mask")
    os.environ["SCOUTOUT_DB_PASSWORD_SYNTH"] = os.environ["SCOUTOUT_RO_PASSWORD"]
    os.environ["SCOUTOUT_DB_PASSWORD_REPO"] = os.environ["SCOUTOUT_RO_PASSWORD"]
    store = Store(root / "scoutout.db")
    base = Config(scan=ScanConfig(), paths=PathsConfig(str(root)))
    save_source(store, Source("SYNTH", server_cfg.server, "ScoutOutSynthSmall", user="scoutout_ro", trust=True), "t", root)
    save_source(store, Source("REPO", server_cfg.server, REPO, user="scoutout_ro", trust=True, enabled=False), "t", root)
    Runner(config_for(base, get_source(store, "SYNTH")), store, source="SYNTH").run()
    store.set_setting(SETTING, {"source": "REPO", "query": QUERY, "interval_min": 60}, "t")
    yield store, base, root
    store.close()


def statuses(store):
    decisions = {d["id"]: d for d in store.latest_decisions()}
    out = {}
    for i, s in column_statuses(store).items():
        d = decisions[i]
        out[f"{d['schema_name']}.{d['table_name']}.{d['column_name']}"] = s["status"]
    return out


def test_list_is_read_and_compared_using_real_database_names(world):
    store, base, _ = world
    res = sync(store, base, "t")
    assert res["ok"] and res["changed"] and res["count"] == 4
    st = statuses(store)
    assert st["bank.Customer.TCKN"] == "correct" and st["ops.CustomerNote.LongNote"] == "correct"
    assert st["bank.TxnLog.RefNo"] == "unnecessary" and st["ops.LegacyImport.TCKN"] == "unnecessary"
    assert st["ops.LegacyImport.Aciklama"] == "missing" and st["bank.Account.Iban"] == "missing"
    assert "bank.TxnLog.Amount" not in st  # clean and not listed: nothing to say
    assert sync(store, base, "t")["changed"] is False  # unchanged list -> no new version


def test_algorithm_is_checked_against_the_data(world):
    store, base, _ = world
    sync(store, base, "t")
    decisions = {d["id"]: d for d in store.latest_decisions()}
    fits = {decisions[i]["column_name"] + "@" + decisions[i]["table_name"]: (s["algorithm"], s["fit"])
            for i, s in column_statuses(store).items() if s["listed"]}
    assert fits["TCKN@Customer"] == ("S_RND_PHONE", "mismatch")
    assert fits["LongNote@CustomerNote"] == ("S_NULLIFICATION", "partial")
    assert fits["RefNo@TxnLog"] == ("N_DET_NUMBER", None)


def test_changes_in_the_main_system_become_new_versions(world, repo):
    store, base, _ = world
    sync(store, base, "t")
    repo.execute("UPDATE dbo.MaskingRule SET IsActive = 0 WHERE ColumnName = 'RefNo'")
    repo.execute("INSERT INTO dbo.MaskingRule VALUES ('ScoutOutSynthSmall','bank','Account','Iban',1,'S_DET_IBAN',N'IBAN')")
    res = sync(store, base, "t")
    assert res["changed"] and res["added"] == ["ScoutOutSynthSmall.bank.Account.Iban"]
    assert res["removed"] == ["ScoutOutSynthSmall.bank.TxnLog.RefNo"]
    st = statuses(store)
    assert st["bank.Account.Iban"] == "correct" and "bank.TxnLog.RefNo" not in st


def test_failure_keeps_last_good_list_and_warns(world):
    store, base, _ = world
    good, _ = current(store)
    store.set_setting(SETTING, {"source": "REPO", "query": "SELECT x FROM dbo.Yok", "interval_min": 60}, "t")
    res = sync(store, base, "t")
    assert not res["ok"]
    assert current(store)[0]["version"] == good["version"]
    assert "okunamadı" in staleness(store)
    store.set_setting(SETTING, {"source": "REPO", "query": QUERY, "interval_min": 60}, "t")
    assert sync(store, base, "t")["ok"] and staleness(store) is None

