import random

import pytest

from scoutout.decision_engine.engine import Thresholds, decide_type
from scoutout.decision_engine.stats import rogan_gladen, wilson_lower, wilson_upper
from scoutout.detectors.generic import DetectorMeta

TCKN = DetectorMeta("tckn", "1", "TCKN", "span", chance=0.01)
NAME = DetectorMeta("name", "1", "PERSON_NAME", "span", se=0.95, sp=0.995)


def test_wilson_known_values():
    # x=0, n=400, one-sided 99%: (z^2/n)/(1+z^2/n) with z=2.326
    assert wilson_upper(0, 400, 0.99) == pytest.approx(0.01335, abs=2e-4)
    assert wilson_lower(50, 100, 0.95) == pytest.approx(0.4189, abs=2e-3)
    assert wilson_lower(0, 10, 0.95) == 0.0 and wilson_upper(10, 10, 0.95) == 1.0


def test_rogan_gladen():
    assert rogan_gladen(0.5, 1.0, 1.0) == 0.5
    assert rogan_gladen(0.01, 0.99, 0.99) == 0.0  # pure false-positive level
    assert rogan_gladen(0.9116, 0.99, 0.99) == pytest.approx(0.92, abs=1e-3)
    with pytest.raises(ValueError):
        rogan_gladen(0.5, 0.5, 0.5)


@pytest.mark.parametrize(
    "x, n, cands, zone",
    [
        (0, 400, 0, "CLEAN"),
        (400, 400, 400, "SENSITIVE"),
        (12, 400, 12, "SPARSE"),  # 3% real TCKNs
        (4, 400, 400, "CLEAN"),  # 1% random 11-digit refs passing the checksum by chance
        (200, 400, 200, "SPARSE"),
        (370, 400, 370, "UNDECIDED"),  # 92.5% at n=400: cannot yet say >= 90%
        (4620, 5000, 4620, "SENSITIVE"),  # 92.4% at n=5000
        (0, 0, 0, "EMPTY"),
    ],
)
def test_zones(x, n, cands, zone):
    assert decide_type("TCKN", x, n, cands, TCKN, Thresholds(), looks=4).zone == zone


def test_clean_needs_no_evidence_of_presence():
    """D-006: a few real hits at a low rate are SPARSE, never CLEAN, even if the rate is below 5%."""
    assert decide_type("PERSON_NAME", 50, 2500, 0, NAME, Thresholds(), looks=4).zone == "SPARSE"  # 2%


def test_monte_carlo_false_clean_rate_is_below_target():
    """Columns with real 3% contamination must (practically) never come out CLEAN."""
    rng = random.Random(1)
    false_clean = 0
    for _ in range(2000):
        x = sum(rng.random() < 0.03 for _ in range(400))
        false_clean += decide_type("TCKN", x, 400, x, TCKN, Thresholds(), looks=4).zone == "CLEAN"
    assert false_clean / 2000 < 0.01


def test_per_type_threshold_override():
    th = Thresholds(per_type={"PERSON_NAME": {"p_sensitive": 0.5}})
    assert decide_type("PERSON_NAME", 300, 400, 0, NAME, th, looks=4).zone == "SENSITIVE"
    assert decide_type("PERSON_NAME", 300, 400, 0, NAME, Thresholds(), looks=4).zone == "SPARSE"
