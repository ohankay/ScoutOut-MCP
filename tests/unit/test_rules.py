import json

import httpx
import numpy as np
import pytest

from scoutout.knowledge import h64, normalize_value, predict, train_classifier
from scoutout.llm import LlmClient, check_endpoint
from scoutout.rules import check_syntax_and_redos, leak_reason, literals, measure, shape_pattern, submit, validate_llm_output
from scoutout.store import Store

KEY = b"test-key"


@pytest.fixture
def refdir(tmp_path):
    d = tmp_path / "refsets"
    d.mkdir()
    np.save(d / "TCKN.npy", np.unique(np.array([h64(KEY, "TCKN", "10000000146")], dtype=np.uint64)))
    np.save(d / "SURNAME.npy", np.unique(np.array([h64(KEY, "SURNAME", "yilmaz")], dtype=np.uint64)))
    return d


def test_literals_cover_branches_and_groups():
    assert set(literals(r"(?:TC|kimlik) ?(10000000146|abc)\d{2}")) >= {"TC", "kimlik", "10000000146", "abc"}


@pytest.mark.parametrize(
    "body",
    [
        {"pattern": r"10000000146"},  # literal value from the baseline
        {"pattern": r"(?:99999|10000000146)"},  # hidden in an alternation
        {"pattern": r"\d{11}", "context": ["Yılmaz"]},  # real surname used as a keyword
        {"pattern": r"MN0001\d{4}"},  # 4+ digit constant
    ],
)
def test_leaking_rules_are_rejected(body, refdir):
    assert leak_reason(body, KEY, refdir) is not None


def test_clean_rule_passes_leak_check(refdir):
    assert leak_reason({"pattern": r"(?<!\d)MN\d{8}(?!\d)", "context": ["müşteri no"]}, KEY, refdir) is None


@pytest.mark.parametrize("pattern", [r"(a+)+$", r"(\d*)*x", r"(?:\w+\s?)+$"])
def test_catastrophic_backtracking_rejected(pattern):
    assert check_syntax_and_redos(pattern) is not None


@pytest.mark.parametrize("pattern", [r"([a-z", "", r"x*"])
def test_invalid_or_trivial_patterns_rejected(pattern):
    assert check_syntax_and_redos(pattern) is not None


def test_shape_pattern_generalises_without_values():
    vals = [f"MN{i:08d}" for i in range(1, 400, 3)]
    pat = shape_pattern(vals)
    assert pat == r"(?<![A-Za-z0-9])MN\d{8}(?![A-Za-z0-9])"
    assert not any(v in pat for v in vals)
    m = measure(pat, ["kayıt MN00012345 güncellendi"], ["ref 12345678", "MN123"])
    assert m["sensitivity"] == 1.0 and m["specificity"] == 1.0


def test_gate_records_rejections_with_reason(tmp_path, refdir):
    store = Store(tmp_path / "s.db")
    pos, neg = ["MN00012345"] * 50, ["X"] * 50
    assert submit(store, "u", "CUSTOMER_NO", {"pattern": "MN00012345"}, "llm-derived", KEY, refdir, pos, neg) == "REJECTED"
    assert submit(store, "u", "CUSTOMER_NO", {"pattern": r"[A-Z]+"}, "llm-derived", KEY, refdir, pos, ["ABC"] * 50) == "REJECTED"
    assert submit(store, "u", "CUSTOMER_NO", {"pattern": r"MN\d{8}"}, "llm-derived", KEY, refdir, pos, neg) == "CANDIDATE"
    assert submit(store, "u", "CUSTOMER_NO", {"pattern": r"MN\d{8}"}, "llm-derived", KEY, refdir, pos, neg) == "DUPLICATE"
    reasons = [r["reason"] for r in store.q("SELECT reason FROM rules WHERE status='REJECTED'")]
    assert any("haneli sayı" in r for r in reasons) and any("eşik altında" in r for r in reasons)


def test_llm_output_validation():
    out = validate_llm_output({"rules": [{"data_type": "customer_no", "pattern": "x", "context": "nope"}, {"bad": 1}]})
    assert out == [{"data_type": "CUSTOMER_NO", "pattern": "x", "context": [], "description": ""}]
    with pytest.raises(ValueError):
        validate_llm_output(["not", "a", "dict"])


@pytest.mark.parametrize("url", ["https://api.openai.com/v1", "http://10.0.0.5:11434/v1", "ftp://localhost/x", "localhost:11434"])
def test_remote_llm_endpoints_refused(url):
    with pytest.raises(ValueError):
        check_endpoint(url, ["localhost", "127.0.0.1", "::1", "ollama"])


def test_llm_client_talks_openai_compatible_json():
    def handler(request: httpx.Request):
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"} and body["temperature"] == 0
        return httpx.Response(200, json={"choices": [{"message": {"content": 'Tamam: {"rules": []}'}}]})

    c = LlmClient("http://localhost:11434/v1", "m", ["localhost"], transport=httpx.MockTransport(handler))
    assert c.chat_json("sys", "user") == {"rules": []}


def test_normalisation_for_reference_sets():
    assert normalize_value("IBAN", "TR33 0006 1005 1978 6457 8413 26") == ["TR330006100519786457841326"]
    assert normalize_value("PHONE", "+90 532 123 45 67") == ["5321234567"]
    assert normalize_value("GIVEN_NAME", "Ayşe Gül") == ["ayse", "gul"]


def test_classifier_roundtrip():
    rng = np.random.default_rng(0)
    rows = [(rng.normal(0, 1, 5) + (3 if i % 2 else 0), "A" if i % 2 else "B") for i in range(40)]
    model = train_classifier(rows)
    assert json.loads(json.dumps(model)) == model  # plain JSON weights, no pickle
    assert predict(model, np.full(5, 3.0))[0][0] == "A"


def test_llm_client_for_an_internal_vllm_gateway():
    from scoutout.core.config import LlmConfig

    calls = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        calls.append((request.headers.get("authorization"), body))
        if "response_format" in body:
            return httpx.Response(400, json={"error": "response_format not supported"})
        return httpx.Response(200, json={"choices": [{"message": {"content": '<think>{"x": 1}</think> {"rules": [1]}'}}]})

    cfg = LlmConfig(endpoint="https://llm-gw.kurum.local/vllm-qwen3/v1", model="/data/models--Qwen--Qwen3-235B-FP8",
                    allowed_hosts=["llm-gw.kurum.local"], max_tokens=1024, api_key_env="TEST_LLM_KEY",
                    extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    import os

    os.environ["TEST_LLM_KEY"] = "k1"
    try:
        c = LlmClient.from_config(cfg, transport=httpx.MockTransport(handler))
    finally:
        os.environ.pop("TEST_LLM_KEY")
    assert c.chat_json("s", "u") == {"rules": [1]}  # thinking block removed, retried without response_format
    auth, body = calls[-1]
    assert auth == "Bearer k1" and body["max_tokens"] == 1024 and body["temperature"] == 0
    assert body["chat_template_kwargs"] == {"enable_thinking": False} and body["model"].endswith("Qwen3-235B-FP8")
    assert not c.http._trust_env  # never through a proxy
    with pytest.raises(ValueError, match="izin listesinde"):
        LlmClient.from_config(LlmConfig(endpoint="https://api.openai.com/v1"))


def test_slow_pattern_is_rejected_within_the_time_budget(tmp_path, monkeypatch):
    import time

    import scoutout.rules as r

    monkeypatch.setattr(r, "MEASURE_SECONDS", -1.0)  # stands in for a pattern that times out on every value
    store = Store(tmp_path / "s.db")
    t0 = time.monotonic()
    status = submit(store, "u", "X_TYPE", {"pattern": r"[A-Z]{2}\d{8}"}, "agent", KEY, None, ["MN12345678"] * 400, ["x"] * 400)
    assert status == "REJECTED" and time.monotonic() - t0 < 5
    assert "yavaş" in store.one("SELECT reason FROM rules")["reason"]


def test_context_gated_variant_is_not_a_duplicate(tmp_path):
    store = Store(tmp_path / "s.db")
    pos = [f"müşteri no MN{i:08d} kaydı" for i in range(60)]
    neg = [f"ref MN{i:08d}" for i in range(60)]
    assert submit(store, "u", "CUSTOMER_NO", {"pattern": r"MN\d{8}"}, "agent", KEY, None, pos, ["x"] * 60) == "CANDIDATE"
    assert submit(store, "u", "CUSTOMER_NO", {"pattern": r"MN\d{8}"}, "agent", KEY, None, pos, neg) == "DUPLICATE"
    gated = {"pattern": r"MN\d{8}", "context": ["müşteri"]}
    assert submit(store, "u", "CUSTOMER_NO", gated, "agent", KEY, None, pos, neg) == "CANDIDATE"
    assert submit(store, "u", "CUSTOMER_NO", {"pattern": r"MN\d{8}", "context": ["Müşteri"]}, "agent", KEY, None, pos, neg) == "DUPLICATE"
