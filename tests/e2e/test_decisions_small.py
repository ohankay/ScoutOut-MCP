"""M4 acceptance: every column of the small synthetic DB lands in its expected zone; false CLEAN = 0."""

import pytest

from scoutout.core.config import ScanConfig
from scoutout.decision_engine.table_scan import scan_table
from tests.integration.helpers import adapter

pytestmark = pytest.mark.mssql


@pytest.fixture(scope="module")
def results(server_cfg, small_db):
    _, manifest = small_db
    db = adapter(server_cfg, "ScoutOutSynthSmall", user="scoutout_ro")
    try:
        cat = db.discover_catalog()
        out = {}
        for t in cat.tables:
            r = scan_table(db, t, ScanConfig())
            for d in r.decisions:
                out[(t.full_name, d.column)] = d
        return out, manifest
    finally:
        db.close()


def expected(manifest):
    """Columns generic detectors must get right (institution-specific types need a baseline, see M7)."""
    for t in manifest["tables"]:
        for c in t["columns"]:
            if not c.get("requires_baseline"):
                yield f"{t['schema']}.{t['name']}", c


def test_zone_accuracy_and_no_false_clean(results):
    decisions, manifest = results
    wrong, false_clean = [], []
    for table, c in expected(manifest):
        d = decisions[(table, c["name"])]
        if d.zone != c["expected_zone"]:
            wrong.append(f"{table}.{c['name']}: {d.zone} (beklenen {c['expected_zone']}, "
                         f"p={d.p_corrected} tip={d.sensitive_type})")
        if d.zone == "CLEAN" and c["expected_zone"] in ("SENSITIVE", "SPARSE"):
            false_clean.append(f"{table}.{c['name']}")
    assert not false_clean, false_clean
    assert not wrong, "\n".join(wrong)


def test_expected_type_is_among_evidence(results):
    decisions, manifest = results
    for table, c in expected(manifest):
        if c["expected_type"]:
            d = decisions[(table, c["name"])]
            found = {e.data_type: e.zone for e in d.evidence}
            assert found.get(c["expected_type"]) == c["expected_zone"], (table, c["name"], found)


def test_misleading_metadata(results):
    decisions, _ = results
    aciklama = decisions[("ops.LegacyImport", "Aciklama")]
    assert aciklama.zone == "SENSITIVE" and aciklama.sensitive_type == "TCKN" and aciklama.conflict
    assert decisions[("ops.LegacyImport", "TCKN")].zone == "EMPTY"


def test_examples_are_redacted(results):
    decisions, _ = results
    note = decisions[("ops.CustomerNote", "Note92")]
    assert note.redacted_examples
    for ex in note.redacted_examples:
        assert "*" * 11 in ex["snippet"]
        assert not any(ch.isdigit() for ch in ex["snippet"].split("*" * 11)[0][-3:])
