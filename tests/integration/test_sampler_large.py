"""Scale checks on the large synthetic DB (spec §12): no scans, bounded TABLESAMPLE, time budget."""

import json

import pytest

from scoutout.core.config import ScanConfig
from scoutout.sampler import Sampler
from scoutout.sampler.plancheck import scan_operators
from tests.conftest import ROOT
from tests.integration.helpers import Recorder, adapter

pytestmark = [pytest.mark.mssql, pytest.mark.large]

MANIFEST = ROOT / "synth_out" / "ScoutOutSynthLarge" / "manifest.json"
TIME_BUDGET_S = 5.0


@pytest.fixture(scope="module")
def large(server_cfg):
    if not MANIFEST.exists():
        pytest.skip("run `scoutout synth large` first")
    a = adapter(server_cfg, json.loads(MANIFEST.read_text(encoding="utf-8"))["database"])
    yield a, a.discover_catalog()
    a.close()


@pytest.mark.parametrize("table", ["bank.Account", "bank.TxnLog", "ops.LegacyImport"])
def test_large_table_sampling_is_cheap(large, table):
    db, cat = large
    t = cat.table(table)
    assert t.row_count >= 1_000_000
    rec = Recorder(db)
    s = Sampler(rec, ScanConfig()).sample(t, 400)
    assert s.record.n_actual >= 350
    assert s.record.duration_ms / 1000 < TIME_BUDGET_S, s.record
    for sql in rec.sql:
        if "TABLESAMPLE" in sql:
            continue
        assert scan_operators(db.estimated_plan(sql)) == [], sql[:300]
    if s.record.strategy == "tablesample":
        assert s.record.strata["tablesample_percent"] / 10000 <= ScanConfig().max_tablesample_percent
