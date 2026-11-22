"""The small synthetic database matches its manifest (ground truth must itself be correct)."""

import re

import pyodbc
import pytest

from scoutout.detectors.validators.checksums import iban_valid, luhn_valid, tckn_valid, vkn_valid, ykn_valid
from scoutout.synth.build import build

pytestmark = pytest.mark.mssql

DIRTY_MARKER = "ile kayıt güncellendi"


def _col(manifest, table, column):
    t = next(t for t in manifest["tables"] if t["name"] == table)
    return next(c for c in t["columns"] if c["name"] == column)


def _values(conn, sql):
    return [r[0] for r in conn.execute(sql).fetchall()]


@pytest.mark.parametrize(
    "sql, validator",
    [
        ("SELECT TCKN FROM bank.Customer", tckn_valid),
        ("SELECT Aciklama FROM ops.LegacyImport", tckn_valid),
        ("SELECT YabanciKimlik FROM ops.LegacyImport WHERE YabanciKimlik IS NOT NULL", ykn_valid),
        ("SELECT TaxNo FROM bank.Account WHERE TaxNo IS NOT NULL", vkn_valid),
        ("SELECT Iban FROM bank.Account", iban_valid),
        ("SELECT CardNo FROM bank.Card", luhn_valid),
    ],
)
def test_generated_identifiers_are_valid(small_db, sql, validator):
    conn, _ = small_db
    values = _values(conn, sql)
    assert values
    invalid = [v for v in values if not validator(v.strip())]
    assert not invalid, f"{len(invalid)} invalid values"


def test_identifiers_are_mostly_unique(small_db):
    conn, _ = small_db
    tckns = _values(conn, "SELECT TCKN FROM bank.Customer")
    assert len(set(tckns)) / len(tckns) > 0.99


def test_hard_negatives_occasionally_pass_checksum(small_db):
    """Random 11-digit refs pass the TCKN checksum ~1% of the time — that is the point."""
    conn, _ = small_db
    refs = _values(conn, "SELECT RefNo FROM bank.TxnLog")
    rate = sum(tckn_valid(r) for r in refs) / len(refs)
    assert 0.005 < rate < 0.02


@pytest.mark.parametrize("column", ["Note00", "Note03", "Note08", "Note50", "Note92", "Note99", "LongNote"])
def test_free_text_contamination_matches_manifest(small_db, column):
    conn, manifest = small_db
    texts = _values(conn, f"SELECT {column} FROM ops.CustomerNote")
    realised = sum(DIRTY_MARKER in t for t in texts) / len(texts)
    assert realised == pytest.approx(_col(manifest, "CustomerNote", column)["true_rate"], abs=1e-6)


def test_embedded_tckn_values_are_valid(small_db):
    conn, _ = small_db
    texts = _values(conn, "SELECT Note92 FROM ops.CustomerNote")
    found = [m for t in texts if DIRTY_MARKER in t for m in re.findall(r"(?<!\d)\d{11}(?!\d)", t)]
    assert found and all(tckn_valid(v) for v in found)


def test_lob_values_exceed_4000_chars_with_value_inside_prefix(small_db):
    conn, _ = small_db
    rows = conn.execute(
        f"SELECT TOP 50 LEN(LongNote), CHARINDEX(N'{DIRTY_MARKER}', LongNote) FROM ops.CustomerNote "
        f"WHERE LongNote LIKE N'%{DIRTY_MARKER}%'"
    ).fetchall()
    assert rows and all(length > 4000 and 0 < pos < 4000 for length, pos in rows)


def test_misleading_metadata(small_db):
    conn, manifest = small_db
    assert conn.execute("SELECT COUNT(*) FROM ops.LegacyImport WHERE TCKN IS NOT NULL").fetchone()[0] == 0
    assert _col(manifest, "LegacyImport", "TCKN")["expected_zone"] == "EMPTY"
    assert _col(manifest, "LegacyImport", "Aciklama")["expected_type"] == "TCKN"


def test_expected_zones_cover_all_contamination_levels(small_db):
    _, manifest = small_db
    zones = {c["name"]: c["expected_zone"] for c in next(t for t in manifest["tables"] if t["name"] == "CustomerNote")["columns"]}
    assert zones == {
        "NoteId": "CLEAN", "Note00": "CLEAN", "Note03": "SPARSE", "Note08": "SPARSE", "Note50": "SPARSE",
        "Note92": "SENSITIVE", "Note99": "SENSITIVE", "LongNote": "SPARSE",
    }


def test_table_shapes(small_db):
    conn, _ = small_db
    rows = conn.execute(
        """SELECT s.name + '.' + t.name, i.type_desc
           FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id
           JOIN sys.indexes i ON i.object_id = t.object_id AND i.index_id IN (0, 1)"""
    ).fetchall()
    kinds = dict((name, kind) for name, kind in rows)
    assert kinds["ops.LegacyImport"] == "HEAP"
    assert all(kinds[t] == "CLUSTERED" for t in ("bank.Customer", "bank.Account", "bank.Card", "bank.TxnLog"))


def test_partitions_have_data_and_trailing_empty_ones(small_db):
    conn, _ = small_db
    counts = [r[0] for r in conn.execute(
        "SELECT p.rows FROM sys.partitions p WHERE p.object_id = OBJECT_ID('bank.TxnLog') AND p.index_id = 1 "
        "ORDER BY p.partition_number"
    ).fetchall()]
    assert len(counts) == 33
    assert all(c > 0 for c in counts[:24])  # 2024-11 .. 2026-10
    assert all(c == 0 for c in counts[25:])  # future months are empty


def test_statistics_histograms_exist(small_db):
    conn, _ = small_db
    steps = conn.execute(
        "SELECT COUNT(*) FROM sys.stats s CROSS APPLY sys.dm_db_stats_histogram(s.object_id, s.stats_id) h "
        "WHERE s.object_id = OBJECT_ID('bank.Account')"
    ).fetchone()[0]
    assert steps > 10


def test_read_only_login_cannot_write(server_cfg, small_db):
    import os

    from scoutout.synth.build import ServerConfig

    ro = ServerConfig(server_cfg.server, "scoutout_ro", os.environ["SCOUTOUT_RO_PASSWORD"], server_cfg.driver)
    conn = ro.connect("ScoutOutSynthSmall")
    assert conn.execute("SELECT COUNT(*) FROM ops.LegacyImport").fetchone()[0] > 0
    with pytest.raises(pyodbc.Error):
        conn.execute("INSERT INTO ops.LegacyImport (Kod) VALUES ('X')")


def test_same_seed_rebuilds_identical_database(server_cfg, small_db, tmp_path):
    conn, manifest = small_db
    checksum_sql = "SELECT CHECKSUM_AGG(BINARY_CHECKSUM(*)) FROM ops.CustomerNote"
    db = "ScoutOutSynthRepro"
    build("small", manifest["seed"], tmp_path, server_cfg, log=lambda *_: None, database=db)
    try:
        repro = server_cfg.connect(db)
        assert repro.execute(checksum_sql).fetchone()[0] == conn.execute(checksum_sql).fetchone()[0]
        repro.close()
        build("small", manifest["seed"] + 1, tmp_path, server_cfg, log=lambda *_: None, database=db)
        repro = server_cfg.connect(db)
        assert repro.execute(checksum_sql).fetchone()[0] != conn.execute(checksum_sql).fetchone()[0]
        repro.close()
    finally:
        server_cfg.connect().execute(
            f"ALTER DATABASE [{db}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{db}]"
        )
