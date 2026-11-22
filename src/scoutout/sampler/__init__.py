"""Deterministic, index-seek based sampling (spec §3.2)."""

from .sampler import Sample, Sampler, SampleRecord, choose_strategy

__all__ = ["Sample", "SampleRecord", "Sampler", "choose_strategy"]
