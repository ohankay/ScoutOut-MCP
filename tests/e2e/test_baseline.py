"""M7 + M8 acceptance: baseline improves on generic-only; exported pack carries no reference sets or raw values;
LLM-proposed rules go through the gate (fake local LLM); everything still works without an LLM."""

import json
import zipfile

import pytest

from scoutout.core.config import Config, ConnectionConfig, LlmConfig, PathsConfig, ScanConfig, SecurityConfig
from scoutout.evaluation import evaluate, markdown
from scoutout.knowledge import build_pack, export_pack, import_pack, load_active_pack
from scoutout.rules import mine_rules
from scoutout.runner import Runner
from scoutout.store import Store, now
from tests.integration.helpers import adapter

pytestmark = pytest.mark.mssql

BASELINE = [
    ("bank", "Customer", "TCKN", "TCKN"),
    ("bank", "Customer", "FirstName", "GIVEN_NAME"),
    ("bank", "Customer", "LastName", "SURNAME"),
    ("bank", "Customer", "CustomerNo", "CUSTOMER_NO"),
    ("bank", "TxnLog", "RefNo", "NEGATIVE"),
    ("ops", "LegacyImport", "DekontNo", "NEGATIVE"),
]


def cfg_for(server_cfg, root):
    return Config(
        ConnectionConfig(server=server_cfg.server, database="ScoutOutSynthSmall", user="scoutout_ro",
                         password_env="SCOUTOUT_RO_PASSWORD", trust_server_certificate=True),
        SecurityConfig("refuse"), ScanConfig(incremental_threshold=0.0), PathsConfig(str(root)),
        LlmConfig(endpoint="http://localhost:11434/v1"),
    )


def zones(store, run_id):
    return {(f"{d['schema_name']}.{d['table_name']}", d["column_name"]): d["zone"]
            for d in store.q("SELECT * FROM decisions WHERE run_id=?", run_id)}


@pytest.fixture(scope="module")
def world(server_cfg, small_db, tmp_path_factory):
    root = tmp_path_factory.mktemp("kb")
    cfg = cfg_for(server_cfg, root)
    store = Store(root / "scoutout.db")
    generic_run = Runner(cfg, store).run()
    for s, t, c, dt in BASELINE:
        store.x("INSERT INTO baseline_columns (schema_name, table_name, column_name, data_type, added_by, added_at) "
                "VALUES (?,?,?,?,?,?)", s, t, c, dt, "test", now())
    result = build_pack(cfg, store, "test")
    rule = store.one("SELECT * FROM rules WHERE status='CANDIDATE' AND data_type='CUSTOMER_NO'")
    assert rule is not None, "base-derived rule for the customer number was not proposed"
    store.x("UPDATE rules SET status='ACTIVE', decided_by='test' WHERE id=?", rule["id"])
    based_run = Runner(cfg, store, pack=load_active_pack(cfg, store)).run()
    yield cfg, store, generic_run, based_run, result, root
    store.close()


def test_baseline_measurably_improves_on_generic(world, small_db):
    _, store, generic_run, based_run, _, root = world
    _, manifest = small_db
    g = evaluate(manifest, zones(store, generic_run))
    b = evaluate(manifest, zones(store, based_run))
    (root / "baseline_eval.md").write_text(markdown("Taban etkisi", {"Sadece generic": g, "Generic + taban": b}),
                                           encoding="utf-8")
    assert len(g["false_clean"]) == 3  # the three institution-specific customer-number columns
    assert b["false_clean"] == [] and b["accuracy"] == 1.0, b["wrong"]
    assert b["accuracy"] > g["accuracy"]


def test_pack_adds_signals_to_decisions(world):
    _, store, _, based_run, result, _ = world
    rec = json.loads(store.one("SELECT record_json FROM decisions WHERE run_id=? AND column_name='Ref'", based_run)["record_json"])
    assert rec["knowledge_pack_version"] == result.version and rec["sensitive_type"] == "CUSTOMER_NO"
    assert rec["classifier_top_k"] and rec["vector_margin"] is not None


def test_baseline_cleaning_and_naming(world):
    cfg, _, _, _, result, _ = world
    naming = json.loads((cfg.paths.root / "packs" / result.version / "naming.json").read_text(encoding="utf-8"))
    assert "customer" in naming.get("CUSTOMER_NO", {}) or "customer_no" in naming.get("CUSTOMER_NO", {})
    assert result.stats["reference_values"] > 20_000


def test_exported_pack_has_no_reference_sets_or_raw_values(world, server_cfg, tmp_path):
    cfg, store, _, _, result, _ = world
    out = export_pack(cfg, store, result.version, tmp_path / "pack.zip", "test")
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        blob = b"".join(z.read(n) for n in names).decode("utf-8")
    assert names and all(n.endswith(".json") for n in names)
    assert not any("refsets" in n or n.endswith((".npy", ".npz")) for n in names)
    sa = adapter(server_cfg, "ScoutOutSynthSmall")
    try:
        values = [str(r[0]) for sql in ("SELECT TOP 2000 TCKN FROM bank.Customer", "SELECT TOP 2000 CustomerNo FROM bank.Customer")
                  for r in sa.query(sql)]
    finally:
        sa.close()
    assert not [v for v in values if v in blob]

    other = Store(tmp_path / "other.db")
    other_cfg = cfg_for(server_cfg, tmp_path / "other")
    version = import_pack(other_cfg, other, out, "test")
    pack = load_active_pack(other_cfg, other)
    assert pack.version == version and pack.ref_dir is None  # works without reference sets
    assert any(r["data_type"] == "CUSTOMER_NO" for r in other.q("SELECT * FROM rules WHERE status='ACTIVE'"))


class FakeLlm:
    """Stands in for a local model: one good rule, one that leaks a real value, one catastrophic regex."""

    def __init__(self, leak_value):
        self.leak_value = leak_value
        self.seen = ""

    def chat_json(self, system, user):
        self.seen = user
        return {"rules": [
            {"data_type": "CUSTOMER_NO", "pattern": r"\bMN\d{8}\b", "context": ["müşteri no"],
             "description": "Müşteri numarası"},
            {"data_type": "CUSTOMER_NO", "pattern": self.leak_value, "description": "sızdıran kural"},
            {"data_type": "CUSTOMER_NO", "pattern": r"(MN\d+)+$", "description": "kötü desen"},
        ]}


def test_llm_rules_go_through_the_gate(world, server_cfg):
    cfg, store, _, _, _, _ = world
    sa = adapter(server_cfg, "ScoutOutSynthSmall")
    try:
        real = sa.query("SELECT TOP 1 CustomerNo FROM bank.Customer")[0][0]
    finally:
        sa.close()
    fake = FakeLlm(real)
    msg = mine_rules(cfg, store, "test", client=fake)
    assert "öneri" in msg
    assert real not in fake.seen  # the model only ever sees redacted context
    rows = {json.loads(r["body_json"])["pattern"]: r for r in store.q("SELECT * FROM rules WHERE source='llm-derived'")}
    assert rows[real]["status"] == "REJECTED" and ("değer" in rows[real]["reason"] or "haneli" in rows[real]["reason"])
    assert rows[r"(MN\d+)+$"]["status"] == "REJECTED"
    good = rows[r"\bMN\d{8}\b"]
    assert good["status"] == "CANDIDATE" and json.loads(good["metrics_json"])["sensitivity"] >= 0.9


def test_system_runs_without_llm(world):
    cfg, store, _, _, _, _ = world
    cfg.llm.endpoint = ""
    with pytest.raises(ValueError, match="Dil modeli"):
        mine_rules(cfg, store, "test")
