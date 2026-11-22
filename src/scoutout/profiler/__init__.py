"""Column profiles computed on the sample only (spec §5.3)."""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import asdict, dataclass, field

import numpy as np

NGRAM_BUCKETS = 64
CHAR_PROFILE_PREFIX = 300
SHAPE_BUCKETS = 32


def shape(value: str, max_len: int = 40) -> str:
    """'Ahmet Yılmaz' -> 'Xxxxx Xxxxxx', '0532 123 45 67' -> 'dddd ddd dd dd'."""
    out = []
    for ch in value[:max_len]:
        if ch.isdigit():
            out.append("d")
        elif ch.isalpha():
            out.append("X" if ch.isupper() else "x")
        else:
            out.append(ch)
    return "".join(out)


def _bucket(s: str, n: int) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode(), digest_size=4).digest(), "big") % n


@dataclass
class ColumnProfile:
    n: int = 0
    null_rate: float = 0.0
    empty_rate: float = 0.0
    distinct_rate: float = 0.0
    len_min: int = 0
    len_max: int = 0
    len_p50: float = 0.0
    len_p95: float = 0.0
    digit_rate: float = 0.0
    alpha_rate: float = 0.0
    upper_rate: float = 0.0
    tr_char_rate: float = 0.0
    space_rate: float = 0.0
    punct_rate: float = 0.0
    top_shapes: list[tuple[str, float]] = field(default_factory=list)
    ngram_vector: list[float] = field(default_factory=list)
    shape_vector: list[float] = field(default_factory=list)

    def summary(self) -> dict:
        d = asdict(self)
        d.pop("ngram_vector")
        d.pop("shape_vector")
        return d

    def vector(self) -> np.ndarray:
        """Fixed-length profile vector (statistics + hashed character 3-grams + hashed shapes)."""
        stats = [
            self.null_rate, self.empty_rate, self.distinct_rate, min(self.len_p50, 200) / 200,
            min(self.len_p95, 400) / 400, self.digit_rate, self.alpha_rate, self.upper_rate,
            self.tr_char_rate, self.space_rate, self.punct_rate,
        ]
        return np.array(stats + self.ngram_vector + self.shape_vector, dtype=np.float32)


TR_CHARS = set("çğıöşüÇĞİÖŞÜ")


def profile(values: list) -> ColumnProfile:
    n = len(values)
    p = ColumnProfile(n=n)
    if n == 0:
        p.ngram_vector, p.shape_vector = [0.0] * NGRAM_BUCKETS, [0.0] * SHAPE_BUCKETS
        return p
    texts = [str(v) for v in values if v is not None]
    p.null_rate = 1 - len(texts) / n
    if not texts:
        p.ngram_vector, p.shape_vector = [0.0] * NGRAM_BUCKETS, [0.0] * SHAPE_BUCKETS  # fixed vector length
        return p
    p.empty_rate = sum(1 for t in texts if not t.strip()) / len(texts)
    p.distinct_rate = len(set(texts)) / len(texts)
    lengths = np.array([len(t) for t in texts])
    p.len_min, p.len_max = int(lengths.min()), int(lengths.max())
    p.len_p50, p.len_p95 = float(np.percentile(lengths, 50)), float(np.percentile(lengths, 95))
    counts = Counter()
    total = 0
    for t in texts:
        t = t[:CHAR_PROFILE_PREFIX]  # character classes from the head of long values (lengths use the full value)
        total += len(t)
        for ch in t:
            if ch.isdigit():
                counts["d"] += 1
            elif ch.isalpha():
                counts["a"] += 1
                if ch.isupper():
                    counts["u"] += 1
                if ch in TR_CHARS:
                    counts["tr"] += 1
            elif ch.isspace():
                counts["s"] += 1
            else:
                counts["p"] += 1
    total = max(total, 1)
    p.digit_rate, p.alpha_rate = counts["d"] / total, counts["a"] / total
    p.upper_rate, p.tr_char_rate = counts["u"] / total, counts["tr"] / total
    p.space_rate, p.punct_rate = counts["s"] / total, counts["p"] / total

    shapes = Counter(shape(t) for t in texts)
    p.top_shapes = [(s, round(c / len(texts), 4)) for s, c in shapes.most_common(5)]
    sv = np.zeros(SHAPE_BUCKETS)
    for s, c in shapes.items():
        sv[_bucket(s, SHAPE_BUCKETS)] += c
    ng = np.zeros(NGRAM_BUCKETS)
    for t in texts[:2000]:
        low = t.lower()[:200]
        for i in range(len(low) - 2):
            ng[_bucket(low[i : i + 3], NGRAM_BUCKETS)] += 1
    p.shape_vector = (sv / max(sv.sum(), 1)).round(5).tolist()
    p.ngram_vector = (ng / max(ng.sum(), 1)).round(5).tolist()
    return p


def is_free_text(p: ColumnProfile) -> bool:
    return p.len_p50 >= 25 and p.space_rate >= 0.08 and p.alpha_rate >= 0.5
