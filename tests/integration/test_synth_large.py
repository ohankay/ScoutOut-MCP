"""Large synthetic database: row counts match the manifest; values valid across chunk seams."""

import json

import pytest

from scoutout.detectors.validators.checksums import iban_valid, tckn_valid, vkn_valid
from tests.conftest import ROOT

pytestmark = [pytest.mark.mssql, pytest.mark.large]

MANIFEST = ROOT / "synth_out" / "ScoutOutSynthLarge" / "manifest.json"


@pytest.fixture(scope="module")
def large_db(server_cfg):
    if not MANIFEST.exists():
        pytest.skip("run `scoutout synth large` first")
    conn = server_cfg.connect("ScoutOutSynthLarge")
    yield conn, json.loads(MANIFEST.read_text(encoding="utf-8"))
    conn.close()


def test_row_counts_match_manifest(large_db):
    conn, manifest = large_db
    for t in manifest["tables"]:
        rows = conn.execute(
            "SELECT SUM(row_count) FROM sys.dm_db_partition_stats WHERE object_id = OBJECT_ID(?) AND index_id IN (0, 1)",
            f"{t['schema']}.{t['name']}",
        ).fetchone()[0]
        assert rows == t["rows"], t["name"]


def test_values_valid_around_chunk_boundaries(large_db):
    from scoutout.synth.build import CHUNK_ROWS

    conn, _ = large_db
    rows = conn.execute(
        "SELECT Iban, TaxNo FROM bank.Account WHERE AccountNo BETWEEN ? AND ?", CHUNK_ROWS - 50, CHUNK_ROWS + 50
    ).fetchall()
    assert rows and all(iban_valid(i) for i, _ in rows) and all(vkn_valid(v) for _, v in rows if v)
    tckns = [r[0] for r in conn.execute("SELECT TOP 1000 Aciklama FROM ops.LegacyImport").fetchall()]
    assert all(tckn_valid(v) for v in tckns)
