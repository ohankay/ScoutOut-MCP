"""End to end through the MCP protocol (in-process client): what the TDM agent does, and that it never sees a value.

The tests in this module share one world and run in order; the last one scans everything the agent received.
"""

import asyncio
import json
import time

import pytest
from mcp import Client

from scoutout.core.config import Config, ConnectionConfig, LlmConfig, PathsConfig, ScanConfig
from scoutout_mcp.server import build_server
from scoutout_mcp.service import Service
from scoutout_mcp.settings import McpSettings
from tests.integration.helpers import adapter

pytestmark = pytest.mark.mssql
SYNTH = "ScoutOutSynthSmall"
CATALOG = "ScoutOutMcpCatalog"  # stands in for BoaCatalog
SEEN: list[str] = []  # every text the agent received


class FakeLlm:
    """A local model stand-in. It gets real values (allowed); its answers are checked before anything reaches the agent."""

    def __init__(self):
        self.seen = ""
        self.leak = ""

    def chat_json(self, system, user):
        self.seen = user
        msg = json.loads(user)
        self.leak = msg["types"]["CUSTOMER_NO"]["examples"][0]
        return {"rules": [
            {"data_type": "CUSTOMER_NO", "pattern": r"(?<![A-Za-z0-9])MN\d{8}(?![0-9])", "column_names": ["musteri_no", "ref"],
             "description": f"örnek {self.leak}"},
            {"data_type": "CUSTOMER_NO", "pattern": self.leak},  # a real value
            {"data_type": "CUSTOMER_NO", "pattern": r"MN\d{8}", "context": [self.leak]},
            {"data_type": "CUSTOMER_NO", "pattern": r"(?:MUSTERI)\d+"},  # long constant
            {"data_type": "TCKN", "pattern": r"\d{11}"},  # type not asked for
            {"data_type": "CUSTOMER_NO", "pattern": r"(MN\d+)+$"},  # catastrophic backtracking
        ]}


@pytest.fixture(scope="module")
def world(server_cfg, small_db, tmp_path_factory):
    import os

    master = server_cfg.connect()
    master.execute(f"IF DB_ID('{CATALOG}') IS NOT NULL BEGIN ALTER DATABASE [{CATALOG}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
                   f"DROP DATABASE [{CATALOG}]; END")
    master.execute(f"CREATE DATABASE [{CATALOG}]")
    db = server_cfg.connect(CATALOG)
    db.execute("CREATE SCHEMA Dtg")
    db.execute("CREATE TABLE Dtg.TableColumnDataMaskDetail (TableColumnDataMaskDetailId int IDENTITY PRIMARY KEY, "
               "TermName nvarchar(100), DatabaseName sysname, SchemaName sysname, TableName sysname, ColumnName sysname, "
               "DataType varchar(30), DataMaskTypeId int, DataMaskType varchar(50), Production bit, Development bit)")
    db.execute("INSERT INTO Dtg.TableColumnDataMaskDetail (TermName, DatabaseName, SchemaName, TableName, ColumnName, DataType, "
               "DataMaskTypeId, DataMaskType, Production, Development) VALUES "
               f"(N'Kimlik','{SYNTH}','bank','Customer','TCKN','char',1,'S_RND_PHONE',1,1), "          # wrong algorithm
               f"(N'Referans','{SYNTH}','bank','TxnLog','RefNo','varchar',2,'N_DET_NUMBER',1,1), "     # unnecessary
               f"(N'Kimlik','{SYNTH}','ops','LegacyImport','TCKN','char',2,'N_DET_NUMBER',1,1), "      # empty -> unnecessary
               f"(N'Açıklama','{SYNTH}','ops','CustomerNote','LongNote','nvarchar',3,'S_NULLIFICATION',1,1), "  # lighter
               f"(N'Kart','{SYNTH}','bank','Card','CardNo','varchar',4,'S_DET_CARD',1,1), "             # correct
               f"(N'Anne','{SYNTH}','bank','Customer','AnneKizlikSoyadi','nvarchar',5,'S_DET_NAME',1,1)")  # not in the DB
    db.execute("CREATE USER scoutout_ro FOR LOGIN scoutout_ro")
    db.execute("ALTER ROLE db_datareader ADD MEMBER scoutout_ro")
    db.close()

    root = tmp_path_factory.mktemp("mcp")
    cfg = Config(connection=ConnectionConfig(server=server_cfg.server, database="master", user="scoutout_ro",
                                             password_env="SCOUTOUT_RO_PASSWORD", trust_server_certificate=True),
                 scan=ScanConfig(), paths=PathsConfig(str(root)), llm=LlmConfig(endpoint="http://localhost:11434/v1"))
    os.environ["SCOUTOUT_DATA_DIR"] = str(root)
    from scoutout.core.log import setup_logging

    setup_logging(cfg.paths.logs)
    llm = FakeLlm()
    svc = Service(cfg, McpSettings(databases=[SYNTH], masking_database=CATALOG), llm_client=llm)
    yield svc, build_server(lambda: svc), llm, root
    svc.store.close()
    os.environ.pop("SCOUTOUT_DATA_DIR", None)
    master.execute(f"ALTER DATABASE [{CATALOG}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{CATALOG}]")
    master.close()


def call(server, tool: str, **args) -> dict:
    async def go():
        async with Client(server) as c:
            return await c.call_tool(tool, args)

    r = asyncio.run(go())
    text = "".join(getattr(x, "text", "") for x in r.content)
    SEEN.append(text)
    return json.loads(text)


def wait(server, job_id: str) -> dict:
    for _ in range(240):
        s = call(server, "scan_status", job_id=job_id)
        if s["status"] != "running":
            return s
        time.sleep(0.5)
    raise AssertionError("scan did not finish")


def findings(server, **kw) -> dict:
    return {f"{f['table']}.{f['column']}": f for f in call(server, "get_findings", database=SYNTH, limit=1000, **kw)["findings"]}


# --- discovery and scanning ------------------------------------------------------------------------------

def test_lists_databases_and_tables(world):
    _, server, _, _ = world
    dbs = {d["database"] for d in call(server, "list_databases")["databases"]}
    assert SYNTH in dbs and CATALOG in dbs and "master" not in dbs
    tables = call(server, "list_tables", database=SYNTH, filter="bank.*")["tables"]
    assert {t["table"] for t in tables} == {"bank.Customer", "bank.Account", "bank.Card", "bank.TxnLog"}
    assert "error" in call(server, "list_tables", database="YokBoyleDB")


def test_scan_chosen_tables_then_audit_reports_unscanned(world):
    _, server, _, _ = world
    job = call(server, "scan", databases=[SYNTH], tables=["bank.Customer", "bank.Card"])
    assert job["tables"] == 2
    assert "error" in call(server, "scan", databases=[SYNTH])  # one scan at a time
    status = wait(server, job["job_id"])
    assert status["status"] == "done" and status["databases"][0]["done"] == 2
    f = findings(server)
    assert f["bank.Customer.TCKN"]["zone"] == "SENSITIVE" and f["bank.Customer.TCKN"]["sensitive_type"] == "TCKN"
    assert "bank.TxnLog.RefNo" not in f
    audit = call(server, "audit_masking")
    assert {"bank.TxnLog", "ops.LegacyImport", "ops.CustomerNote"} <= set(audit["not_scanned"][0]["tables"])


def test_full_scan_and_findings(world):
    _, server, _, _ = world
    wait(server, call(server, "scan", databases=[SYNTH])["job_id"])
    f = findings(server)
    assert f["bank.Card.CardNo"]["zone"] == "SENSITIVE"
    assert f["ops.CustomerNote.Note03"]["zone"] == "SPARSE"
    assert f["ops.LegacyImport.TCKN"]["zone"] == "EMPTY"
    assert f["bank.TxnLog.RefNo"]["zone"] == "CLEAN"
    sens = call(server, "get_findings", database=SYNTH, zone="SENSITIVE")
    assert all(x["zone"] == "SENSITIVE" for x in sens["findings"]) and sens["summary"] == {"SENSITIVE": sens["matched"]}


def test_explain_finding(world):
    _, server, _, _ = world
    e = call(server, "explain_finding", database=SYNTH, table="bank.Customer", column="TCKN")
    tckn = next(x for x in e["evidence"] if x["type"] == "TCKN")
    assert tckn["matched_rows"] > 0 and any(r["detector"] == "tckn" for r in tckn["by_rule"])
    assert "Kontrol hanesi" in tckn["by_rule"][0]["rule"]
    assert e["name_signal"].get("TCKN", 0) > 0.5


def test_profile_column_shows_shapes_not_values(world):
    _, server, _, _ = world
    p = call(server, "profile_column", database=SYNTH, table="bank.Customer", column="CustomerNo")
    assert p["shapes"][0]["shape"] == "XXdddddddd" and p["shapes"][0]["share"] > 0.9
    assert any(x["prefix"].startswith("MN") and x["share"] == 1.0 for x in p["frequent_prefixes"])
    assert len({x["share"] for x in p["frequent_prefixes"]}) == len(p["frequent_prefixes"])  # no redundant chain
    t = call(server, "profile_column", database=SYNTH, table="bank.Customer", column="TCKN")
    assert t["checksums"]["tckn"]["rate"] > 0.95


# --- masking ---------------------------------------------------------------------------------------------

def test_masking_audit(world):
    _, server, _, _ = world
    a = call(server, "audit_masking", databases=[SYNTH])
    cols = {k: {f"{x['table']}.{x['column']}": x for x in a[k]} for k in ("unnecessary", "missing", "algorithm", "uncertain")}
    assert {"bank.TxnLog.RefNo", "ops.LegacyImport.TCKN"} <= set(cols["unnecessary"])
    assert cols["algorithm"]["bank.Customer.TCKN"]["issue"] == "mismatch"
    assert cols["algorithm"]["ops.CustomerNote.LongNote"]["issue"] == "partial"
    assert {"bank.Customer.Email", "bank.Account.Iban"} <= set(cols["missing"])
    assert a["summary"]["correct"] >= 3 and a["summary"]["tables_not_scanned"] == 0
    assert a["masking_list"]["columns"] == 6
    gone = {(x["table"], x["column"]): x["note"] for x in a["not_in_database"]}
    assert gone == {("bank.Customer", "AnneKizlikSoyadi"): "Kolon bu tabloda yok"}


def test_masking_list_changes_are_versioned(world, server_cfg):
    _, server, _, _ = world
    db = server_cfg.connect(CATALOG)
    db.execute(f"INSERT INTO Dtg.TableColumnDataMaskDetail (TermName, DatabaseName, SchemaName, TableName, ColumnName, "
               f"DataMaskType) VALUES (N'E-posta','{SYNTH}','bank','Customer','Email','S_RND_MAIL')")
    db.close()
    s = call(server, "masking_sync")
    assert s["changed"] and s["added_columns"] == [f"{SYNTH}.bank.Customer.Email"]
    a = call(server, "audit_masking", databases=[SYNTH], tables=["bank.Customer"])
    assert "Email" not in {x["column"] for x in a["missing"]}


# --- rules ------------------------------------------------------------------------------------------------

def test_mine_rules_with_local_llm(world):
    svc, server, llm, _ = world
    before = len(call(server, "list_rules")["learned"])
    r = call(server, "mine_rules", entries=[{"database": SYNTH, "table": "bank.Customer", "column": "CustomerNo",
                                             "type": "CUSTOMER_NO"}], hint="müşteri numarası MN ile başlar")
    assert llm.leak and llm.leak in llm.seen  # the local model saw real values
    assert r["values_sent_to_local_llm"] > 100
    by = [x.get("status") for x in r["rules"]]
    assert by.count("CANDIDATE") == 1 and by.count("INVALID") == 2, r
    good = next(x for x in r["rules"] if x.get("status") == "CANDIDATE")
    assert good["metrics"]["sensitivity"] >= 0.9 and good["column_names"] == ["musteri_no", "ref"]
    leaks = [x for x in r["rules"] if x.get("status") == "REJECTED" and x.get("pattern") is None]
    assert len(leaks) == 2  # the value itself and a long constant: rejected and never stored
    # context with digits (the value as a "word") and a type nobody asked for are invalid
    rows = svc.store.q("SELECT body_json FROM rules")
    assert not any(llm.leak in r["body_json"] for r in rows)
    assert len(call(server, "list_rules")["learned"]) == before + 2  # good + catastrophic (rejected by the core gate)


def test_activated_rule_is_used_by_the_next_scan(world):
    _, server, _, _ = world
    before = findings(server)["ops.CrmLog.Ref"]["zone"]
    rule = next(x for x in call(server, "list_rules", data_type="CUSTOMER_NO", status="CANDIDATE")["learned"])
    assert call(server, "decide_rule", rule_id=rule["id"], action="activate", reason="test")["status"] == "ACTIVE"
    wait(server, call(server, "scan", databases=[SYNTH], tables=["ops.CrmLog"], force=True)["job_id"])
    after = findings(server)
    assert before != "SENSITIVE" and after["ops.CrmLog.Ref"]["zone"] == "SENSITIVE"
    assert after["ops.CrmLog.Ref"]["sensitive_type"] == "CUSTOMER_NO"


def test_agent_rule_is_measured(world):
    _, server, _, _ = world
    rule = {"data_type": "PAN", "pattern": r"(?<!\d)\d{16}(?!\d)", "checksum": "luhn"}
    r = call(server, "propose_rule", rule=rule, positives=[{"database": SYNTH, "table": "bank.Card", "column": "CardNo"}])
    assert r["status"] == "CANDIDATE" and r["metrics"]["sensitivity"] >= 0.9, r
    probe = call(server, "propose_rule", rule={"data_type": "PAN", "pattern": r"^4\d{15}$"},
                 positives=[{"database": SYNTH, "table": "bank.Card", "column": "CardNo"}])
    assert probe["status"] in ("CANDIDATE", "REJECTED")
    lit = call(server, "propose_rule", rule={"data_type": "PAN", "pattern": "45431234"},
               positives=[{"database": SYNTH, "table": "bank.Card", "column": "CardNo"}])
    assert lit["status"] == "REJECTED" and lit["pattern"] is None


def test_teach_baseline(world):
    _, server, _, _ = world
    r = call(server, "teach_baseline", entries=[
        {"database": SYNTH, "table": "bank.Customer", "column": "TCKN", "type": "TCKN"},
        {"database": SYNTH, "table": "bank.Customer", "column": "FirstName", "type": "GIVEN_NAME"},
        {"database": SYNTH, "table": "bank.Customer", "column": "LastName", "type": "SURNAME"},
    ], negatives=[{"database": SYNTH, "table": "bank.TxnLog", "column": "RefNo"}])
    assert r["reference_values"] > 1000 and r["baseline_columns"]["TCKN"] == 1
    assert call(server, "list_rules")["baseline"]["version"] == r["pack_version"]


def test_review_and_reports(world):
    _, server, _, root = world
    rv = call(server, "review_finding", database=SYNTH, table="bank.TxnLog", column="RefNo", decision="approve",
              reason="kullanıcı onayladı")
    assert rv["review"] == "APPROVED"
    for kind in ("findings", "masking_audit", "rules"):
        r = call(server, "export_report", kind=kind)
        assert r["rows"] > 0 and r["path"].endswith(".xlsx")


def test_write_login_is_refused(world, server_cfg):
    svc, _, _, root = world
    import copy

    from scoutout.store import Store

    cfg = copy.deepcopy(svc.cfg)
    cfg.connection.user, cfg.connection.password_env = "scoutout_rw", "SCOUTOUT_RW_PASSWORD"
    other = Service(cfg, McpSettings(databases=[SYNTH]), store=Store(root / "rw.db"))
    r = call(build_server(lambda: other), "list_tables", database=SYNTH)
    assert "yazma yetkisi" in r["error"]


# --- the agent never saw a value ---------------------------------------------------------------------------

def test_nothing_the_agent_saw_contains_a_value(world, server_cfg, small_db):
    svc, _, _, root = world
    _, manifest = small_db
    sa = adapter(server_cfg, SYNTH)
    values = set()
    try:
        for t in manifest["tables"]:
            for c in t["columns"]:
                if c["expected_zone"] in ("SENSITIVE", "SPARSE") or c["expected_type"]:
                    for (v,) in sa.query(f"SELECT TOP 300 CAST([{c['name']}] AS nvarchar(4000)) FROM [{t['schema']}].[{t['name']}] "
                                         f"WHERE [{c['name']}] IS NOT NULL"):
                        v = str(v).strip()
                        if len(v) >= 6:
                            values.add(v)
                            values.update(w for w in v.split() if len(w) >= 8 and any(ch.isdigit() for ch in w))
    finally:
        sa.close()
    assert len(values) > 1000
    seen = "\n".join(SEEN)
    assert len(SEEN) > 30
    assert any(v in world[2].seen for v in values)  # positive control: the check finds values where they really are
    leaked = [v for v in values if v in seen]
    assert not leaked, leaked[:5]

    # the store, the log and the reports are no place for values either
    import openpyxl

    dump = "\n".join(str(tuple(r)) for t in ("decisions", "rules", "audit", "baseline_columns", "masking_lists", "settings")
                     for r in svc.store.q(f"SELECT * FROM {t}"))
    logs = "\n".join(p.read_text(encoding="utf-8") for p in (root / "logs").glob("*.log"))
    xlsx = []
    for p in (root / "reports").glob("*.xlsx"):
        for ws in openpyxl.load_workbook(p, read_only=True):
            xlsx += [str(c) for row in ws.iter_rows(values_only=True) for c in row if c is not None]
    for name, text in (("store", dump), ("log", logs), ("reports", "\n".join(xlsx))):
        bad = [v for v in values if v in text]
        # decision records keep ScoutOut's redacted examples (masked); no raw value may appear
        assert not bad, (name, bad[:5])
