"""Guard and rule-language checks (no database)."""

import pytest

from scoutout_mcp.guard import GuardError, guard, kcount, tool_result
from scoutout_mcp.rules_dsl import generality_reason, normalize, shown_value_reason
from scoutout_mcp.service import metrics_view


def test_guard_refuses_value_fields():
    with pytest.raises(GuardError):
        guard({"findings": [{"column": "TCKN", "examples": ["x"]}]})
    with pytest.raises(GuardError):
        guard({"redacted_examples": []})


def test_guard_scrubs_free_text_but_keeps_identifiers():
    out = guard({"note": "değer 12345678901 bulundu 'Ahmet'", "table": "bank.T20240101", "run_id": "20261122-101010-ab12",
                 "rate": 0.5, "n": 3, "ok": True, "x": None})
    assert "12345678901" not in out["note"] and "Ahmet" not in out["note"]
    assert out["table"] == "bank.T20240101" and out["run_id"] == "20261122-101010-ab12"
    assert out["rate"] == 0.5 and out["ok"] is True and out["x"] is None


def test_tool_errors_are_short_and_scrubbed():
    @tool_result
    def boom():
        raise RuntimeError("Conversion failed when converting the varchar value '12345678901' to int")

    r = boom()
    assert "12345678901" not in r["error"] and "RuntimeError" in r["error"]

    @tool_result
    def leaky():
        return {"values": ["secret"]}

    assert "güvenlik" in leaky()["error"]


def test_kcount():
    assert kcount(0, 20) == 0 and kcount(5, 20) == "<20" and kcount(25, 20) == 25


def test_metrics_hide_small_match_counts():
    m = metrics_view({"tp": 3, "fp": 2, "sensitivity": 0.003, "precision": 0.6, "specificity": 0.998,
                      "positives": 1000, "negatives": 1000}, 20)
    assert m["sensitivity"] is None and m["matched_positives"] == "<20" and m["matched_negatives"] == "<20"
    full = metrics_view({"tp": 990, "fp": 0, "sensitivity": 0.99, "precision": 1.0, "specificity": 1.0}, 20)
    assert full["sensitivity"] == 0.99 and full["matched_negatives"] == 0


def test_normalize_rule():
    r = normalize({"data_type": "customer_no", "pattern": r"MN\d{8}", "context": ["müşteri  no"],
                   "column_names": ["MusteriNo", "cust_no"], "checksum": None, "description": "x"})
    assert r["data_type"] == "CUSTOMER_NO"
    assert r["body"] == {"pattern": r"MN\d{8}", "context": ["müşteri no"], "checksum": None,
                         "column_names": ["cust_no", "musteri_no"], "ignore_case": False}
    for bad in ({"data_type": "x", "pattern": "a"}, {"data_type": "AB", "pattern": ""},
                {"data_type": "AB", "pattern": "a", "context": ["no 123"]}, {"data_type": "AB", "pattern": "a", "checksum": "md5"},
                {"data_type": "AB", "pattern": "a", "sql": "drop"}):
        with pytest.raises(ValueError):
            normalize(bad)


@pytest.mark.parametrize("pattern,ok", [
    (r"(?<![A-Z0-9])MN\d{8}(?!\d)", True), (r"TR\d{2}\s?\d{4}", True), (r"\b[A-Z]{3}-\d{6}\b", True),
    ("MN12345678", False), (r"(?:AHMET|MEHMET)", False), (r"musteri\d+", False),
])
def test_generality(pattern, ok):
    assert (generality_reason(pattern) is None) == ok


def test_shown_value_checks():
    shown = ["MN12345678", "Kayıt müşteri MN11112222 için", "Ayşe Yılmaz", "Nadir kelime Zorbalık"] + ["müşteri ref"] * 30
    assert shown_value_reason({"pattern": r"MN\d{8}", "context": ["müşteri"]}, shown, 20) is None
    assert "adı" in shown_value_reason({"pattern": r"\d{8}", "context": ["Ayşe"]}, shown, 20)
    assert "seyrek" in shown_value_reason({"pattern": r"\d{8}", "context": ["Zorbalık"]}, shown, 20)
    assert "aynı" in shown_value_reason({"pattern": r"\d{8}", "context": ["ayşe yılmaz"]}, shown, 20)
