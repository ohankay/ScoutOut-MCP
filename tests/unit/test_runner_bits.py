import time
from datetime import datetime

from scoutout.core.log import scrub
from scoutout.report import diff
from scoutout.runner import RateLimiter, in_window


def test_maintenance_window():
    at = datetime(2026, 1, 1, 23, 30)
    assert in_window("", at)
    assert in_window("22:00-06:00", at)
    assert in_window("22:00-06:00", datetime(2026, 1, 1, 5, 59))
    assert not in_window("22:00-06:00", datetime(2026, 1, 1, 12, 0))
    assert in_window("09:00-17:00", datetime(2026, 1, 1, 12, 0))


def test_rate_limiter_spaces_queries():
    rl = RateLimiter(20)
    t0 = time.monotonic()
    for _ in range(6):
        rl.wait()
    assert time.monotonic() - t0 >= 0.24


def test_scrub_masks_literals_and_long_numbers():
    msg = "Conversion failed when converting N'10000000146' near 12345678901 in query"
    out = scrub(msg)
    assert "10000000146" not in out and "12345678901" not in out and "Conversion failed" in out


def row(col, zone, t=""):
    return {"schema": "s", "table": "t", "column": col, "zone": zone, "sensitive_type": t}


def test_report_diff():
    before = [row("a", "SENSITIVE", "TCKN"), row("b", "CLEAN"), row("c", "SPARSE", "PHONE")]
    after = [row("a", "CLEAN"), row("b", "SENSITIVE", "IBAN"), row("d", "SENSITIVE", "PAN")]
    d = diff(before, after)
    assert d["now_clean"] == [":s.t.a"]
    assert sorted(d["new_sensitive"]) == [":s.t.b", ":s.t.d"]
    assert d["removed"] == [":s.t.c"] and d["added"] == [":s.t.d"]


# --- incremental scan on live servers and on restored copies (D-043) ----------------------------------

from scoutout.runner import can_carry  # noqa: E402

BASE = {"stats": "2026-11-20T10:00:00", "write": "2026-11-21T09:00:00", "server": "2026-11-01T00:00:00",
        "db": "2026-01-01T00:00:00", "rows": 1000, "mods": 3}


def sig(**kw):
    return BASE | kw


def test_live_server_unchanged_table_is_carried():
    assert can_carry(BASE, sig(), 0.0, 0.05, age_days=1, max_days=7)


def test_live_server_any_write_rescans():
    assert not can_carry(BASE, sig(write="2026-11-22T08:00:00"), 0.0, 0.05, 1, 7)
    assert not can_carry(BASE, sig(server="2026-11-22T00:00:00"), 0.0, 0.05, 1, 7)  # restart reset the usage stats


def test_restored_copy_unknown_write_time():
    restored = sig(write=None, db="2026-11-22T05:00:00")
    prev = sig(write=None, db="2026-11-21T05:00:00")
    assert can_carry(prev, restored, 0.0, 0.05, 1, 7)  # same rows and statistics: static table
    assert not can_carry(prev, restored | {"rows": 1001}, 0.0, 0.05, 1, 7)  # one new row in production -> scan
    assert not can_carry(prev, restored | {"stats": "2026-11-22T01:00:00"}, 0.0, 0.05, 1, 7)
    assert not can_carry(prev, restored | {"mods": 4}, 0.0, 0.05, 1, 7)


def test_first_scan_after_moving_to_a_restored_copy_rescans():
    """Previously a live write time was known; on the copy it is unknown -> the rows/mods rule still applies."""
    assert not can_carry(BASE, sig(write=None, rows=1200), 0.0, 0.05, 1, 7)


def test_safety_net_rescans_old_results():
    assert not can_carry(BASE, sig(), 0.0, 0.05, age_days=7.5, max_days=7)
    assert not can_carry(["old", "format"], sig(), 0.0, 0.05, 1, 7)
