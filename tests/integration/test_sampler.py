"""M2 acceptance on the small DB with the small-table shortcut disabled (so every strategy runs)."""

from datetime import timedelta

import pytest

from scoutout.core.config import ScanConfig
from scoutout.sampler import Sampler, choose_strategy
from scoutout.sampler.plancheck import scan_operators
from tests.integration.helpers import Recorder, adapter

pytestmark = pytest.mark.mssql

EXPECTED = {
    "bank.Customer": "histogram_seek",
    "bank.Account": "histogram_seek",  # composite key: leading column
    "bank.Card": "histogram_seek",  # text key
    "bank.TxnLog": "histogram_seek",  # partition aligned with the clustered key
    "ops.CustomerNote": "histogram_seek",
    "ops.AccountEvent": "partitioned_seek",  # partition column != leading key
    "ops.LegacyImport": "tablesample",  # heap
}
FORBIDDEN = ("COUNT(", "NEWID(", "CHECKSUM(", "HASHBYTES(")


@pytest.fixture(scope="module")
def db(server_cfg, small_db):
    a = adapter(server_cfg, "ScoutOutSynthSmall")
    yield a
    a.close()


@pytest.fixture(scope="module")
def catalog(db):
    return db.discover_catalog()


def cfg(**kw):
    base = dict(small_table_rows=0, seed=7)
    base.update(kw)
    return ScanConfig(**base)


def sample(db, catalog, table, n=400, **kw):
    rec = Recorder(db)
    s = Sampler(rec, cfg(**kw)).sample(catalog.table(table), n)
    return s, rec


@pytest.mark.parametrize("table, strategy", EXPECTED.items())
def test_strategy_choice(catalog, table, strategy):
    assert choose_strategy(catalog.table(table), cfg()) == strategy


@pytest.mark.parametrize("table", EXPECTED)
def test_no_scan_in_plans_and_no_forbidden_sql(db, catalog, table):
    s, rec = sample(db, catalog, table)
    assert s.record.n_actual > 0
    for sql in rec.sql:
        assert not any(f in sql.upper() for f in FORBIDDEN), sql[:200]
        if s.record.strategy != "tablesample":
            assert scan_operators(db.estimated_plan(sql)) == [], sql[:300]


def test_tablesample_reads_bounded_fraction(db, catalog):
    s, rec = sample(db, catalog, "ops.LegacyImport", n=200)
    assert "page_clustered" in s.record.bias_flags
    assert all("TABLESAMPLE SYSTEM" in sql for sql in rec.sql)
    pct = s.record.strata["tablesample_percent"] / 10000
    assert pct <= ScanConfig().max_tablesample_percent


@pytest.mark.parametrize("table", EXPECTED)
def test_same_seed_same_sample_and_different_seed_differs(db, catalog, table):
    a, _ = sample(db, catalog, table, seed=11)
    b, _ = sample(db, catalog, table, seed=11)
    c, _ = sample(db, catalog, table, seed=12)
    pa, pb, pc = ({r[0] for r in x.rows} for x in (a, b, c))
    assert pa == pb
    assert pa != pc


def test_rounds_are_additive(db, catalog):
    t = catalog.table("bank.Account")
    sampler = Sampler(db, cfg())
    seen: set = set()
    first = sampler.sample(t, 400, 0, seen)
    second = sampler.sample(t, 600, 1, seen)
    assert not ({r[0] for r in first.rows} & {r[0] for r in second.rows})
    assert first.record.n_actual + second.record.n_actual == len(seen) >= 0.8 * 1000


def test_sample_spreads_over_key_space_and_recent_stratum(db, catalog):
    s, _ = sample(db, catalog, "bank.TxnLog", n=1000)
    idx = [c.name for c in s.columns].index("TxnTime")
    times = [r[1][idx] for r in s.rows]
    months = {(t.year, t.month) for t in times}
    assert len(months) >= 20  # 24 months of data
    assert s.record.strata.get("recent", 0) >= 150
    newest = max(times)
    recent_rows = [t for t in times if t >= newest - timedelta(days=183)]
    assert len(recent_rows) / len(times) > 0.33  # ~25% by time share alone; the extra stratum adds ~15%


def test_partitioned_proportional_and_skips_empty(db, catalog):
    t = catalog.table("ops.AccountEvent")
    s, rec = sample(db, catalog, "ops.AccountEvent", n=1000)
    nonempty = [i + 1 for i, r in enumerate(t.partition.rows_per_partition) if r > 0]
    assert set(s.record.strata) == {f"p{p}" for p in nonempty}
    assert not any(f"= {p})" in sql for p in range(1, 34) if p not in nonempty for sql in rec.sql)
    total = sum(t.partition.rows_per_partition)
    for p in nonempty:
        expected = 1000 * t.partition.rows_per_partition[p - 1] / total
        assert abs(s.record.strata[f"p{p}"] - expected) <= 0.2 * expected + 10  # seeks return 10 rows each


def test_lob_values_are_truncated(db, catalog):
    s, _ = sample(db, catalog, "ops.CustomerNote", n=200, lob_chars=4000)
    idx = [c.name for c in s.columns].index("LongNote")
    assert max(len(r[1][idx]) for r in s.rows) <= 4000


def test_small_table_shortcut_is_deterministic(db, catalog):
    small = ScanConfig(seed=3)
    t = catalog.table("bank.Customer")  # 20k rows: below the small-table threshold
    a = Sampler(db, small).sample(t, 400)
    b = Sampler(db, small).sample(t, 400)
    assert a.record.strategy == "small_table" and a.record.n_actual == 400
    assert [r[0] for r in a.rows] == [r[0] for r in b.rows]
